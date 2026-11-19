"""Offline tests for the fleet monitor and its LaunchAgent."""

from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ast
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ops import fleet_monitor as monitor, fleet_monitor_probe as probe


NOW = datetime(2026, 11, 18, 18, tzinfo=timezone.utc)
SOON = "2026-11-19T18:00:00+00:00"
LATER = "2026-11-23T18:00:00+00:00"
SALT = "a1" * 32
HOSTS = {"m5": {"name": "m5", "ssh": None}, "will": {"name": "will", "ssh": "will.example.test"}}

FIXTURES = {
    "claude-index": {"limits": [
        {"kind": "weekly_all", "percent": 92, "resets_at": LATER},
        {"kind": "weekly_scoped", "percent": 100, "resets_at": SOON,
         "scope": {"model": {"display_name": "Fable"}}}],
        "cedar_ember": {"cooldown_until": SOON, "grants": [
            {"id": "private-grant-id", "resets_left": 1, "usable_now": True}]},
        "five_hour": {"utilization": 5, "resets_at": SOON}},
    "codex-index": {"rate_limit": {
        "primary_window": {"limit_window_seconds": 604800, "used_percent": 72,
                           "reset_at": datetime.fromisoformat(LATER).timestamp()},
        "secondary_window": {"limit_window_seconds": 18000, "used_percent": 1, "reset_after_seconds": 30}}},
    "kimi-index": {"usages": {"limit_5h": {"used_ratio": .22, "reset_time": SOON},
                            "limit_month_total": {"used_ratio": .93, "reset_time": LATER},
                            "limit_month_code": {"used_ratio": "1", "reset_time": LATER}},
                   "usage": {"used": 10, "limit": 100, "resetTime": SOON},
                   "limits": [{"window": {"duration": 5, "timeUnit": "TIME_UNIT_HOUR"},
                               "detail": {"remaining": 900, "limit": 1000, "resetTime": SOON}}]},
    "zai-index": {"success": True, "data": {"limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 35,
         "nextResetTime": datetime.fromisoformat(SOON).timestamp() * 1000},
        {"type": "TIME_LIMIT", "unit": 5, "number": 1, "currentValue": 3, "usage": 10}]}},
}

FILES = [
    {"provider": "claude", "email": "synthetic" + chr(64) + "example.test",
     "id": "claude-file", "auth_index": "claude-index", "account_type": "oauth"},
    {"provider": "codex", "id": "codex-file", "auth_index": "codex-index", "account_type": "oauth",
     "id_token": {"chatgpt_account_id": "fake-codex-account"}},
    {"provider": "kimi", "id": "kimi-file", "auth_index": "kimi-index", "account_type": "oauth"},
    {"provider": "zai-coding-plan", "id": "zai-file", "auth_index": "zai-index", "account_type": "api_key"},
    {"provider": "codex", "id": "static-api-key", "auth_index": "wrong-index", "account_type": "api_key"},
]

GROUPS = [
    {"name": "unverified-target", "base-url": "https://api.z.ai.example.test/paas/v4",
     "keys": [{"auth_index": "wrong-index", "api-key": "fixture-unrelated-key"}]},
    {"name": "zai-coding-plan", "base-url": "https://api.z.ai/api/coding/paas/v4",
     "keys": [{"auth_index": "zai-index", "api-key": "fixture-zai-key"}]},
]


def sample(tag="test-account", percent=89, status="ok"):
    reading = probe.quota_window("weekly", "weekly", percent, LATER)
    return {"provider": "claude", "account_id": tag, "label": "t3-demo", "status": status,
            "windows": [reading] if status == "ok" else [], "cooldowns": [], "grants": None,
            "attempted_at": NOW.timestamp() if status == "ok" else None}


def host_report(accounts=(), state="ok", egress="ok"):
    return {"reachability": state, "egress": {"api.anthropic.com": egress},
            "accounts": list(accounts), "quota_errors": [] if state == "ok" else ["proxy"]}


class ProviderTests(unittest.TestCase):
    def test_quota_reads_through_v8_worker_with_local_fakes(self):
        posts = []
        management_value = "fixture-management-key"

        class FakeProxy(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/healthz":
                    self.respond({"status": "ok"})
                elif self.headers.get("Authorization") != "Bearer " + management_value:
                    self.respond({}, 401)
                elif self.path == probe.CREDENTIALS:
                    self.respond({"files": FILES})
                elif self.path == probe.COMPATIBILITY:
                    self.respond(GROUPS)
                elif self.path.startswith(probe.CREDENTIALS + "/models?"):
                    self.respond({"models": [{"id": "test-model"}, {"id": "t3-demo/test-model"}]})
                else:
                    self.respond({}, 404)

            def do_POST(self):
                if (self.path != "/v8/management/requests/api-call"
                        or self.headers.get("Authorization") != "Bearer " + management_value):
                    self.respond({}, 401)
                    return
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                posts.append(request)
                self.respond({"status_code": 200, "body": json.dumps(FIXTURES[request["auth_index"]])})

        with ThreadingHTTPServer(("127.0.0.1", 0), FakeProxy) as service, tempfile.TemporaryDirectory() as directory:
            thread = threading.Thread(target=service.serve_forever, daemon=True)
            thread.start()
            fixture = Path(directory)
            (fixture / ".env").write_text("MANAGEMENT_PASSWORD=" + management_value)
            # A stand-in for Docker. It verifies the invocation, and runs no container process.
            docker = fixture / "docker"
            docker.write_text('''#!/bin/sh
[ "$1 $2 $3 $4" = "exec cli-proxy-api bash -c" ] || exit 77
case "$5" in *"/dev/tcp/"*"/443"*) ;; *) exit 78 ;; esac
shift 6
for target do printf '%s ok\n' "$target"; done
''')
            docker.chmod(0o700)
            env = {**os.environ, "PATH": str(fixture) + os.pathsep + os.environ["PATH"]}
            host = {**HOSTS["will"], "port": service.server_port, "instance_root": directory}
            worker_args = [sys.executable, "-I", "-B", "-"]
            try:
                program = monitor.remote_program(json.dumps([host, SALT, [], {}]))
                worker = subprocess.run(worker_args, input=program, env=env, capture_output=True,
                                        text=True, timeout=8, check=False)
                self.assertEqual(worker.returncode, 0, worker.stderr)
                report = json.loads(worker.stdout.splitlines()[-1])
                self.assertEqual(report["reachability"], "ok")
                self.assertTrue(all(state == "ok" for state in report["egress"].values()))
                self.assertEqual(report["quota_errors"], [])
                for private in (management_value, FILES[0]["email"], "fixture-zai-key", "fake-codex-account",
                                "private-grant-id", "zai-file", "claude-index"):
                    self.assertNotIn(private, worker.stdout)

                accounts = {item["provider"]: item for item in report["accounts"]}
                self.assertEqual(set(accounts), {"claude", "codex", "kimi", "zai"})
                self.assertEqual({row["window"] for row in accounts["claude"]["windows"]},
                                 {"session", "weekly", "weekly-fable"})
                self.assertEqual(accounts["claude"]["label"], "t3-demo")
                self.assertEqual(accounts["claude"]["grants"]["resets_left"], 1)
                by_window = {w["window"]: w for w in accounts["kimi"]["windows"]}
                self.assertEqual(by_window["session"]["used_percent"], 22)
                self.assertEqual(by_window["monthly-total"]["reset_at"], LATER)
                self.assertTrue(by_window["monthly-code"]["capped"])
                self.assertIn(SOON, [row["reset_at"] for row in accounts["zai"]["windows"]])

                requests = {row["auth_index"]: row for row in posts}
                self.assertEqual(len(posts), 4)
                self.assertTrue(all(row["method"] == "GET" and "data" not in row for row in posts))
                codex = requests["codex-index"]
                self.assertEqual(codex["url"], "https://chatgpt.com/backend-api/wham/usage")
                self.assertEqual(codex["header"]["ChatGPT-Account-Id"], "fake-codex-account")
                self.assertEqual(requests["kimi-index"]["url"], "https://api.kimi.com/coding/v1/usages")
                self.assertEqual(requests["claude-index"]["url"], probe.claude.USAGE_URL)
                zai = requests["zai-index"]
                self.assertEqual(zai["url"], "https://api.z.ai/api/monitor/usage/quota/limit")
                self.assertEqual(zai["header"], {"Authorization": "$TOKEN$", "Accept-Language": "en-US,en",
                                                "Content-Type": "application/json"})

                program = monitor.remote_program(json.dumps([host, SALT, [accounts["claude"]["account_id"]], {}]))
                worker = subprocess.run(worker_args, input=program, env=env, capture_output=True,
                                        text=True, timeout=8, check=True)
                duplicate = json.loads(worker.stdout.splitlines()[-1])["accounts"]
                self.assertEqual(next(row["status"] for row in duplicate if row["provider"] == "claude"),
                                 "polled-on-m5")
                self.assertEqual([row["auth_index"] for row in posts].count("claude-index"), 1)

                program = monitor.remote_program(json.dumps([host, SALT, [], {"_skip_claude": True}]))
                worker = subprocess.run(worker_args, input=program, env=env, capture_output=True,
                                        text=True, timeout=8, check=True)
                deferred = json.loads(worker.stdout.splitlines()[-1])["accounts"]
                self.assertEqual(next(row["status"] for row in deferred if row["provider"] == "claude"),
                                 "quota-deferred")
                counts = {key: sum(row["auth_index"] == key for row in posts) for key in requests}
                self.assertEqual(counts, {"claude-index": 1, "codex-index": 3, "kimi-index": 3, "zai-index": 3})
            finally:
                service.shutdown()
                thread.join(timeout=3)

    def test_credential_identity_dedupes_across_host_indexes(self):
        first = probe.quota_accounts(FILES, GROUPS, SALT)
        files, groups = deepcopy((FILES, GROUPS))
        files[0]["auth_index"] = "second-claude-index"
        files[1]["auth_index"] = "second-codex-index"
        files[3]["auth_index"] = groups[1]["keys"][0]["auth_index"] = "second-zai-index"
        self.assertEqual(first.keys(), probe.quota_accounts(files, groups, SALT).keys())

    def test_zai_destinations_require_allowlisted_https_origin(self):
        for url in ("http://api.z.ai", "https://api.z.ai:80", "https://api.z.ai.example.test",
                    "https://untrusted@api.z.ai", "https://api.z.ai?x=1", "https://api.z.ai#x"):
            self.assertIsNone(probe.zai_destination(url), url)
        self.assertEqual(probe.zai_destination("https://open.bigmodel.cn:443/api/v4"), "open.bigmodel.cn")

    def test_zai_credit_limits_from_current_coding_plan_reply(self):
        # Shape of the Coding Plan reply observed on 2026-11-19, with synthetic numbers.
        reset = datetime.fromisoformat(SOON).timestamp() * 1000
        body = {"code": 200, "msg": "Operation successful", "success": True, "data": {"level": "lite", "limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 2000, "currentValue": 395,
             "remaining": 1605, "percentage": 19, "nextResetTime": reset},
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 10000, "currentValue": 482,
             "remaining": 9518, "nextResetTime": reset}]}}
        api = type("FakeAPI", (), {"quota": lambda self, request: {"status_code": 200, "body": json.dumps(body)}})()
        item = {"provider": "zai", "index": "zai-index", "zai_host": "api.z.ai"}
        status, windows, _ = probe.fetch_windows(api, item, NOW)
        self.assertEqual(status, "ok")
        self.assertEqual([window["used_percent"] for window in windows], [19, 4.82])
        self.assertEqual(len({window["window"] for window in windows}), 2)
        self.assertTrue(all(window["window"].startswith("credits-") for window in windows))
        self.assertEqual({window["reset_at"] for window in windows}, {SOON})

    def test_kimi_reply_with_only_a_five_hour_limit_is_still_read(self):
        # Kimi stopped returning its weekly and monthly entries on 2026-11-19.
        body = {"limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                            "detail": {"limit": "100", "remaining": "100", "resetTime": SOON}}],
                "usages": {"limit_5h": {"used_ratio": 0}}}
        api = type("FakeAPI", (), {"quota": lambda self, request: {"status_code": 200, "body": json.dumps(body)}})()
        status, windows, _ = probe.fetch_windows(api, {"provider": "kimi", "index": "kimi-index"}, NOW)
        self.assertEqual(status, "ok")
        self.assertEqual([(window["window"], window["used_percent"]) for window in windows], [("session", 0)])

    def test_quota_reads_wait_for_slow_kimi_within_the_remote_deadline(self):
        # Kimi's usage endpoint answered in about 10.5 s on 2026-11-19. A worker polls at most two
        # accounts after a Claude wait of up to 15 s, and Will's worker ends at 63 s.
        self.assertGreaterEqual(probe.HTTP_TIMEOUT, 15)
        self.assertLess(15 + 2 * probe.HTTP_TIMEOUT, 63)
        response = type("Response", (), {"status": 200, "read": lambda self, *size: b"{}"})()
        with (patch.object(probe.http.client, "HTTPConnection") as connection,
              patch.object(probe.QuotaAPI, "management_key", return_value="fixture-management-key")):
            connection.return_value.getresponse.return_value = response
            self.assertEqual(probe.QuotaAPI(1, Path("/nonexistent/.env")).quota({}), {})
        self.assertEqual(connection.call_args.kwargs["timeout"], probe.HTTP_TIMEOUT)

    def test_claude_waits_until_due_and_skips_if_too_early(self):
        account = next(row for row in probe.quota_accounts(FILES, [], SALT).values() if row["provider"] == "claude")
        with (patch.object(probe, "route_label", return_value="t3-demo"),
              patch.object(probe, "fetch_windows", return_value=("ok", [], None)) as fetch,
              patch.object(probe.time, "sleep") as sleep,
              patch.object(probe.time, "time", side_effect=[100, 100, 109])):
            self.assertEqual(probe.collect_account(None, account, NOW, (), {"_all": 121}, {})["status"],
                             "quota-deferred")
            self.assertIsNotNone(probe.collect_account(None, account, NOW, (), {"_all": 109}, {})["attempted_at"])
            sleep.assert_called_once_with(9)
            fetch.assert_called_once()

    def test_failed_worker_preserves_health_and_identifies_uncertain_poll(self):
        partial = dict(host_report(), stage=probe.WORKER_POLLING, quota_errors=["inventory-incomplete"])
        report = monitor.host_output((json.dumps(partial) + "\npartial").encode(), None, {}, "will.example.test")
        self.assertEqual(report["reachability"], "ok")
        self.assertTrue(report["uncertain"])
        absent = monitor.host_output("", 255, {"uncertain": False}, "will.example.test")
        self.assertEqual(absent, {"reachability": "unreachable", "ssh_unreachable": True, "uncertain": True})
        self.assertEqual(monitor.host_output("", None, {}, "will.example.test"), absent)


class MonitorTests(unittest.TestCase):
    def test_tick_cadence_daily_summary_alerts_and_history(self):
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(monitor, "DAILY_AT", time(0)),
              patch.object(monitor, "run_host") as runs,
              patch.object(monitor, "run_parity", side_effect=[
                  {"status": "equal"}, {"status": "drift"}, {"status": "drift"}]) as parity,
              patch.object(monitor, "notify_message", return_value=monitor.TEXT_SENT) as notify):
            root = Path(directory)

            for tick, (offset, reading, will_state, will_egress) in enumerate((
                    (0, 89, "ok", "ok"), (300, 92, "unreachable", "unknown"),
                    (600, 92, "unreachable", "unknown"), (3600, 100, "ok", "down"),
                    (3900, 100, "ok", "down"), (7200, 100, "ok", "ok"))):
                reading_now = NOW + timedelta(seconds=offset)
                current = dict(sample(percent=reading), provider="codex", attempted_at=reading_now.timestamp())

                def collect(host, salt, visited, due):
                    # Claude is read by elapsed time: the tick after the 55-minute gap reads it.
                    self.assertEqual(bool(due.get("_skip_claude")), (False, True, True, False, True, False)[tick])
                    if host["name"] == "m5":
                        self.assertEqual(visited, set())
                        return host_report([current])
                    self.assertEqual(visited, {"test-account"})
                    return host_report(state=will_state, egress=will_egress)

                runs.side_effect = collect
                with patch.object(monitor, "now_utc", return_value=reading_now):
                    monitor.tick(root, deepcopy(HOSTS), root, [], no_notify=False)

            self.assertEqual(parity.call_count, 3)
            messages = [call.args[0] for call in notify.call_args_list]
            self.assertEqual(len(messages), 6)
            self.assertEqual(sum("daily summary" in message for message in messages), 1)
            self.assertIn("92% used", messages[1])
            self.assertIn("Will proxy unreachable", messages[2])
            self.assertIn("weekly capped", messages[3])
            self.assertIn("Will egress down", messages[4])
            self.assertIn("drift appeared", messages[5])
            history_path = root / "quota-history-2026-11.jsonl"
            history = [json.loads(line) for line in history_path.read_text().splitlines()]
            self.assertEqual(len(history), 6)
            self.assertEqual(history[-1]["windows"][0]["used_percent"], 100)
            self.assertEqual(stat.S_IMODE(history_path.stat().st_mode), 0o600)
            health = monitor.read_json(root / "health.json", {})
            self.assertEqual(health["hosts"]["will"]["status"], "ok")
            latest = monitor.read_json(root / "latest.json", {})
            self.assertEqual(latest["status"], "drift")
            self.assertEqual(latest["health"]["will"]["status"], "ok")

    def test_claude_429_backs_off_only_that_account_across_hosts(self):
        files = [FILES[0], dict(FILES[0], auth_index="other-index", email=None)]
        accounts = list(probe.quota_accounts(files, [], SALT).values())
        limited, other = (account["account_id"] for account in accounts)
        requests = []

        class FakeAPI:
            def quota(self, request):
                index = request["auth_index"]
                requests.append(index)
                if index == "claude-index" and requests.count(index) == 1:
                    return {"status_code": 429}
                return {"status_code": 200, "body": FIXTURES["claude-index"]}

        def collect(host, salt, visited, due):
            members = accounts if host["name"] == "m5" else accounts[:1]
            return host_report(probe.collect_account(FakeAPI(), account, clock(), visited, due, {})
                               for account in members)

        with (tempfile.TemporaryDirectory() as directory,
              patch.object(monitor, "now_utc") as clock,
              patch.object(probe.time, "time", side_effect=lambda: clock().timestamp()),
              patch.object(probe, "route_label", return_value="t3-demo"),
              patch.object(monitor, "run_host", side_effect=collect),
              patch.object(monitor, "run_parity", return_value={"status": "equal"})):
            root = Path(directory)
            for offset in range(0, 1801, 300):
                clock.return_value = NOW + timedelta(seconds=offset)
                monitor.tick(root, deepcopy(HOSTS), root, [], no_notify=True)

            self.assertEqual(requests, ["claude-index", "other-index", "other-index",
                                        "claude-index", "other-index"])
            history = [json.loads(line) for line in (root / "quota-history-2026-11.jsonl").read_text().splitlines()]
            self.assertEqual([row["status"] for row in history if row["account_id"] == limited][::3],
                             ["quota-rate-limited", "quota-deferred", "ok"])
            self.assertEqual([row["status"] for row in history if row["account_id"] == other][::3],
                             ["ok", "ok", "ok"])
            self.assertFalse((root / "ops/runtime/claude-resets").exists())

    def test_will_ssh_failure_does_not_guard_mac_claude_reads(self):
        account = next(iter(probe.quota_accounts(FILES[:1], [], SALT).values()))

        def collect(host, salt, visited, due):
            if host["name"] == "will":
                return monitor.host_output("", 255, host_report(state="unreachable"), host["ssh"])
            return host_report([probe.collect_account(None, account, clock(), visited, due, {})])

        with (tempfile.TemporaryDirectory() as directory,
              patch.object(monitor, "now_utc") as clock,
              patch.object(probe.time, "time", side_effect=lambda: clock().timestamp()),
              patch.object(probe, "route_label", return_value="t3-demo"),
              patch.object(probe, "fetch_windows", return_value=("ok", sample()["windows"], None)) as fetch,
              patch.object(monitor, "run_host", side_effect=collect),
              patch.object(monitor, "run_parity", return_value={"status": "equal"}),
              patch.object(monitor, "CLAUDE_POLL_DUE", 0)):
            root = Path(directory)
            # Every tick is due for Claude, so the 300 s account floor spaces the Mac reads
            # and rapid ticks exercise the guard before Will's reservation expires.
            for offset in range(0, 721, 120):
                clock.return_value = NOW + timedelta(seconds=offset)
                monitor.tick(root, deepcopy(HOSTS), root, [], no_notify=True)
            self.assertEqual(fetch.call_count, 3)
            state = monitor.read_json(root / "monitor-state.json", {})
            self.assertEqual(set(state["claude_host_guards"]), {"will"})
            self.assertGreater(state["claude_host_guards"]["will"], clock().timestamp())

    def test_health_hysteresis_and_ssh_only_wording(self):
        state = {}
        reports = {"m5": host_report()}
        failure = dict(host_report(state="unreachable"), ssh_unreachable=True)
        clean = host_report()
        unknown = host_report(state="unknown")

        def alerts(sample):
            return monitor.status_alerts(state, dict(reports, will=sample), None)

        for sample in (failure, clean, failure, unknown, failure, clean):
            self.assertEqual(alerts(sample), [])
        self.assertEqual(alerts(failure), [])
        self.assertEqual(alerts(failure), ["Will is not reachable over SSH from the Mac."])
        self.assertEqual(alerts(failure), [])
        for sample in (clean, unknown, clean, failure, clean):
            self.assertEqual(alerts(sample), [])
        self.assertEqual(alerts(clean), ["Will health checks recovered."])
        self.assertEqual(alerts(clean), [])
        proxy_down = host_report(state="unreachable")
        self.assertEqual(alerts(proxy_down), [])
        self.assertIn("Select the _mac profile", alerts(proxy_down)[0])

    def test_short_cooldowns_share_one_episode_until_an_hour_quiet(self):
        state = {}
        record = sample()

        def alerts(minutes, cooldown):
            now = NOW + timedelta(minutes=minutes)
            record["cooldowns"] = ([
                {"scope": "credential", "reason": "quota", "until": probe.iso(now + timedelta(seconds=120))}
            ] if cooldown else [])
            return monitor.quota_alerts(state, [record], now)

        self.assertEqual(sum(len(alerts(minute, True)) for minute in (0, 5, 10, 15)), 1)
        self.assertEqual(alerts(25, False), [])
        self.assertEqual(alerts(77, True)[0].split(" until ")[0], "claude t3-demo in proxy quota cooldown")

    def test_deferred_claude_tick_suppresses_cooldown_text_for_known_cap(self):
        capped = sample(percent=100)
        cooldown = {"scope": "credential", "reason": "quota", "until": SOON}
        deferred = dict(sample(status="quota-deferred"), cooldowns=[cooldown])
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(monitor, "run_host") as runs,
              patch.object(monitor, "run_parity", return_value={"status": "equal"}),
              patch.object(monitor, "notify_message", return_value=monitor.TEXT_SENT) as notify):
            root = Path(directory)
            monitor.write_json(root / "monitor-state.json", {"salt": SALT, "summary_day": "2026-11-18"})
            for tick, reading in enumerate((capped, deferred, deferred, capped)):
                runs.side_effect = [host_report([reading]), host_report()]
                with patch.object(monitor, "now_utc", return_value=NOW + timedelta(minutes=5 * tick)):
                    monitor.tick(root, deepcopy(HOSTS), root, [], no_notify=False)

            notify.assert_called_once()
            self.assertIn("weekly capped", notify.call_args.args[0])
            self.assertNotIn("proxy quota cooldown", notify.call_args.args[0])

    def test_no_notification_or_transition_claim_in_dry_run(self):
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(monitor, "now_utc", return_value=NOW),
              patch.object(monitor, "run_host", side_effect=[host_report(state="unreachable"), host_report()]),
              patch.object(monitor, "run_parity", return_value={"status": "drift"}),
              patch.object(monitor, "notify_message") as notify):
            root = Path(directory)
            monitor.tick(root, deepcopy(HOSTS), root, [], no_notify=True)
            notify.assert_not_called()
            state = monitor.read_json(root / "monitor-state.json", {})
            self.assertNotIn("summary_day", state)
            self.assertNotIn("health_alerts", state)
            self.assertEqual(state["outages"], ["Mac proxy unreachable"])

    def test_incomplete_inventory_preserves_account_without_stale_percent(self):
        old = {"stale": {"provider": "claude", "label": "t3-demo", "hosts": ["will"]}}
        observed = {"m5": host_report(), "will": host_report(state="unreachable")}
        records, inventory = monitor.quota_records(observed, old, probe.iso(NOW))
        self.assertEqual(inventory, old)
        self.assertEqual(records[0]["status"], "quota-unavailable")
        self.assertEqual(records[0]["windows"], [])
        self.assertIsNone(records[0]["source_host"])

    def test_missing_sample_does_not_rearm_threshold(self):
        state = {}
        record = sample(percent=92)
        self.assertEqual(len(monitor.quota_alerts(state, [record], NOW)), 1)
        self.assertEqual(monitor.quota_alerts(state, [sample(status="quota-unavailable")], NOW), [])
        self.assertEqual(monitor.quota_alerts(state, [record], NOW), [])
        monitor.quota_alerts(state, [sample(percent=50)], NOW)
        self.assertEqual(len(monitor.quota_alerts(state, [record], NOW)), 1)

    def test_definite_notification_failure_retries_health_quota_and_summary(self):
        with (tempfile.TemporaryDirectory() as directory, patch.object(monitor, "DAILY_AT", time(0)),
              patch.object(monitor, "run_host") as runs,
              patch.object(monitor, "run_parity", return_value={"status": "equal"}),
              patch.object(monitor, "notify_message", side_effect=[
                  monitor.TEXT_NOT_SENT, monitor.TEXT_NOT_SENT, monitor.TEXT_SENT]) as notify,
              patch("sys.stderr")):
            root = Path(directory)
            for tick in range(3):
                reading = sample(percent=92) if tick == 0 else sample(status="quota-deferred")
                runs.side_effect = [host_report([reading]), host_report(state="unreachable")]
                with patch.object(monitor, "now_utc", return_value=NOW + timedelta(minutes=5 * tick)):
                    monitor.tick(root, deepcopy(HOSTS), root, [], no_notify=False)
                state = monitor.read_json(root / "monitor-state.json", {})
                if tick < 2:
                    self.assertNotIn("summary_day", state)
                    self.assertNotIn("health_alerts", state)
                    self.assertNotIn("quota_alerts", state)
                    self.assertIn("92% used", state["pending_alerts"]["messages"][0])
                    self.assertEqual(state["last_notification"]["status"], "not-sent")

            messages = [call.args[0] for call in notify.call_args_list]
            self.assertEqual(len(messages), 3)
            self.assertNotIn("Select the _mac profile", messages[0])
            for message in messages[1:]:
                for text in ("Select the _mac profile", "92% used", "daily summary"):
                    self.assertIn(text, message)
            self.assertEqual(state["last_notification"]["status"], "sent")
            self.assertNotIn("pending_alerts", state)
            self.assertTrue(state["health_alerts"]["will-unreachable"]["active"])
            self.assertTrue(state["quota_alerts"]["test-account"]["weekly"]["above"])

    def test_summary_labels_recent_claude_reading_during_a_deferred_tick(self):
        state = {}
        reading = dict(sample(percent=92), captured_at=probe.iso(NOW))
        monitor.summary_records(state, [reading], NOW)
        deferred = sample(status="quota-deferred")
        records = monitor.summary_records(state, [deferred], NOW + timedelta(seconds=900))
        summary = monitor.daily_message("fixture day", records, {}, {}, [])
        self.assertIn("weekly 92%", summary)
        self.assertIn("as of ", summary)
        self.assertEqual(deferred["windows"], [])
        records = monitor.summary_records(state, [deferred], NOW + timedelta(minutes=35))
        self.assertEqual(records, [deferred])

    def test_ambiguous_notification_is_not_resent(self):
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(monitor, "DAILY_AT", time(0)),
              patch.object(monitor, "now_utc", return_value=NOW),
              patch.object(monitor, "run_host") as runs,
              patch.object(monitor, "run_parity", return_value={"status": "equal"}),
              patch.object(monitor.subprocess, "run", side_effect=subprocess.TimeoutExpired("fake", 1)) as notify,
              patch("sys.stderr")):
            root = Path(directory)
            for _ in range(2):
                runs.side_effect = [host_report([sample(percent=92)]), host_report()]
                monitor.tick(root, deepcopy(HOSTS), root, [], no_notify=False)
            notify.assert_called_once()
            health = monitor.read_json(root / "health.json", {})
            self.assertEqual(health["last_notification"]["status"], "unconfirmed")

    def test_interrupted_history_append_is_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.jsonl"
            history.write_text('{"row": 1}\n{"broken":')
            monitor.append_history(history, [{"row": 2}])
            self.assertEqual([json.loads(line) for line in history.read_text().splitlines()],
                             [{"row": 1}, {"row": 2}])

    def test_text_me_worker_posts_only_text_to_loopback(self):
        from io import StringIO

        with (patch.object(monitor.http.client, "HTTPConnection") as connection,
              patch.object(monitor.sys, "stdin", StringIO("fixture notification"))):
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = '{"ok": true, "result": "fixture"}'
            self.assertEqual(monitor.text_me_worker(), 0)
            connection.assert_called_once_with("127.0.0.1", monitor.TEXT_PORT, timeout=10)
            method, route, body, _ = connection.return_value.request.call_args.args
            self.assertEqual((method, route), ("POST", "/text"))
            self.assertEqual(json.loads(body), {"text": "fixture notification"})

    def test_text_me_worker_distinguishes_failed_delivery_from_timeout(self):
        from io import StringIO

        for failed_at, error, status, body, expected in (
                ("connect", ConnectionRefusedError(), 200, "", monitor.TEXT_NOT_SENT),
                ("getresponse", TimeoutError(), 200, "", monitor.TEXT_UNCONFIRMED),
                (None, None, 503, "", monitor.TEXT_NOT_SENT),
                (None, None, 200, '{"ok": false}', monitor.TEXT_NOT_SENT),
                (None, None, 200, "invalid", monitor.TEXT_UNCONFIRMED)):
            with (self.subTest(failed_at=failed_at, status=status, body=body),
                  patch.object(monitor.http.client, "HTTPConnection") as factory,
                  patch.object(monitor.sys, "stdin", StringIO("fixture notification"))):
                connection = factory.return_value
                if failed_at:
                    getattr(connection, failed_at).side_effect = error
                response = connection.getresponse.return_value
                response.status = status
                response.read.return_value = body
                self.assertEqual(monitor.text_me_worker(), expected)

    def test_pluggable_notifier_receives_text_on_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            received = Path(directory) / "received"
            command = [sys.executable, "-c", "import pathlib, sys; "
                       "pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())", str(received)]
            monitor.notify_message("fixture; literal", command)
            self.assertEqual(received.read_text(), "fixture; literal\n")

    def test_portable_launch_agent_contract(self):
        plist = plistlib.loads((monitor.OPS / "com.rever.cliproxy-fleet-monitor.plist").read_bytes())
        self.assertEqual(plist["Label"], "com.rever.cliproxy-fleet-monitor")
        self.assertEqual(plist["StartInterval"], probe.TICK_INTERVAL)
        self.assertTrue(plist["RunAtLoad"])
        self.assertNotIn("KeepAlive", plist)
        self.assertEqual(plist["EnvironmentVariables"]["CLIPROXY_ROOT"], "/Users/rever/cliproxy")
        # Apple's interpreter: macOS Local Network privacy blocks SSH from Homebrew's python3 under launchd.
        self.assertEqual(plist["ProgramArguments"],
                         ["/usr/bin/python3", "-B", "/Users/rever/cliproxy-live/ops/fleet_monitor.py"])

    def test_go_nanosecond_cooldown_is_kept(self):
        # Go emits up to nine fractional digits; Python 3.9 parses them only through parse_time.
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        item = {"cooldowns": [{"reason": "credential_quota", "scope": "credential",
                               "retry_at": "2099-01-01T00:00:00.123456789Z"}]}
        self.assertTrue(probe.local_cooldowns(item, now))
        self.assertEqual(probe.timestamp("2099-01-01T00:00:00.5Z").microsecond, 500000)

    def test_monitor_modules_stay_compatible_with_apple_python_39(self):
        # /usr/bin/python3 is 3.9: no newer syntax, and no `X | Y` annotations evaluated at runtime.
        for name in ("fleet_monitor", "fleet_monitor_probe", "fleet_quota_chart", "fleetctl",
                     "fleet_routing", "claude_usage_resets"):
            source = (monitor.OPS / (name + ".py")).read_text(encoding="utf-8")
            tree = ast.parse(source, feature_version=(3, 9))
            if "from __future__ import annotations" in source:
                continue
            for node in ast.walk(tree):
                notes = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    every = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                    every += [a for a in (node.args.vararg, node.args.kwarg) if a]
                    notes = [a.annotation for a in every if a.annotation]
                    notes += [node.returns] if node.returns else []
                elif isinstance(node, ast.AnnAssign):
                    notes = [node.annotation]
                for note in notes:
                    self.assertFalse(any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr) for n in ast.walk(note)),
                                     f"{name}.py line {note.lineno} uses a runtime X | Y annotation")


if __name__ == "__main__":
    unittest.main()
