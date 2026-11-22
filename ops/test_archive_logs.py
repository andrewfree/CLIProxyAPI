import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ops import archive_logs
from ops.archive_logs import ArchiveConfig, LogArchiver


class LogArchiverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "logs"
        self.archive = self.root / "archive"
        self.index = self.root / "ops/runtime/archive-index.json"
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
            index_path=self.index,
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

    def test_index_hit_does_not_hash_or_read_manifest(self) -> None:
        source = self.write_log("existing.log", b"closed\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)
        inode = self.index.stat().st_ino
        self.assertEqual(self.index.stat().st_mode & 0o777, 0o600)

        with (patch.object(LogArchiver, "_sha256", wraps=LogArchiver._sha256) as sha256,
              patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read_text,
              self.assertLogs(self.logger, level="INFO") as logs):
            result = self.archiver(prune=False).run()

        sha256.assert_not_called()
        self.assertEqual([call.args[0] for call in read_text.call_args_list], [self.index])
        self.assertEqual((result.skipped_by_index, result.failures), (1, 0))
        self.assertIn("skipped_by_index=1", logs.output[-1])
        self.assertEqual(self.index.stat().st_ino, inode)
        self.assertTrue(source.exists())

    def test_changed_fingerprint_is_verified_and_updates_index(self) -> None:
        source = self.write_log("changed.log", b"before\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)
        self.write_log(source.name, b"after!\n", age_seconds=7_201)

        with patch.object(LogArchiver, "_sha256", wraps=LogArchiver._sha256) as sha256:
            result = self.archiver(prune=False).run()

        self.assertIn(source, [call.args[0] for call in sha256.call_args_list])
        self.assertEqual((result.archived, result.skipped_by_index, result.failures), (1, 0, 0))
        self.assertEqual(self.archiver(prune=False).run().skipped_by_index, 1)

    def test_missing_or_short_archive_copy_is_rearchived(self) -> None:
        source = self.write_log("repair.log", b"closed\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)
        destination = next((self.archive / "requests").rglob(source.name))
        destination.unlink()

        result = self.archiver(prune=False).run()
        self.assertEqual((result.archived, result.skipped_by_index, result.failures), (1, 0, 0))
        self.assertEqual(destination.read_bytes(), source.read_bytes())

        destination.write_bytes(b"short")
        result = self.archiver(prune=False).run()
        self.assertEqual((result.archived, result.skipped_by_index, result.failures), (1, 0, 0))
        repaired = next(destination.parent.glob(f"{source.name}.sha256-*"))
        self.assertEqual(repaired.read_bytes(), source.read_bytes())
        self.assertEqual(self.archiver(prune=False).run().skipped_by_index, 1)

    def test_corrupt_index_is_rebuilt_and_manifest_is_read_once(self) -> None:
        for name in ("one.log", "two.log"):
            self.write_log(name, b"closed\n", age_seconds=7_200)
        with self.assertLogs(self.logger, level="WARNING") as logs:
            self.assertEqual(self.archiver(prune=False).run().archived, 2)
        self.assertIn("archive-index.json", logs.output[0])
        self.index.write_text("{broken", encoding="utf-8")

        with (self.assertLogs(self.logger, level="WARNING") as logs,
              patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read_text):
            result = self.archiver(prune=False).run()

        self.assertIn("archive-index.json", logs.output[0])
        manifest = self.archive / "SHA256SUMS"
        self.assertEqual([call.args[0] for call in read_text.call_args_list].count(manifest), 1)
        self.assertEqual((result.already_archived, result.skipped_by_index, result.failures), (2, 0, 0))
        self.assertEqual(len(manifest.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(self.archiver(prune=False).run().skipped_by_index, 2)

    def test_archive_only_cli_never_unlinks_source(self) -> None:
        source = self.write_log("keep.log", b"closed\n", age_seconds=7_200)
        timestamp = time.time() - 7_200
        os.utime(source, (timestamp, timestamp))
        argv = ["archive_logs.py", "--archive-only", "--destination", str(self.archive),
                "--mount-point", str(self.root)]
        with (patch.dict(os.environ, {"CLIPROXY_ROOT": str(self.root)}, clear=True),
              patch.object(sys, "argv", argv),
              patch.object(Path, "is_mount", return_value=True),
              patch.object(archive_logs, "build_logger", return_value=self.logger),
              patch.object(Path, "unlink", autospec=True, side_effect=Path.unlink) as unlink,
              self.assertLogs(self.logger, level="INFO") as logs):
            self.assertEqual(archive_logs.main(), 0)

        self.assertNotIn(source.resolve(), [call.args[0] for call in unlink.call_args_list])
        self.assertTrue(source.exists())
        self.assertTrue(self.index.exists())
        self.assertIn("pruned=0", logs.output[-1])

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
