#!/usr/bin/env python3
"""Archive Will's closed request logs during the copy-only pilot."""

import base64
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time

try:
    from compression import zstd
except ImportError:
    zstd = None

MOUNT_PATH = Path("/srv/share/drives/disk12")
INSTANCE_PATH = Path("/home/andrew/cliproxy")
STATUS_PATH = INSTANCE_PATH / "ops/runtime/will-log-mover"
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
MODIFY = 0x00000002
DELETE_SELF = 0x00000400
MOVE_SELF = 0x00000800
UNMOUNT = 0x00002000
OVERFLOW = 0x00004000
IGNORED = 0x00008000
IS_DIR = 0x40000000
WATCH_MASK = CLOSE_WRITE | MOVED_TO | CREATE | MODIFY | DELETE_SELF | MOVE_SELF
WATCH_LOST = DELETE_SELF | MOVE_SELF | UNMOUNT | IGNORED
EVENT_HEADER = struct.Struct("=iIII")


class IntegrityError(Exception):
    pass


class Paused(Exception):
    pass


@contextmanager
def directory(name, parent=None, create=False, device=None):
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        # Make the directory entry durable even if an earlier attempt failed here.
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
    def __init__(self, uuid):
        self.uuid = uuid

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
            fd = os.open(MOUNT_PATH, DIR_FLAGS)
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

    @staticmethod
    def verify_mount(fd, device):
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
                if (info_fields[2:5] != [major_minor, "/", str(MOUNT_PATH)]
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
        if zstd is not None:
            try:
                with zstd.ZstdFile(target, "wb", level=3) as compressed:
                    return transfer(source, compressed, limit)
            except zstd.ZstdError:
                raise IntegrityError from None
        with subprocess.Popen(["zstd", "-q", "-3", "-c"], stdin=subprocess.PIPE,
                              stdout=target, stderr=subprocess.DEVNULL) as process:
            with process.stdin:
                result = transfer(source, process.stdin, limit)
            if process.wait():
                raise IntegrityError
            return result

    def decompress_digest(self, source, limit):
        if zstd is not None:
            try:
                with zstd.ZstdFile(source, "rb") as restored:
                    return transfer(restored, limit=limit)
            except (EOFError, zstd.ZstdError):
                raise IntegrityError from None
        with subprocess.Popen(["zstd", "-q", "-d", "-c"], stdin=source,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            try:
                result = transfer(process.stdout, limit=limit)
            except BaseException:
                process.kill()
                raise
            if process.wait():
                raise IntegrityError
            return result


def content_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


@dataclass
class Record:
    name: bytes
    info: os.stat_result
    evidence: str
    fd: int | None = None
    observed: float = 0
    retry_at: float = 0
    pending: bool = True
    from_staging: bool = False

    @property
    def key(self):
        return self.name, content_identity(self.info)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def manifest(self):
        return {
            "version": 1,
            "source_basename_b64": base64.b64encode(self.name).decode("ascii"),
            "completion_evidence": self.evidence,
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

    @contextmanager
    def destination(self, minimum, reserve=0):
        with self.mount.open() as (mount_fd, free):
            if free < minimum + reserve:
                raise Paused("low_space")
            with directory_tree(mount_fd, self.components) as archive_fd:
                if os.fstatvfs(archive_fd).f_flag & os.ST_RDONLY:
                    raise Paused("archive_read_only")
                yield archive_fd

    def pause_reason(self, was_paused):
        try:
            with self.destination(self.resume if was_paused else self.floor):
                return None
        except Paused as error:
            return str(error)
        except OSError:
            return "archive_unavailable"

    def capture(self, record):
        size = record.info.st_size
        # Zstd's worst-case expansion is below this reserve, including small files.
        with self.destination(self.floor, size + size // 256 + 4 * CHUNK_SIZE) as root:
            with directory_tree(root, hour_parts(record.info.st_mtime_ns), create=True) as hour:
                return self.capture_in_hour(record, hour)

    def capture_in_hour(self, record, hour):
        before = os.fstat(record.fd)
        if not same_source(record, before):
            raise IntegrityError
        expected = record.manifest()
        stage = ".pending-" + source_key(expected)
        with directory(stage, hour, create=True, device=os.fstat(hour).st_dev) as stage_fd:
            try:
                saved = read_json(stage_fd, "manifest.json")
            except FileNotFoundError:
                replace_json(stage_fd, "manifest.json", expected)
                saved = expected
            try:
                matches = source_key(saved) == source_key(expected) and (
                    saved["source_identity"]["ctime_unix_ns"] == record.info.st_ctime_ns
                    or before.st_nlink == 0
                )
            except (KeyError, ValueError, TypeError):
                raise IntegrityError from None
            if not matches:
                raise IntegrityError

            if "archive_id" in saved:
                self.verify(stage_fd, saved)
            else:
                try:
                    os.unlink("body.zst", dir_fd=stage_fd)
                except FileNotFoundError:
                    pass
                with os.fdopen(os.dup(record.fd), "rb") as source:
                    source.seek(0)
                    with new_file(stage_fd, "body.zst") as target:
                        original = self.codec.compress(source, target, before.st_size)
                        target.flush()
                        os.fsync(target.fileno())
                after = os.fstat(record.fd)
                if original[0] != before.st_size or not same_source(record, after):
                    raise IntegrityError
                with read_file(stage_fd, "body.zst") as compressed:
                    stored = transfer(compressed)
                    compressed.seek(0)
                    if self.codec.decompress_digest(compressed, original[0]) != original:
                        raise IntegrityError

                saved = dict(expected, original_sha256=original[1],
                             compressed_bytes=stored[0], compressed_sha256=stored[1])
                saved["archive_id"] = archive_key(saved)
                replace_json(stage_fd, "manifest.json", saved)
            if not same_source(record, os.fstat(record.fd)):
                raise IntegrityError
            created = self.commit(hour, stage, stage_fd, saved)
            return created, os.fstat(record.fd).st_nlink == 0, saved["compressed_bytes"]

    def verify(self, archive_fd, manifest):
        try:
            original = manifest["original_bytes"], manifest["original_sha256"]
            stored = manifest["compressed_bytes"], manifest["compressed_sha256"]
            if (manifest["version"] != 1 or manifest["codec"] != "zstd"
                    or manifest["compression_level"] != 3
                    or manifest["completion_evidence"] not in ("close_write", "moved_to")
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

    def commit(self, hour, stage, stage_fd, manifest):
        archive_id = manifest["archive_id"]
        os.fsync(stage_fd)
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
            # Only dispose of our duplicate staging copy after verifying publication.
            os.fsync(hour)
            for name in ("body.zst", "manifest.json"):
                os.unlink(name, dir_fd=stage_fd)
            os.rmdir(stage, dir_fd=hour)
            os.fsync(hour)
            return False
        os.fsync(hour)
        return True

    def recover(self):
        # Only inspect private staging directories. Committed records are
        # verified individually if a new observation encounters their ID.
        with self.destination(self.floor) as root:
            yield from self.find_stages(root, [])

    def find_stages(self, parent, parts):
        device = os.fstat(parent).st_dev
        with os.scandir(parent) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if len(parts) < 4:
                    width = 4 if not parts else 2
                    if re.fullmatch(rf"[0-9]{{{width}}}", entry.name):
                        with directory(entry.name, parent, device=device) as child:
                            yield from self.find_stages(child, [*parts, entry.name])
                elif entry.name.startswith(".pending-"):
                    with directory(entry.name, parent, device=device) as staging:
                        try:
                            manifest = read_json(staging, "manifest.json")
                            name = base64.b64decode(manifest["source_basename_b64"], validate=True)
                            if (not request_name(name)
                                    or hour_parts(manifest["mtime_unix_ns"]) != parts
                                    or entry.name != ".pending-" + source_key(manifest)
                                    or type(manifest["original_bytes"]) is not int
                                    or manifest["original_bytes"] < 0
                                    or type(manifest["source_identity"]["ctime_unix_ns"]) is not int
                                    or manifest["completion_evidence"] not in ("close_write", "moved_to")):
                                raise IntegrityError
                            if "archive_id" in manifest:
                                with self.destination(self.floor):
                                    self.verify(staging, manifest)
                                    created = self.commit(parent, entry.name, staging, manifest)
                                yield "published" if created else "existing", name, manifest
                            else:
                                yield "retry", name, manifest
                        except (IntegrityError, OSError, ValueError, TypeError, KeyError, OverflowError):
                            yield "failure", None, None


class Pilot:
    def __init__(self, archive, max_fds=MAX_FDS, max_bytes=MAX_BYTES):
        self.archive = archive
        self.max_fds = max_fds
        self.max_bytes = max_bytes
        self.source_fd = None
        self.watch = None
        self.source_unavailable = False
        self.records = OrderedDict()
        self.held_fds = 0
        self.held_bytes = 0
        self.started_at = time.time()
        self.paused = None
        self.unconfirmed = (0, 0)
        self.watch_gaps = 0
        self.queue_overflows = 0
        self.unknown_miss_bytes = 0
        self.stored_bytes = 0
        self.counts = {
            label: {"files": 0, "bytes": 0}
            for label in ("completed", "archived", "recovered_after_unlink", "missed",
                          "integrity_failures", "archive_failures", "resumed_stages")
        }

    def count(self, label, size=0):
        self.counts[label]["files"] += 1
        self.counts[label]["bytes"] += size

    def release(self, record):
        if record.fd is not None:
            self.held_fds -= 1
            self.held_bytes -= record.info.st_size
            record.close()

    def finish(self, record):
        self.release(record)
        record.pending = False

    def arm(self, source):
        try:
            with directory(source) as visible:
                if (self.watch is not None
                        and os.path.samestat(os.fstat(visible), os.fstat(self.source_fd))):
                    return
                self.disarm(gap=True)
                self.source_fd = os.dup(visible)
                self.watch = Inotify(self.source_fd)
                self.source_unavailable = False
        except OSError:
            if self.watch is None and not self.source_unavailable:
                self.watch_gaps += 1
            self.source_unavailable = True
            self.disarm(gap=True)

    def disarm(self, gap=False):
        if self.watch is not None:
            self.watch.close()
            self.watch = None
            self.watch_gaps += gap
        if self.source_fd is not None:
            os.close(self.source_fd)
            self.source_fd = None

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

    def offer(self, name, evidence, now, recovered_manifest=None):
        try:
            fd, info = self.open_source(name)
        except FileNotFoundError:
            self.miss(recovered_manifest["original_bytes"] if recovered_manifest else None)
            return None
        except OSError:
            self.count("archive_failures")
            return None
        if fd is None:
            return None
        record = Record(name, info, evidence, observed=now,
                        from_staging=recovered_manifest is not None)
        if recovered_manifest and (
            source_key(recovered_manifest) != source_key(record.manifest())
            or recovered_manifest["source_identity"]["ctime_unix_ns"] != info.st_ctime_ns
        ):
            os.close(fd)
            self.miss(recovered_manifest["original_bytes"])
            return None
        if record.key in self.records:
            os.close(fd)
            return None
        if recovered_manifest is None:
            self.count("completed", info.st_size)
        if len(self.records) >= MAX_RECORDS or info.st_size > self.max_bytes:
            self.queue_overflows += 1
            os.close(fd)
            self.miss(info.st_size)
            return None
        self.records[record.key] = record
        self.hold(record, fd)
        return record

    def miss(self, size):
        self.count("missed", size or 0)
        if size is None:
            self.unknown_miss_bytes += 1
            self.count("completed")

    def drain(self, now):
        if self.watch is None:
            return False
        reconcile = False
        newly_opened = {}
        for mask, name in self.watch.events():
            if mask & WATCH_LOST:
                self.disarm(gap=True)
                return True
            if mask & OVERFLOW:
                self.watch_gaps += 1
                reconcile = True
            if mask & IS_DIR or not request_name(name):
                continue
            if mask & (CREATE | MODIFY | MOVED_TO) and name in newly_opened:
                # A queued close can refer to a name since reused by a new writer.
                # Its inode is unknown until open, so discard ambiguous observations.
                old = newly_opened.pop(name)
                self.miss(old.info.st_size)
                self.finish(old)
                del self.records[old.key]
            if mask & (CLOSE_WRITE | MOVED_TO):
                observation = "close_write" if mask & CLOSE_WRITE else "moved_to"
                if record := self.offer(name, observation, now):
                    newly_opened[name] = record
        return reconcile

    def reconcile(self, source, in_flight):
        self.arm(source)
        if self.watch is None:
            return
        seen = set()
        unknown_count = 0
        unknown_bytes = 0
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
                key = name, content_identity(info)
                record = self.records.get(key)
                if record is None:
                    unknown_count += 1
                    unknown_bytes += info.st_size
                else:
                    seen.add(key)
                    if record.pending and record.fd is None and self.held_fds < self.max_fds:
                        try:
                            fd, current = self.open_source(name)
                        except FileNotFoundError:
                            continue
                        if fd is not None:
                            if same_source(record, current):
                                self.hold(record, fd)
                            else:
                                os.close(fd)
        self.unconfirmed = unknown_count, unknown_bytes
        for key, record in list(self.records.items()):
            if key in seen or record is in_flight or record.fd is not None:
                continue
            if record.pending:
                self.miss(record.info.st_size)
            del self.records[key]

    def next_record(self, now):
        return min(
            (record for record in self.records.values()
             if record.pending and record.fd is not None and record.retry_at <= now),
            key=lambda record: (record.info.st_mtime_ns, record.observed),
            default=None,
        )

    def capture_result(self, record, job, now):
        size = record.info.st_size
        try:
            created, recovered, stored_bytes = job.result()
        except Paused as error:
            self.paused = str(error)
        except IntegrityError:
            self.count("integrity_failures", size)
            record.retry_at = now + SCAN_INTERVAL
        except OSError as error:
            self.count("archive_failures", size)
            if error.errno in (errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS):
                self.paused = "archive_io"
            record.retry_at = now + SCAN_INTERVAL
        else:
            if created:
                self.count("archived", size)
                self.stored_bytes += stored_bytes
                if record.from_staging:
                    self.count("resumed_stages", size)
                if recovered:
                    self.count("recovered_after_unlink", size)
            self.finish(record)

    def recovery_result(self, result, now):
        outcome, name, manifest = result
        if outcome == "failure":
            self.count("integrity_failures")
        elif outcome == "retry":
            if self.watch is not None:
                self.offer(name, manifest["completion_evidence"], now, manifest)
        elif outcome == "published":
            self.count("resumed_stages", manifest["original_bytes"])
            self.count("archived", manifest["original_bytes"])
            self.stored_bytes += manifest["compressed_bytes"]
            if self.watch is not None:
                try:
                    info = os.stat(name, dir_fd=self.source_fd, follow_symlinks=False)
                    absent = (info.st_dev, info.st_ino) != (
                        manifest["source_identity"]["device"], manifest["source_identity"]["inode"]
                    )
                except FileNotFoundError:
                    absent = True
                if absent:
                    self.count("recovered_after_unlink", manifest["original_bytes"])

    def status(self, now):
        pending = [record for record in self.records.values() if record.pending]
        return {
            "started_at_unix": self.started_at,
            "heartbeat_unix": time.time(),
            "paused_reason": self.paused,
            "source_watch_active": self.watch is not None,
            "watch_gaps": self.watch_gaps,
            "queue_overflows": self.queue_overflows,
            "missed_unknown_bytes": self.unknown_miss_bytes,
            "archived_compressed_bytes": self.stored_bytes,
            "pending": {"files": len(pending), "bytes": sum(r.info.st_size for r in pending)},
            "unconfirmed": {"files": self.unconfirmed[0], "bytes": self.unconfirmed[1]},
            "oldest_pending_seconds": max((now - r.observed for r in pending), default=0),
            **self.counts,
        }

    def run(self, source, status_fd, stop):
        scan_due = heartbeat_due = 0
        job = record = None
        stages = None
        recovered = False
        with ThreadPoolExecutor(max_workers=1) as worker:
            try:
                while not stop():
                    now = time.monotonic()
                    if job is not None and job.done():
                        if record is not None:
                            self.capture_result(record, job, now)
                        else:
                            try:
                                outcome = job.result()
                            except Paused as error:
                                self.paused = str(error)
                                stages = None
                            except OSError:
                                self.count("archive_failures")
                                self.paused = "archive_unavailable"
                                stages = None
                            else:
                                if outcome is None:
                                    recovered = True
                                else:
                                    self.recovery_result(outcome, now)
                        job = record = None

                    reconcile = self.drain(now)
                    if now >= scan_due or reconcile:
                        self.reconcile(source, record)
                        self.drain(now)
                        scan_due = now + SCAN_INTERVAL
                    if now >= heartbeat_due:
                        self.paused = self.archive.pause_reason(self.paused is not None)
                        replace_json(status_fd, STATUS_NAME, self.status(now))
                        heartbeat_due = now + HEARTBEAT_INTERVAL
                    if self.paused is None and job is None:
                        if not recovered and self.watch is not None:
                            if stages is None:
                                stages = self.archive.recover()
                            job = worker.submit(next, stages, None)
                        elif record := self.next_record(now):
                            job = worker.submit(self.archive.capture, record)
                    watch_fd = [self.watch.fd] if self.watch is not None else []
                    select.select(watch_fd, [], [], 0.05 if job is not None else 0.5)
            finally:
                if job is not None:
                    worker.shutdown(wait=True)
                    if record is not None:
                        self.capture_result(record, job, time.monotonic())
                if stages is not None:
                    stages.close()
                self.close()
                replace_json(status_fd, STATUS_NAME, self.status(time.monotonic()))


def watchdog(mount, pilot_unit):
    live = subprocess.run(["systemctl", "is-active", "--quiet", pilot_unit],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if live.returncode:
        print("<4>Will CLIProxy log mover: pilot service is inactive", file=sys.stderr)
    try:
        with mount.open():
            pass
    except Paused:
        print("<4>Will CLIProxy log mover: expected disk12 mount is unavailable", file=sys.stderr)
    try:
        with directory(STATUS_PATH) as fd:
            status = read_json(fd, STATUS_NAME)
        age = time.time() - status["heartbeat_unix"]
        if not 0 <= age <= STALE_AFTER:
            raise IntegrityError
    except (OSError, IntegrityError, KeyError, TypeError):
        print("<4>Will CLIProxy log mover: heartbeat is missing or stale", file=sys.stderr)
        return
    if status.get("paused_reason"):
        print("<4>Will CLIProxy log mover: archive writes are paused", file=sys.stderr)
    if not status.get("source_watch_active"):
        print("<4>Will CLIProxy log mover: source watch is unavailable", file=sys.stderr)
    if status.get("oldest_pending_seconds", 0) >= 30:
        print("<4>Will CLIProxy log mover: pending capture is at least 30 seconds old", file=sys.stderr)
    if (status.get("watch_gaps") or status.get("queue_overflows")
            or status.get("missed", {}).get("files")):
        print("<4>Will CLIProxy log mover: incomplete event coverage", file=sys.stderr)
    if status.get("integrity_failures", {}).get("files"):
        print("<4>Will CLIProxy log mover: archive integrity check failed", file=sys.stderr)
    if status.get("archive_failures", {}).get("files"):
        print("<4>Will CLIProxy log mover: archive I/O failed", file=sys.stderr)


def main():
    os.umask(0o077)
    mount = Disk12(os.environ.get("WILL_LOG_MOVER_DISK_UUID", ""))
    archive_root = os.environ.get("WILL_LOG_MOVER_ARCHIVE_ROOT", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", archive_root):
        print("<3>Will CLIProxy log mover: invalid archive root", file=sys.stderr)
        return 2
    if sys.argv[1:] == ["--watchdog"]:
        watchdog(mount, f"cliproxy-will-log-mover@{archive_root}.service")
        return 0
    if sys.argv[1:]:
        return 2
    if zstd is None and shutil.which("zstd") is None:
        print("<3>Will CLIProxy log mover: zstd is unavailable", file=sys.stderr)
        return 1

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
