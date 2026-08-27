#!/usr/bin/env python3
"""Report CLIProxy OAuth health without reading token values into output."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REFRESH_RESULT_RE = re.compile(
    r"refreshed\s+(?P<provider>[^,]+),\s+(?P<filename>[^,]+),\s+(?P<result>.*)$",
    re.IGNORECASE,
)
AUTH_FILE_CHANGED_RE = re.compile(
    r"auth file changed \([^)]*\):\s+(?P<filename>[^,]+),\s+processing incrementally",
    re.IGNORECASE,
)
AUTH_FAILURE_RE = re.compile(
    r"invalid_grant|refresh token expired|\b(?:status\s*)?(?:401|403)\b|unauthori[sz]ed|forbidden",
    re.IGNORECASE,
)
NETWORK_FAILURE_RE = re.compile(
    r"lookup .*127\.0\.0\.11|connection refused|no such host|timed? out|tls|dns",
    re.IGNORECASE,
)
SHORT_LIVED_PROVIDERS = frozenset({"claude", "kimi"})


@dataclass(frozen=True)
class AuthStatus:
    filename: str
    provider: str
    email: str
    level: str
    disabled: bool
    expires_at: datetime | None
    last_refresh: datetime | None
    refresh_state: str
    refresh_failures: int
    reasons: tuple[str, ...]
    mode: str

    def safe_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        result["last_refresh"] = (
            self.last_refresh.isoformat() if self.last_refresh else None
        )
        result["reasons"] = list(self.reasons)
        return result


@dataclass
class RefreshStatus:
    state: str = "unknown"
    failures: int = 0


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_log_tail(path: Path, max_bytes: int = 8 * 1024 * 1024) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            offset = max(0, size - max_bytes)
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return ""
    if offset > 0:
        _, separator, data = data.partition(b"\n")
        if not separator:
            return ""
    return data.decode("utf-8", errors="replace")


def scan_refresh_results(log_path: Path) -> dict[str, RefreshStatus]:
    results: dict[str, RefreshStatus] = {}
    for line in read_log_tail(log_path).splitlines():
        changed = AUTH_FILE_CHANGED_RE.search(line)
        if changed:
            filename = Path(changed.group("filename").strip()).name
            results[filename] = RefreshStatus(
                state="credential_reloaded", failures=0
            )
            continue
        match = REFRESH_RESULT_RE.search(line)
        if not match:
            continue
        filename = Path(match.group("filename").strip()).name
        result = match.group("result").strip()
        status = results.setdefault(filename, RefreshStatus())
        if result == "<nil>":
            status.state = "ok"
            status.failures = 0
            continue
        status.failures = status.failures + 1 if status.state != "ok" else 1
        if AUTH_FAILURE_RE.search(result):
            status.state = "invalid_grant" if "invalid_grant" in result.lower() else "unauthorized"
        elif NETWORK_FAILURE_RE.search(result):
            status.state = "network_error"
        else:
            status.state = "refresh_error"
    return results


def _raise_level(current: str, proposed: str) -> str:
    ranks = {"ok": 0, "warning": 1, "critical": 2}
    return proposed if ranks[proposed] > ranks[current] else current


def _invalid_json_status(path: Path) -> AuthStatus:
    try:
        mode = stat.filemode(path.stat().st_mode)
    except OSError:
        mode = "unknown"
    return AuthStatus(
        filename=path.name,
        provider="unknown",
        email="-",
        level="critical",
        disabled=False,
        expires_at=None,
        last_refresh=None,
        refresh_state="unknown",
        refresh_failures=0,
        reasons=("invalid_json",),
        mode=mode,
    )


def scan_auth_directory(
    auth_dir: Path,
    log_path: Path,
    *,
    now: datetime | None = None,
    warn_days: int = 3,
) -> list[AuthStatus]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    warning_window = timedelta(days=max(0, warn_days))
    refresh_results = scan_refresh_results(log_path)
    statuses: list[AuthStatus] = []

    for path in sorted(auth_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            statuses.append(_invalid_json_status(path))
            continue
        if not isinstance(payload, dict):
            statuses.append(_invalid_json_status(path))
            continue

        provider = str(payload.get("type") or "unknown").strip().lower()
        email_value = payload.get("email")
        email = email_value.strip() if isinstance(email_value, str) else "-"
        disabled = payload.get("disabled") is True
        expires_at = parse_timestamp(payload.get("expired"))
        last_refresh = parse_timestamp(payload.get("last_refresh"))
        refresh = refresh_results.get(path.name, RefreshStatus())
        reasons: list[str] = []
        level = "ok"

        try:
            file_mode = path.stat().st_mode
            mode = stat.filemode(file_mode)
            if file_mode & 0o077:
                reasons.append("permissions_too_open")
                level = _raise_level(level, "warning")
        except OSError:
            mode = "unknown"
            reasons.append("stat_failed")
            level = _raise_level(level, "warning")

        if disabled:
            reasons.append("disabled")
            level = "critical"

        if refresh.state in {"invalid_grant", "unauthorized"}:
            reasons.append(refresh.state)
            level = "critical"
        elif refresh.state in {"network_error", "refresh_error"}:
            reasons.append(refresh.state)
            level = _raise_level(level, "warning")

        if expires_at is None:
            reasons.append("expiry_missing_or_invalid")
            level = _raise_level(level, "warning")
        else:
            remaining = expires_at - current
            if remaining <= timedelta(0):
                reasons.append("expired")
                level = "critical"
            elif remaining <= warning_window:
                recently_refreshed = bool(
                    last_refresh
                    and current - last_refresh <= timedelta(hours=2)
                    and refresh.state in {"ok", "credential_reloaded"}
                )
                token_lifetime = expires_at - last_refresh if last_refresh else None
                short_lived_and_refreshing = bool(
                    provider in SHORT_LIVED_PROVIDERS
                    and recently_refreshed
                    and token_lifetime
                    and token_lifetime <= timedelta(days=1)
                )
                if short_lived_and_refreshing:
                    if refresh.state == "credential_reloaded":
                        reasons.append("short_lived_recent_grant")
                    else:
                        reasons.append("short_lived_auto_refresh_ok")
                else:
                    reasons.append("expires_soon")
                    level = _raise_level(level, "warning")

        statuses.append(
            AuthStatus(
                filename=path.name,
                provider=provider,
                email=email,
                level=level,
                disabled=disabled,
                expires_at=expires_at,
                last_refresh=last_refresh,
                refresh_state=refresh.state,
                refresh_failures=refresh.failures,
                reasons=tuple(reasons or ["healthy"]),
                mode=mode,
            )
        )
    return statuses


def _relative_expiry(expires_at: datetime | None, now: datetime) -> str:
    if expires_at is None:
        return "unknown"
    seconds = int((expires_at - now).total_seconds())
    prefix = "in" if seconds >= 0 else "ago"
    value = abs(seconds)
    if value >= 86_400:
        amount = f"{value / 86_400:.1f}d"
    elif value >= 3_600:
        amount = f"{value / 3_600:.1f}h"
    else:
        amount = f"{max(1, value // 60)}m"
    return f"{prefix} {amount}" if prefix == "in" else f"{amount} ago"


def format_text(
    statuses: list[AuthStatus], *, now: datetime | None = None
) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    header = "LEVEL\tPROVIDER\tEMAIL\tEXPIRES\tREFRESH\tFAILURES\tFILE\tREASONS"
    rows = [header]
    for status in statuses:
        rows.append(
            "\t".join(
                [
                    status.level.upper(),
                    status.provider,
                    status.email,
                    _relative_expiry(status.expires_at, current),
                    status.refresh_state,
                    str(status.refresh_failures),
                    status.filename,
                    ",".join(status.reasons),
                ]
            )
        )
    return "\n".join(rows)


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-dir", type=Path, default=project_root / "auths")
    parser.add_argument("--log", type=Path, default=project_root / "logs" / "main.log")
    parser.add_argument(
        "--warn-days",
        type=int,
        default=int(os.environ.get("CLIPROXY_AUTH_WARN_DAYS", "3")),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    statuses = scan_auth_directory(
        args.auth_dir, args.log, warn_days=args.warn_days
    )
    if args.json:
        print(json.dumps([status.safe_dict() for status in statuses], indent=2))
    else:
        print(format_text(statuses))
    if any(status.level == "critical" for status in statuses):
        return 2
    if any(status.level == "warning" for status in statuses):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
