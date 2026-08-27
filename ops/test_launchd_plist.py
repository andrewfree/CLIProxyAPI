import plistlib
import unittest
from pathlib import Path


class LaunchdPlistTest(unittest.TestCase):
    def test_watchdog_runs_frequently_with_bounded_output(self) -> None:
        root = Path(__file__).resolve().parent.parent
        path = root / "ops" / "com.rever.cliproxy-watchdog.plist"

        with path.open("rb") as handle:
            plist = plistlib.load(handle)

        self.assertEqual(plist["Label"], "com.rever.cliproxy-watchdog")
        self.assertTrue(plist["RunAtLoad"])
        self.assertLessEqual(plist["StartInterval"], 180)
        self.assertEqual(plist["WorkingDirectory"], str(root))
        self.assertIn(
            str(root / "ops" / "cliproxy_watchdog.py"),
            plist["ProgramArguments"],
        )
        self.assertEqual(plist["StandardOutPath"], "/dev/null")
        self.assertEqual(plist["StandardErrorPath"], "/dev/null")

    def test_archive_job_runs_frequently_and_targets_thecloud(self) -> None:
        root = Path(__file__).resolve().parent.parent
        path = root / "ops" / "com.rever.cliproxy-log-archive.plist"

        with path.open("rb") as handle:
            plist = plistlib.load(handle)

        self.assertEqual(plist["Label"], "com.rever.cliproxy-log-archive")
        self.assertTrue(plist["RunAtLoad"])
        self.assertLessEqual(plist["StartInterval"], 600)
        self.assertEqual(plist["WorkingDirectory"], str(root))
        self.assertIn(
            str(root / "ops" / "archive_logs.py"),
            plist["ProgramArguments"],
        )
        environment = plist["EnvironmentVariables"]
        self.assertEqual(
            environment["CLIPROXY_LOG_ARCHIVE_MOUNT"],
            "/Users/rever/Volumes/TheCloud",
        )
        self.assertEqual(
            environment["CLIPROXY_LOG_ARCHIVE_ROOT"],
            "/Users/rever/Volumes/TheCloud/backups/llm-logs/cliproxy/m5/live",
        )
        self.assertGreaterEqual(
            int(environment["CLIPROXY_LOG_MIN_AGE_SECONDS"]), 3_600
        )


if __name__ == "__main__":
    unittest.main()
