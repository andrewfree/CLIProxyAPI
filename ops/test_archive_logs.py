from contextlib import redirect_stderr
import io
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

    def archiver(self, *, prune: bool = True, min_age_seconds: int = 3_600) -> LogArchiver:
        config = ArchiveConfig(
            source_dir=self.source,
            archive_root=self.archive,
            mount_point=self.root,
            min_age_seconds=min_age_seconds,
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

    def test_index_hit_does_not_hash_or_load_full_manifest(self) -> None:
        source = self.write_log("existing.log", b"closed\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)
        inode = self.index.stat().st_ino
        self.assertEqual(self.index.stat().st_mode & 0o777, 0o600)

        job = self.archiver(prune=False)
        with (patch.object(LogArchiver, "_sha256", wraps=LogArchiver._sha256) as sha256,
              patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read_text,
              patch.object(job, "_load_manifest", wraps=job._load_manifest) as load_manifest,
              self.assertLogs(self.logger, level="INFO") as logs):
            result = job.run()

        sha256.assert_not_called()
        self.assertEqual([call.args[0] for call in read_text.call_args_list], [self.index])
        load_manifest.assert_not_called()
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

    def test_same_size_source_rewrite_with_restored_mtime_is_archived(self) -> None:
        source = self.write_log("rewrite.log", b"before\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)
        original = source.stat()
        source.write_bytes(b"after!\n")
        os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertNotEqual(source.stat().st_ctime_ns, original.st_ctime_ns)

        result = self.archiver(prune=False).run()

        self.assertEqual((result.archived, result.skipped_by_index, result.failures), (1, 0, 0))
        updated = next((self.archive / "requests").rglob(f"{source.name}.sha256-*"))
        self.assertEqual(updated.read_bytes(), source.read_bytes())

    def test_same_size_archive_changes_are_reverified(self) -> None:
        for change in ("replacement", "rewrite"):
            with self.subTest(change=change):
                source = self.write_log(f"{change}.log", b"before\n", age_seconds=7_200)
                self.assertEqual(self.archiver(prune=False).run().archived, 1)
                destination = next((self.archive / "requests").rglob(source.name))
                original = destination.stat()
                updated = destination.with_name("replacement") if change == "replacement" else destination
                updated.write_bytes(b"after!\n")
                os.utime(updated, ns=(original.st_atime_ns, original.st_mtime_ns))
                if updated != destination:
                    updated.replace(destination)

                result = self.archiver(prune=False).run()

                self.assertEqual((result.archived, result.failures), (1, 0))
                repaired = next(destination.parent.glob(f"{source.name}.sha256-*"))
                self.assertEqual(repaired.read_bytes(), source.read_bytes())
                self.assertEqual(destination.read_bytes(), b"after!\n")

    def test_index_survives_device_number_changes(self) -> None:
        self.write_log("existing.log", b"closed\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)
        stat = Path.stat
        fstat = os.fstat

        def remap(metadata):
            mode, inode, device, *rest = metadata
            times = {name: getattr(metadata, name) for name in ("st_atime_ns", "st_mtime_ns", "st_ctime_ns")}
            return os.stat_result((mode, inode, device + 1, *rest), times)

        def observe(path, *args, **kwargs):
            return remap(stat(path, *args, **kwargs))

        with (patch.object(Path, "stat", autospec=True, side_effect=observe),
              patch.object(archive_logs.os, "fstat", side_effect=lambda fd: remap(fstat(fd))),
              patch.object(LogArchiver, "_sha256", wraps=LogArchiver._sha256) as sha256):
            result = self.archiver(prune=False).run()

        sha256.assert_not_called()
        self.assertEqual((result.skipped_by_index, result.failures), (1, 0))

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

        job = self.archiver(prune=False)
        with (self.assertLogs(self.logger, level="WARNING") as logs,
              patch.object(job, "_load_manifest", wraps=job._load_manifest) as load_manifest):
            result = job.run()

        self.assertIn("archive-index.json", logs.output[0])
        manifest = self.archive / "SHA256SUMS"
        load_manifest.assert_called_once()
        self.assertEqual((result.already_archived, result.skipped_by_index, result.failures), (2, 0, 0))
        self.assertEqual(len(manifest.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(self.archiver(prune=False).run().skipped_by_index, 2)

    def test_missing_replaced_or_shortened_manifest_rebuilds_records(self) -> None:
        self.write_log("record.log", b"closed\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)
        manifest = self.archive / "SHA256SUMS"
        record = manifest.read_bytes()
        for change in ("missing", "replaced", "shortened"):
            with self.subTest(change=change):
                if change == "missing":
                    manifest.unlink()
                elif change == "replaced":
                    replacement = manifest.with_name("replacement")
                    replacement.write_bytes(b"x" * (len(record) - 1) + b"\n")
                    replacement.replace(manifest)
                else:
                    manifest.write_bytes(b"")

                result = self.archiver(prune=False).run()

                self.assertEqual((result.already_archived, result.skipped_by_index, result.failures), (1, 0, 0))
                self.assertTrue(manifest.read_bytes().endswith(record))
                self.assertEqual(self.archiver(prune=False).run().skipped_by_index, 1)

    def test_unterminated_manifest_record_is_separated_from_next_record(self) -> None:
        self.write_log("a.log", b"first\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)
        manifest = self.archive / "SHA256SUMS"
        first = manifest.read_bytes().removesuffix(b"\n")
        manifest.write_bytes(first)
        self.write_log("b.log", b"second\n", age_seconds=7_200)

        with self.assertLogs(self.logger, level="ERROR") as logs:
            result = self.archiver(prune=False).run()

        self.assertEqual((result.archived, result.failures), (1, 1))
        self.assertIn("unterminated", "\n".join(logs.output))
        records = manifest.read_bytes().splitlines()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], first)
        self.assertTrue(records[1].endswith(b"/b.log"))
        self.assertTrue(manifest.read_bytes().endswith(b"\n"))
        self.assertEqual(self.archiver(prune=False).run().skipped_by_index, 2)

    def test_grown_manifest_allows_an_index_hit(self) -> None:
        self.write_log("existing.log", b"closed\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)
        with (self.archive / "SHA256SUMS").open("ab") as manifest:
            manifest.write(b"fixture append\n")
        with patch.object(LogArchiver, "_sha256", wraps=LogArchiver._sha256) as sha256:
            result = self.archiver(prune=False).run()

        sha256.assert_not_called()
        self.assertEqual((result.skipped_by_index, result.failures), (1, 0))

    def test_malformed_tail_blocks_index_and_appends_until_repaired(self) -> None:
        self.write_log("existing.log", b"closed\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)
        manifest = self.archive / "SHA256SUMS"
        original = manifest.read_bytes()
        malformed = original + b"a" * 63 + b"  requests/partial.log"
        manifest.write_bytes(malformed)
        source = self.write_log("new.log", b"new log\n", age_seconds=7_200)

        for _ in range(2):
            with self.assertLogs(self.logger, level="ERROR") as logs:
                result = self.archiver(prune=False).run()
            self.assertEqual((result.skipped_by_index, result.failures), (0, 1))
            self.assertIn("malformed unterminated final record", "\n".join(logs.output))
            self.assertEqual(manifest.read_bytes(), malformed)
            self.assertTrue(source.exists())
            self.assertFalse(list((self.archive / "requests").rglob(source.name)))

        manifest.write_bytes(original)
        result = self.archiver(prune=False).run()
        self.assertEqual((result.archived, result.skipped_by_index, result.failures), (1, 1, 0))
        self.assertTrue(manifest.read_bytes().endswith(b"/new.log\n"))

    def test_parseable_truncated_name_tail_requires_a_matching_archive_copy(self) -> None:
        source = self.write_log("almost-complete.log", b"closed\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)
        manifest = self.archive / "SHA256SUMS"
        original = manifest.read_bytes()
        self.assertTrue(original.endswith(b"/almost-complete.log\n"))
        truncated = original[:-2]
        manifest.write_bytes(truncated)
        copy = next((self.archive / "requests").rglob(source.name))

        for wrong_copy_exists in (False, True):
            with self.subTest(wrong_copy_exists=wrong_copy_exists):
                if wrong_copy_exists:
                    copy.with_suffix(".lo").write_bytes(b"wrong!\n")

                with self.assertLogs(self.logger, level="ERROR") as logs:
                    result = self.archiver(prune=False).run()

                self.assertEqual((result.skipped_by_index, result.failures), (0, 1))
                self.assertIn("malformed unterminated final record", "\n".join(logs.output))
                self.assertEqual(manifest.read_bytes(), truncated)
                self.assertTrue(source.exists())

    def test_symlink_manifest_keeps_its_source_target_untouched(self) -> None:
        source = self.write_log("keep.log", b"closed\n", age_seconds=7_200)
        self.archive.mkdir()
        manifest = self.archive / "SHA256SUMS"
        manifest.symlink_to(source)

        result = self.archiver().run()

        self.assertEqual((result.pruned, result.failures), (0, 1))
        self.assertTrue(manifest.is_symlink())
        self.assertEqual(source.read_bytes(), b"closed\n")

    def test_manifest_symlink_created_after_initial_check_is_refused(self) -> None:
        source = self.write_log("keep.log", b"closed\n", age_seconds=7_200)
        manifest = self.archive / "SHA256SUMS"
        open_file = os.open

        def install_symlink(path, flags, *args, **kwargs):
            if path == manifest and flags & os.O_RDWR:
                manifest.symlink_to(source)
            return open_file(path, flags, *args, **kwargs)

        with patch.object(archive_logs.os, "open", side_effect=install_symlink):
            result = self.archiver().run()

        self.assertEqual((result.pruned, result.failures), (0, 1))
        self.assertTrue(manifest.is_symlink())
        self.assertEqual(source.read_bytes(), b"closed\n")

    def test_manifest_hard_link_created_before_append_is_refused(self) -> None:
        source = self.write_log("keep.log", b"closed\n", age_seconds=7_200)
        manifest = self.archive / "SHA256SUMS"
        open_file = os.open

        def install_link(path, flags, *args, **kwargs):
            if path == manifest and flags & os.O_RDWR:
                os.link(source, manifest)
            return open_file(path, flags, *args, **kwargs)

        with (patch.object(archive_logs.os, "open", side_effect=install_link),
              self.assertLogs(self.logger, level="ERROR") as logs):
            result = self.archiver().run()

        self.assertEqual((result.pruned, result.failures), (0, 1))
        self.assertIn("exactly one link", "\n".join(logs.output))
        self.assertEqual(manifest.read_bytes(), b"closed\n")
        self.assertEqual(source.read_bytes(), b"closed\n")

    def test_failed_source_listing_keeps_existing_index(self) -> None:
        self.write_log("retained.log", b"closed\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)
        hidden = self.source.rename(self.root / "temporarily-unavailable")
        try:
            result = self.archiver(prune=False).run()
        finally:
            hidden.rename(self.source)

        self.assertEqual(result.failures, 1)
        self.assertEqual(self.archiver(prune=False).run().skipped_by_index, 1)

    def test_raised_minimum_age_keeps_index_for_present_logs(self) -> None:
        self.write_log("retained.log", b"closed\n", age_seconds=7_200)
        self.assertEqual(self.archiver(prune=False).run().archived, 1)

        result = self.archiver(prune=False, min_age_seconds=10_000).run()

        self.assertEqual((result.archived, result.skipped_by_index, result.failures), (0, 0, 0))
        self.assertEqual(self.archiver(prune=False).run().skipped_by_index, 1)

    def test_concurrent_publication_does_not_overwrite_existing_copy(self) -> None:
        source = self.write_log("race.log", b"mine\n", age_seconds=7_200)
        link = os.link

        def publish(temporary, destination):
            if destination.name == source.name:
                destination.write_bytes(b"theirs\n")
            link(temporary, destination)

        with patch.object(archive_logs.os, "link", side_effect=publish):
            result = self.archiver(prune=False).run()

        self.assertEqual((result.archived, result.failures), (1, 0))
        destination = next((self.archive / "requests").rglob(source.name))
        self.assertEqual(destination.read_bytes(), b"theirs\n")
        collision = next(destination.parent.glob(f"{source.name}.sha256-*"))
        self.assertEqual(collision.read_bytes(), source.read_bytes())

    def test_cli_pruning_requires_explicit_opt_in(self) -> None:
        env = {
            "CLIPROXY_ROOT": str(self.root),
            "CLIPROXY_LOG_ARCHIVE_ROOT": str(self.archive),
            "CLIPROXY_LOG_ARCHIVE_MOUNT": str(self.root),
        }
        for flags, prune in (((), False), (("--archive-only",), False), (("--prune",), True)):
            with self.subTest(flags=flags):
                source = self.write_log("keep.log", b"closed\n", age_seconds=7_200).resolve()
                timestamp = time.time() - 7_200
                os.utime(source, (timestamp, timestamp))
                with (patch.dict(os.environ, env, clear=True),
                      patch.object(sys, "argv", ["archive_logs.py", *flags]),
                      patch.object(Path, "is_mount", return_value=True),
                      patch.object(archive_logs, "build_logger", return_value=self.logger),
                      patch.object(Path, "unlink", autospec=True, side_effect=Path.unlink) as unlink,
                      self.assertLogs(self.logger, level="INFO") as logs):
                    self.assertEqual(archive_logs.main(), 0)
                self.assertEqual(source in [call.args[0] for call in unlink.call_args_list], prune)
                self.assertEqual(source.exists(), not prune)
                self.assertIn(f"pruned={int(prune)}", logs.output[-1])

    def test_cli_rejects_both_archive_modes(self) -> None:
        argv = ["archive_logs.py", "--archive-only", "--prune"]
        with (patch.object(sys, "argv", argv), redirect_stderr(io.StringIO()) as stderr,
              self.assertRaises(SystemExit) as exit_):
            archive_logs.main()

        self.assertEqual(exit_.exception.code, 2)
        self.assertIn("not allowed with argument --archive-only", stderr.getvalue())

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
