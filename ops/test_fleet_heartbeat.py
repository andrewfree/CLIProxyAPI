"""Offline tests for the Mac heartbeat relay and its Mint sync peer."""

from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ops import fleet_heartbeat as heartbeat


NOW = 1795440000
OLD = NOW - heartbeat.MAX_AGE - 1


class FleetHeartbeatTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name)
        self.mint = folder / "mint"
        self.mac = folder / "mac"
        self.mac.mkdir()
        self.receipt = self.mac / "heartbeat-alert.json"
        self.now = NOW
        self.awake = NOW - 3600
        self.patch(heartbeat.time, "time", side_effect=lambda: self.now)
        self.patch(heartbeat, "awake_since", side_effect=lambda: self.awake)
        self.patch(sys, "stderr", new=StringIO())
        self.desktop = self.patch(heartbeat.subprocess, "run", return_value=None)
        self.exchange = self.patch(heartbeat, "ask_mint", side_effect=self.mint_exchange)
        self.send = self.patch(heartbeat, "post_text", return_value=True)

    def patch(self, obj, name, **options):
        patcher = patch.object(obj, name, **options)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def mint_exchange(self, sample, now):
        return heartbeat.mint_status(self.mint, now, sample, sync=True)["status"]

    def write_snapshot(self, epoch):
        checked_at = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (self.mac / "health.json").write_text(json.dumps({"checked_at": checked_at}), encoding="utf-8")

    def test_mint_records_sync_time_and_ages_last_observation(self):
        self.assertEqual(heartbeat.mint_status(self.mint, NOW)["status"], "waiting")
        self.assertEqual(heartbeat.mint_status(self.mint, NOW, NOW - 60, sync=True),
                         {"status": "fresh", "synced_at": NOW, "checked_at": NOW - 60})
        deadline = NOW - 60 + heartbeat.MAX_AGE
        self.assertEqual(heartbeat.mint_status(self.mint, deadline)["status"], "fresh")
        self.assertEqual(heartbeat.mint_status(self.mint, deadline + 1)["status"], "stale")
        self.assertEqual(heartbeat.mint_status(self.mint, deadline + 1, sync=True),
                         {"status": "stale", "synced_at": deadline + 1, "checked_at": NOW - 60})
        self.assertEqual(heartbeat.mint_status(self.mint, deadline + 2, NOW - 120, sync=True)["status"], "stale")
        self.assertEqual(heartbeat.mint_status(self.mint, deadline + 3, deadline, sync=True)["status"], "fresh")

    def test_relay_runs_in_isolation_without_monitor_modules(self):
        script = self.mint.parent / "fleet_heartbeat.py"
        shutil.copyfile(heartbeat.__file__, script)
        self.write_snapshot(NOW)
        # Only replace the external SSH transport and wake time in the isolated interpreter.
        harness = """
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import patch

now = int(sys.argv[3])
relay = runpy.run_path(sys.argv[1])["main"]
with patch.dict(relay.__globals__, {"awake_since": lambda: now - 3600,
                                   "time": SimpleNamespace(time=lambda: now),
                                   "ask_mint": lambda snapshot, now: "fresh"}):
    sys.exit(relay(["relay", "--runtime", sys.argv[2]]))
"""
        run = subprocess.Popen([sys.executable, "-I", "-B", "-c", harness, str(script), str(self.mac), str(NOW)],
                               cwd=script.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _, errors = run.communicate(timeout=10)
        self.assertEqual(run.returncode, 0, errors)
        self.assertEqual(heartbeat.read_state(self.mac / "heartbeat-watch.json", {})["checked_at"], NOW)

    def test_relay_alerts_on_two_stale_snapshots_without_a_fresh_poll(self):
        self.write_snapshot(OLD)
        self.assertEqual(heartbeat.relay(self.mac), 0)
        self.assertEqual(heartbeat.relay(self.mac), 0)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(heartbeat.read_state(self.receipt, {})["status"], "acknowledged")
        self.now += 2 * heartbeat.MAX_AGE
        self.write_snapshot(OLD + heartbeat.MAX_AGE)
        self.assertEqual(heartbeat.relay(self.mac), 0)
        self.assertEqual(self.send.call_count, 2)
        self.assertNotEqual(self.send.call_args_list[0].args[1], self.send.call_args_list[1].args[1])

    def test_interrupted_and_failed_attempts_retry_the_same_request_until_acknowledged(self):
        self.write_snapshot(OLD)
        self.send.side_effect = [KeyboardInterrupt, False, True]
        with self.assertRaises(KeyboardInterrupt):
            heartbeat.relay(self.mac)
        self.assertEqual(heartbeat.read_state(self.receipt, {})["status"], "pending")
        self.now += heartbeat.POLL_INTERVAL
        self.assertEqual(heartbeat.relay(self.mac), 1)
        self.desktop.assert_called_once()
        self.now += heartbeat.POLL_INTERVAL
        self.assertEqual(heartbeat.relay(self.mac), 0)
        self.assertEqual(heartbeat.relay(self.mac), 0)
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.send.call_args_list, [self.send.call_args] * 3)
        self.assertEqual(heartbeat.read_state(self.receipt, {})["status"], "acknowledged")

    def test_legacy_attempting_receipt_does_not_suppress_a_stale_alert(self):
        heartbeat.write_state(self.receipt, {"status": "attempting"})
        self.write_snapshot(OLD)
        self.assertEqual(heartbeat.relay(self.mac), 0)
        self.send.assert_called_once()

    def test_relay_ignores_presleep_age_until_the_monitor_has_time_to_write(self):
        self.write_snapshot(OLD - 3600)
        self.awake = NOW - 30
        self.assertEqual(heartbeat.relay(self.mac), 0)
        self.now += 105
        self.write_snapshot(self.now)
        self.assertEqual(heartbeat.relay(self.mac), 0)
        self.send.assert_not_called()

        self.now += heartbeat.MAX_AGE + 1
        self.assertEqual(heartbeat.relay(self.mac), 0)
        self.send.assert_called_once()

    def test_prolonged_mint_failure_uses_local_age_and_alerts_once(self):
        self.write_snapshot(OLD)
        self.exchange.side_effect = subprocess.TimeoutExpired("ssh", 15)
        self.assertEqual(heartbeat.relay(self.mac), 1)
        self.now += heartbeat.MAX_AGE
        self.assertEqual(heartbeat.relay(self.mac), 1)
        self.send.assert_not_called()

        self.now += 1
        self.assertEqual(heartbeat.relay(self.mac), 1)
        self.assertEqual(heartbeat.relay(self.mac), 1)
        self.send.assert_called_once()
        self.assertIn("40 minutes old", self.send.call_args.args[0])
        self.assertIn("could not reach or validate its Mint peer", self.send.call_args.args[0])
        self.assertIn("could not sync with Mint", sys.stderr.getvalue())
        self.desktop.assert_called_once()
        self.send.reset_mock()
        self.exchange.side_effect = self.mint_exchange
        self.assertEqual(heartbeat.relay(self.mac), 0)
        self.send.assert_not_called()

    def test_mint_clock_skew_and_corrupt_reply_use_local_age(self):
        self.write_snapshot(OLD)
        with patch.object(heartbeat, "ask_mint", new=REAL_ASK_MINT):
            self.desktop.side_effect = [
                subprocess.CompletedProcess([], 0, '{"status":"fresh","synced_at":' + str(NOW - 100) + '}'),
                subprocess.CompletedProcess([], 0, '{"broken":'),
                None,
            ]
            self.assertEqual(heartbeat.relay(self.mac), 1)
            self.now += heartbeat.MAX_AGE + 1
            self.assertEqual(heartbeat.relay(self.mac), 1)
        self.send.assert_called_once()
        self.assertIn("could not reach or validate its Mint peer", self.send.call_args.args[0])
        self.assertIn("JSONDecodeError", sys.stderr.getvalue())

    def test_missing_snapshot_ages_from_first_observation(self):
        self.assertEqual(heartbeat.relay(self.mac), 0)
        self.send.assert_not_called()
        self.now += heartbeat.MAX_AGE + 1
        self.assertEqual(heartbeat.relay(self.mac), 0)
        self.assertIn("No monitor snapshot", self.send.call_args.args[0])

    def test_text_me_uses_idempotency_header_and_requires_an_affirmative_response(self):
        with (patch.object(heartbeat, "post_text", new=REAL_POST_TEXT),
              patch.object(heartbeat.http.client, "HTTPConnection") as connect):
            connection = connect.return_value
            accepted = BytesIO(b'{"ok":true}')
            accepted.status = 200
            refused = BytesIO(b'{"ok":false}')
            refused.status = 200
            connection.request.side_effect = [ConnectionRefusedError, None, None, None]
            connection.getresponse.side_effect = [refused, accepted, TimeoutError]
            self.assertFalse(heartbeat.post_text("alert", "episode-key"))
            self.assertFalse(heartbeat.post_text("alert", "episode-key"))
            self.assertTrue(heartbeat.post_text("alert", "episode-key"))
            self.assertFalse(heartbeat.post_text("alert", "episode-key"))
            connection.request.assert_called_with(
                "POST", "/text", '{"text": "alert"}',
                {"Content-Type": "application/json", "Idempotency-Key": "episode-key"},
            )

    def test_relay_launchagent_runs_on_its_own_schedule(self):
        ops = Path(heartbeat.__file__).resolve().parent
        name = "com.rever.cliproxy-fleet-heartbeat"
        plist = plistlib.loads((ops / (name + ".plist")).read_bytes())
        self.assertEqual(plist["Label"], name)
        self.assertEqual(plist["StartInterval"], heartbeat.POLL_INTERVAL)
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["ProgramArguments"],
                         ["/usr/bin/python3", "-I", "-B", "/Users/rever/cliproxy-live/ops/fleet_heartbeat.py", "relay"])
        self.assertNotIn("KeepAlive", plist)


REAL_ASK_MINT = heartbeat.ask_mint
REAL_POST_TEXT = heartbeat.post_text
