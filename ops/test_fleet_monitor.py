"""Offline tests for the fleet monitor and its LaunchAgent."""

from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        self.assertEqual(absent, {"reachability": "unreachable", "uncertain": False})


class MonitorTests(unittest.TestCase):
    def test_tick_cadence_daily_summary_alerts_and_history(self):
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(monitor, "DAILY_AT", time(0)),
              patch.object(monitor, "run_host") as runs,
              patch.object(monitor, "run_parity", side_effect=[{"status": "equal"}, {"status": "drift"}]) as parity,
              patch.object(monitor, "notify_message") as notify):
            root = Path(directory)

            for offset, reading, will_state, will_egress in ((0, 89, "ok", "ok"),
                      (300, 92, "unreachable", "unknown"), (600, 92, "unreachable", "unknown"),
                      (3600, 100, "ok", "down"), (3900, 100, "ok", "down")):
                reading_now = NOW + timedelta(seconds=offset)
                current = sample(percent=reading)
                current["attempted_at"] = reading_now.timestamp()
                runs.side_effect = [host_report([current]), host_report(state=will_state, egress=will_egress)]
                with patch.object(monitor, "now_utc", return_value=reading_now):
                    monitor.tick(root, deepcopy(HOSTS), root, [], no_notify=False)
                self.assertEqual(runs.call_args.args[2], {"test-account"})
                if offset == 300:
                    self.assertEqual(runs.call_args.args[3]["test-account"], NOW.timestamp() + 300)

            self.assertEqual(parity.call_count, 2)
            messages = [call.args[0] for call in notify.call_args_list]
            self.assertEqual(len(messages), 3)
            self.assertEqual(sum("daily summary" in message for message in messages), 1)
            self.assertIn("Will unreachable", messages[1])
            self.assertIn("92% used", messages[1])
            self.assertIn("Will egress down", messages[2])
            self.assertIn("drift appeared", messages[2])
            self.assertIn("weekly capped", messages[2])
            history = [json.loads(line) for line in (root / "quota-history.jsonl").read_text().splitlines()]
            self.assertEqual(len(history), 5)
            self.assertEqual(history[-1]["windows"][0]["used_percent"], 100)
            self.assertEqual(stat.S_IMODE((root / "quota-history.jsonl").stat().st_mode), 0o600)
            health = monitor.read_json(root / "health.json", {})
            self.assertEqual(health["hosts"]["will"]["status"], "egress-down")
            latest = monitor.read_json(root / "latest.json", {})
            self.assertEqual(latest["status"], "drift")
            self.assertEqual(latest["health"]["will"]["status"], "egress-down")

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

    def test_notification_timeout_does_not_resend_claimed_transition(self):
        with (tempfile.TemporaryDirectory() as directory,
              patch.object(monitor, "DAILY_AT", time(0)),
              patch.object(monitor, "now_utc", return_value=NOW),
              patch.object(monitor, "run_host", return_value=host_report(state="unreachable")),
              patch.object(monitor, "run_parity", return_value={"status": "unreachable"}),
              patch.object(monitor, "notify_message", side_effect=subprocess.TimeoutExpired("fake", 1)) as notify,
              patch("sys.stderr")):
            root = Path(directory)
            monitor.tick(root, deepcopy(HOSTS), root, [], no_notify=False)
            monitor.tick(root, deepcopy(HOSTS), root, [], no_notify=False)
            notify.assert_called_once()
            health = monitor.read_json(root / "health.json", {})
            self.assertEqual(health["last_notification"]["status"], "unconfirmed")
            state = monitor.read_json(root / "monitor-state.json", {})
            self.assertEqual(state["outages"], ["Mac proxy unreachable", "Will proxy unreachable"])

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
        self.assertEqual(plist["StartInterval"], 300)
        self.assertTrue(plist["RunAtLoad"])
        self.assertNotIn("KeepAlive", plist)
        self.assertEqual(plist["EnvironmentVariables"]["CLIPROXY_ROOT"], "/Users/rever/cliproxy")
        self.assertEqual(plist["ProgramArguments"],
                         ["/opt/homebrew/bin/python3", "-B", "/Users/rever/cliproxy-live/ops/fleet_monitor.py"])


if __name__ == "__main__":
    unittest.main()
