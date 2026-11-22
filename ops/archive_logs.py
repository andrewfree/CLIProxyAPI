#!/usr/bin/env python3
"""Archive closed CLIProxy logs and optionally prune verified local copies."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISREG
from typing import BinaryIO, Callable, NamedTuple

if __package__:
    from .instance_root import instance_root, MissingInstanceRoot
else:
    from instance_root import instance_root, MissingInstanceRoot


DEFAULT_ARCHIVE_ROOT = Path(
    "/Users/rever/Volumes/TheCloud/backups/llm-logs/cliproxy/m5/live"
)
DEFAULT_MOUNT_POINT = Path("/Users/rever/Volumes/TheCloud")

FileFingerprint = tuple[int, int, int, int, int]
CachedFileFingerprint = tuple[int, int, int, int]
ManifestFingerprint = tuple[int, int]


class VerifiedCopy(NamedTuple):
    source: CachedFileFingerprint
    relative: str
    destination: CachedFileFingerprint


@dataclass(frozen=True)
class ArchiveConfig:
    source_dir: Path
    archive_root: Path
    mount_point: Path
    min_age_seconds: int = 3_600
    prune: bool = False
    require_mounted_destination: bool = True
    index_path: Path | None = None


@dataclass
class ArchiveResult:
    archived: int = 0
    already_archived: int = 0
    pruned: int = 0
    skipped: int = 0
    failures: int = 0
    skipped_by_index: int = 0


class LogArchiver:
    def __init__(
        self,
        config: ArchiveConfig,
        *,
        logger: logging.Logger,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.logger = logger
        self.clock = clock
        self._manifest_lines: set[bytes] | None = None
        self._manifest_state: ManifestFingerprint | None = None

    @staticmethod
    def _fingerprint(path: Path) -> FileFingerprint:
        stat = path.lstat()
        if not S_ISREG(stat.st_mode):
            raise OSError(f"not a regular file: {path}")
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    @staticmethod
    def _sha256(path: Path, *, limit: int | None = None) -> str:
        digest = hashlib.sha256()
        remaining = limit
        with path.open("rb") as handle:
            while remaining is None or remaining > 0:
                size = 4 * 1024 * 1024
                if remaining is not None:
                    size = min(size, remaining)
                chunk = handle.read(size)
                if not chunk:
                    break
                digest.update(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
        if remaining is not None and remaining != 0:
            raise OSError(f"short read while hashing {path.name}")
        return digest.hexdigest()

    def _destination_ready(self) -> bool:
        try:
            mount = self.config.mount_point.resolve(strict=True)
            archive = self.config.archive_root.resolve(strict=False)
            archive.relative_to(mount)
        except (FileNotFoundError, OSError, ValueError) as error:
            self.logger.error("Archive destination is unsafe or unavailable: %s", error)
            return False
        if self.config.require_mounted_destination and not mount.is_mount():
            self.logger.error("Archive mount is not mounted: %s", mount)
            return False
        try:
            archive.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self.logger.error("Cannot create archive root: %s", error)
            return False
        return True

    def _candidates(self, now: float) -> tuple[set[str], list[Path]]:
        entries = sorted(self.config.source_dir.iterdir(), key=lambda path: path.name)
        candidates: list[Path] = []
        for path in entries:
            if path.name == "main.log" or "\n" in path.name:
                continue
            if not (path.name.endswith(".log") or path.name.endswith(".log.gz")):
                continue
            try:
                stat = path.lstat()
            except OSError as error:
                self.logger.warning("Cannot stat %s: %s", path.name, error)
                continue
            if not S_ISREG(stat.st_mode):
                continue
            if now - stat.st_mtime < self.config.min_age_seconds:
                continue
            candidates.append(path)
        return {path.name for path in entries}, candidates

    def _destination_for(self, source: Path) -> Path:
        stat = source.lstat()
        day = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y/%m/%d")
        return self.config.archive_root / "requests" / day / source.name

    @staticmethod
    def _valid_fingerprint(value: object, length: int) -> bool:
        return isinstance(value, list) and len(value) == length and all(type(part) is int for part in value)

    def _read_index(self, path: Path, archive: str) -> dict[str, VerifiedCopy] | None:
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(index, dict) or not self._valid_fingerprint(index.get("manifest"), 2):
                raise ValueError("invalid index header")
            if index.get("archive") != archive or not self._manifest_covers(
                self._manifest_state, tuple(index["manifest"])
            ):
                self.logger.warning("Rebuilding archive index %s: archive or manifest changed", path)
                return None
            files = index.get("files")
            if not isinstance(files, dict):
                raise ValueError("invalid index entries")
            verified = {}
            for name, record in files.items():
                if not isinstance(record, list) or len(record) != 3:
                    raise ValueError("invalid index entry")
                source, relative, destination = record
                if (not self._valid_fingerprint(source, 4) or not self._valid_fingerprint(destination, 4)
                        or not isinstance(relative, str) or not relative.startswith("requests/")
                        or ".." in relative.split("/") or "\0" in relative):
                    raise ValueError("invalid index entry")
                verified[name] = VerifiedCopy(tuple(source), relative, tuple(destination))
            return verified
        except (OSError, ValueError, RecursionError) as error:
            self.logger.warning("Rebuilding archive index %s: %s", path, error)
            return None

    @staticmethod
    def _write_index(
        path: Path, archive: str, manifest: ManifestFingerprint,
        files: dict[str, VerifiedCopy],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staged = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=f".{path.name}.",
                dir=path.parent, delete=False,
            ) as handle:
                staged = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"archive": archive, "manifest": manifest, "files": files}, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            staged.replace(path)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

    def _archive_copy_matches(self, copy: VerifiedCopy) -> bool:
        try:
            return self._fingerprint(self.config.archive_root / copy.relative)[1:] == copy.destination
        except OSError:
            return False

    @property
    def _manifest_path(self) -> Path:
        return self.config.archive_root / "SHA256SUMS"

    @staticmethod
    def _manifest_fingerprint(stat: os.stat_result) -> ManifestFingerprint:
        if not S_ISREG(stat.st_mode):
            raise OSError("archive manifest is not a regular file")
        if stat.st_nlink != 1:
            raise OSError("archive manifest must have exactly one link")
        return stat.st_ino, stat.st_size

    def _stat_manifest(self) -> ManifestFingerprint | None:
        try:
            return self._manifest_fingerprint(self._manifest_path.lstat())
        except FileNotFoundError:
            return None

    @staticmethod
    def _manifest_covers(current: ManifestFingerprint | None, recorded: ManifestFingerprint) -> bool:
        return current is not None and current[0] == recorded[0] and current[1] >= recorded[1]

    @staticmethod
    def _manifest_tail_complete(handle: BinaryIO, size: int) -> bool:
        if not size:
            return True
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"

    def _inspect_manifest(self) -> tuple[ManifestFingerprint | None, bool]:
        try:
            descriptor = os.open(self._manifest_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None, True
        with os.fdopen(descriptor, "rb", buffering=0) as handle:
            state = self._manifest_fingerprint(os.fstat(handle.fileno()))
            return state, self._manifest_tail_complete(handle, state[1])

    def _open_manifest_for_append(self) -> BinaryIO:
        descriptor = os.open(
            self._manifest_path,
            os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        return os.fdopen(descriptor, "r+b")

    @staticmethod
    def _load_manifest(handle: BinaryIO) -> tuple[set[bytes], bytes]:
        handle.seek(0)
        records = handle.read().split(b"\n")
        return set(records), records[-1]

    def _append_manifest(self, handle: BinaryIO, data: bytes) -> ManifestFingerprint:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        return self._manifest_fingerprint(os.fstat(handle.fileno()))

    def _manifest_tail_matches_archive(self, tail: bytes) -> bool:
        record = re.fullmatch(rb"([0-9a-fA-F]{64})  (requests/[^\x00\r\n]+)", tail)
        if record is None or any(part in (b"", b".", b"..") for part in record[2].split(b"/")):
            return False
        try:
            archive = self.config.archive_root.resolve(strict=True)
            destination = archive / os.fsdecode(record[2])
            if destination.resolve(strict=True) != destination:
                return False
            fingerprint = self._fingerprint(destination)
            digest = self._sha256(destination)
            return digest == record[1].decode("ascii").lower() and self._fingerprint(destination) == fingerprint
        except (OSError, RuntimeError):
            return False

    def _prepare_manifest(self, handle: BinaryIO) -> set[bytes]:
        state = self._manifest_fingerprint(os.fstat(handle.fileno()))
        if self._manifest_state is not None and not self._manifest_covers(state, self._manifest_state):
            raise OSError("archive manifest changed during run")
        if self._manifest_lines is not None and state == self._manifest_state:
            return self._manifest_lines

        lines, tail = self._load_manifest(handle)
        if tail:
            if not self._manifest_tail_matches_archive(tail):
                raise OSError("SHA256SUMS has a malformed unterminated final record; repair it before archiving")
            state = self._append_manifest(handle, b"\n")
        self._manifest_state = state
        self._manifest_lines = lines
        return lines

    def _ensure_manifest(self, digest: str, destination: Path) -> None:
        relative = destination.relative_to(self.config.archive_root).as_posix()
        line = f"{digest}  {relative}".encode("utf-8")
        try:
            with self._open_manifest_for_append() as handle:
                lines = self._prepare_manifest(handle)
                if line not in lines:
                    self._manifest_state = self._append_manifest(handle, line + b"\n")
                    lines.add(line)
        except OSError as error:
            raise OSError(f"cannot update archive manifest: {error}") from error

    def _verified_destination(
        self, source: Path, source_digest: str, fingerprint: FileFingerprint
    ) -> tuple[Path, FileFingerprint, bool]:
        primary = self._destination_for(source)
        primary.parent.mkdir(parents=True, exist_ok=True)
        collision = primary.with_name(f"{primary.name}.sha256-{source_digest}")
        temporary = primary.with_name(
            f".{primary.name}.partial-{os.getpid()}-{uuid.uuid4().hex}"
        )
        staged = None
        try:
            for destination in (primary, collision):
                try:
                    found = self._fingerprint(destination)
                except FileNotFoundError:
                    if staged is None:
                        shutil.copyfile(source, temporary)
                        os.chmod(temporary, 0o600)
                        if self._sha256(temporary) != source_digest:
                            raise OSError("destination checksum mismatch")
                        if self._fingerprint(source) != fingerprint:
                            raise OSError("source changed during archive copy")
                        with temporary.open("rb") as handle:
                            os.fsync(handle.fileno())
                        staged = self._fingerprint(temporary)
                    try:
                        os.link(temporary, destination)
                    except FileExistsError:
                        found = self._fingerprint(destination)
                    else:
                        temporary.unlink()
                        published = self._fingerprint(destination)
                        # Linking and unlinking the staging name update ctime.
                        if published[:4] != staged[:4]:
                            raise OSError("destination changed during archive publish")
                        return destination, published, True
                if self._sha256(destination) == source_digest:
                    if self._fingerprint(destination) != found:
                        raise OSError("destination changed while hashing")
                    return destination, found, False
            raise OSError("hash-addressed archive collision")
        finally:
            temporary.unlink(missing_ok=True)

    def _archive_one(
        self, source: Path, fingerprint: FileFingerprint, result: ArchiveResult
    ) -> VerifiedCopy:
        source_digest = self._sha256(source)
        if self._fingerprint(source) != fingerprint:
            raise OSError("source changed while hashing")
        destination, archived_fingerprint, created = self._verified_destination(
            source, source_digest, fingerprint
        )
        self._ensure_manifest(source_digest, destination)
        if created:
            result.archived += 1
            self.logger.info("Archived %s", source.name)
        else:
            result.already_archived += 1
        if self.config.prune:
            if self._fingerprint(source) != fingerprint:
                raise OSError("source changed before local prune")
            source.unlink()
            result.pruned += 1
            self.logger.info("Pruned verified local copy %s", source.name)
        relative = destination.relative_to(self.config.archive_root).as_posix()
        # Device numbers can change on reattachment, so keep them in memory only.
        return VerifiedCopy(fingerprint[1:], relative, archived_fingerprint[1:])

    def run(self, *, check_only: bool = False) -> ArchiveResult:
        result = ArchiveResult()
        if not self._destination_ready():
            result.failures += 1
            return result
        try:
            source_names, candidates = self._candidates(self.clock())
        except OSError as error:
            result.failures += 1
            self.logger.error("Cannot scan log source: %s", error)
            return result
        if check_only:
            result.skipped = len(candidates)
            self.logger.info("Archive check found %d eligible closed log(s)", len(candidates))
            return result
        self._manifest_lines = None
        # Pruning always requires a fresh content check before deleting a source.
        index_path = None if self.config.prune else self.config.index_path
        previous = None
        try:
            self._manifest_state, manifest_terminated = self._inspect_manifest()
            if not manifest_terminated:
                with self._open_manifest_for_append() as handle:
                    self._prepare_manifest(handle)
                result.failures += 1
                self.logger.error("SHA256SUMS had an unterminated final record; added the missing newline")
            if index_path is not None:
                archive = str(self.config.archive_root.resolve())
                if manifest_terminated:
                    previous = self._read_index(index_path, archive)
        except OSError as error:
            result.failures += 1
            self.logger.error("Cannot check archive state: %s", error)
            return result
        verified = {name: copy for name, copy in (previous or {}).items() if name in source_names}
        for source in candidates:
            cached = verified.pop(source.name, None)
            try:
                fingerprint = self._fingerprint(source)
                if cached is not None and cached.source == fingerprint[1:] and self._archive_copy_matches(cached):
                    verified[source.name] = cached
                    result.skipped_by_index += 1
                    continue
                copy = self._archive_one(source, fingerprint, result)
                if index_path is not None:
                    verified[source.name] = copy
            except OSError as error:
                result.failures += 1
                self.logger.error("Kept local %s: %s", source.name, error)
        if index_path is not None and self._manifest_state is not None and verified != previous:
            try:
                if not self._manifest_covers(self._stat_manifest(), self._manifest_state):
                    raise OSError("archive manifest changed during run")
                self._write_index(index_path, archive, self._manifest_state, verified)
            except OSError as error:
                result.failures += 1
                self.logger.error("Cannot save archive index %s: %s", index_path, error)
        self.logger.info(
            "Archive run archived=%d already_archived=%d skipped_by_index=%d pruned=%d failures=%d",
            result.archived,
            result.already_archived,
            result.skipped_by_index,
            result.pruned,
            result.failures,
        )
        return result


def build_logger(path: Path, *, verbose: bool = False) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cliproxy-log-archive")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    if verbose:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(console)
    logger.propagate = False
    return logger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=os.environ.get("CLIPROXY_LOG_SOURCE"),
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(
            os.environ.get("CLIPROXY_LOG_ARCHIVE_ROOT", DEFAULT_ARCHIVE_ROOT)
        ),
    )
    parser.add_argument(
        "--mount-point",
        type=Path,
        default=Path(os.environ.get("CLIPROXY_LOG_ARCHIVE_MOUNT", DEFAULT_MOUNT_POINT)),
    )
    parser.add_argument(
        "--min-age-seconds",
        type=int,
        default=int(os.environ.get("CLIPROXY_LOG_MIN_AGE_SECONDS", "3600")),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--archive-only", action="store_true", help="Keep local logs (the default)")
    mode.add_argument("--prune", action="store_true", help="Delete local logs after archive verification")
    parser.add_argument(
        "--check-only", action="store_true", help="Check mount and report eligible logs"
    )
    parser.add_argument("--verbose", action="store_true", help="Also write status to stderr")
    args = parser.parse_args()
    try:
        project_root = instance_root(__file__)
    except MissingInstanceRoot as error:
        parser.error(str(error))
    if args.min_age_seconds < 60:
        parser.error("--min-age-seconds must be at least 60")

    runtime = project_root / "ops/runtime"
    logger = build_logger(runtime / "archive.log", verbose=args.verbose)
    lock_path = runtime / "archive.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Another archive run is already active")
            return 0
        config = ArchiveConfig(
            source_dir=args.source if args.source is not None else project_root / "logs",
            archive_root=args.destination,
            mount_point=args.mount_point,
            min_age_seconds=args.min_age_seconds,
            prune=args.prune,
            index_path=runtime / "archive-index.json",
        )
        result = LogArchiver(config, logger=logger).run(check_only=args.check_only)
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main())
