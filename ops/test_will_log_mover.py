import errno
import io
import json
import os
from pathlib import Path
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future
from contextlib import contextmanager, redirect_stderr
from unittest.mock import patch
import zlib

from ops import will_log_mover as mover


NORMAL = b"v1-responses-2026-11-22T050300-a1b2c3d4.log"
ERROR = b"error-v1-responses-2026-11-22T050301_9-17.log"
SYNTHETIC = b"synthetic request log fixture\n" * 256


class TestCodec:
    def compress(self, source, target, limit):
        data = source.read()
        target.write(zlib.compress(data))
        return mover.transfer(io.BytesIO(data), limit=limit)

    def decompress_digest(self, source, limit):
        try:
            decoded = zlib.decompress(source.read())
        except zlib.error:
            raise mover.IntegrityError from None
        return mover.transfer(io.BytesIO(decoded), limit=limit)


class FakeMount:
    def __init__(self, root):
        self.root = root
        self.free = 100 * mover.CHUNK_SIZE
        self.reason = None

    @contextmanager
    def open(self):
        if self.reason:
            raise mover.Paused(self.reason)
        with mover.directory(self.root) as fd:
            yield fd, self.free


class FakeWatch:
    def __init__(self):
        self.pending = []

    def close(self):
        pass

    def events(self):
        yield from self.pending
        self.pending.clear()


def portable_publish(parent, staging, archive_id):
    try:
        os.stat(archive_id, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(staging, archive_id, src_dir_fd=parent, dst_dir_fd=parent)
        return
    raise FileExistsError


class LogMoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.disk = self.root / "disk"
        self.disk.mkdir()
        self.destination = self.disk / "archive"
        self.destination.mkdir(mode=0o700)
        self.mount = FakeMount(self.disk)
        self.archive = mover.Archive(self.mount, TestCodec(), ("archive",),
                                     20 * mover.CHUNK_SIZE, 25 * mover.CHUNK_SIZE)
        self.pilot = self.start_pilot()
        if sys.platform != "linux":
            replacement = patch.object(mover, "publish", portable_publish)
            replacement.start()
            self.addCleanup(replacement.stop)

    def start_pilot(self, **kwargs):
        pilot = mover.Pilot(self.archive, **kwargs)
        pilot.source_fd = os.open(self.source, mover.DIR_FLAGS)
        pilot.watch = FakeWatch()
        self.addCleanup(pilot.close)
        return pilot

    def source_path(self, name=NORMAL):
        return self.source / os.fsdecode(name)

    def offer(self, name=NORMAL, data=SYNTHETIC):
        self.source_path(name).write_bytes(data)
        self.pilot.watch.pending.append((mover.CLOSE_WRITE, name))
        self.pilot.drain(10)
        return self.pilot.next_record(10)

    def capture(self, record):
        job = Future()
        try:
            job.set_result(self.archive.capture(record))
        except (OSError, mover.IntegrityError, mover.Paused) as error:
            job.set_exception(error)
        self.pilot.capture_result(record, job, 10)

    def directories(self):
        return [item for item in self.destination.glob("*/*/*/*/*")
                if item.is_dir() and not item.name.startswith(".")]

    def test_close_captures_and_verifies_content_once(self):
        with patch.object(mover, "Inotify", return_value=self.pilot.watch):
            with self.source_path().open("wb") as writer:
                writer.write(SYNTHETIC)
                writer.flush()
                self.pilot.reconcile(self.source, None)
                self.assertIsNone(self.pilot.next_record(10))
                self.assertEqual(self.pilot.unconfirmed, (1, len(SYNTHETIC)))
        self.pilot.watch.pending.extend([(mover.CLOSE_WRITE, NORMAL)] * 2)
        self.pilot.drain(10)
        self.capture(self.pilot.next_record(10))
        self.assertEqual(len(self.directories()), 1)
        self.assertEqual(self.source_path().read_bytes(), SYNTHETIC)
        self.assertEqual(self.pilot.counts["completed"]["files"], 1)
        self.assertEqual(self.pilot.counts["archived"]["bytes"], len(SYNTHETIC))

    @unittest.skipUnless(sys.platform == "linux", "Linux inotify")
    def test_kernel_delivers_close_and_move_events(self):
        self.pilot.disarm()
        self.pilot.arm(self.source)
        self.assertIsNotNone(self.pilot.watch)
        self.assertFalse(self.pilot.drain(1))
        with self.source_path().open("wb") as writer:
            writer.write(SYNTHETIC)
            writer.flush()
            self.pilot.drain(10)
            self.assertIsNone(self.pilot.next_record(10))
        ready, _, _ = select.select([self.pilot.watch.fd], [], [], 2)
        self.assertTrue(ready)
        self.pilot.drain(10)
        self.assertEqual(self.pilot.next_record(10).evidence, "close_write")
        self.capture(self.pilot.next_record(10))

        temporary = self.root / "ready"
        temporary.write_bytes(SYNTHETIC)
        temporary.rename(self.source_path(ERROR))
        ready, _, _ = select.select([self.pilot.watch.fd], [], [], 2)
        self.assertTrue(ready)
        self.pilot.drain(10)
        self.assertEqual(self.pilot.next_record(10).evidence, "moved_to")
        self.capture(self.pilot.next_record(10))
        self.assertEqual(len(self.directories()), 2)

    @unittest.skipUnless(sys.platform == "linux", "Linux inotify")
    def test_service_loop_finishes_with_aggregate_status(self):
        pilot = mover.Pilot(self.archive)
        watch_ready = threading.Event()
        archived = threading.Event()
        stop = threading.Event()
        write_status = mover.replace_json

        def observe_status(fd, name, value):
            write_status(fd, name, value)
            if name == mover.STATUS_NAME and value["source_watch_active"]:
                watch_ready.set()
                if value["archived"]["files"]:
                    archived.set()

        with (
            mover.directory(self.root) as fd,
            patch.object(mover, "replace_json", observe_status),
            patch.object(mover, "HEARTBEAT_INTERVAL", 0.05),
        ):
            runner = threading.Thread(
                target=pilot.run, args=(self.source, fd, stop.is_set), daemon=True
            )
            runner.start()
            try:
                self.assertTrue(watch_ready.wait(3))
                self.source_path().write_bytes(SYNTHETIC)
                self.assertTrue(archived.wait(3))
            finally:
                stop.set()
                runner.join(3)
            self.assertFalse(runner.is_alive())
            self.assertEqual(mover.read_json(fd, mover.STATUS_NAME)["watch_gaps"], 0)
        self.assertEqual(len(self.directories()), 1)

    def test_cleaner_unlinks_after_open(self):
        record = self.offer()
        self.source_path().unlink()
        self.capture(record)
        self.assertEqual(len(self.directories()), 1)
        self.assertEqual(self.pilot.counts["recovered_after_unlink"],
                         {"files": 1, "bytes": len(SYNTHETIC)})

    def test_cleaner_unlinks_before_open(self):
        self.source_path().write_bytes(SYNTHETIC)
        self.source_path().unlink()
        self.pilot.watch.pending.append((mover.CLOSE_WRITE, NORMAL))
        self.pilot.drain(10)
        self.assertEqual(self.pilot.counts["missed"], {"files": 1, "bytes": 0})
        self.assertEqual(self.pilot.unknown_miss_bytes, 1)
        self.assertEqual(self.directories(), [])

    def test_reused_name_does_not_copy_a_new_writer_on_an_old_close(self):
        self.source_path().write_bytes(SYNTHETIC)
        self.source_path().unlink()
        with self.source_path().open("wb") as writer:
            writer.write(SYNTHETIC)
            writer.flush()
            self.pilot.watch.pending.extend([(mover.CLOSE_WRITE, NORMAL), (mover.CREATE, NORMAL)])
            self.pilot.drain(10)
            self.assertIsNone(self.pilot.next_record(10))
        self.assertEqual(self.pilot.counts["missed"]["files"], 1)

    def test_excludes_application_logs_temps_and_nonregular_entries(self):
        excluded = (b"main.log", b"main-2026-11-22T05-03-00.000.log", b"main.log.1",
                    b"response-body-238.tmp", b"request-log-parts-123", b"test.log")
        for name in excluded:
            self.source_path(name).write_bytes(SYNTHETIC)
        self.source_path(NORMAL).symlink_to(self.source_path(excluded[0]))
        self.source_path(ERROR).mkdir()
        self.pilot.watch.pending.extend((mover.CLOSE_WRITE, name) for name in (*excluded, NORMAL))
        self.pilot.watch.pending.append((mover.IS_DIR | mover.CLOSE_WRITE, ERROR))
        self.pilot.drain(10)
        self.assertIsNone(self.pilot.next_record(10))
        self.assertEqual(self.pilot.counts["completed"]["files"], 0)

    def test_overflow_requests_reconcile_without_copying_unconfirmed_file(self):
        self.source_path().write_bytes(SYNTHETIC)
        self.pilot.watch.pending.append((mover.OVERFLOW, b""))
        self.assertTrue(self.pilot.drain(10))
        self.pilot.reconcile(self.source, None)
        self.assertEqual(self.pilot.watch_gaps, 1)
        self.assertEqual(self.pilot.unconfirmed, (1, len(SYNTHETIC)))
        self.assertIsNone(self.pilot.next_record(10))

    def test_restart_idempotence_preserves_committed_inode(self):
        self.capture(self.offer())
        archive = self.directories()[0]
        before = archive.stat()
        self.pilot = self.start_pilot()
        record = self.pilot.offer(NORMAL, "close_write", 10)
        self.capture(record)
        self.assertEqual(len(self.directories()), 1)
        self.assertEqual(archive.stat().st_ino, before.st_ino)
        self.assertEqual(self.pilot.counts["archived"]["files"], 0)

    def test_corrupt_or_truncated_data_never_publishes(self):
        class BrokenCodec(TestCodec):
            def compress(self, source, target, limit):
                data = source.read()
                target.write(zlib.compress(data)[:-4])
                return mover.transfer(io.BytesIO(data), limit=limit)

        self.archive.codec = BrokenCodec()
        self.capture(self.offer())
        self.assertEqual(self.directories(), [])
        self.assertEqual(self.pilot.counts["integrity_failures"]["files"], 1)

    def test_recovers_completed_staging_after_interrupted_rename(self):
        record = self.offer()
        with patch.object(mover, "publish", side_effect=OSError(errno.EIO, "synthetic")):
            self.capture(record)
        self.assertEqual(self.directories(), [])
        self.source_path().unlink()
        restarted = self.start_pilot()
        for result in self.archive.recover():
            restarted.recovery_result(result, 10)
        self.assertEqual(len(self.directories()), 1)
        self.assertEqual(restarted.counts["recovered_after_unlink"]["files"], 1)
        self.assertEqual(restarted.counts["resumed_stages"]["files"], 1)

    def test_restarts_incomplete_staging_from_unchanged_source(self):
        record = self.offer()
        with patch.object(self.archive.codec, "compress", side_effect=OSError(errno.EIO, "synthetic")):
            self.capture(record)
        self.pilot = self.start_pilot()
        for result in self.archive.recover():
            self.pilot.recovery_result(result, 10)
        recovered = self.pilot.next_record(10)
        self.assertIsNotNone(recovered)
        self.capture(recovered)
        self.assertEqual(len(self.directories()), 1)

    def test_pending_fds_and_bytes_are_bounded(self):
        self.pilot = self.start_pilot(max_fds=1, max_bytes=len(SYNTHETIC))
        first = self.offer()
        self.offer(ERROR)
        self.assertEqual(self.pilot.held_fds, 1)
        self.assertEqual(self.pilot.held_bytes, len(SYNTHETIC))
        self.assertEqual(self.pilot.status(10)["pending"]["files"], 2)
        self.capture(first)
        self.pilot.reconcile(self.source, None)
        self.capture(self.pilot.next_record(10))
        self.assertEqual(self.pilot.counts["archived"]["files"], 2)

    def test_space_floor_hysteresis_and_reserve(self):
        record = self.offer()
        self.mount.free = self.archive.floor - 1
        self.capture(record)
        self.assertEqual(self.pilot.paused, "low_space")
        self.assertEqual(self.pilot.status(10)["pending"]["files"], 1)
        self.mount.free = self.archive.floor + 1
        self.assertEqual(self.archive.pause_reason(True), "low_space")
        self.assertIsNone(self.archive.pause_reason(False))
        self.capture(record)
        self.assertEqual(self.directories(), [])
        self.mount.free = self.archive.resume
        self.assertIsNone(self.archive.pause_reason(True))
        self.capture(record)
        self.assertEqual(len(self.directories()), 1)

    def test_missing_or_wrong_mount_writes_no_archive_entries(self):
        record = self.offer()
        for reason in ("mount_unavailable", "mount_identity"):
            self.mount.reason = reason
            self.capture(record)
            self.assertEqual(self.pilot.paused, reason)
            self.assertEqual(list(self.destination.iterdir()), [])
            self.assertEqual(self.pilot.status(10)["pending"]["files"], 1)

    def test_symlinked_archive_path_is_rejected(self):
        decoy = self.root / "decoy"
        decoy.mkdir()
        self.destination.rmdir()
        self.destination.symlink_to(decoy, target_is_directory=True)
        self.capture(self.offer())
        self.assertEqual(list(decoy.iterdir()), [])
        self.assertEqual(self.pilot.counts["archive_failures"]["files"], 1)

    def test_private_modes_and_aggregate_status(self):
        previous = os.umask(0o077)
        try:
            self.capture(self.offer(ERROR))
            with mover.directory(self.root) as fd:
                mover.replace_json(fd, mover.STATUS_NAME, self.pilot.status(10))
        finally:
            os.umask(previous)
        for item in self.destination.rglob("*"):
            self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o700 if item.is_dir() else 0o600)
        status = self.root / mover.STATUS_NAME
        self.assertEqual(stat.S_IMODE(status.stat().st_mode), 0o600)
        data = status.read_bytes()
        self.assertEqual(json.loads(data)["archived"]["files"], 1)
        self.assertFalse(ERROR in data or SYNTHETIC[:30] in data)

    def test_watchdog_warns_on_mount_and_stale_heartbeat(self):
        self.mount.reason = "mount_unavailable"
        with mover.directory(self.root) as fd:
            mover.replace_json(fd, mover.STATUS_NAME, {"heartbeat_unix": 0})
        stderr = io.StringIO()
        with (
            patch.object(mover, "STATUS_PATH", self.root),
            patch.object(mover.subprocess, "run", return_value=subprocess.CompletedProcess([], 3)),
            redirect_stderr(stderr),
        ):
            mover.watchdog(self.mount, "cliproxy-will-log-mover@synthetic.service")
        self.assertIn("mount is unavailable", stderr.getvalue())
        self.assertIn("heartbeat is missing or stale", stderr.getvalue())
        self.assertIn("pilot service is inactive", stderr.getvalue())

    @unittest.skipUnless(mover.zstd is not None or shutil.which("zstd"), "zstd unavailable")
    def test_production_codec_round_trip(self):
        self.archive.codec = mover.ZstdCodec()
        self.capture(self.offer())
        self.assertEqual(len(self.directories()), 1)
        self.assertEqual(self.pilot.counts["archived"]["bytes"], len(SYNTHETIC))


@unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze unavailable")
class SystemdUnitTests(unittest.TestCase):
    def test_verify(self):
        instance = Path(__file__).parent / "instances/will"
        units = [str(item) for item in sorted(instance.glob("cliproxy-will-log-mover*"))]
        result = subprocess.run(["systemd-analyze", "verify", *units], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
