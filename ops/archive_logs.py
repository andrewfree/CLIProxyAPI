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
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISREG
from typing import Callable

if __package__:
    from .instance_root import instance_root, MissingInstanceRoot
else:
    from instance_root import instance_root, MissingInstanceRoot


DEFAULT_ARCHIVE_ROOT = Path(
    "/Users/rever/Volumes/TheCloud/backups/llm-logs/cliproxy/m5/live"
)
DEFAULT_MOUNT_POINT = Path("/Users/rever/Volumes/TheCloud")


@dataclass(frozen=True)
class ArchiveConfig:
    source_dir: Path
    archive_root: Path
    mount_point: Path
    min_age_seconds: int = 3_600
    prune: bool = True
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
        self._manifest_lines: set[str] | None = None

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, int, int, int]:
        stat = path.lstat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

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

    def _candidates(self, now: float) -> list[Path]:
        candidates: list[Path] = []
        try:
            entries = sorted(self.config.source_dir.iterdir(), key=lambda path: path.name)
        except OSError as error:
            self.logger.error("Cannot scan log source: %s", error)
            return candidates
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
        return candidates

    def _destination_for(self, source: Path) -> Path:
        stat = source.lstat()
        day = datetime.fromtimestamp(stat.st_mtime, timezone.utc).strftime("%Y/%m/%d")
        return self.config.archive_root / "requests" / day / source.name

    def _read_index(self, path: Path, archive_root: str) -> dict[str, str] | None:
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(index, dict) or not isinstance(index.get("archive_root"), str):
                raise ValueError("invalid index header")
            files = index.get("files")
            if not isinstance(files, dict) or any(
                not isinstance(relative, str) or not relative.startswith("requests/")
                or ".." in relative.split("/") or "\0" in relative
                for relative in files.values()
            ):
                raise ValueError("invalid index entries")
            return files if index["archive_root"] == archive_root else None
        except (OSError, ValueError, RecursionError) as error:
            self.logger.warning("Rebuilding archive index %s: %s", path, error)
            return None

    @staticmethod
    def _write_index(path: Path, archive_root: str, files: dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staged = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=f".{path.name}.",
                dir=path.parent, delete=False,
            ) as handle:
                staged = Path(handle.name)
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"archive_root": archive_root, "files": files}, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            staged.replace(path)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

    def _archive_copy_matches_size(self, relative: str, size: int) -> bool:
        try:
            stat = (self.config.archive_root / relative).lstat()
        except OSError:
            return False
        return S_ISREG(stat.st_mode) and stat.st_size == size

    def _ensure_manifest(self, digest: str, destination: Path) -> None:
        relative = destination.relative_to(self.config.archive_root).as_posix()
        line = f"{digest}  {relative}\n"
        manifest = self.config.archive_root / "SHA256SUMS"
        try:
            if self._manifest_lines is None:
                try:
                    existing = manifest.read_text(encoding="utf-8")
                except FileNotFoundError:
                    existing = ""
                self._manifest_lines = set(existing.splitlines(keepends=True))
            if line in self._manifest_lines:
                return
            descriptor = os.open(
                manifest,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self._manifest_lines.add(line)
        except OSError as error:
            raise OSError(f"cannot update archive manifest: {error}") from error

    def _verified_destination(
        self, source: Path, source_digest: str, fingerprint: tuple[int, int, int, int]
    ) -> tuple[Path, bool]:
        destination = self._destination_for(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if self._sha256(destination) == source_digest:
                return destination, False
            destination = destination.with_name(
                f"{destination.name}.sha256-{source_digest}"
            )
            if destination.exists():
                if self._sha256(destination) == source_digest:
                    return destination, False
                raise OSError("hash-addressed archive collision")

        temporary = destination.with_name(
            f".{destination.name}.partial-{os.getpid()}-{uuid.uuid4().hex}"
        )
        try:
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o600)
            if self._sha256(temporary) != source_digest:
                raise OSError("destination checksum mismatch")
            if self._fingerprint(source) != fingerprint:
                raise OSError("source changed during archive copy")
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination, True

    def _archive_one(
        self, source: Path, fingerprint: tuple[int, int, int, int], result: ArchiveResult
    ) -> Path:
        source_digest = self._sha256(source)
        if self._fingerprint(source) != fingerprint:
            raise OSError("source changed while hashing")
        destination, created = self._verified_destination(
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
        return destination

    def run(self, *, check_only: bool = False) -> ArchiveResult:
        result = ArchiveResult()
        if not self._destination_ready():
            result.failures += 1
            return result
        candidates = self._candidates(self.clock())
        if check_only:
            result.skipped = len(candidates)
            self.logger.info("Archive check found %d eligible closed log(s)", len(candidates))
            return result
        self._manifest_lines = None
        # Pruning always requires a fresh content check before deleting a source.
        index_path = None if self.config.prune else self.config.index_path
        archive_root = str(self.config.archive_root.resolve()) if index_path is not None else ""
        previous = self._read_index(index_path, archive_root) if index_path is not None else None
        cached = previous or {}
        verified: dict[str, str] = {}
        for source in candidates:
            try:
                fingerprint = self._fingerprint(source)
                if index_path is not None:
                    # Hard-linked source names each need their own archive copy.
                    key = ":".join((*map(str, fingerprint), source.name))
                    relative = cached.get(key)
                    if relative is not None and self._archive_copy_matches_size(relative, fingerprint[2]):
                        verified[key] = relative
                        result.skipped_by_index += 1
                        continue
                destination = self._archive_one(source, fingerprint, result)
                if index_path is not None:
                    verified[key] = destination.relative_to(self.config.archive_root).as_posix()
            except OSError as error:
                result.failures += 1
                self.logger.error("Kept local %s: %s", source.name, error)
        if index_path is not None and verified != previous:
            try:
                self._write_index(index_path, archive_root, verified)
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
    parser.add_argument(
        "--archive-only", action="store_true", help="Verify archive copies but keep local logs"
    )
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
            prune=not args.archive_only,
            index_path=runtime / "archive-index.json",
        )
        result = LogArchiver(config, logger=logger).run(check_only=args.check_only)
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main())
