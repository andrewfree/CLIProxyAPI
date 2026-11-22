#!/usr/bin/env python3
"""Archive Will's closed request logs during the copy-only pilot."""

import base64
from collections import Counter
from concurrent.futures import Future
from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Queue
import re
import select
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid

try:
    from compression import zstd
except ImportError:
    zstd = None

MOUNT_PATH = Path("/srv/share/drives/disk12")
INSTANCE_PATH = Path("/home/andrew/cliproxy")
STATUS_PATH = Path("/run/cliproxy-will-log-mover")
WATCHDOG_PATH = Path("/run/cliproxy-will-log-mover-watchdog")
STATUS_NAME = "status.json"
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
NEW_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
GIB = 1024**3
MIN_FREE = 200 * GIB
RESUME_FREE = 250 * GIB
MAX_FDS = 512
MAX_BYTES = 2 * GIB
MAX_RECORDS = 16_384
SCAN_INTERVAL = 15
HEARTBEAT_INTERVAL = 5
STALE_AFTER = 75
UNCONFIRMED_AGE = 60
PILOT_DURATION = 24 * 60 * 60
DRAIN_SECONDS = 5 * 60
HISTORY_INTERVAL = 60
CHUNK_SIZE = 1024 * 1024

# Go takes up to eight bytes of a supplied request ID or emits a uint64 counter.
REQUEST_NAME = re.compile(
    rb"[^./\\-][^/\\]*-\d{4}-\d{2}-\d{2}T\d{6}(?:_\d{1,4})?"
    rb"-(?:[^/\\]{1,8}|[0-9]{1,20})\.log\Z"
)
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
CLOSE_WRITE = 0x00000008
MOVED_TO = 0x00000080
CREATE = 0x00000100
MOVED_FROM = 0x00000040
DELETE = 0x00000200
DELETE_SELF = 0x00000400
MOVE_SELF = 0x00000800
UNMOUNT = 0x00002000
OVERFLOW = 0x00004000
IGNORED = 0x00008000
EXCL_UNLINK = 0x04000000
IS_DIR = 0x40000000
# A close from an already-unlinked writer cannot attest a reused pathname.
WATCH_MASK = (CLOSE_WRITE | MOVED_TO | MOVED_FROM | CREATE | DELETE
              | DELETE_SELF | MOVE_SELF | EXCL_UNLINK)
WATCH_LOST = DELETE_SELF | MOVE_SELF | UNMOUNT | IGNORED
EVENT_HEADER = struct.Struct("=iIII")


class IntegrityError(Exception):
    pass


class Paused(Exception):
    pass


class CaptureCancelled(Exception):
    pass


@contextmanager
def directory(name, parent=None, create=False, device=None):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            os.fsync(parent)
    fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    try:
        opened = os.fstat(fd)
        entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(entry.st_mode) or not os.path.samestat(opened, entry):
            raise OSError(errno.ELOOP, "directory entry is unsafe")
        if device is not None and opened.st_dev != device:
            raise Paused("mount_identity")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def directory_tree(root, components, create=False):
    device = os.fstat(root).st_dev
    with ExitStack() as stack:
        for component in components:
            root = stack.enter_context(directory(component, root, create, device))
        yield root


def read_file(fd, name):
    return os.fdopen(os.open(name, READ_FLAGS, dir_fd=fd), "rb")


def new_file(fd, name):
    return os.fdopen(os.open(name, NEW_FLAGS, 0o600, dir_fd=fd), "wb")


def read_json(fd, name):
    with read_file(fd, name) as source:
        raw = source.read(16_385)
    if len(raw) > 16_384:
        raise IntegrityError
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise IntegrityError from None
    if not isinstance(value, dict):
        raise IntegrityError
    return value


def replace_json(fd, name, value):
    temporary = "." + name + ".part"
    try:
        os.unlink(temporary, dir_fd=fd)
    except FileNotFoundError:
        pass
    with new_file(fd, temporary) as target:
        target.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)


def linux_call(function, *args):
    result = getattr(ctypes.CDLL(None, use_errno=True), function)(*args)
    if result == -1:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def publish(parent, staging, archive_id):
    linux_call("renameat2", parent, os.fsencode(staging), parent,
               os.fsencode(archive_id), 1)  # RENAME_NOREPLACE


def request_name(name):
    return REQUEST_NAME.fullmatch(name) is not None and b".." not in name


class Inotify:
    def __init__(self, source_fd):
        self.fd = linux_call("inotify_init1", os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            linux_call("inotify_add_watch", self.fd,
                       os.fsencode(f"/proc/self/fd/{source_fd}"), WATCH_MASK)
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    def events(self):
        while True:
            try:
                data = os.read(self.fd, 64 * 1024)
            except BlockingIOError:
                return
            offset = 0
            while offset < len(data):
                _, mask, _, length = EVENT_HEADER.unpack_from(data, offset)
                offset += EVENT_HEADER.size
                yield mask, data[offset:offset + length].split(b"\0", 1)[0]
                offset += length


class Disk12:
    def __init__(self, uuid, path=MOUNT_PATH):
        self.uuid = uuid
        self.path = path

    @contextmanager
    def open(self):
        # The FD's mount ID distinguishes the configured mount from an ordinary
        # directory, a bind of a subtree, or a mount replaced during the check.
        try:
            if not re.fullmatch(r"[0-9A-Fa-f-]{36}", self.uuid):
                raise Paused("mount_identity")
            device = os.stat("/dev/disk/by-uuid/" + self.uuid)
            if not stat.S_ISBLK(device.st_mode):
                raise Paused("mount_identity")
            fd = os.open(self.path, DIR_FLAGS)
        except OSError:
            raise Paused("mount_unavailable") from None
        try:
            try:
                self.verify_mount(fd, device)
                space = os.fstatvfs(fd)
            except (OSError, StopIteration):
                raise Paused("mount_unavailable") from None
            yield fd, space.f_bavail * space.f_frsize
        finally:
            os.close(fd)

    def verify_mount(self, fd, device):
        with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as info:
            mount_id = next(line.split()[1] for line in info if line.startswith("mnt_id:"))
        major_minor = f"{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}"
        with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as mounts:
            for line in mounts:
                prefix, suffix = line.split(" - ", 1)
                info_fields = prefix.split()
                if info_fields[0] != mount_id:
                    continue
                filesystem, _, super_options = suffix.split()
                if (info_fields[2:5] != [major_minor, "/", str(self.path)]
                        or filesystem != "ext4"):
                    raise Paused("mount_identity")
                if "rw" not in super_options.split(","):
                    raise Paused("mount_read_only")
                return
        raise Paused("mount_unavailable")


def transfer(source, target=None, limit=None):
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(CHUNK_SIZE):
        size += len(chunk)
        if limit is not None and size > limit:
            raise IntegrityError
        digest.update(chunk)
        if target is not None:
            target.write(chunk)
    return size, digest.hexdigest()


class ZstdCodec:
    def compress(self, source, target, limit):
        try:
            with zstd.ZstdFile(target, "wb", level=3) as compressed:
                return transfer(source, compressed, limit)
        except zstd.ZstdError:
            raise IntegrityError from None

    def decompress_digest(self, source, limit):
        try:
            with zstd.ZstdFile(source, "rb") as restored:
                return transfer(restored, limit=limit)
        except (EOFError, zstd.ZstdError):
            raise IntegrityError from None


def inode(info):
    return info.st_dev, info.st_ino


def content_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@dataclass
class Record:
    name: bytes
    info: os.stat_result
    evidence: str
    observed: float
    fd: int | None = None
    retry_at: float = 0
    pending: bool = True
    cancelled: threading.Event = field(default_factory=threading.Event)

    @property
    def key(self):
        return self.name, content_identity(self.info)

    def manifest(self):
        return {
            "version": 1,
            "source_basename_b64": base64.b64encode(self.name).decode("ascii"),
            "completion": self.evidence,
            "source_identity": {
                "device": self.info.st_dev,
                "inode": self.info.st_ino,
                "ctime_unix_ns": self.info.st_ctime_ns,
            },
            "mtime_unix_ns": self.info.st_mtime_ns,
            "original_bytes": self.info.st_size,
            "codec": "zstd",
            "compression_level": 3,
        }


def source_key(manifest):
    identity = manifest["source_identity"]
    fields = [manifest["source_basename_b64"], identity["device"], identity["inode"],
              manifest["mtime_unix_ns"], manifest["original_bytes"]]
    return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def archive_key(manifest):
    return hashlib.sha256(
        bytes.fromhex(source_key(manifest)) + bytes.fromhex(manifest["original_sha256"])
    ).hexdigest()


def hour_parts(mtime_ns):
    utc = datetime.fromtimestamp(mtime_ns // 10**9, timezone.utc)
    return utc.strftime("%Y/%m/%d/%H").split("/")


def same_source(record, info):
    return content_identity(info) == content_identity(record.info) and (
        info.st_ctime_ns == record.info.st_ctime_ns
        or info.st_nlink == 0 < record.info.st_nlink
    )


class Archive:
    def __init__(self, mount, codec, components, floor=MIN_FREE, resume=RESUME_FREE):
        self.mount = mount
        self.codec = codec
        self.components = components
        self.floor = floor
        self.resume = resume
        self.meta = (components[0], "meta")

    @contextmanager
    def destination(self, mount_fd, components):
        with directory_tree(mount_fd, components) as archive_fd:
            if os.fstatvfs(archive_fd).f_flag & os.ST_RDONLY:
                raise Paused("archive_read_only")
            yield archive_fd

    def probe(self, paused, count_staging, history):
        abandoned = None
        history_written = False
        try:
            with self.mount.open() as (mount_fd, free):
                with self.destination(mount_fd, self.components) as archive_fd:
                    if free < (self.resume if paused else self.floor):
                        raise Paused("low_space")
                    if count_staging:
                        abandoned = self.count_staging(archive_fd)
                    if history is not None:
                        with self.destination(mount_fd, self.meta) as meta_fd:
                            self.append_history(meta_fd, history)
                        history_written = True
        except Paused as error:
            return str(error), abandoned, history_written
        except OSError:
            return "archive_unavailable", abandoned, history_written
        return None, abandoned, history_written

    def count_staging(self, parent, depth=0):
        total = 0
        with os.scandir(parent) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if depth == 4:
                    total += entry.name.startswith(".pending-")
                elif re.fullmatch(r"[0-9]{4}" if depth == 0 else r"[0-9]{2}", entry.name):
                    with directory(entry.name, parent, device=os.fstat(parent).st_dev) as child:
                        total += self.count_staging(child, depth + 1)
        return total

    @staticmethod
    def append_history(meta_fd, row):
        name = f"history-{int(row['started_at_unix'] * 1_000_000_000)}.jsonl"
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        created = False
        try:
            target = new_file(meta_fd, name)
            created = True
        except FileExistsError:
            target = os.fdopen(os.open(name, flags, dir_fd=meta_fd), "ab")
        with target:
            if not stat.S_ISREG(os.fstat(target.fileno()).st_mode):
                raise OSError(errno.EINVAL, "history is not a regular file")
            target.write(json.dumps(row, separators=(",", ":")).encode() + b"\n")
            target.flush()
            os.fsync(target.fileno())
        if created:
            os.fsync(meta_fd)

    def capture(self, record, fd, allowed):
        if not allowed():
            raise CaptureCancelled
        size = record.info.st_size
        reserve = size + size // 256 + 4 * CHUNK_SIZE
        with self.mount.open() as (mount_fd, free):
            if free < self.floor + reserve:
                raise Paused("low_space")
            with self.destination(mount_fd, self.components) as root:
                with directory_tree(root, hour_parts(record.info.st_mtime_ns), create=True) as hour:
                    return self.capture_in_hour(record, fd, hour, allowed)

    def capture_in_hour(self, record, fd, hour, allowed):
        if not same_source(record, os.fstat(fd)):
            raise IntegrityError
        manifest = record.manifest()
        stage = ".pending-" + uuid.uuid4().hex
        os.mkdir(stage, 0o700, dir_fd=hour)
        published = False
        try:
            with directory(stage, hour, device=os.fstat(hour).st_dev) as stage_fd:
                with os.fdopen(os.dup(fd), "rb") as source:
                    source.seek(0)
                    with new_file(stage_fd, "body.zst") as target:
                        original = self.codec.compress(source, target, record.info.st_size)
                        target.flush()
                        os.fsync(target.fileno())
                if original[0] != record.info.st_size or not same_source(record, os.fstat(fd)):
                    raise IntegrityError
                with read_file(stage_fd, "body.zst") as compressed:
                    stored = transfer(compressed)
                    compressed.seek(0)
                    if self.codec.decompress_digest(compressed, original[0]) != original:
                        raise IntegrityError
                manifest.update(original_sha256=original[1], compressed_bytes=stored[0],
                                compressed_sha256=stored[1])
                manifest["archive_id"] = archive_key(manifest)
                replace_json(stage_fd, "manifest.json", manifest)
                if not same_source(record, os.fstat(fd)):
                    raise IntegrityError
                published = self.commit(hour, stage, manifest, allowed)
            return published, os.fstat(fd).st_nlink == 0, stored[0], time.monotonic()
        finally:
            if not published:
                self.remove_stage(hour, stage)

    @staticmethod
    def remove_stage(hour, stage):
        try:
            with directory(stage, hour, device=os.fstat(hour).st_dev) as fd:
                for name in ("body.zst", "manifest.json", ".manifest.json.part"):
                    try:
                        os.unlink(name, dir_fd=fd)
                    except FileNotFoundError:
                        pass
            os.rmdir(stage, dir_fd=hour)
        except (OSError, Paused):
            # The partial directory stays private and can be counted next run.
            pass

    def verify(self, archive_fd, manifest):
        try:
            original = manifest["original_bytes"], manifest["original_sha256"]
            stored = manifest["compressed_bytes"], manifest["compressed_sha256"]
            if (manifest["version"] != 1 or manifest["codec"] != "zstd"
                    or manifest["compression_level"] != 3
                    or manifest.get("completion", manifest.get("completion_evidence"))
                    not in ("close_write", "unconfirmed")
                    or any(type(size) is not int or size < 0 or not HEX_SHA256.fullmatch(digest)
                           for size, digest in (original, stored))
                    or manifest["archive_id"] != archive_key(manifest)):
                raise IntegrityError
        except (KeyError, ValueError, TypeError):
            raise IntegrityError from None
        with read_file(archive_fd, "body.zst") as compressed:
            if transfer(compressed, limit=stored[0]) != stored:
                raise IntegrityError
            compressed.seek(0)
            if self.codec.decompress_digest(compressed, original[0]) != original:
                raise IntegrityError

    def commit(self, hour, stage, manifest, allowed):
        archive_id = manifest["archive_id"]
        if not allowed():
            raise CaptureCancelled
        try:
            publish(hour, stage, archive_id)
        except FileExistsError:
            with directory(archive_id, hour, device=os.fstat(hour).st_dev) as committed:
                saved = read_json(committed, "manifest.json")
                self.verify(committed, saved)
                if any(saved.get(field) != manifest[field] for field in (
                    "archive_id", "original_sha256", "original_bytes",
                )):
                    raise IntegrityError
            os.fsync(hour)
            return False
        os.fsync(hour)
        return True


class DiskWorker:
    """One serial disk worker. The watcher can exit if the kernel stalls an I/O."""

    def __init__(self):
        self.tasks = Queue(maxsize=1)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while task := self.tasks.get():
            job, function, args = task
            try:
                job.set_result(function(*args))
            except Exception as error:
                job.set_exception(error)

    def submit(self, function, *args):
        job = Future()
        self.tasks.put_nowait((job, function, args))
        return job

    def close(self):
        self.tasks.put_nowait(None)


class Pilot:
    def __init__(self, archive, max_fds=MAX_FDS, max_bytes=MAX_BYTES, deadline=None):
        self.archive = archive
        self.max_fds = max_fds
        self.max_bytes = max_bytes
        self.started_at = time.time()
        start = time.monotonic()
        self.deadline = start + PILOT_DURATION - DRAIN_SECONDS if deadline is None else deadline
        self.deadline_unix = self.started_at + self.deadline - start
        self.source_fd = None
        self.watch = None
        self.source_unavailable = False
        self.seen = {}
        self.records = {}
        self.pending_files = self.pending_bytes = 0
        self.held_fds = self.held_bytes = 0
        self.peak_files = self.peak_bytes = 0
        self.peak_age = 0
        self.latencies = Counter()
        self.max_latency = 0
        self.paused = "checking"
        self.phase = "capturing"
        self.watch_gaps = self.queue_overflows = self.ambiguous_closes = 0
        self.abandoned_staging = None
        self.unknown_miss_bytes = self.stored_bytes = 0
        self.counts = {
            label: {"files": 0, "bytes": 0}
            for label in ("completed", "archived", "unconfirmed", "archived_unconfirmed",
                          "recovered_after_unlink", "missed", "integrity_failures",
                          "archive_failures", "abandoned_pending")
        }

    def count(self, label, size=0):
        self.counts[label]["files"] += 1
        self.counts[label]["bytes"] += size

    def release(self, record):
        if record.fd is not None:
            self.held_fds -= 1
            self.held_bytes -= record.info.st_size
            os.close(record.fd)
            record.fd = None

    def finish(self, record):
        self.release(record)
        if record.pending:
            self.pending_files -= 1
            self.pending_bytes -= record.info.st_size
            record.pending = False

    def discard_pending(self):
        self.observe_age(time.monotonic())
        for record in self.records.values():
            if record.pending:
                record.cancelled.set()
                self.count("abandoned_pending", record.info.st_size)
                self.finish(record)
        self.records = {key: item for key, item in self.records.items()
                        if not item.cancelled.is_set()}

    def disarm(self):
        if self.watch is not None:
            self.watch.close()
            self.watch = None
        if self.source_fd is not None:
            os.close(self.source_fd)
            self.source_fd = None

    def watch_gap(self):
        self.watch_gaps += 1
        self.discard_pending()
        self.seen.clear()
        self.disarm()

    def arm(self, source):
        try:
            with directory(source) as visible:
                if self.watch is not None:
                    if os.path.samestat(os.fstat(visible), os.fstat(self.source_fd)):
                        return
                    self.watch_gap()
                self.source_fd = os.dup(visible)
                self.watch = Inotify(self.source_fd)
                self.source_unavailable = False
        except OSError:
            if not self.source_unavailable:
                self.watch_gap()
            else:
                self.disarm()
            self.source_unavailable = True

    def close(self):
        self.disarm()
        for record in self.records.values():
            self.release(record)

    def open_source(self, name):
        fd = os.open(name, READ_FLAGS, dir_fd=self.source_fd)
        try:
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode):
                return fd, info
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
        return None, None

    def hold(self, record, fd):
        size = record.info.st_size
        if self.held_fds >= self.max_fds or self.held_bytes + size > self.max_bytes:
            os.close(fd)
            return
        record.fd = fd
        self.held_fds += 1
        self.held_bytes += size

    def remember(self, name, identity):
        if name in self.seen or len(self.seen) < MAX_RECORDS:
            self.seen[name] = identity
        else:
            self.queue_overflows += 1

    def arrived(self, name):
        try:
            info = os.stat(name, dir_fd=self.source_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISREG(info.st_mode):
            self.remember(name, inode(info))

    def observed_close(self, name, now):
        try:
            fd, info = self.open_source(name)
        except FileNotFoundError:
            self.miss(None)
            return None
        except OSError:
            self.count("archive_failures")
            return None
        if fd is None:
            return None
        if self.seen.get(name) != inode(info):
            self.ambiguous_closes += 1
            os.close(fd)
            return None
        record = Record(name, info, "close_write", now)
        self.hold(record, fd)
        return record

    def admit(self, record):
        known = self.records.get(record.key)
        if known is not None:
            if known.evidence == "unconfirmed" and record.evidence == "close_write":
                if known.pending:
                    known.cancelled.set()
                    self.finish(known)
                else:
                    self.count("completed", record.info.st_size)
                    known.evidence = "close_write"
                    self.release(record)
                    return
            else:
                self.release(record)
                return
        size = record.info.st_size
        if record.evidence == "close_write":
            self.count("completed", size)
        else:
            self.count("unconfirmed", size)
        if (known is None and len(self.records) >= MAX_RECORDS) or size > self.max_bytes:
            self.queue_overflows += 1
            self.release(record)
            if record.evidence == "close_write":
                self.miss(size)
            return
        self.records[record.key] = record
        self.pending_files += 1
        self.pending_bytes += size
        self.peak_files = max(self.peak_files, self.pending_files)
        self.peak_bytes = max(self.peak_bytes, self.pending_bytes)

    def miss(self, size):
        self.count("missed", size or 0)
        if size is None:
            self.unknown_miss_bytes += 1

    def drain(self, now):
        if self.watch is None or now >= self.deadline:
            return False
        candidates = {}
        for mask, name in self.watch.events():
            if mask & (WATCH_LOST | OVERFLOW):
                for candidate in candidates.values():
                    self.release(candidate)
                self.watch_gap()
                return True
            if mask & IS_DIR or not request_name(name):
                continue
            if mask & (CREATE | MOVED_TO):
                if old := candidates.pop(name, None):
                    self.release(old)
                    self.ambiguous_closes += 1
                if mask & CREATE:
                    self.arrived(name)
                else:
                    self.remember(name, None)
            if mask & (DELETE | MOVED_FROM):
                self.seen.pop(name, None)
            if mask & CLOSE_WRITE and name not in candidates:
                if len(candidates) >= MAX_RECORDS:
                    self.queue_overflows += 1
                elif record := self.observed_close(name, now):
                    candidates[name] = record
        for record in candidates.values():
            if time.monotonic() < self.deadline:
                self.admit(record)
            else:
                self.release(record)
        return False

    def reconcile(self, source, now, in_flight=None):
        self.arm(source)
        if self.watch is None:
            return
        present = set()
        live_records = set()
        old_mtime = time.time_ns() - UNCONFIRMED_AGE * 10**9
        with os.scandir(self.source_fd) as entries:
            for entry in entries:
                name = os.fsencode(entry.name)
                if not request_name(name):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                if len(present) >= MAX_RECORDS:
                    self.queue_overflows += 1
                    break
                present.add(name)
                if name not in self.seen:
                    self.remember(name, inode(info))
                key = name, content_identity(info)
                if key not in self.records and info.st_mtime_ns <= old_mtime:
                    self.admit(Record(name, info, "unconfirmed", now))
                live_records.add(key)
        self.seen = {name: identity for name, identity in self.seen.items() if name in present}
        for key, record in list(self.records.items()):
            if key in live_records or record is in_flight or record.fd is not None:
                continue
            if record.pending:
                if record.evidence == "close_write":
                    self.miss(record.info.st_size)
                self.finish(record)
            del self.records[key]
        self.refill(in_flight)

    def refill(self, in_flight=None):
        if self.source_fd is None or self.held_fds >= self.max_fds:
            return
        waiters = sorted((r for r in self.records.values()
                          if r.pending and r.fd is None and r is not in_flight),
                         key=lambda r: (r.info.st_mtime_ns, r.observed))
        for record in waiters:
            if self.held_fds >= self.max_fds:
                break
            if self.held_bytes + record.info.st_size > self.max_bytes:
                continue
            try:
                fd, info = self.open_source(record.name)
            except FileNotFoundError:
                if record.evidence == "close_write":
                    self.miss(record.info.st_size)
                self.finish(record)
                continue
            except OSError:
                self.count("archive_failures", record.info.st_size)
                self.finish(record)
                continue
            if fd is not None and same_source(record, info):
                self.hold(record, fd)
            else:
                if fd is not None:
                    os.close(fd)
                if record.evidence == "close_write":
                    self.miss(record.info.st_size)
                self.finish(record)

    def next_record(self, now):
        if now >= self.deadline:
            return None
        return min(
            (record for record in self.records.values()
             if record.pending and record.fd is not None and record.retry_at <= now),
            key=lambda record: (record.info.st_mtime_ns, record.observed),
            default=None,
        )

    def allowed(self, record):
        return not record.cancelled.is_set() and time.monotonic() < self.deadline

    def capture(self, record, fd):
        try:
            return self.archive.capture(record, fd, lambda: self.allowed(record))
        finally:
            os.close(fd)

    def capture_result(self, record, job, now):
        size = record.info.st_size
        self.observe_age(now)
        try:
            created, recovered, stored_bytes, published_at = job.result()
        except CaptureCancelled:
            if not record.cancelled.is_set():
                self.count("abandoned_pending", size)
            self.finish(record)
        except Paused as error:
            self.paused = str(error)
        except IntegrityError:
            self.count("integrity_failures", size)
            self.finish(record)
        except OSError as error:
            self.count("archive_failures", size)
            if error.errno in (errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS):
                self.paused = "archive_io"
            record.retry_at = now + SCAN_INTERVAL
        else:
            if created:
                self.count("archived", size)
                self.stored_bytes += stored_bytes
                if record.evidence == "unconfirmed":
                    self.count("archived_unconfirmed", size)
                else:
                    latency = max(0, published_at - record.observed)
                    bucket = math.ceil(latency * 10) if latency < 60 else math.ceil(latency) * 10
                    self.latencies[bucket] += 1
                    self.max_latency = max(self.max_latency, latency)
                if recovered:
                    self.count("recovered_after_unlink", size)
            self.finish(record)

    def observe_age(self, now):
        current = max((now - record.observed for record in self.records.values()
                       if record.pending), default=0)
        self.peak_age = max(current, self.peak_age)
        return current

    def latency_summary(self):
        total = self.latencies.total()
        p50 = p90 = None
        cumulative = 0
        for bucket, count in sorted(self.latencies.items()):
            cumulative += count
            if p50 is None and cumulative >= math.ceil(total * 0.5):
                p50 = bucket / 10
            if cumulative >= math.ceil(total * 0.9):
                p90 = bucket / 10
                break
        return {"p50": p50, "p90": p90, "max": self.max_latency if total else None}

    def status(self, now):
        oldest = self.observe_age(now)
        return {
            "started_at_unix": self.started_at,
            "capture_deadline_unix": self.deadline_unix,
            "heartbeat_unix": time.time(),
            "phase": self.phase,
            "paused_reason": self.paused,
            "source_watch_active": self.watch is not None,
            "watch_gaps": self.watch_gaps,
            "queue_overflows": self.queue_overflows,
            "ambiguous_closes": self.ambiguous_closes,
            "abandoned_staging": self.abandoned_staging,
            "missed_unknown_bytes": self.unknown_miss_bytes,
            "archived_compressed_bytes": self.stored_bytes,
            "pending": {"files": self.pending_files, "bytes": self.pending_bytes},
            "oldest_pending_seconds": oldest,
            "max_pending_files": self.peak_files,
            "max_pending_bytes": self.peak_bytes,
            "max_oldest_pending_seconds": self.peak_age,
            "close_to_publish_seconds": self.latency_summary(),
            **{key: value.copy() for key, value in self.counts.items()},
        }

    def run(self, source, status_fd, stop):
        scan_due = heartbeat_due = probe_due = history_due = 0
        worker = DiskWorker()
        job = record = None
        history_requested = False
        drain_end = None
        try:
            while True:
                now = time.monotonic()
                if job is not None and job.done():
                    if record is not None:
                        self.capture_result(record, job, now)
                        if self.phase == "capturing" and now < self.deadline and not stop():
                            self.refill()
                    else:
                        self.paused, abandoned, wrote_history = job.result()
                        if abandoned is not None:
                            self.abandoned_staging = abandoned
                        if history_requested and wrote_history:
                            history_due = now + HISTORY_INTERVAL
                    job = record = None

                if self.phase == "capturing" and (now >= self.deadline or stop()):
                    self.phase = "draining" if now >= self.deadline else "interrupted"
                    drain_end = (self.deadline + DRAIN_SECONDS - 1 if now >= self.deadline
                                 else now + min(30, DRAIN_SECONDS - 1))
                    drain_end = max(drain_end, now)
                    self.disarm()
                    self.discard_pending()
                    heartbeat_due = 0

                if self.phase == "capturing":
                    reconcile = self.drain(now)
                    if now >= scan_due or reconcile:
                        self.reconcile(source, now, record)
                        scan_due = now if self.drain(now) else now + SCAN_INTERVAL
                    if job is None and now >= probe_due:
                        history_requested = now >= history_due
                        row = self.status(now) if history_requested else None
                        was_paused = self.paused not in (None, "checking")
                        job = worker.submit(self.archive.probe, was_paused,
                                            self.abandoned_staging is None, row)
                        probe_due = now + HEARTBEAT_INTERVAL
                    elif job is None and self.paused is None:
                        if record := self.next_record(now):
                            job = worker.submit(self.capture, record, os.dup(record.fd))
                elif job is None or now >= drain_end:
                    if self.phase == "draining":
                        self.phase = "finished"
                    break

                if now >= heartbeat_due:
                    replace_json(status_fd, STATUS_NAME, self.status(now))
                    heartbeat_due = now + HEARTBEAT_INTERVAL
                watch_fd = [self.watch.fd] if self.watch is not None else []
                select.select(watch_fd, [], [], 0.05 if job is not None else 0.5)
        finally:
            # A stuck I/O must not keep this process or its inotify loop alive.
            if job is None:
                worker.close()
            self.discard_pending()
            self.close()
            replace_json(status_fd, STATUS_NAME, self.status(time.monotonic()))


ALERTS = {
    "inactive": "pilot service exited without a clean deadline",
    "heartbeat": "heartbeat is missing or stale",
    "mount": "expected disk12 mount is unavailable",
    "paused": "archive writes are paused",
    "missed": "a completed log could not be captured",
    "gap": "source event coverage is incomplete",
    "integrity": "archive integrity check failed",
    "io": "archive I/O failed",
    "backlog": "pending capture is at least 30 seconds old",
}


def journal_alert(message):
    print("<4>Will CLIProxy log mover: " + message, file=sys.stderr)


def watchdog(mount, pilot_unit, state_fd, alert=journal_alert):
    alerts = set()
    try:
        with mount.open():
            pass
    except Paused:
        alerts.add("mount")
    finished_now = False
    try:
        with directory(STATUS_PATH) as fd:
            status = read_json(fd, STATUS_NAME)
        now = time.time()
        age = now - status["heartbeat_unix"]
        if not 0 <= age <= STALE_AFTER:
            alerts.add("heartbeat")
        else:
            finished_now = (status.get("phase") == "finished"
                            and status["capture_deadline_unix"] <= now)
    except (OSError, IntegrityError, KeyError, TypeError):
        status = {}
        alerts.add("heartbeat")

    live = subprocess.run(["/bin/systemctl", "is-active", "--quiet", pilot_unit],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if live.returncode and not finished_now:
        alerts.add("inactive")
    if status.get("paused_reason") not in (None, "checking"):
        alerts.add("paused")
    if status.get("watch_gaps") or status.get("queue_overflows"):
        alerts.add("gap")
    if status.get("missed", {}).get("files"):
        alerts.add("missed")
    if status.get("integrity_failures", {}).get("files"):
        alerts.add("integrity")
    if status.get("archive_failures", {}).get("files"):
        alerts.add("io")
    if status.get("oldest_pending_seconds", 0) >= 30:
        alerts.add("backlog")

    started_at = status.get("started_at_unix")
    try:
        previous = read_json(state_fd, "alerts.json")
    except (OSError, IntegrityError):
        previous = {}
    # Carry outage alerts through a missing heartbeat and reset once a new pilot is seen.
    if started_at is None:
        started_at = previous.get("started_at_unix")
    old_alerts = (set(previous.get("alerts", []))
                  if previous.get("started_at_unix") == started_at else set())
    for changed in sorted(alerts - old_alerts):
        alert(ALERTS[changed])
    for changed in sorted(old_alerts - alerts):
        alert("cleared: " + ALERTS[changed])
    if alerts != old_alerts or previous.get("started_at_unix") != started_at:
        replace_json(state_fd, "alerts.json",
                     {"started_at_unix": started_at, "alerts": sorted(alerts)})


def stop_post(archive_root):
    if os.environ.get("SERVICE_RESULT") != "success":
        return 0
    try:
        with directory(STATUS_PATH) as fd:
            status = read_json(fd, STATUS_NAME)
    except (OSError, IntegrityError):
        return 0
    if status.get("phase") != "finished":
        return 0
    timer = f"cliproxy-will-log-mover-watchdog@{archive_root}.timer"
    return subprocess.run(["/bin/systemctl", "--no-block", "stop", timer],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def main():
    os.umask(0o077)
    archive_root = os.environ.get("WILL_LOG_MOVER_ARCHIVE_ROOT", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", archive_root):
        print("<3>Will CLIProxy log mover: invalid archive root", file=sys.stderr)
        return 2
    if sys.argv[1:] == ["--stop-post"]:
        return stop_post(archive_root)
    mount = Disk12(os.environ.get("WILL_LOG_MOVER_DISK_UUID", ""))
    if sys.argv[1:] == ["--watchdog"]:
        with directory(WATCHDOG_PATH) as fd:
            watchdog(mount, f"cliproxy-will-log-mover@{archive_root}.service", fd)
        return 0
    if sys.argv[1:] or zstd is None:
        print("<3>Will CLIProxy log mover: requires in-process compression.zstd", file=sys.stderr)
        return 2

    linux_call("prctl", 4, 0, 0, 0, 0)  # PR_SET_DUMPABLE
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with directory(STATUS_PATH) as fd:
        lock = os.open("mover.lock", os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            archive = Archive(mount, ZstdCodec(), (archive_root, "will", "requests"))
            Pilot(archive).run(INSTANCE_PATH / "logs", fd, lambda: stopping)
        finally:
            os.close(lock)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Exception text can contain a private request basename.
        print("<3>Will CLIProxy log mover: stopped after an unexpected error", file=sys.stderr)
        sys.exit(1)
