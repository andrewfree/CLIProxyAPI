#!/usr/bin/env python3
"""Encrypted, never-deleting daily backup of the Mac CLIProxy log archive.

The archive job (archive_logs.py) copies closed request logs to the archive root
and records each copy in the append-only SHA256SUMS manifest. This job packs the
manifest's files for each closed UTC day into one tar stream, compresses it with
zstd and encrypts it with age to a public recipient. Each part is written as:

    <day>.partNN.tar.zst.age   ciphertext
    <day>.partNN.members       "<sha256>  <path>" for every file in the part
    <day>.partNN.sha256        "<sha256>  <ciphertext name>", written last

A part counts only once all three exist. Files are created exclusively and
published with a hard link, so no existing name is ever replaced, part numbers
are never reused, and committed parts are never deleted. A failed attempt
removes only the uncommitted files it created itself. Files that reach a day
after its first part go into the next part. Every file's SHA-256 is checked
against the manifest while it is packed. One failing day does not stop later
days; the run reports every failure and exits 1.

The backup needs only the public recipient. `--check` re-hashes each committed
ciphertext against its .sha256 without any key. `--verify --identity FILE`
decrypts each committed part with the private key, checks the ciphertext and
every member, and appends a line to <day>.partNN.verified.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable

DEFAULT_ARCHIVE_ROOT = Path("/Volumes/Storage/backups/llm-logs/cliproxy/m5/live")
DEFAULT_BACKUP_ROOT = Path("/Volumes/Innie/backups/llm-logs-encrypted/cliproxy/m5")
DEFAULT_BACKUP_MOUNT = Path("/Volumes/Innie")
AGE = "/opt/homebrew/bin/age"
ZSTD = "/opt/homebrew/bin/zstd"
CHUNK = 1024 * 1024
FREE_SPACE_RESERVE = 2 * 1024**3
LOCK_NAME = ".backup.lock"
MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})  (requests/(\d{4})/(\d{2})/(\d{2})/[^/\0]+)$")
PART_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.part(\d{2,})\.")
RECIPIENT = re.compile(r"^age1[0-9a-z]{58}$")
RUN_ERRORS = (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError)

log = logging.getLogger("backup_log_archive")


class BackupError(RuntimeError):
    """A precondition or integrity check failed."""


@dataclass(frozen=True)
class Config:
    archive_root: Path
    backup_root: Path
    backup_mount: Path
    recipient: str
    age: str = AGE
    zstd: str = ZSTD
    level: int = 6
    reserve: int = FREE_SPACE_RESERVE


def manifest_by_day(archive_root: Path) -> dict[str, list[tuple[str, str]]]:
    """Map each UTC day to its (sha256, relative path) manifest records.

    An unterminated last line is ignored: the archiver may still be writing it.
    """
    data = (archive_root / "SHA256SUMS").read_bytes()
    days: dict[str, list[tuple[str, str]]] = {}
    seen: set[str] = set()
    for raw in data.split(b"\n")[:-1]:
        match = MANIFEST_LINE.match(raw.decode("utf-8"))
        if match is None:
            raise BackupError(f"unexpected manifest line: {raw[:120]!r}")
        digest, relative, year, month, day = match.groups()
        if relative in seen:
            continue  # the archiver never re-records a path with new content
        seen.add(relative)
        days.setdefault(f"{year}-{month}-{day}", []).append((digest, relative))
    return days


def marker_days(backup_root: Path) -> list[str]:
    return sorted({m.group(1) for p in backup_root.glob("*.part*.sha256") if (m := PART_NAME.match(p.name))})


def committed_parts(backup_root: Path, day: str) -> list[Path]:
    """The committed parts of a day, as paths without suffix, in part order.

    A part counts only when its .sha256 marker, ciphertext and .members all
    exist, so files of a part damaged by a crash are packed again.
    """
    parts = []
    for path in backup_root.glob(f"{day}.part*.sha256"):
        match = PART_NAME.match(path.name)
        if not match:
            continue
        base = backup_root / path.name[: -len(".sha256")]
        if Path(str(base) + ".tar.zst.age").is_file() and Path(str(base) + ".members").is_file():
            parts.append((int(match.group(2)), base))
        else:
            log.warning("Ignoring %s: its ciphertext or .members file is missing", path.name)
    return [base for _, base in sorted(parts)]


def next_part_number(backup_root: Path, day: str) -> int:
    """One above every part number in use, committed or not, so no file is reused."""
    numbers = [int(m.group(2)) for p in backup_root.glob(f"{day}.part*")
               if (m := PART_NAME.match(p.name))]
    return max(numbers, default=0) + 1


def read_members(base: Path) -> list[tuple[str, str]]:
    members = []
    seen: set[str] = set()
    for line in Path(str(base) + ".members").read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        if relative in seen:
            raise BackupError(f"{base.name}.members lists {relative} twice")
        seen.add(relative)
        members.append((digest, relative))
    return members


class HashingReader:
    """A file wrapper that hashes what tarfile reads."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.hash = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.hash.update(data)
        return data


def fsync_path(path: Path) -> None:
    """Flush a file or directory to the device (F_FULLFSYNC on macOS)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        full = getattr(fcntl, "F_FULLFSYNC", None)
        if full is not None:
            try:
                fcntl.fcntl(fd, full)
                return
            except OSError:
                pass
        os.fsync(fd)
    finally:
        os.close(fd)


def create_exclusive(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def write_exclusive(path: Path, text: str) -> None:
    with os.fdopen(create_exclusive(path), "w", encoding="utf-8") as stream:
        stream.write(text)


def publish(partial: Path, final: Path) -> None:
    """Make a finished file durable under its final name, refusing an existing name."""
    fsync_path(partial)
    os.link(partial, final)
    os.unlink(partial)
    fsync_path(final.parent)


def file_state(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns


def add_member(tar: tarfile.TarFile, path: Path, relative: str, digest: str) -> None:
    """Add one archive file, refusing it if it changes while it is read."""
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"{relative} is not a regular file")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        info = tarfile.TarInfo(relative)
        info.size, info.mtime, info.mode = before.st_size, int(before.st_mtime), 0o600
        reader = HashingReader(stream)
        tar.addfile(info, reader)
        after = os.fstat(stream.fileno())
    if file_state(after) != file_state(before) or file_state(os.lstat(path)) != file_state(before):
        raise BackupError(f"{relative} changed while it was packed")
    if reader.hash.hexdigest() != digest:
        raise BackupError(f"{relative} does not match its manifest SHA-256")


def close_pipe(stream: BinaryIO | None) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (BrokenPipeError, OSError):
        pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def child_message(stream: BinaryIO) -> str:
    stream.seek(0)
    return stream.read(2000).decode("utf-8", "replace").strip() or "no message"


def check_space(config: Config, members: list[tuple[str, str]]) -> None:
    """Refuse a part that could not fit, counting the uncompressed size plus a reserve."""
    pending = sum(os.lstat(config.archive_root / relative).st_size for _, relative in members)
    free = shutil.disk_usage(config.backup_root).free
    if free < pending + config.reserve:
        raise BackupError(f"not enough free space on {config.backup_mount}: {free} bytes free, "
                          f"{pending} pending plus a {config.reserve}-byte reserve")


def write_part(config: Config, day: str, members: list[tuple[str, str]]) -> Path:
    """Pack, compress and encrypt members as the day's next part, then commit it.

    The ciphertext and .members are durable under their final names before the
    .sha256 marker is published, so a crash never leaves a marker without them.
    On failure, every file this call created is removed; the marker never is.
    """
    base = config.backup_root / f"{day}.part{next_part_number(config.backup_root, day):02d}"
    cipher = Path(str(base) + ".tar.zst.age")
    partial = Path(str(cipher) + ".partial")
    created: list[Path] = []
    try:
        fd = create_exclusive(partial)
        created.append(partial)
        with tempfile.TemporaryFile() as age_err, tempfile.TemporaryFile() as zstd_err:
            try:
                age = subprocess.Popen([config.age, "-r", config.recipient],
                                       stdin=subprocess.PIPE, stdout=fd, stderr=age_err)
            finally:
                os.close(fd)
            zstd = None
            try:
                zstd = subprocess.Popen([config.zstd, "-q", "-T0", f"-{config.level}", "-c"],
                                        stdin=subprocess.PIPE, stdout=age.stdin, stderr=zstd_err)
                close_pipe(age.stdin)
                with tarfile.open(fileobj=zstd.stdin, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                    for digest, relative in members:
                        add_member(tar, config.archive_root / relative, relative, digest)
                close_pipe(zstd.stdin)
            except BaseException:
                for process in (zstd, age):
                    if process is not None:
                        process.kill()
                raise
            finally:
                if zstd is not None:
                    close_pipe(zstd.stdin)
                close_pipe(age.stdin)
                zstd_rc = zstd.wait() if zstd is not None else None
                age_rc = age.wait()
            if zstd_rc != 0 or age_rc != 0:
                raise BackupError(f"zstd exited {zstd_rc} ({child_message(zstd_err)}), "
                                  f"age exited {age_rc} ({child_message(age_err)})")
        publish(partial, cipher)
        created.append(cipher)
        members_partial = Path(str(base) + ".members.partial")
        write_exclusive(members_partial, "".join(f"{d}  {r}\n" for d, r in members))
        created.append(members_partial)
        publish(members_partial, Path(str(base) + ".members"))
        created.append(Path(str(base) + ".members"))
        commit_partial = Path(str(base) + ".sha256.partial")
        write_exclusive(commit_partial, f"{sha256_file(cipher)}  {cipher.name}\n")
        created.append(commit_partial)
        publish(commit_partial, Path(str(base) + ".sha256"))
    except BaseException:
        for path in created:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    return base


def verify_part(config: Config, base: Path, identity: Path) -> dict:
    """Decrypt one committed part and check its ciphertext and every member."""
    cipher = Path(str(base) + ".tar.zst.age")
    recorded = Path(str(base) + ".sha256").read_text(encoding="utf-8").split("  ", 1)[0]
    if sha256_file(cipher) != recorded:
        raise BackupError(f"{cipher.name} does not match its .sha256")
    expected = dict((relative, digest) for digest, relative in read_members(base))
    age = subprocess.Popen([config.age, "-d", "-i", str(identity), str(cipher)],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    zstd = subprocess.Popen([config.zstd, "-q", "-dc"], stdin=age.stdout, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    assert age.stdout is not None and zstd.stdout is not None
    age.stdout.close()
    found: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=zstd.stdout, mode="r|") as tar:
            for member in tar:
                stream = tar.extractfile(member)
                if not member.isfile() or stream is None:
                    raise BackupError(f"{base.name} holds a non-file entry {member.name!r}")
                if member.name in found:
                    raise BackupError(f"{base.name} holds {member.name} twice")
                digest = hashlib.sha256()
                while chunk := stream.read(CHUNK):
                    digest.update(chunk)
                found[member.name] = digest.hexdigest()
    except tarfile.TarError as err:
        raise BackupError(f"{base.name} could not be read after decryption: {err}") from None
    finally:
        close_pipe(zstd.stdout)
        zstd_rc = zstd.wait()
        age_rc = age.wait()
    if zstd_rc != 0 or age_rc != 0:
        raise BackupError(f"decrypting {cipher.name}: zstd exited {zstd_rc}, age exited {age_rc}")
    if found != expected:
        raise BackupError(f"{base.name} members differ from its .members list")
    receipt = {"verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "members": len(found), "ciphertext_sha256": recorded}
    with open(str(base) + ".verified", "a", encoding="utf-8") as stream:
        stream.write(json.dumps(receipt) + "\n")
    return receipt


def check_part(base: Path) -> None:
    """Re-hash one committed ciphertext against its .sha256, without any key."""
    cipher = Path(str(base) + ".tar.zst.age")
    recorded = Path(str(base) + ".sha256").read_text(encoding="utf-8").split("  ", 1)[0]
    if sha256_file(cipher) != recorded:
        raise BackupError(f"{cipher.name} does not match its .sha256")
    read_members(base)


def prepare_destination(config: Config, create: bool) -> None:
    mount = config.backup_mount.resolve(strict=True)
    if not os.path.ismount(mount):
        raise BackupError(f"{mount} is not a mounted volume")
    if create:
        config.backup_root.mkdir(parents=True, exist_ok=True)
    elif not config.backup_root.is_dir():
        raise BackupError(f"{config.backup_root} does not exist")
    if not config.backup_root.resolve(strict=True).is_relative_to(mount):
        raise BackupError(f"{config.backup_root} is not on {mount}")


def back_up(config: Config, today: datetime.date) -> dict:
    """Write a part for every closed day that has unbacked-up manifest files.

    A failing day is logged and skipped; the remaining days still run, and the
    failures are raised together at the end.
    """
    prepare_destination(config, create=True)
    if not RECIPIENT.match(config.recipient):
        raise BackupError("the age recipient is missing or malformed")
    closed = (today - datetime.timedelta(days=1)).isoformat()
    written = members_written = 0
    failures = []
    for day, records in sorted(manifest_by_day(config.archive_root).items()):
        if day >= closed:
            continue  # the archiver may still add files for the last full day
        try:
            done = {relative for base in committed_parts(config.backup_root, day)
                    for _, relative in read_members(base)}
            pending = [(d, r) for d, r in records if r not in done]
            if not pending:
                continue
            check_space(config, pending)
            base = write_part(config, day, pending)
        except (BackupError, *RUN_ERRORS) as err:
            log.error("Backing up %s failed: %s", day, err)
            failures.append(f"{day}: {err}")
            continue
        written += 1
        members_written += len(pending)
        log.info("Wrote %s with %d files", base.name, len(pending))
    if failures:
        log.info("Backup wrote %d parts with %d files before failing", written, members_written)
        raise BackupError(f"{len(failures)} day(s) failed: " + "; ".join(failures))
    return {"parts_written": written, "files_written": members_written}


def each_committed_part(config: Config, days: Iterable[str] | None):
    """Yield (day, base) for committed parts, and the requested days that have none."""
    wanted = set(days) if days else None
    seen: set[str] = set()
    parts = []
    for day in marker_days(config.backup_root):
        if wanted is not None and day not in wanted:
            continue
        for base in committed_parts(config.backup_root, day):
            seen.add(day)
            parts.append((day, base))
    missing = sorted(wanted - seen) if wanted is not None else []
    return parts, missing


def verify_all(config: Config, identity: Path, days: Iterable[str] | None = None) -> dict:
    prepare_destination(config, create=False)
    parts, missing = each_committed_part(config, days)
    verified = 0
    failures = []
    for _, base in parts:
        try:
            receipt = verify_part(config, base, identity)
        except (BackupError, *RUN_ERRORS) as err:
            log.error("Verifying %s failed: %s", base.name, err)
            failures.append(str(err))
            continue
        verified += 1
        log.info("Verified %s: %d files", base.name, receipt["members"])
    if missing:
        failures.append(f"no committed part for {', '.join(missing)}")
    if failures:
        log.info("Verified %d parts before failing", verified)
        raise BackupError("; ".join(failures))
    return {"parts_verified": verified}


def check_all(config: Config, days: Iterable[str] | None = None) -> dict:
    prepare_destination(config, create=False)
    parts, missing = each_committed_part(config, days)
    checked = 0
    failures = []
    for _, base in parts:
        try:
            check_part(base)
        except (BackupError, *RUN_ERRORS) as err:
            log.error("Checking %s failed: %s", base.name, err)
            failures.append(str(err))
            continue
        checked += 1
    if missing:
        failures.append(f"no committed part for {', '.join(missing)}")
    if failures:
        raise BackupError("; ".join(failures))
    return {"parts_checked": checked}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive-root", type=Path,
                        default=Path(os.environ.get("CLIPROXY_LOG_ARCHIVE_ROOT", DEFAULT_ARCHIVE_ROOT)))
    parser.add_argument("--backup-root", type=Path,
                        default=Path(os.environ.get("CLIPROXY_LOG_BACKUP_ROOT", DEFAULT_BACKUP_ROOT)))
    parser.add_argument("--backup-mount", type=Path,
                        default=Path(os.environ.get("CLIPROXY_LOG_BACKUP_MOUNT", DEFAULT_BACKUP_MOUNT)))
    parser.add_argument("--recipient", default=os.environ.get("CLIPROXY_LOG_BACKUP_RECIPIENT", ""))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="decrypt and check committed parts")
    mode.add_argument("--check", action="store_true", help="re-hash committed ciphertexts, no key needed")
    parser.add_argument("--identity", type=Path, help="age private key, required with --verify")
    parser.add_argument("--day", action="append", help="limit --verify or --check to this UTC day (repeatable)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.verify and args.identity is None:
        parser.error("--verify needs --identity")
    if args.day and not (args.verify or args.check):
        parser.error("--day needs --verify or --check")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    config = Config(args.archive_root, args.backup_root, args.backup_mount, args.recipient)
    reading = args.verify or args.check
    runtime = Path(os.environ.get("CLIPROXY_ROOT", Path(__file__).resolve().parent.parent)) / "ops" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(runtime / "log-backup.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        prepare_destination(config, create=not reading)
        # The lock lives with the backup, so runs from any checkout exclude each other.
        with (config.backup_root / LOCK_NAME).open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log.warning("Another backup run holds the lock")
                return 1 if reading else 0
            if args.verify:
                summary = verify_all(config, args.identity, args.day)
            elif args.check:
                summary = check_all(config, args.day)
            else:
                summary = back_up(config, datetime.datetime.now(datetime.timezone.utc).date())
                (runtime / "log-backup-last-success").write_text(
                    datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds") + "\n")
    except (BackupError, *RUN_ERRORS) as err:
        log.error("Backup run failed: %s", err)
        return 1
    except Exception:
        log.exception("Backup run failed unexpectedly")
        return 1
    log.info("Backup run %s", " ".join(f"{k}={v}" for k, v in summary.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
