import json
import plistlib
import unittest
from pathlib import Path


class LaunchdPlistTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ops = Path(__file__).resolve().parent
        self.host = json.loads((self.ops / "fleet/hosts.json").read_text(encoding="utf-8"))["m5"]

    def test_watchdog_runs_frequently_with_bounded_output(self) -> None:
        path = self.ops / "com.rever.cliproxy-watchdog.plist"

        with path.open("rb") as handle:
            plist = plistlib.load(handle)

        self.assertEqual(plist["Label"], "com.rever.cliproxy-watchdog")
        self.assertTrue(plist["RunAtLoad"])
        self.assertLessEqual(plist["StartInterval"], 180)
        self.assertEqual(plist["WorkingDirectory"], self.host["instance_root"])
        self.assertEqual(plist["EnvironmentVariables"]["CLIPROXY_ROOT"], self.host["instance_root"])
        self.assertIn(
            str(Path(self.host["bundle_root"]) / "cliproxy_watchdog.py"),
            plist["ProgramArguments"],
        )
        self.assertEqual(plist["StandardOutPath"], "/dev/null")
        self.assertEqual(plist["StandardErrorPath"], "/dev/null")
        self.assertEqual(
            plist["EnvironmentVariables"]["CLIPROXY_COMPOSE_PULL_POLICY"],
            "never",
        )

    def test_archive_job_runs_frequently_and_targets_storage(self) -> None:
        path = self.ops / "com.rever.cliproxy-log-archive.plist"

        with path.open("rb") as handle:
            plist = plistlib.load(handle)

        self.assertEqual(plist["Label"], "com.rever.cliproxy-log-archive")
        self.assertTrue(plist["RunAtLoad"])
        self.assertLessEqual(plist["StartInterval"], 600)
        self.assertEqual(plist["WorkingDirectory"], self.host["instance_root"])
        self.assertEqual(plist["EnvironmentVariables"]["CLIPROXY_ROOT"], self.host["instance_root"])
        self.assertEqual(
            plist["ProgramArguments"],
            ["/opt/homebrew/bin/python3", str(Path(self.host["bundle_root"]) / "archive_logs.py"),
             "--archive-only"],
        )
        environment = plist["EnvironmentVariables"]
        self.assertEqual(
            environment["CLIPROXY_LOG_ARCHIVE_MOUNT"],
            "/Volumes/Storage",
        )
        self.assertEqual(
            environment["CLIPROXY_LOG_ARCHIVE_ROOT"],
            "/Volumes/Storage/backups/llm-logs/cliproxy/m5/live",
        )
        self.assertGreaterEqual(
            int(environment["CLIPROXY_LOG_MIN_AGE_SECONDS"]), 3_600
        )


if __name__ == "__main__":
    unittest.main()
