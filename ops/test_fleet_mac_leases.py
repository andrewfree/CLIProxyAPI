from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from ops import fleet_mac


class LeaseStorageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        (self.bundle / "fleet_mac.py").write_text("def read_leases(root): pass", encoding="utf-8")
        (self.bundle / "cliproxy_watchdog.py").write_text("fleet_mac.read_leases(root)", encoding="utf-8")
        self.now = datetime(2026, 11, 22, 5, 4, tzinfo=timezone.utc)
        clock = patch.object(fleet_mac, "datetime", wraps=datetime)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.clock.now.return_value = self.now

    def acquire(self, **overrides):
        fields = dict(thread="work-thread", project="omxlapi", purpose="benchmark", why_mac="local sandbox")
        return fleet_mac.acquire_lease(self.root, self.bundle, **{**fields, **overrides})

    def test_acquire_renew_release_and_exact_expiry(self):
        lease = self.acquire(approved="Go ahead")
        lease_id = lease["id"]
        self.assertRegex(lease_id, r"^[0-9a-f]{16}$")
        directory = fleet_mac.lease_dir(self.root)
        filename = directory / f"{lease_id}.json"
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(filename.stat().st_mode), 0o600)
        self.assertEqual(json.loads(filename.read_text()), lease)
        self.assertEqual(lease["created_at"], self.now.isoformat())
        self.assertEqual(lease["expires_at"], (self.now + timedelta(hours=3)).isoformat())

        self.clock.now.return_value = self.now + timedelta(hours=1)
        renewed = fleet_mac.renew_lease(self.root, lease_id, hours=12)
        self.assertEqual(renewed["expires_at"], (self.now + timedelta(hours=13)).isoformat())
        self.assertEqual(renewed["created_at"], lease["created_at"])
        self.assertEqual(stat.S_IMODE(filename.stat().st_mode), 0o600)

        self.clock.now.return_value = self.now + timedelta(hours=13)
        self.assertFalse(fleet_mac.read_leases(self.root)[0]["live"])
        with self.assertRaisesRegex(fleet_mac.MacError, "expired"):
            fleet_mac.renew_lease(self.root, lease_id, hours=3)
        self.assertFalse(fleet_mac.release_lease(self.root, lease_id))
        self.assertEqual(fleet_mac.read_leases(self.root), [])

    def test_acquire_validates_hours_and_metadata(self):
        for duration in (0, -1, 13, 1e-15, float("nan"), float("inf")):
            with self.subTest(hours=duration), self.assertRaisesRegex(fleet_mac.MacError, "--hours"):
                self.acquire(hours=duration)
        for purpose in ("", " ", "a\nb", "x" * 201):
            with self.subTest(purpose=purpose), self.assertRaisesRegex(fleet_mac.MacError, "--purpose"):
                self.acquire(purpose=purpose)
        with self.assertRaisesRegex(fleet_mac.MacError, "hexadecimal"):
            fleet_mac.release_lease(self.root, "../mac-paused")
        self.assertEqual(fleet_mac.read_leases(self.root), [])

    def test_acquire_refuses_an_older_watchdog(self):
        (self.bundle / "cliproxy_watchdog.py").write_text("fleet_mac.read_marker(root)", encoding="utf-8")
        with self.assertRaisesRegex(fleet_mac.MacError, "Ship the bundle first"):
            self.acquire()
        self.assertEqual(fleet_mac.read_leases(self.root), [])

    def test_acquire_times_out_when_transition_lock_is_held(self):
        with fleet_mac.transition_lock(self.root):
            with (patch.object(fleet_mac.time, "monotonic", side_effect=[100, 100, 130]),
                  patch.object(fleet_mac.time, "sleep") as sleep,
                  self.assertRaisesRegex(fleet_mac.MacError, "Mac transition in progress; retry shortly")):
                self.acquire()
        sleep.assert_called_once_with(0.25)
        self.assertEqual(fleet_mac.read_leases(self.root), [])

    def test_acquire_cannot_overwrite_a_lease_with_a_colliding_id(self):
        first, second = "a" * 16, "b" * 16
        with patch.object(fleet_mac.secrets, "token_hex", side_effect=[first, first, second]):
            self.assertEqual(self.acquire()["id"], first)
            self.assertEqual(self.acquire(project="other")["id"], second)
        self.assertEqual([(lease["id"], lease["project"]) for lease in fleet_mac.read_leases(self.root)],
                         [(first, "omxlapi"), (second, "other")])

    def test_only_lease_names_count_and_bad_contents_are_live(self):
        folder = fleet_mac.lease_dir(self.root)
        folder.mkdir(parents=True)
        lease_id = "a" * 16
        for name in (f".tmp-{lease_id}.json", "garbage.json", "f" * 15 + ".json", "B" * 16 + ".json"):
            (folder / name).write_text("{")
        self.assertEqual(fleet_mac.read_leases(self.root), [])
        (folder / f"{lease_id}.json").write_text("{")
        self.assertEqual(fleet_mac.live_leases(self.root), [{"id": lease_id, "live": True, "unknown": True}])
        with self.assertRaisesRegex(fleet_mac.MacError, "unreadable"):
            fleet_mac.release_lease(self.root, lease_id)

    def test_file_and_directory_permission_errors_fail_closed(self):
        lease_id = self.acquire()["id"]
        with patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertEqual(fleet_mac.live_leases(self.root), [{"id": lease_id, "live": True, "unknown": True}])
        with patch.object(Path, "iterdir", side_effect=PermissionError):
            self.assertEqual(fleet_mac.live_leases(self.root), [{"id": None, "live": True, "unknown": True}])
