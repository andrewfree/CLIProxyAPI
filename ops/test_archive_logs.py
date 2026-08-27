import logging
import os
import tempfile
import unittest
from pathlib import Path

from ops.archive_logs import ArchiveConfig, LogArchiver


class LogArchiverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "logs"
        self.archive = self.root / "archive"
        self.source.mkdir()
        self.now = 1_800_000_000.0
        self.logger = logging.getLogger(f"archive-test-{id(self)}")
        self.logger.addHandler(logging.NullHandler())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_log(self, name: str, content: bytes, *, age_seconds: int) -> Path:
        path = self.source / name
        path.write_bytes(content)
        timestamp = self.now - age_seconds
        os.utime(path, (timestamp, timestamp))
        return path

    def archiver(self, *, prune: bool = True) -> LogArchiver:
        config = ArchiveConfig(
            source_dir=self.source,
            archive_root=self.archive,
            mount_point=self.root,
            min_age_seconds=3_600,
            prune=prune,
            require_mounted_destination=False,
        )
        return LogArchiver(config, logger=self.logger, clock=lambda: self.now)

    def test_archives_and_prunes_only_old_closed_logs(self) -> None:
        old_request = self.write_log(
            "v1-responses-old.log", b"closed request\n", age_seconds=7_200
        )
        active_main = self.write_log("main.log", b"active\n", age_seconds=7_200)
        recent_request = self.write_log(
            "v1-responses-recent.log", b"recent\n", age_seconds=60
        )
        unrelated = self.write_log("notes.txt", b"not a log\n", age_seconds=7_200)

        result = self.archiver().run()

        self.assertEqual(result.archived, 1)
        self.assertEqual(result.pruned, 1)
        self.assertEqual(result.failures, 0)
        self.assertFalse(old_request.exists())
        self.assertTrue(active_main.exists())
        self.assertTrue(recent_request.exists())
        self.assertTrue(unrelated.exists())
        archived = list((self.archive / "requests").rglob("v1-responses-old.log"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), b"closed request\n")
        manifest = (self.archive / "SHA256SUMS").read_text(encoding="utf-8")
        self.assertIn("requests/", manifest)
        self.assertIn("v1-responses-old.log", manifest)

    def test_existing_verified_archive_allows_later_prune(self) -> None:
        source = self.write_log(
            "v1-responses-existing.log", b"same bytes\n", age_seconds=7_200
        )

        first = self.archiver(prune=False).run()
        second = self.archiver(prune=True).run()

        self.assertEqual(first.archived, 1)
        self.assertEqual(first.pruned, 0)
        self.assertEqual(second.already_archived, 1)
        self.assertEqual(second.pruned, 1)
        self.assertFalse(source.exists())
        manifest_lines = (self.archive / "SHA256SUMS").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(manifest_lines), 1)

    def test_name_collision_preserves_both_versions_before_prune(self) -> None:
        source = self.write_log(
            "v1-responses-collision.log", b"first version\n", age_seconds=7_200
        )
        self.archiver(prune=False).run()
        source.write_bytes(b"second version\n")
        timestamp = self.now - 7_200
        os.utime(source, (timestamp, timestamp))

        result = self.archiver(prune=True).run()

        self.assertEqual(result.archived, 1)
        self.assertEqual(result.pruned, 1)
        self.assertFalse(source.exists())
        versions = list((self.archive / "requests").rglob("*collision*"))
        self.assertEqual(len(versions), 2)
        self.assertEqual(
            {path.read_bytes() for path in versions},
            {b"first version\n", b"second version\n"},
        )
        manifest_lines = (self.archive / "SHA256SUMS").read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(manifest_lines), 2)

    def test_unmounted_destination_never_prunes(self) -> None:
        source = self.write_log(
            "v1-responses-unmounted.log", b"keep me\n", age_seconds=7_200
        )
        config = ArchiveConfig(
            source_dir=self.source,
            archive_root=self.archive,
            mount_point=self.root,
            min_age_seconds=3_600,
            prune=True,
            require_mounted_destination=True,
        )

        result = LogArchiver(
            config, logger=self.logger, clock=lambda: self.now
        ).run()

        self.assertEqual(result.failures, 1)
        self.assertEqual(result.pruned, 0)
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
