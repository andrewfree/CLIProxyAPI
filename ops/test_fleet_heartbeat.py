"""Offline tests for the Mint watcher and its separate Mac text relay."""

from datetime import datetime, timezone
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ops import fleet_heartbeat as heartbeat, fleet_monitor as monitor


NOW = 1795440000


class FleetHeartbeatTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name)
        self.mint = folder / "mint"
        self.mac = folder / "mac"
        self.mac.mkdir()

    def write_snapshot(self, epoch):
        checked_at = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
        (self.mac / "health.json").write_text(json.dumps({"checked_at": checked_at}), encoding="utf-8")

    def test_mint_ages_a_snapshot_without_a_sync_and_keeps_it_on_missing_input(self):
        self.assertEqual(heartbeat.mint_status(self.mint, NOW), "waiting")
        self.assertEqual(heartbeat.mint_status(self.mint, NOW, NOW - 60, sync=True), "fresh")
        deadline = NOW - 60 + heartbeat.MAX_AGE
        self.assertEqual(heartbeat.mint_status(self.mint, deadline), "fresh")
        self.assertEqual(heartbeat.mint_status(self.mint, deadline + 1), "stale")
        self.assertEqual(heartbeat.mint_status(self.mint, deadline + 1, sync=True), "stale")
        self.assertEqual(heartbeat.mint_status(self.mint, deadline + 1, NOW - 120, sync=True), "stale")
        self.assertEqual(heartbeat.mint_status(self.mint, deadline + 1, deadline, sync=True), "fresh")

    def test_mint_accepts_an_initial_timestamp_after_a_missing_snapshot(self):
        self.assertEqual(heartbeat.mint_status(self.mint, NOW, sync=True), "fresh")
        self.assertEqual(heartbeat.mint_status(self.mint, NOW + heartbeat.MAX_AGE + 1), "stale")
        self.assertEqual(heartbeat.mint_status(self.mint, NOW + 1, NOW - heartbeat.MAX_AGE, sync=True),
                         "stale")

    def test_mint_runs_the_standalone_cli_without_installed_monitor_modules(self):
        script = self.mint.parent / "fleet_heartbeat.py"
        script.write_bytes(Path(heartbeat.__file__).read_bytes())
        args = [sys.executable, "-I", "-B", str(script), "--state", str(self.mint)]
        old_epoch = int(time.time()) - heartbeat.MAX_AGE - 1
        sync = subprocess.run([*args, "sync"], input=f"{old_epoch}\n", text=True, capture_output=True, check=True)
        watch = subprocess.run([*args, "watch"], text=True, capture_output=True, check=True)
        self.assertEqual(sync.stdout.strip(), "stale")
        self.assertEqual(watch.stdout.strip(), "stale")

    def test_relay_texts_once_per_stale_episode_and_rearms_after_freshness(self):
        now = NOW

        def mint_exchange(sample):
            return heartbeat.mint_status(self.mint, now, sample, sync=True)

        with (patch.object(heartbeat, "ask_mint", side_effect=mint_exchange) as exchange,
              patch.object(monitor, "notify_message", return_value=monitor.TEXT_SENT) as notify):
            self.write_snapshot(now - heartbeat.MAX_AGE - 1)
            self.assertEqual(heartbeat.relay(self.mac), 0)
            exchange.assert_called_once_with(now - heartbeat.MAX_AGE - 1)
            self.assertEqual(heartbeat.relay(self.mac), 0)
            notify.assert_called_once_with(heartbeat.STALE_ALERT, None)
            self.assertEqual(monitor.read_json(self.mac / "heartbeat-alert.json", {})["status"], "sent")

            self.write_snapshot(now)
            self.assertEqual(heartbeat.relay(self.mac), 0)
            self.assertFalse((self.mac / "heartbeat-alert.json").exists())
            now += heartbeat.MAX_AGE + 1
            self.assertEqual(heartbeat.relay(self.mac), 0)
            self.assertEqual(notify.call_count, 2)

    def test_relay_retries_definite_failure_and_suppresses_ambiguous_delivery(self):
        with (patch.object(heartbeat, "ask_mint", return_value="stale"),
              patch.object(monitor, "notify_message", side_effect=[monitor.TEXT_NOT_SENT,
                                                                   monitor.TEXT_UNCONFIRMED]) as notify,
              patch.object(sys, "stderr")):
            self.assertEqual(heartbeat.relay(self.mac), 1)
            self.assertFalse((self.mac / "heartbeat-alert.json").exists())
            self.assertEqual(heartbeat.relay(self.mac), 1)
            self.assertEqual(heartbeat.relay(self.mac), 0)
            self.assertEqual(notify.call_count, 2)
            self.assertEqual(monitor.read_json(self.mac / "heartbeat-alert.json", {})["status"], "unconfirmed")

    def test_relay_preserves_delivery_claim_when_mint_cannot_be_reached(self):
        receipt = self.mac / "heartbeat-alert.json"
        monitor.write_json(receipt, {"status": "attempting"})
        with (patch.object(heartbeat, "ask_mint", side_effect=[subprocess.TimeoutExpired("ssh", 15), "stale"]),
              patch.object(monitor, "notify_message") as notify,
              patch.object(sys, "stderr")):
            self.assertEqual(heartbeat.main(["relay", "--runtime", str(self.mac)]), 1)
            self.assertEqual(heartbeat.relay(self.mac), 0)
            self.assertEqual(monitor.read_json(receipt, {}), {"status": "attempting"})
            notify.assert_not_called()

    def test_relay_launchagent_runs_on_its_own_schedule(self):
        ops = Path(heartbeat.__file__).resolve().parent
        name = "com.rever.cliproxy-fleet-heartbeat"
        plist = plistlib.loads((ops / (name + ".plist")).read_bytes())
        self.assertEqual(plist["Label"], name)
        self.assertEqual(plist["StartInterval"], 300)
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["ProgramArguments"],
                         ["/usr/bin/python3", "-B", "/Users/rever/cliproxy-live/ops/fleet_heartbeat.py", "relay"])
        self.assertNotIn("KeepAlive", plist)
