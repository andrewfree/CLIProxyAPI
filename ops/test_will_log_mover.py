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
import time
import unittest
from concurrent.futures import Future
from contextlib import contextmanager
from types import SimpleNamespace
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
        pending, self.pending = self.pending, []
        yield from pending


def portable_publish(parent, staging, archive_id, target_parent=None):
    target = parent if target_parent is None else target_parent
    try:
        os.stat(archive_id, dir_fd=target, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(staging, archive_id, src_dir_fd=parent, dst_dir_fd=target)
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
        self.destination = self.disk / "archive/requests"
        self.destination.mkdir(parents=True, mode=0o700)
        self.meta = self.disk / "archive/meta"
        self.meta.mkdir(mode=0o700)
        self.mount = FakeMount(self.disk)
        self.archive = mover.Archive(self.mount, TestCodec(), ("archive", "requests"),
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

    def events(self, *events):
        self.pilot.watch.pending.extend(events)
        return self.pilot.drain(time.monotonic())

    def offer(self, name=NORMAL, data=SYNTHETIC):
        self.source_path(name).write_bytes(data)
        self.events((mover.CREATE, name), (mover.CLOSE_WRITE, name))
        return self.pilot.next_record(time.monotonic())

    def capture(self, record):
        job = Future()
        try:
            job.set_result(self.pilot.capture(record, os.dup(record.fd)))
        except (OSError, mover.IntegrityError, mover.Paused, mover.CaptureCancelled) as error:
            job.set_exception(error)
        self.pilot.capture_result(record, job, time.monotonic())

    def directories(self):
        return [item for item in self.destination.glob("*/*/*/*/*")
                if item.is_dir() and not item.name.startswith(".")]

    def scan(self):
        self.pilot.reconcile(self.source, time.monotonic())

    def make_old(self, path):
        old = time.time() - mover.UNCONFIRMED_AGE - 5
        os.utime(path, (old, old))

    def publish_at_age(self, age_ns, name=NORMAL):
        now_ns = time.time_ns()
        mtime_ns = now_ns - age_ns
        path = self.source_path(name)
        path.write_bytes(SYNTHETIC)
        os.utime(path, ns=(mtime_ns, mtime_ns))
        fd = os.open(path, mover.READ_FLAGS)
        try:
            record = mover.Record(name, os.fstat(fd), "close_write", time.monotonic())
            result = self.archive.capture(record, fd, lambda: True)
        finally:
            os.close(fd)
        archived = self.destination.joinpath(*mover.hour_parts(mtime_ns), result.archive_id)
        self.assertTrue(archived.is_dir())
        return archived, now_ns

    def prune_all(self, now_ns, cursor=None):
        cursor = mover.PruneCursor() if cursor is None else cursor
        results = []
        for _ in range(1000):
            result = self.archive.prune(cursor, now_ns)
            self.assertIsNone(result.paused)
            results.append(result)
            if result.complete:
                return results
        self.fail("synthetic retention pass did not finish")

    def test_close_captures_and_verifies_content_once(self):
        with self.source_path().open("wb") as writer:
            writer.write(SYNTHETIC)
            writer.flush()
            self.scan()
            self.assertIsNone(self.pilot.next_record(time.monotonic()))
        self.events((mover.CLOSE_WRITE, NORMAL), (mover.CLOSE_WRITE, NORMAL))
        self.capture(self.pilot.next_record(time.monotonic()))
        self.assertEqual(len(self.directories()), 1)
        self.assertEqual(self.source_path().read_bytes(), SYNTHETIC)
        self.assertEqual(self.pilot.counts["completed"]["files"], 1)
        self.assertEqual(self.pilot.counts["archived"]["bytes"], len(SYNTHETIC))

    @unittest.skipUnless(sys.platform == "linux", "Linux inotify")
    def test_kernel_confirms_close_but_does_not_confirm_move(self):
        self.pilot.disarm()
        self.pilot.arm(self.source)
        self.assertIsNotNone(self.pilot.watch)
        with self.source_path().open("wb") as writer:
            writer.write(SYNTHETIC)
            writer.flush()
            self.pilot.drain(time.monotonic())
            self.assertIsNone(self.pilot.next_record(time.monotonic()))
        ready, _, _ = select.select([self.pilot.watch.fd], [], [], 2)
        self.assertTrue(ready)
        self.pilot.drain(time.monotonic())
        self.capture(self.pilot.next_record(time.monotonic()))

        temporary = self.root / "ready"
        temporary.write_bytes(SYNTHETIC)
        temporary.rename(self.source_path(ERROR))
        ready, _, _ = select.select([self.pilot.watch.fd], [], [], 2)
        self.assertTrue(ready)
        self.pilot.drain(time.monotonic())
        self.assertIsNone(self.pilot.next_record(time.monotonic()))
        self.assertEqual(len(self.directories()), 1)
        self.assertEqual(self.pilot.counts["completed"]["files"], 1)

    @unittest.skipUnless(sys.platform == "linux", "Linux inotify")
    def test_unlinked_writer_close_cannot_confirm_replacement(self):
        self.pilot.disarm()
        self.pilot.arm(self.source)
        with self.source_path().open("wb") as old_writer:
            old_writer.write(SYNTHETIC)
            self.pilot.drain(time.monotonic())
            self.source_path().unlink()
            with self.source_path().open("wb") as replacement:
                replacement.write(SYNTHETIC)
                replacement.flush()
                self.pilot.drain(time.monotonic())
                old_writer.close()
                self.pilot.drain(time.monotonic())
                self.assertIsNone(self.pilot.next_record(time.monotonic()))
        self.pilot.drain(time.monotonic())
        self.assertEqual(self.pilot.next_record(time.monotonic()).evidence, "close_write")
        self.assertEqual(self.pilot.counts["completed"]["files"], 1)

    @unittest.skipUnless(sys.platform == "linux", "Linux inotify")
    def test_stalled_destination_probe_does_not_block_event_intake(self):
        pilot = mover.Pilot(self.archive)
        blocked = threading.Event()
        release = threading.Event()
        received = threading.Event()
        archived = threading.Event()
        stop = threading.Event()
        write_status, probe = mover.replace_json, self.archive.probe

        def wait_on_disk(*args):
            blocked.set()
            if not release.wait(5):
                raise RuntimeError("synthetic disk was never released")
            return probe(*args)

        def observe_status(fd, name, value):
            write_status(fd, name, value)
            if name == mover.STATUS_NAME and value["source_watch_active"]:
                if value["completed"]["files"]:
                    received.set()
                if value["archived"]["files"]:
                    archived.set()

        with (
            mover.directory(self.root) as fd,
            patch.object(self.archive, "probe", side_effect=wait_on_disk),
            patch.object(mover, "replace_json", observe_status),
            patch.object(mover, "HEARTBEAT_INTERVAL", 0.10),
        ):
            runner = threading.Thread(target=pilot.run, args=(self.source, fd, stop.is_set),
                                      daemon=True)
            runner.start()
            try:
                self.assertTrue(blocked.wait(3))
                self.source_path().write_bytes(SYNTHETIC)
                self.assertTrue(received.wait(3))
                self.assertFalse(archived.is_set())
                release.set()
                self.assertTrue(archived.wait(3))
            finally:
                release.set()
                stop.set()
                runner.join(5)
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
        self.events((mover.CREATE, NORMAL))
        self.source_path().unlink()
        self.events((mover.CLOSE_WRITE, NORMAL))
        self.assertEqual(self.pilot.counts["missed"], {"files": 1, "bytes": 0})
        self.assertEqual(self.pilot.counts["completed"]["files"], 0)

    def test_inode_change_between_create_and_close_is_ambiguous(self):
        self.source_path().write_bytes(SYNTHETIC)
        self.events((mover.CREATE, NORMAL))
        incoming = self.root / "replacement"
        incoming.write_bytes(SYNTHETIC)
        incoming.replace(self.source_path())
        self.events((mover.CLOSE_WRITE, NORMAL))
        self.assertIsNone(self.pilot.next_record(time.monotonic()))
        self.assertEqual(self.pilot.ambiguous_closes, 1)
        self.make_old(self.source_path())
        self.scan()
        self.capture(self.pilot.next_record(time.monotonic()))
        self.assertEqual(self.pilot.counts["completed"]["files"], 0)
        self.assertEqual(self.pilot.counts["archived_unconfirmed"]["files"], 1)

    def test_later_create_disqualifies_close_in_same_event_batch(self):
        self.source_path().write_bytes(SYNTHETIC)
        applied = self.events((mover.CREATE, NORMAL), (mover.CLOSE_WRITE, NORMAL),
                              (mover.DELETE, NORMAL), (mover.CREATE, NORMAL))
        self.assertFalse(applied)
        self.assertEqual(self.pilot.ambiguous_closes, 1)
        self.assertIsNone(self.pilot.next_record(time.monotonic()))

    def test_excludes_application_logs_temps_and_nonregular_entries(self):
        excluded = (b"main.log", b"main-2026-11-22T05-03-00.000.log", b"main.log.1",
                    b"response-body-238.tmp", b"request-log-parts-123", b"test.log")
        for name in excluded:
            self.source_path(name).write_bytes(SYNTHETIC)
        self.source_path(NORMAL).symlink_to(self.source_path(excluded[0]))
        self.source_path(ERROR).mkdir()
        self.events(*((mover.CLOSE_WRITE, name) for name in (*excluded, NORMAL)),
                    (mover.IS_DIR | mover.CLOSE_WRITE, ERROR))
        self.assertIsNone(self.pilot.next_record(time.monotonic()))
        self.assertEqual(self.pilot.counts["completed"]["files"], 0)

    def test_overflow_discards_observation_and_scans_as_unconfirmed(self):
        self.source_path().write_bytes(SYNTHETIC)
        self.make_old(self.source_path())
        self.events((mover.CREATE, NORMAL), (mover.CLOSE_WRITE, NORMAL))
        stale = self.pilot.next_record(time.monotonic())
        self.assertTrue(self.events((mover.OVERFLOW, b"")))
        self.assertTrue(stale.cancelled.is_set())
        with patch.object(mover, "Inotify", return_value=FakeWatch()):
            self.scan()
        self.capture(self.pilot.next_record(time.monotonic()))
        manifest = json.loads((self.directories()[0] / "manifest.json").read_bytes())
        self.assertEqual(manifest["completion"], "unconfirmed")
        self.assertEqual(self.pilot.watch_gaps, 1)
        self.assertEqual(self.pilot.counts["completed"]["files"], 1)
        self.assertEqual(self.pilot.counts["archived_unconfirmed"]["files"], 1)
        self.assertEqual(self.pilot.counts["abandoned_pending"]["files"], 1)

    def test_restart_preserves_committed_inode_and_ignores_old_staging(self):
        self.capture(self.offer())
        archive = self.directories()[0]
        before = archive.stat()
        abandoned = archive.parent / ".pending-old"
        abandoned.mkdir()
        (abandoned / "body.zst").write_bytes(SYNTHETIC)
        self.pilot = self.start_pilot()
        self.events((mover.CREATE, NORMAL), (mover.CLOSE_WRITE, NORMAL))
        self.capture(self.pilot.next_record(time.monotonic()))
        self.assertEqual(len(self.directories()), 1)
        self.assertEqual(archive.stat().st_ino, before.st_ino)
        self.assertEqual(self.archive.probe(False, True, None), (None, 1, False))
        self.assertEqual((abandoned / "body.zst").read_bytes(), SYNTHETIC)

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

    def test_source_changing_during_read_fails_verification(self):
        record = self.offer()
        compress = self.archive.codec.compress

        def change_after_read(source, target, limit):
            result = compress(source, target, limit)
            with self.source_path().open("ab") as writer:
                writer.write(b"synthetic modification")
            return result

        with patch.object(self.archive.codec, "compress", side_effect=change_after_read):
            self.capture(record)
        self.assertEqual(self.directories(), [])
        self.assertEqual(self.pilot.counts["integrity_failures"]["files"], 1)

    def test_cutoff_rejects_new_records_and_deadline_blocks_publication(self):
        record = self.offer()
        self.pilot.admission_cutoff = time.monotonic()
        self.assertIs(self.pilot.next_record(time.monotonic()), record)
        self.events((mover.CLOSE_WRITE, ERROR))
        self.assertEqual(self.pilot.pending_files, 1)
        compress = self.archive.codec.compress

        def reach_deadline(*args):
            result = compress(*args)
            self.pilot.publication_deadline = time.monotonic()
            return result

        with patch.object(self.archive.codec, "compress", side_effect=reach_deadline):
            self.capture(record)
        self.assertEqual(self.directories(), [])
        self.assertIsNone(self.offer(ERROR))
        self.assertEqual(self.pilot.counts["abandoned_pending"]["files"], 1)

    def test_expired_run_writes_finished_status_without_opening_source(self):
        past = time.monotonic() - 1
        pilot = mover.Pilot(self.archive, admission_cutoff=past, publication_deadline=past)
        with mover.directory(self.root) as fd, patch.object(pilot, "arm") as arm:
            self.assertEqual(pilot.run(self.source, fd, lambda: False), 0)
            arm.assert_not_called()
            status = mover.read_json(fd, mover.STATUS_NAME)
        self.assertEqual(status["phase"], "finished")
        self.assertEqual(status["unfinished"], 0)
        self.assertFalse(status["source_watch_active"])

    def test_shutdown_counts_unfinished_probe_or_capture_once(self):
        for ending in ("deadline", "interrupted", "interrupted_then_settled"):
            for blocked_probe in (False, True):
                with self.subTest(ending=ending, blocked_probe=blocked_probe):
                    self.check_unfinished_work(ending, blocked_probe)

    def check_unfinished_work(self, ending, blocked_probe):
        self.pilot = self.start_pilot()
        if not blocked_probe:
            self.offer()
        pilot = self.pilot
        pilot.stop_watch()
        now = started = time.monotonic()
        interrupted = ending != "deadline"
        pilot.admission_cutoff = started + 2
        pilot.publication_deadline = started + (30 if interrupted else 4)
        pending_job = Future()
        submitted = []

        def submit(function, *args):
            submitted.append(function)
            if function == self.archive.probe:
                if blocked_probe:
                    return pending_job
                probe_job = Future()
                probe_job.completed_at = now
                probe_job.set_result((None, 0, False))
                return probe_job
            os.close(args[1])
            return pending_job

        def tick(*_args):
            nonlocal now
            now += 1
            if ending == "interrupted_then_settled" and pilot.phase == "interrupted":
                pending_job.completed_at = now
                if blocked_probe:
                    pending_job.set_result((None, 0, False))
                else:
                    pending_job.set_exception(mover.CaptureCancelled())

        worker = SimpleNamespace(submit=submit, close=lambda: None)
        with (
            mover.directory(self.root) as fd,
            patch.object(pilot, "reconcile"),
            patch.object(mover, "DiskWorker", return_value=worker),
            patch.object(mover.time, "monotonic", side_effect=lambda: now),
            patch.object(mover.select, "select", side_effect=tick),
        ):
            result = pilot.run(self.source, fd, lambda: interrupted and now >= started + 3)
            status = mover.read_json(fd, mover.STATUS_NAME)
        self.assertEqual(result, 3 if interrupted else 1)
        self.assertEqual(status["phase"], "interrupted" if interrupted else "failed")
        self.assertEqual(status["unfinished"], 1)
        self.assertEqual(len(submitted), 1 if blocked_probe else 2)
        self.assertEqual(pending_job.done(), ending == "interrupted_then_settled")

    def test_queued_capture_abandoned_on_interrupt_prevents_success(self):
        self.offer()
        self.pilot.admission_cutoff = time.monotonic() - 1
        with mover.directory(self.root) as fd:
            self.assertEqual(self.pilot.run(self.source, fd, lambda: True), 3)
            status = mover.read_json(fd, mover.STATUS_NAME)
        self.assertEqual(status["phase"], "interrupted")
        self.assertEqual(status["unfinished"], 1)
        self.assertEqual(status["abandoned_pending"]["files"], 1)

    def test_clean_interruption_with_no_work_returns_zero(self):
        with mover.directory(self.root) as fd:
            self.assertEqual(self.pilot.run(self.source, fd, lambda: True), 0)
            status = mover.read_json(fd, mover.STATUS_NAME)
        self.assertEqual(status["phase"], "interrupted")
        self.assertEqual(status["unfinished"], 0)

    def test_stop_signal_after_publication_deadline_cannot_hide_pending_work(self):
        self.offer()
        past = time.monotonic() - 1
        self.pilot.admission_cutoff = self.pilot.publication_deadline = past
        with mover.directory(self.root) as fd:
            self.assertEqual(self.pilot.run(self.source, fd, lambda: True), 1)
            status = mover.read_json(fd, mover.STATUS_NAME)
        self.assertEqual(status["phase"], "failed")
        self.assertEqual(status["unfinished"], 1)

    def test_later_close_confirms_identical_archive_without_replacing_manifest(self):
        path = self.source_path()
        path.write_bytes(SYNTHETIC)
        self.make_old(path)
        self.scan()
        self.capture(self.pilot.next_record(time.monotonic()))
        directory = self.directories()[0]
        manifest = (directory / "manifest.json").read_bytes()
        self.assertEqual(json.loads(manifest)["completion"], "unconfirmed")

        self.events((mover.CLOSE_WRITE, NORMAL))
        self.capture(self.pilot.next_record(time.monotonic()))
        self.assertEqual(len(self.directories()), 1)
        self.assertEqual((directory / "manifest.json").read_bytes(), manifest)
        confirmation = directory / "confirmation.json"
        receipt = json.loads(confirmation.read_bytes())
        self.assertEqual(receipt["completion"], "close_write")
        self.assertEqual(receipt["original_sha256"], json.loads(manifest)["original_sha256"])
        self.assertEqual(self.pilot.counts["completed"]["files"], 1)
        self.assertEqual(self.pilot.counts["archived"]["files"], 1)
        self.assertEqual(self.pilot.counts["archived_unconfirmed"], {"files": 0, "bytes": 0})
        self.assertEqual(self.pilot.counts["confirmed_later"],
                         {"files": 1, "bytes": len(SYNTHETIC)})
        first_inode = confirmation.stat().st_ino
        self.events((mover.CLOSE_WRITE, NORMAL))
        self.assertIsNone(self.pilot.next_record(time.monotonic()))
        self.assertEqual(confirmation.stat().st_ino, first_inode)

    def test_later_close_archives_new_bytes_even_with_same_size_and_mtime(self):
        path = self.source_path()
        path.write_bytes(SYNTHETIC)
        self.make_old(path)
        self.scan()
        self.capture(self.pilot.next_record(time.monotonic()))
        old_archive = self.directories()[0]
        old_stat = path.stat()
        path.write_bytes(b"x" * old_stat.st_size)
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

        self.events((mover.CLOSE_WRITE, NORMAL))
        self.capture(self.pilot.next_record(time.monotonic()))
        self.assertEqual(len(self.directories()), 2)
        self.assertFalse((old_archive / "confirmation.json").exists())
        self.assertCountEqual(
            [json.loads((item / "manifest.json").read_bytes())["completion"]
             for item in self.directories()],
            ["unconfirmed", "close_write"],
        )
        self.assertEqual(self.pilot.counts["archived_unconfirmed"]["files"], 1)
        self.assertEqual(self.pilot.counts["confirmed_later"]["files"], 0)

    def test_pending_budget_refills_oldest_first_and_records_peaks(self):
        self.pilot = self.start_pilot(max_fds=1, max_bytes=len(SYNTHETIC))
        first = self.offer()
        newer = b"v1-responses-2026-11-22T050305-abcd.log"
        for name, seconds_ago in ((newer, 2), (ERROR, 5)):
            self.source_path(name).write_bytes(SYNTHETIC)
            mtime = time.time() - seconds_ago
            os.utime(self.source_path(name), (mtime, mtime))
            self.events((mover.CREATE, name), (mover.CLOSE_WRITE, name))
        self.assertEqual(self.pilot.held_fds, 1)
        self.assertEqual(self.pilot.held_bytes, len(SYNTHETIC))
        self.capture(first)
        self.pilot.refill()
        self.assertTrue(self.pilot.next_record(time.monotonic()).name == ERROR)
        self.capture(self.pilot.next_record(time.monotonic()))
        stats = self.pilot.status(time.monotonic())
        self.assertEqual(stats["max_pending_files"], 3)
        self.assertEqual(stats["max_pending_bytes"], len(SYNTHETIC) * 3)
        self.assertGreater(stats["max_oldest_pending_seconds"], 0)
        self.assertIsNotNone(stats["close_to_publish_seconds"]["p50"])
        self.assertIsNotNone(stats["close_to_publish_seconds"]["p90"])

    def test_space_floor_hysteresis_and_reserve(self):
        record = self.offer()
        self.mount.free = self.archive.floor - 1
        self.capture(record)
        self.assertEqual(self.pilot.paused, "low_space")
        self.mount.free = self.archive.floor + 1
        self.assertEqual(self.archive.probe(True, False, None)[0], "low_space")
        self.assertIsNone(self.archive.probe(False, False, None)[0])
        self.capture(record)
        self.assertEqual(self.directories(), [])
        self.mount.free = self.archive.resume
        self.assertIsNone(self.archive.probe(True, False, None)[0])
        self.capture(record)
        self.assertEqual(len(self.directories()), 1)

    @unittest.skipUnless(sys.platform == "linux", "Linux mount identity")
    def test_real_mount_checker_rejects_plain_directory_without_writing(self):
        uuid = "ef584c76-19ce-4672-8123-b71f5b048e67"
        disk = mover.Disk12(uuid, self.disk)
        os_stat = os.stat

        def stat_device(path, *args, **kwargs):
            if path == "/dev/disk/by-uuid/" + uuid:
                return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 33))
            return os_stat(path, *args, **kwargs)

        with (
            patch.object(mover.os, "stat", side_effect=stat_device),
            patch.object(disk, "verify_mount", wraps=disk.verify_mount) as checked,
        ):
            with self.assertRaises(mover.Paused):
                with disk.open():
                    self.fail("plain directory was accepted as disk12")
            checked.assert_called_once()
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertEqual(list(self.meta.iterdir()), [])

    def test_missing_or_wrong_mount_writes_no_archive_or_history(self):
        record = self.offer()
        for reason in ("mount_unavailable", "mount_identity"):
            self.mount.reason = reason
            self.capture(record)
            self.assertEqual(self.pilot.paused, reason)
            self.assertEqual(self.archive.probe(True, True, self.pilot.status(time.monotonic())),
                             (reason, None, False))
            self.assertEqual(list(self.destination.iterdir()), [])
            self.assertEqual(list(self.meta.iterdir()), [])

    def test_symlinked_archive_path_is_rejected(self):
        decoy = self.root / "decoy"
        decoy.mkdir()
        self.destination.rmdir()
        self.destination.symlink_to(decoy, target_is_directory=True)
        self.capture(self.offer())
        self.assertEqual(list(decoy.iterdir()), [])
        self.assertEqual(self.pilot.counts["archive_failures"]["files"], 1)

    def test_private_modes_and_aggregate_status_and_history(self):
        previous = os.umask(0o077)
        try:
            self.capture(self.offer(ERROR))
            row = self.pilot.status(time.monotonic())
            with mover.directory(self.root) as fd:
                mover.replace_json(fd, mover.STATUS_NAME, row)
            self.assertEqual(self.archive.probe(False, True, row), (None, 0, True))
            self.assertEqual(self.archive.probe(False, False, row), (None, None, True))
        finally:
            os.umask(previous)
        for item in self.destination.rglob("*"):
            self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o700 if item.is_dir() else 0o600)
        status = self.root / mover.STATUS_NAME
        history = next(self.meta.iterdir())
        for path in (status, history):
            data = path.read_bytes()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(data.splitlines()[0])["archived"]["files"], 1)
            self.assertFalse(ERROR in data or SYNTHETIC[:30] in data)
        self.assertEqual(len(history.read_bytes().splitlines()), 2)

    def test_prune_uses_source_mtime_and_exact_age_boundary(self):
        boundary, _ = self.publish_at_age(mover.RETENTION_AGE_NS, ERROR)
        fresh, now_ns = self.publish_at_age(mover.RETENTION_AGE_NS - 1)

        results = self.prune_all(now_ns)

        self.assertFalse(boundary.exists())
        self.assertTrue((fresh / "body.zst").is_file())
        self.assertTrue(self.source_path().is_file())
        self.assertTrue(self.source_path(ERROR).is_file())
        self.assertEqual(sum(result.pruned for result in results), 1)
        self.assertGreater(sum(result.compressed_bytes for result in results), 0)

    def test_prune_handles_empty_hours_and_one_entry_per_worker_step(self):
        first, _ = self.publish_at_age(mover.RETENTION_AGE_NS, NORMAL)
        second, now_ns = self.publish_at_age(mover.RETENTION_AGE_NS, ERROR)
        empty = second.parent.parent / ("00" if second.parent.name != "00" else "01")
        empty.mkdir()
        cursor = mover.PruneCursor()
        pruned = []
        for _ in range(1000):
            result = self.archive.prune(cursor, now_ns)
            self.assertIsNone(result.paused)
            self.assertLessEqual(result.pruned, 1)
            if result.pruned:
                pruned.append(result)
                break
            self.assertFalse(result.complete)
        self.assertEqual(len(pruned), 1)
        self.assertEqual(first.exists() + second.exists(), 1)
        self.prune_all(now_ns, cursor)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(empty.is_dir())

    def test_prune_continues_below_capture_space_floor(self):
        archived, now_ns = self.publish_at_age(mover.RETENTION_AGE_NS)
        self.mount.free = self.archive.floor - 1

        self.prune_all(now_ns)

        self.assertFalse(archived.exists())
        record = self.offer(ERROR)
        self.capture(record)
        self.assertEqual(self.pilot.paused, "low_space")
        self.assertEqual(self.directories(), [])
        self.assertTrue(self.source_path(ERROR).is_file())

    def test_prune_preserves_foreign_files_symlinks_staging_and_benchmarks(self):
        foreign_entry, _ = self.publish_at_age(mover.RETENTION_AGE_NS, NORMAL)
        (foreign_entry / "foreign.txt").write_bytes(b"do not delete")
        prunable, _ = self.publish_at_age(mover.RETENTION_AGE_NS, ERROR)
        linked, now_ns = self.publish_at_age(mover.RETENTION_AGE_NS, NORMAL)
        external = self.root / "external"
        external.write_bytes(b"external data")
        (linked / "body.zst").unlink()
        (linked / "body.zst").symlink_to(external)
        hour = prunable.parent
        stage = hour / ".pending-old"
        stage.mkdir()
        (stage / "body.zst").write_bytes(b"partial stage")
        foreign_directory = hour / "foreign"
        foreign_directory.mkdir()
        unmarked_empty = hour / ("a" * 64)
        unmarked_empty.mkdir()
        benchmark = self.destination / "benchmark-archive"
        benchmark.mkdir()
        (benchmark / "manifest.json").write_bytes(b"benchmark")
        link = hour / ("f" * 64)
        link.symlink_to(prunable, target_is_directory=True)

        self.prune_all(now_ns)

        self.assertFalse(prunable.exists())
        self.assertTrue(os.path.lexists(link))
        self.assertEqual((foreign_entry / "foreign.txt").read_bytes(), b"do not delete")
        self.assertTrue((foreign_entry / "manifest.json").is_file())
        self.assertTrue((linked / "manifest.json").is_file())
        self.assertEqual(external.read_bytes(), b"external data")
        self.assertEqual((stage / "body.zst").read_bytes(), b"partial stage")
        self.assertTrue(foreign_directory.is_dir())
        self.assertTrue(unmarked_empty.is_dir())
        self.assertEqual((benchmark / "manifest.json").read_bytes(), b"benchmark")

    def test_prune_restarts_after_interrupted_deletion(self):
        for interrupted in ("body_unlink", "directory_unlink"):
            with self.subTest(interrupted=interrupted):
                name = NORMAL if interrupted == "body_unlink" else ERROR
                archived, now_ns = self.publish_at_age(mover.RETENTION_AGE_NS, name)
                real_unlink, real_rmdir = os.unlink, os.rmdir

                def unlink(name, *args, **kwargs):
                    real_unlink(name, *args, **kwargs)
                    if name == "body.zst":
                        raise OSError(errno.EIO, "synthetic interruption")

                def rmdir(name, *args, **kwargs):
                    raise OSError(errno.EIO, "synthetic interruption")

                replacement = unlink if interrupted == "body_unlink" else rmdir
                with patch.object(mover.os, "unlink" if interrupted == "body_unlink" else "rmdir",
                                  side_effect=replacement):
                    results = self.prune_all(now_ns)
                self.assertEqual(sum(result.failures for result in results), 1)
                retiring = archived.with_name(mover.EXPIRED_PREFIX + archived.name)
                self.assertTrue(retiring.is_dir())
                self.prune_all(now_ns)
                self.assertFalse(archived.exists())
                self.assertFalse(retiring.exists())
                self.assertTrue(self.source_path(name).is_file())

    def test_pruned_source_is_not_recaptured(self):
        archived, now_ns = self.publish_at_age(mover.RETENTION_AGE_NS)
        self.prune_all(now_ns)
        self.assertFalse(archived.exists())

        self.pilot = self.start_pilot(continuous=True)
        self.scan()
        self.scan()

        self.assertIsNone(self.pilot.next_record(time.monotonic()))
        self.assertEqual(self.pilot.counts["expired_sources"],
                         {"files": 1, "bytes": len(SYNTHETIC)})
        self.assertEqual(self.pilot.counts["archived"]["files"], 0)
        self.assertTrue(self.source_path().is_file())

    def test_pilot_keeps_copying_aged_sources_without_expiry(self):
        source = self.source_path()
        source.write_bytes(SYNTHETIC)
        old = time.time_ns() - mover.RETENTION_AGE_NS - 10**9
        os.utime(source, ns=(old, old))
        duration = self.pilot.admission_cutoff - self.pilot.started_at_monotonic

        self.scan()
        self.capture(self.pilot.next_record(time.monotonic()))

        self.assertEqual(len(self.directories()), 1)
        self.assertAlmostEqual(duration, mover.ADMISSION_WINDOW)
        self.assertEqual(self.pilot.counts["archived"]["files"], 1)
        self.assertEqual(self.pilot.counts["pruned"]["files"], 0)

    def test_prune_status_counts_and_continuous_contract(self):
        archived, now_ns = self.publish_at_age(mover.RETENTION_AGE_NS)
        compressed_bytes = json.loads((archived / "manifest.json").read_bytes())["compressed_bytes"]
        results = self.prune_all(now_ns, self.pilot.retention_cursor)
        for result in results:
            self.pilot.prune_result(result, 100)
        status = self.pilot.status(time.monotonic())
        self.assertEqual(status["pruned"], {"files": 1, "bytes": compressed_bytes})
        self.assertEqual(self.pilot.retention_due, 100 + mover.RETENTION_INTERVAL)

        next_archive, next_now = self.publish_at_age(mover.RETENTION_AGE_NS, ERROR)
        for result in self.prune_all(next_now, self.pilot.retention_cursor):
            self.pilot.prune_result(result, 100 + mover.RETENTION_INTERVAL)
        self.assertFalse(next_archive.exists())
        self.assertEqual(self.pilot.counts["pruned"]["files"], 2)

        continuous = self.start_pilot(continuous=True)
        self.assertEqual(continuous.admission_cutoff, float("inf"))
        self.assertEqual(continuous.publication_deadline, float("inf"))
        status = continuous.status(time.monotonic())
        self.assertIsNone(status["admission_cutoff_unix"])
        self.assertIsNone(status["publication_deadline_unix"])
        properties = {
            "ActiveState": "inactive", "Result": "success", "ExecMainStatus": "0",
            "ActiveEnterTimestampMonotonic": "1000000",
            "ExecMainExitTimestampMonotonic": "2000000",
        }
        self.assertIn("inactive", mover.watchdog_alerts(properties, None, 3, continuous=True))

    def test_watchdog_uses_manager_exit_timestamps_and_result(self):
        started = 1_234_567
        cutoff = started + mover.ADMISSION_WINDOW
        properties = {
            "ActiveState": "inactive",
            "Result": "success",
            "ExecMainStatus": "0",
            "ActiveEnterTimestampMonotonic": str(started * 1_000_000),
            "ExecMainExitTimestampMonotonic": str(cutoff * 1_000_000 - 1),
            # Conflicting wall-clock values cannot affect the decision.
            "ActiveEnterTimestamp": "2034-01-02",
            "InactiveEnterTimestamp": "2003-01-01",
        }
        self.assertEqual(mover.watchdog_alerts(properties, cutoff + 20, cutoff + 20),
                         {"inactive"})
        properties["ExecMainExitTimestampMonotonic"] = str(cutoff * 1_000_000)
        self.assertEqual(mover.watchdog_alerts(properties, None, cutoff + 20), set())
        for failure in ({"ExecMainStatus": "3"}, {"Result": "timeout"},
                        {"ActiveState": "failed"}):
            with self.subTest(failure=failure):
                self.assertEqual(mover.watchdog_alerts(properties | failure, None, cutoff + 20),
                                 {"failed"})
        self.assertEqual(mover.watchdog_alerts({}, None, cutoff + 20), {"manager"})

    def test_watchdog_zero_start_reports_not_started_even_after_startup_failure(self):
        properties = {
            "ActiveState": "inactive", "Result": "success", "ExecMainStatus": "0",
            "ActiveEnterTimestampMonotonic": "0", "ExecMainExitTimestampMonotonic": "0",
        }
        self.assertEqual(mover.watchdog_alerts(properties, None, 100), {"not_started"})
        properties.update(ActiveState="failed", Result="exit-code", ExecMainStatus="1")
        self.assertEqual(mover.watchdog_alerts(properties, None, 100), {"not_started", "failed"})

    def test_watchdog_alert_hook_only_fires_on_state_changes(self):
        started = 7_200
        now = started + mover.STALE_AFTER + 1
        active = {
            "ActiveState": "active", "Result": "success",
            "ActiveEnterTimestampMonotonic": str(started * 1_000_000),
        }
        self.mount.reason = "mount_unavailable"
        messages = []
        alerts = mover.watchdog_poll(self.mount, active, started - 1, now, set(), messages.append)
        self.assertEqual(alerts, {"heartbeat", "mount"})
        mover.watchdog_poll(self.mount, active, started - 1, now, alerts, messages.append)
        self.assertEqual(len(messages), 2)
        self.mount.reason = None
        alerts = mover.watchdog_poll(self.mount, active, now, now, alerts, messages.append)
        self.assertEqual(alerts, set())
        self.assertEqual(len(messages), 4)

    def test_watchdog_loop_reads_monotonic_heartbeat_without_consulting_wall_time(self):
        now = 7_499
        active = {"ActiveState": "active", "ActiveEnterTimestampMonotonic": "7300000000"}
        stopped = threading.Event()
        status = {"heartbeat_monotonic": now - 1, "heartbeat_unix": 1_900_000_000}
        with mover.directory(self.root) as fd:
            mover.replace_json(fd, mover.STATUS_NAME, status)
        with (
            patch.object(mover, "STATUS_PATH", self.root),
            patch.object(mover, "manager_properties", return_value=active),
            patch.object(mover, "watchdog_poll", wraps=mover.watchdog_poll) as poll,
            patch.object(mover.time, "monotonic", return_value=now),
            patch.object(mover.time, "time", side_effect=AssertionError("wall clock consulted")),
            patch.object(stopped, "wait", side_effect=lambda _: stopped.set()),
        ):
            mover.watchdog_loop(self.mount, "synthetic.service", stopped)
        poll.assert_called_once_with(self.mount, active, now - 1, now, set())

    @unittest.skipUnless(mover.zstd is not None, "in-process zstd requires Python 3.14")
    def test_production_codec_round_trip(self):
        self.archive.codec = mover.ZstdCodec()
        self.capture(self.offer())
        self.assertEqual(len(self.directories()), 1)
        self.assertEqual(self.pilot.counts["archived"]["bytes"], len(SYNTHETIC))

    @unittest.skipUnless(mover.zstd is not None, "in-process zstd requires Python 3.14")
    def test_production_codec_verifies_before_pruning(self):
        self.archive.codec = mover.ZstdCodec()
        archived, now_ns = self.publish_at_age(mover.RETENTION_AGE_NS)
        results = self.prune_all(now_ns)
        self.assertFalse(archived.exists())
        self.assertEqual(sum(result.pruned for result in results), 1)
        self.assertTrue(self.source_path().is_file())


@unittest.skipUnless(shutil.which("systemd-analyze") and Path("/usr/bin/python3.14").exists(),
                     "unit verification needs systemd-analyze and the pinned interpreter path")
class SystemdUnitTests(unittest.TestCase):
    def test_verify(self):
        instance = Path(__file__).parent / "instances/will"
        units = [str(item) for item in sorted(instance.glob("cliproxy-will-log-mover*"))]
        result = subprocess.run(["systemd-analyze", "verify", *units], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
