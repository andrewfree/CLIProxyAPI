#!/usr/bin/env python3
"""Inspect, watch, and redeem Anthropic usage resets for CLIProxy Claude accounts.

Every upstream call goes through the local proxy's management `api-call` route,
so the account's OAuth token never leaves CLIProxy. `status` and `watch` are
read-only. `claim` consumes a reset grant and requires `--yes` (or `--dry-run`).

The claim request mirrors Claude Code 2.1.322: POST
/api/organizations/{org}/reset_rate_limits with {program, grant_id, request_id}.
The request_id is persisted per account and grant, so a rerun after an
ambiguous failure repeats the same request instead of claiming another grant.
See ops/CLAUDE_USAGE_RESETS.md.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime
import fcntl
import hmac
import json
import math
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "ops" / "runtime" / "claude-resets"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CLAIM_URL = "https://api.anthropic.com/api/organizations/{org}/reset_rate_limits"
PROGRAM = "cedar_ember"
OAUTH_HEADERS = {
    "Authorization": "Bearer $TOKEN$",
    "anthropic-beta": "oauth-2025-04-20",
    "User-Agent": "claude-cli/2.1.322 (external, cli)",
    "x-app": "cli",
}
# Claude Code validates these shapes before sending a claim.
GRANT_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
USAGE_INTERVAL = 300


class ResetError(RuntimeError):
    """A precondition failed; nothing was claimed."""


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def load_management_key(root: pathlib.Path) -> str:
    for line in (root / ".env").read_text().splitlines():
        if line.startswith("MANAGEMENT_PASSWORD="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise ResetError(f"MANAGEMENT_PASSWORD missing from {root / '.env'}")


class Proxy:
    """Thin client for the CLIProxy management API."""

    def __init__(self, base_url: str, key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.key = key

    def mgmt(self, route: str, payload: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(
            self.base_url + route,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)

    def claude_accounts(self) -> list[dict[str, Any]]:
        files = self.mgmt("/v0/management/auth-files")["files"]
        return [f for f in files if f.get("provider") == "claude"]

    def upstream(self, email: str, method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        account = next((a for a in self.claude_accounts() if a.get("email") == email), None)
        if account is None:
            raise ResetError(f"no Claude credential for {email}")
        payload: dict[str, Any] = {"auth_index": account["auth_index"], "method": method, "url": url,
                                   "header": dict(OAUTH_HEADERS)}
        if body is not None:
            payload["header"]["Content-Type"] = "application/json"
            payload["data"] = json.dumps(body)
        result = self.mgmt("/v8/management/requests/api-call", payload)
        raw = result.get("body")
        try:
            raw = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            pass
        return result.get("status_code"), raw


def summarize_usage(body: dict[str, Any]) -> dict[str, Any]:
    """Reduce an /api/oauth/usage body to limits and reset grants."""
    limits = body.get("limits") or []
    shares = {s["limit_index"]: s for s in body.get("weekly_scoped_shares") or []}
    summary: dict[str, Any] = {"weekly_percent": None, "weekly_resets_at": None, "scoped": {}}
    for index, limit in enumerate(limits):
        if limit.get("kind") == "weekly_all":
            summary["weekly_percent"] = limit.get("percent")
            summary["weekly_resets_at"] = limit.get("resets_at")
        elif limit.get("kind") == "weekly_scoped":
            name = ((limit.get("scope") or {}).get("model") or {}).get("display_name") or f"scoped-{index}"
            share = shares.get(index, {})
            summary["scoped"][name] = {
                "percent": limit.get("percent"),
                "used_percent_of_weekly": share.get("used_percent_of_weekly"),
                "allowance_percent_of_weekly": share.get("allowance_percent_of_weekly"),
                "resets_at": limit.get("resets_at"),
            }
    resets = body.get("cedar_ember")
    if isinstance(resets, dict):
        summary["resets"] = {
            "eligible": resets.get("eligible"),
            "ineligible_reason": resets.get("ineligible_reason"),
            "cooldown_until": resets.get("cooldown_until"),
            "next_grant_id": resets.get("next_grant_id"),
            "grants": [{k: g.get(k) for k in ("id", "label", "type", "clears", "resets_left", "usable_now",
                                              "unavailable_reason", "ends_at", "gives_back_weekly")}
                       for g in resets.get("grants") or []],
        }
    return summary


def limit_percent(summary: dict[str, Any], limit: str) -> int | None:
    if limit.lower() in ("all", "weekly"):
        return summary["weekly_percent"]
    for name, scoped in summary["scoped"].items():
        if name.lower() == limit.lower():
            return scoped["percent"]
    return None


def usage_account_id(salt: str, identity: str) -> str:
    return hmac.digest(bytes.fromhex(salt), identity.strip().lower().encode(), "sha256").hex()


@contextmanager
def locked_usage(root: pathlib.Path):
    """Share attempts and successful status snapshots with the Mac fleet monitor."""
    directory = root / "ops/runtime/claude-resets"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = directory / "usage.json"

    def save():
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as out:
            staging = pathlib.Path(out.name)
            try:
                json.dump(state, out, allow_nan=False)
                out.flush()
                os.fsync(out.fileno())
                staging.replace(cache)
            finally:
                staging.unlink(missing_ok=True)

    with os.fdopen(os.open(directory / "usage.lock", os.O_CREAT | os.O_RDWR, 0o600), "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = json.loads(cache.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {"salt": secrets.token_hex(32), "accounts": {}}
        yield state, save


def request_usage(proxy: Proxy, email: str) -> tuple[int, dict[str, Any] | None]:
    status, body = proxy.upstream(email, "GET", USAGE_URL)
    return status, summarize_usage(body) if status == 200 else None


def status_usage(proxy: Proxy, email: str, root: pathlib.Path) -> tuple[int, dict[str, Any] | None]:
    while True:
        with locked_usage(root) as (state, save):
            tag = usage_account_id(state["salt"], email)
            entry = state["accounts"].get(tag, {})
            now = time.time()
            due = entry["attempted_at"] + USAGE_INTERVAL if entry else 0
            if now < due and "summary" in entry:
                return 200, entry["summary"]
            delay = max(due, state.get("guard_until", 0)) - now
            if delay <= 0:
                state["accounts"][tag] = {"attempted_at": now}
                save()
                status, summary = request_usage(proxy, email)
                if summary is not None:
                    state["accounts"][tag]["summary"] = summary
                    save()
                return status, summary
        time.sleep(delay)


def fetch_usage(proxy: Proxy, email: str, tries: int = 6, wait: float = 60,
                sleep: Callable[[float], None] = time.sleep,
                read: Callable = request_usage) -> dict[str, Any]:
    """Read usage, backing off on 429/5xx. The usage endpoint rate-limits frequent polls."""
    status = None
    for attempt in range(1, tries + 1):
        status, summary = read(proxy, email)
        if status == 200:
            return summary
        if status not in RETRYABLE_STATUS or attempt == tries:
            break
        print(f"{now_iso()} usage HTTP {status}; retry {attempt}/{tries - 1} in {wait:g}s", file=sys.stderr, flush=True)
        sleep(wait)
    raise ResetError(f"usage endpoint returned HTTP {status} for {email}")


def check_claimable(summary: dict[str, Any], grant_id: str) -> dict[str, Any]:
    """Return the grant if it can be claimed now; raise ResetError otherwise."""
    if not GRANT_ID_RE.match(grant_id):
        raise ResetError(f"malformed grant id {grant_id!r}")
    resets = summary.get("resets")
    if not resets:
        raise ResetError("usage response has no reset (cedar_ember) block")
    if not resets["eligible"]:
        raise ResetError(f"account not eligible: {resets['ineligible_reason']}")
    if resets["cooldown_until"]:
        raise ResetError(f"account in reset cooldown until {resets['cooldown_until']}")
    grant = next((g for g in resets["grants"] if g["id"] == grant_id), None)
    if grant is None:
        raise ResetError(f"grant {grant_id} not offered; have {[g['id'] for g in resets['grants']]}")
    if not grant["usable_now"] or not grant["resets_left"]:
        raise ResetError(f"grant {grant_id} not usable now ({grant['unavailable_reason']}, "
                         f"{grant['resets_left']} left)")
    return grant


def request_id_for(state_dir: pathlib.Path, email: str, grant_id: str) -> str:
    """Return the persisted request_id for this account and grant, creating it once."""
    path = state_dir / "request-ids.json"
    ids = json.loads(path.read_text()) if path.exists() else {}
    key = f"{email}|{grant_id}"
    if key not in ids:
        ids[key] = uuid.uuid4().hex
        state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ids, indent=2) + "\n")
    if not REQUEST_ID_RE.match(ids[key]):
        raise ResetError(f"stored request id for {key} is malformed")
    return ids[key]


def claim(proxy: Proxy, email: str, grant_id: str, state_dir: pathlib.Path, receipt_path: pathlib.Path,
          dry_run: bool, sleep: Callable[[float], None] = time.sleep,
          min_fable_percent: int | None = None) -> dict[str, Any]:
    if min_fable_percent is not None and not 0 <= min_fable_percent <= 100:
        raise ResetError("minimum Fable percent must be between 0 and 100")
    receipt: dict[str, Any] = {"started_at": now_iso(), "account": email, "grant_id": grant_id, "dry_run": dry_run}
    status, profile = proxy.upstream(email, "GET", PROFILE_URL)
    if status != 200 or (profile.get("account") or {}).get("email") != email:
        raise ResetError(f"profile check failed for {email} (HTTP {status})")
    org = profile["organization"]["uuid"]
    receipt["before"] = before = fetch_usage(proxy, email, sleep=sleep)
    if min_fable_percent is not None:
        percent = limit_percent(before, "Fable")
        if percent is None:
            raise ResetError(f"Fable limit is missing from the usage response for {email}; "
                             f"cannot check the required {min_fable_percent}%")
        try:
            finite = not isinstance(percent, bool) and isinstance(percent, (int, float)) and math.isfinite(percent)
        except OverflowError:
            finite = False  # an integer too large for a float
        if not finite:
            raise ResetError(f"Fable usage percent is not a finite number ({type(percent).__name__})")
        if percent < min_fable_percent:
            raise ResetError(f"Fable usage {percent}% is below required {min_fable_percent}%")
        receipt["min_fable_percent"] = min_fable_percent
    check_claimable(before, grant_id)
    request_id = request_id_for(state_dir, email, grant_id)
    receipt["request"] = {"url": CLAIM_URL.format(org="<org>"),
                          "body": {"program": PROGRAM, "grant_id": grant_id, "request_id": request_id}}
    if dry_run:
        receipt["result"] = "dry_run"
    else:
        status, body = proxy.upstream(email, "POST", CLAIM_URL.format(org=org),
                                      {"program": PROGRAM, "grant_id": grant_id, "request_id": request_id})
        receipt["claim_http_status"] = status
        receipt["claim_response"] = body
        receipt["result"] = body.get("result") if isinstance(body, dict) else f"http_{status}"
        write_receipt(receipt_path, receipt)  # keep the claim outcome even if the after-check fails
        try:
            receipt["after"] = fetch_usage(proxy, email, sleep=sleep)
        except ResetError as err:
            receipt["after_error"] = str(err)
    receipt["finished_at"] = now_iso()
    write_receipt(receipt_path, receipt)
    return receipt


def write_receipt(path: pathlib.Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2) + "\n")


def format_status(email: str, tier: str | None, summary: dict[str, Any]) -> str:
    lines = [f"{email} ({tier or 'unknown tier'})",
             f"  weekly total: {summary['weekly_percent']}% used, resets {summary['weekly_resets_at']}"]
    for name, s in summary["scoped"].items():
        share = ""
        if s["allowance_percent_of_weekly"] is not None:
            share = (f" = {s['used_percent_of_weekly']}% of the week out of a "
                     f"{s['allowance_percent_of_weekly']}% allowance")
        lines.append(f"  {name}: {s['percent']}% of its cap{share}")
    resets = summary.get("resets")
    if resets:
        lines.append(f"  resets: eligible={resets['eligible']} cooldown_until={resets['cooldown_until']} "
                     f"next={resets['next_grant_id']}")
        for g in resets["grants"]:
            lines.append(f"    {g['id']}: {g['resets_left']} left, usable_now={g['usable_now']}, "
                         f"clears={','.join(g['clears'] or [])}, ends {g['ends_at']} - {g['label']}")
    return "\n".join(lines)


def cmd_status(proxy: Proxy, args: argparse.Namespace) -> int:
    report = []
    for account in proxy.claude_accounts():
        email = account.get("email")
        if args.account and email != args.account:
            continue
        status, profile = proxy.upstream(email, "GET", PROFILE_URL)
        tier = (profile.get("organization") or {}).get("rate_limit_tier") if status == 200 else None
        summary = fetch_usage(
            proxy, email, read=lambda client, identity: status_usage(client, identity, args.root))
        report.append({"account": email, "tier": tier, **summary})
        if not args.json:
            print(format_status(email, tier, summary))
    if args.json:
        print(json.dumps({"captured_at": now_iso(), "accounts": report}, indent=2))
    return 0 if report else 1


def cmd_watch(proxy: Proxy, args: argparse.Namespace) -> int:
    failures = 0
    while True:
        try:
            summary = fetch_usage(proxy, args.account, tries=1)
            failures = 0
            percent = limit_percent(summary, args.limit)
            if percent is None:
                raise ResetError(f"no {args.limit} limit reported for {args.account}")
            print(f"{now_iso()} {args.limit}={percent}% weekly={summary['weekly_percent']}%", flush=True)
            if percent >= args.threshold:
                print(f"{now_iso()} TRIGGER {args.limit} reached {percent}% (threshold {args.threshold}%)", flush=True)
                if args.notify:
                    subprocess.run(["osascript", "-e", f'display notification "{args.account}: {args.limit} '
                                    f'at {percent}%" with title "Claude usage watch"'], check=False)
                return 0
        except (ResetError, urllib.error.URLError, OSError) as err:
            failures += 1
            print(f"{now_iso()} poll failed ({failures}/{args.max_failures}): {err}", flush=True)
            if failures >= args.max_failures:
                return 2
        time.sleep(args.interval)


def cmd_claim(proxy: Proxy, args: argparse.Namespace) -> int:
    if not (args.yes or args.dry_run):
        print("claim consumes a reset grant; pass --yes to redeem or --dry-run to check", file=sys.stderr)
        return 2
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    receipt_path = args.receipt or STATE_DIR / f"claim-{args.grant}-{stamp}{'-dryrun' if args.dry_run else ''}.json"
    receipt = claim(proxy, args.account, args.grant, STATE_DIR, receipt_path, args.dry_run,
                    min_fable_percent=args.min_fable_percent)
    print(json.dumps(receipt, indent=2))
    print(f"receipt: {receipt_path}", file=sys.stderr)
    return 0 if receipt["result"] in ("reset", "dry_run") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=pathlib.Path, default=ROOT, help="deployment root holding .env")
    parser.add_argument("--base-url", default="http://127.0.0.1:8317")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="read-only usage, scoped limits, and reset grants")
    p.add_argument("--account", help="Claude account email; default all")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("watch", help="poll one limit and exit 0 when it reaches the threshold")
    p.add_argument("--account", required=True)
    p.add_argument("--limit", default="Fable", help="scoped model name, or 'all' for the weekly total")
    p.add_argument("--threshold", type=int, default=99)
    p.add_argument("--interval", type=int, default=300, help="seconds; 180 drew HTTP 429s on 2026-11-11")
    p.add_argument("--max-failures", type=int, default=10)
    p.add_argument("--notify", action="store_true", help="show a macOS notification on trigger")

    p = sub.add_parser("claim", help="redeem one reset grant (consumes it)")
    p.add_argument("--account", required=True)
    p.add_argument("--grant", required=True, help="grant id from `status`, e.g. launch-2610-fable")
    p.add_argument("--receipt", type=pathlib.Path)
    p.add_argument("--min-fable-percent", type=int, metavar="0..100",
                   help="refuse a claim unless a fresh read reports a finite Fable usage of at least this percent")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--yes", action="store_true", help="really redeem the grant")
    mode.add_argument("--dry-run", action="store_true", help="run every check but do not send the claim")

    args = parser.parse_args(argv)
    proxy = Proxy(args.base_url, load_management_key(args.root))
    try:
        return {"status": cmd_status, "watch": cmd_watch, "claim": cmd_claim}[args.command](proxy, args)
    except ResetError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
