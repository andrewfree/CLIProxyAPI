import contextlib
import http.server
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from typing import Any
from unittest import mock

from ops import claude_usage_resets as resets

EMAIL = "person@example.com"


def usage_body(fable: int = 99, weekly: int = 77, cooldown: str | None = None,
               fable_left: int = 1, eligible: bool = True) -> dict[str, Any]:
    """Shape of a 2026-11-11 /api/oauth/usage?cedar_ember=1 response, trimmed."""
    return {
        "limits": [
            {"kind": "weekly_all", "percent": weekly, "resets_at": "2026-11-18T02:00:00+00:00", "scope": None},
            {"kind": "weekly_scoped", "percent": fable, "resets_at": "2026-11-18T02:00:00+00:00",
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        ],
        "weekly_scoped_shares": [
            {"limit_index": 1, "used_percent_of_weekly": fable // 2, "allowance_percent_of_weekly": 50},
        ],
        "cedar_ember": {
            "eligible": eligible, "ineligible_reason": None if eligible else "tier",
            "cooldown_until": cooldown, "next_grant_id": "full-reset-2611",
            "grants": [
                {"id": "full-reset-2611", "label": "One full limit reset", "type": "weekly",
                 "clears": ["seven_day", "seven_day_overage_included"], "resets_left": 1, "usable_now": True,
                 "unavailable_reason": None, "ends_at": "2026-12-10T22:00:00+00:00", "gives_back_weekly": False},
                {"id": "launch-2610-fable", "label": "New model launch: one Fable reset", "type": "full",
                 "clears": ["seven_day_overage_included"], "resets_left": fable_left,
                 "usable_now": fable_left > 0, "unavailable_reason": None if fable_left else "unavailable",
                 "ends_at": "2027-03-09T07:59:00+00:00", "gives_back_weekly": True},
            ],
        },
    }


class FakeProxy(resets.Proxy):
    def __init__(self, usage: list[tuple[int, Any]], claim_response: Any = None, profile_email: str = EMAIL):
        super().__init__("http://proxy.invalid", "unused")
        self.usage = list(usage)
        self.claim_response = claim_response or {"result": "reset", "resets_left": 0}
        self.profile_email = profile_email
        self.calls: list[tuple[str, str, Any]] = []

    def upstream(self, email, method, url, body=None):
        self.calls.append((method, url, body))
        if url == resets.PROFILE_URL:
            return 200, {"account": {"email": self.profile_email}, "organization": {"uuid": "org-1"}}
        if url == resets.USAGE_URL:
            return self.usage.pop(0)
        return 200, self.claim_response

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class SummarizeUsageTest(unittest.TestCase):
    def test_scoped_limit_joins_its_weekly_share(self):
        summary = resets.summarize_usage(usage_body(fable=89, weekly=70))
        self.assertEqual(summary["weekly_percent"], 70)
        self.assertEqual(summary["scoped"]["Fable"]["percent"], 89)
        self.assertEqual(summary["scoped"]["Fable"]["allowance_percent_of_weekly"], 50)
        self.assertEqual(resets.limit_percent(summary, "fable"), 89)
        self.assertEqual(resets.limit_percent(summary, "all"), 70)
        self.assertIsNone(resets.limit_percent(summary, "Opus"))

    def test_grants_are_listed(self):
        grants = resets.summarize_usage(usage_body())["resets"]["grants"]
        self.assertEqual([g["id"] for g in grants], ["full-reset-2611", "launch-2610-fable"])


class CheckClaimableTest(unittest.TestCase):
    def check(self, body, grant="launch-2610-fable"):
        return resets.check_claimable(resets.summarize_usage(body), grant)

    def test_usable_grant_passes(self):
        self.assertEqual(self.check(usage_body())["id"], "launch-2610-fable")

    def test_refusals(self):
        cases = {
            "cooldown": usage_body(cooldown="2026-11-11T17:49:09+00:00"),
            "eligible": usage_body(eligible=False),
            "not usable": usage_body(fable_left=0),
        }
        for message, body in cases.items():
            with self.subTest(message), self.assertRaisesRegex(resets.ResetError, message):
                self.check(body)
        with self.assertRaisesRegex(resets.ResetError, "not offered"):
            self.check(usage_body(), "launch-2610-fable-6mo")
        with self.assertRaisesRegex(resets.ResetError, "malformed"):
            self.check(usage_body(), "../reset")


class ClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name)
        self.receipt = self.state / "receipt.json"

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self, proxy, dry_run=False):
        return resets.claim(proxy, EMAIL, "launch-2610-fable", self.state, self.receipt, dry_run, sleep=lambda _: None)

    def test_claim_posts_claude_code_shape_and_records_before_after(self):
        proxy = FakeProxy([(200, usage_body(fable=100)), (200, usage_body(fable=2, weekly=28, fable_left=0))])
        receipt = self.claim(proxy)
        [(_, url, body)] = proxy.posts()
        self.assertEqual(url, "https://api.anthropic.com/api/organizations/org-1/reset_rate_limits")
        self.assertEqual(body["program"], "cedar_ember")
        self.assertEqual(body["grant_id"], "launch-2610-fable")
        self.assertRegex(body["request_id"], resets.REQUEST_ID_RE)
        self.assertEqual(receipt["result"], "reset")
        self.assertEqual(receipt["after"]["scoped"]["Fable"]["percent"], 2)
        self.assertNotIn("org-1", self.receipt.read_text())

    def test_dry_run_sends_nothing(self):
        receipt = self.claim(FakeProxy([(200, usage_body())]), dry_run=True)
        self.assertEqual(receipt["result"], "dry_run")
        self.assertTrue(self.receipt.exists())

    def test_rate_limited_precheck_retries_then_claims(self):
        proxy = FakeProxy([(429, {}), (429, {}), (200, usage_body()), (200, usage_body(fable=2))])
        self.assertEqual(self.claim(proxy)["result"], "reset")
        self.assertEqual(len(proxy.posts()), 1)

    def test_failed_after_check_keeps_claim_outcome(self):
        proxy = FakeProxy([(200, usage_body())] + [(429, {})] * 6)
        receipt = self.claim(proxy)
        self.assertEqual(receipt["result"], "reset")
        self.assertIn("HTTP 429", receipt["after_error"])
        self.assertEqual(json.loads(self.receipt.read_text())["result"], "reset")

    def test_wrong_identity_or_ineligible_never_posts(self):
        for proxy in (FakeProxy([(200, usage_body())], profile_email="other@example.com"),
                      FakeProxy([(200, usage_body(cooldown="2026-11-11T17:49:09+00:00"))])):
            with self.assertRaises(resets.ResetError):
                self.claim(proxy)
            self.assertEqual(proxy.posts(), [])

    def test_rerun_reuses_request_id(self):
        first = resets.request_id_for(self.state, EMAIL, "launch-2610-fable")
        self.assertEqual(resets.request_id_for(self.state, EMAIL, "launch-2610-fable"), first)
        self.assertNotEqual(resets.request_id_for(self.state, EMAIL, "full-reset-2611"), first)

    def test_threshold_guard_refuses_low_missing_or_non_numeric_fable_without_posting(self):
        def with_fable_percent(value):
            body = usage_body()
            body["limits"][1]["percent"] = value
            return body

        missing = usage_body()
        missing["limits"] = missing["limits"][:1]
        cases = {
            "low": (usage_body(fable=98), "below required 99%"),
            "missing": (missing, "Fable limit is missing"),
            "string": (with_fable_percent("99"), "not a finite number"),
            "bool": (with_fable_percent(True), "not a finite number"),
            "nan": (with_fable_percent(float("nan")), "not a finite number"),
            "inf": (with_fable_percent(float("inf")), "not a finite number"),
            "huge": (with_fable_percent(10 ** 400), "not a finite number"),
        }
        for name, (body, message) in cases.items():
            for dry_run in (False, True):
                proxy = FakeProxy([(200, body)])
                with self.subTest(name, dry_run=dry_run), self.assertRaisesRegex(resets.ResetError, message):
                    resets.claim(proxy, EMAIL, "launch-2610-fable", self.state, self.receipt,
                                 dry_run, sleep=lambda _: None, min_fable_percent=99)
                self.assertEqual(proxy.posts(), [])
                self.assertFalse((self.state / "request-ids.json").exists())
                self.assertFalse(self.receipt.exists())

    def test_threshold_guard_accepts_99_and_100_and_records_precondition(self):
        for percent in (99, 100):
            proxy = FakeProxy([(200, usage_body(fable=percent)), (200, usage_body(fable=2))])
            with self.subTest(percent=percent):
                receipt = resets.claim(proxy, EMAIL, "launch-2610-fable", self.state, self.receipt,
                                       False, sleep=lambda _: None, min_fable_percent=99)
                self.assertEqual(len(proxy.posts()), 1)
                self.assertEqual(receipt["min_fable_percent"], 99)
                self.assertEqual(receipt["before"]["scoped"]["Fable"]["percent"], percent)

    def test_threshold_guard_rejects_invalid_threshold_before_network(self):
        proxy = FakeProxy([])
        for percent in (-1, 101):
            with self.subTest(percent=percent), self.assertRaisesRegex(resets.ResetError, "between 0 and 100"):
                resets.claim(proxy, EMAIL, "launch-2610-fable", self.state, self.receipt,
                             False, min_fable_percent=percent)
        self.assertEqual(proxy.calls, [])


class CliGuardTest(unittest.TestCase):
    def test_claim_requires_yes_or_dry_run(self):
        args = type("Args", (), {"yes": False, "dry_run": False})()
        self.assertEqual(resets.cmd_claim(FakeProxy([]), args), 2)

    def run_claim_cli(self, proxy, extra):
        """Run `main` with a fake proxy and a temporary state dir; never reads .env."""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(resets, "load_management_key", return_value="unused"), \
                mock.patch.object(resets, "Proxy", return_value=proxy), \
                mock.patch.object(resets, "STATE_DIR", pathlib.Path(tmp)), \
                mock.patch.object(resets, "claim", wraps=resets.claim) as spy, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = resets.main(["claim", "--account", EMAIL, "--grant", "launch-2610-fable",
                                "--receipt", str(pathlib.Path(tmp) / "receipt.json"), *extra])
        return code, spy, err.getvalue()

    def test_cli_min_fable_percent_reaches_claim_and_refuses(self):
        proxy = FakeProxy([(200, usage_body(fable=98))])
        code, spy, err = self.run_claim_cli(proxy, ["--yes", "--min-fable-percent", "99"])
        self.assertEqual(code, 1)
        self.assertEqual(spy.call_args.kwargs["min_fable_percent"], 99)
        self.assertIn("below required 99%", err)
        self.assertEqual(proxy.posts(), [])

    def test_bundled_copy_refuses_to_run_as_a_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = pathlib.Path(tmp).resolve() / ".fleet-releases" / "20261118T000000Z-test"
            (release / "ops").mkdir(parents=True)
            script = release / "ops" / "claude_usage_resets.py"
            script.write_bytes(pathlib.Path(resets.__file__).read_bytes())
            result = subprocess.run([sys.executable, "-B", str(script), "status"],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2)
            self.assertIn("not the ops bundle", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(sorted(path.name for path in release.rglob("*")), ["claude_usage_resets.py", "ops"])

    def test_cli_without_min_fable_percent_passes_none(self):
        proxy = FakeProxy([(200, usage_body(fable=98))])
        code, spy, _ = self.run_claim_cli(proxy, ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIsNone(spy.call_args.kwargs["min_fable_percent"])
        self.assertEqual(proxy.posts(), [])


WILL = {"ssh": "will.local", "instance_root": "/home/andrew/cliproxy", "port": 8317}
UNREACHABLE = (resets.SSH_FAILED, "", True)


class HostSelectionTest(unittest.TestCase):
    def run_main(self, argv, will_result=(0, "", False), will=WILL):
        """Run `main` with Will's SSH session and the local proxy replaced by recorders."""
        calls = {"will": [], "local": []}

        def ask_will(argv_, host, live):
            calls["will"].append(live)
            return will_result

        def run_local(args, label="the local proxy"):
            calls["local"].append(args.command)
            return 0

        with mock.patch.object(resets, "will_host", return_value=will), \
                mock.patch.object(resets, "ask_will", side_effect=ask_will), \
                mock.patch.object(resets, "run_local", side_effect=run_local), \
                mock.patch.object(resets, "notify") as notify, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = resets.main(argv)
        return code, calls, out.getvalue(), err.getvalue(), notify

    def test_status_is_answered_by_will_without_the_local_proxy(self):
        code, calls, out, _, _ = self.run_main(["status"], (0, "report\n", False))
        self.assertEqual((code, out), (0, "report\n"))
        self.assertEqual(calls, {"will": [False], "local": []})

    def test_status_falls_back_only_when_will_is_unreachable(self):
        for result in (UNREACHABLE, (resets.PROXY_UNREACHABLE, "partial\n", True)):
            with self.subTest(code=result[0]):
                code, calls, out, err, _ = self.run_main(["status"], result)
                self.assertEqual((code, calls["local"]), (0, ["status"]))
                self.assertNotIn("partial", out)
                self.assertIn("did not answer; asking the local proxy", err)
        code, calls, out, _, _ = self.run_main(["status"], (1, "", False))
        self.assertEqual((code, calls["local"]), (1, []))

    def test_named_hosts_never_fall_back(self):
        code, calls, _, err, _ = self.run_main(["status", "--host", "will"], UNREACHABLE)
        self.assertEqual((code, calls["local"]), (resets.SSH_FAILED, []))
        self.assertIn("SSH session to Will (will.local) failed", err)
        code, calls, _, _, _ = self.run_main(["status", "--host", "local"])
        self.assertEqual(calls, {"will": [], "local": ["status"]})

    def test_claim_runs_only_on_the_named_host(self):
        claim = ["claim", "--account", EMAIL, "--grant", "launch-2610-fable", "--dry-run"]
        code, calls, _, _, _ = self.run_main(claim)
        self.assertEqual((code, calls), (0, {"will": [], "local": ["claim"]}))
        code, calls, _, err, _ = self.run_main(claim + ["--host", "auto"])
        self.assertEqual((code, calls), (2, {"will": [], "local": []}))
        self.assertIn("pass --host will or --host local", err)
        code, calls, _, _, _ = self.run_main(claim + ["--host", "will"], UNREACHABLE)
        self.assertEqual((code, calls), (resets.SSH_FAILED, {"will": [False], "local": []}))

    def test_host_will_without_a_fleet_entry_refuses(self):
        code, calls, _, err, _ = self.run_main(["status", "--host", "will"], will=None)
        self.assertEqual((code, calls), (2, {"will": [], "local": []}))
        self.assertIn("needs Will's entry", err)

    def test_watch_streams_and_never_switches_hosts_after_output(self):
        code, calls, _, _, _ = self.run_main(["watch", "--account", EMAIL], (resets.SSH_FAILED, "poll\n", True))
        self.assertEqual((code, calls), (resets.SSH_FAILED, {"will": [True], "local": []}))
        code, calls, _, _, _ = self.run_main(["watch", "--account", EMAIL], UNREACHABLE)
        self.assertEqual((code, calls), (0, {"will": [True], "local": ["watch"]}))

    def test_watch_trigger_on_will_notifies_on_this_host(self):
        argv = ["watch", "--account", EMAIL, "--notify"]
        _, _, _, _, notify = self.run_main(argv, (0, "TRIGGER\n", False))
        notify.assert_called_once()
        _, _, _, _, notify = self.run_main(argv, (2, "poll failed\n", False))
        notify.assert_not_called()


class WillHostTest(unittest.TestCase):
    def test_reads_will_from_the_fleet_hosts_file(self):
        self.assertEqual(resets.will_host(), WILL)

    def test_none_inside_wills_instance_without_hosts_or_with_a_bad_destination(self):
        with mock.patch.object(resets, "ROOT", pathlib.Path(WILL["instance_root"])):
            self.assertIsNone(resets.will_host())
        with tempfile.TemporaryDirectory() as tmp:
            hosts = pathlib.Path(tmp) / "hosts.json"
            with mock.patch.object(resets, "HOSTS_FILE", hosts):
                self.assertIsNone(resets.will_host())
                hosts.write_text(json.dumps({"will": dict(WILL, ssh="-oProxyCommand=x")}))
                self.assertIsNone(resets.will_host())


class AskWillTest(unittest.TestCase):
    def test_sends_this_source_over_ssh_stdin_and_streams_watch_output(self):
        sent = {}

        class FakePopen:
            def __init__(self, argv, **kwargs):
                sent["argv"] = argv
                self.stdin = io.StringIO()
                self.stdin.close = lambda: sent.setdefault("program", self.stdin.getvalue())
                self.stdout = iter(["poll 1\n", "poll 2\n"])

            def wait(self):
                return 0

        with mock.patch.object(resets.subprocess, "Popen", FakePopen), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            result = resets.ask_will(["watch", "--account", EMAIL], WILL, live=True)
        self.assertEqual(result, (0, "poll 1\npoll 2\n", False))
        self.assertEqual(out.getvalue(), "poll 1\npoll 2\n")
        self.assertEqual(sent["argv"][-6:], ["--", "will.local", "python3", "-I", "-B", "-"])
        self.assertIn("'/home/andrew/cliproxy/ops/claude_usage_resets.py'", sent["program"])
        self.assertIn("run_remote(['watch', '--account', 'person@example.com']", sent["program"])
        self.assertNotIn(EMAIL, " ".join(sent["argv"]))

    def test_ssh_that_cannot_start_counts_as_unreachable(self):
        with mock.patch.object(resets.subprocess, "Popen", side_effect=FileNotFoundError("ssh")):
            self.assertEqual(resets.ask_will(["status"], WILL, live=False), UNREACHABLE)


class RemoteProgramTest(unittest.TestCase):
    """Run the program Will would receive, here, against a fake management API."""

    def run_program(self, root, port):
        program = resets.remote_program(["status", "--host", "auto"], dict(WILL, instance_root=root, port=port))
        return subprocess.run([sys.executable, "-I", "-B", "-"], input=program,
                              capture_output=True, text=True, timeout=60)

    def test_uses_the_remote_roots_key_and_reports_an_unreachable_proxy(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                body = json.dumps({"files": []}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, ".env").write_text("MANAGEMENT_PASSWORD=remote-key\n")
            server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
            port = server.server_port
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                answered = self.run_program(tmp, port)
            finally:
                server.shutdown()
                server.server_close()
            refused = self.run_program(tmp, port)
        self.assertEqual(answered.returncode, 1, answered.stderr)  # the fake proxy has no Claude accounts
        self.assertEqual(seen, [("/v0/management/auth-files", "Bearer remote-key")])
        self.assertIn(f"answered by Will (will.local) at http://127.0.0.1:{port}", answered.stderr)
        self.assertEqual(refused.returncode, resets.PROXY_UNREACHABLE, refused.stderr)
        self.assertIn(f"no CLIProxy answered at http://127.0.0.1:{port}", refused.stderr)
        self.assertNotIn("Traceback", answered.stderr + refused.stderr)


if __name__ == "__main__":
    unittest.main()
