#!/usr/bin/env python3
"""Inspect, watch, and redeem Anthropic usage resets for CLIProxy Claude accounts.

Every upstream call goes through a proxy's management `api-call` route, so the
account's OAuth token never leaves CLIProxy. `status` and `watch` are read-only
and ask Will first: this script runs there over SSH stdin against Will's own
proxy and .env, and falls back to the local proxy only when SSH or Will's proxy
is unreachable. `claim` consumes a reset grant, requires `--yes` (or
`--dry-run`), and runs only on the host it is given, never falling back.

The claim request mirrors Claude Code 2.1.322: POST
/api/organizations/{org}/reset_rate_limits with {program, grant_id, request_id}.
The request_id is persisted per account and grant, so a rerun after an
ambiguous failure repeats the same request instead of claiming another grant.
See ops/CLAUDE_USAGE_RESETS.md.
"""

from __future__ import annotations

import argparse
import datetime
import http.client
import ipaddress
import json
import math
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "ops" / "runtime" / "claude-resets"
HOSTS_FILE = pathlib.Path(__file__).resolve().parent / "fleet" / "hosts.json"
# The fleet monitor's SSH options, plus keepalives so a dropped link ends a long watch.
SSH_ARGS = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
            "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
SSH_FAILED = 255
PROXY_UNREACHABLE = 3
DEFAULT_BASE_URL = "http://127.0.0.1:8317"
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


class ResetError(RuntimeError):
    """A precondition failed; nothing was claimed."""


class ProxyConnectionError(ResetError, OSError):
    """The management API could not be reached, or the connection broke.

    It is also an OSError, so callers that catch transport errors keep working.
    """


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # urllib then raises the 3xx as an HTTPError


# The management key goes only to the base URL: no ambient HTTP proxy, no redirects.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects)


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
        self.answered = False

    def mgmt(self, route: str, payload: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(
            self.base_url + route,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
        )
        try:
            with OPENER.open(req, timeout=60) as resp:
                self.answered = True
                return json.load(resp)
        except urllib.error.HTTPError:
            self.answered = True
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
            raise ProxyConnectionError(str(getattr(err, "reason", err) or type(err).__name__)) from None

    def claude_accounts(self) -> list[dict[str, Any]]:
        files = self.mgmt("/v0/management/auth-files")["files"]
        return [f for f in files if f.get("provider") == "claude"]

    def claude_credentials(self) -> dict[str, dict[str, Any]]:
        """The proxy's own view of each Claude account (cooldowns, latest failures), by email."""
        files = self.mgmt("/v8/management/credentials").get("files") or []
        return {f.get("email"): f for f in files if f.get("provider") == "claude"}

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


def fetch_usage(proxy: Proxy, email: str, tries: int = 6, wait: float = 60,
                sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Read usage, backing off on 429/5xx. The usage endpoint rate-limits frequent polls."""
    status = None
    for attempt in range(1, tries + 1):
        status, body = proxy.upstream(email, "GET", USAGE_URL)
        if status == 200:
            if not isinstance(body, dict):
                raise ResetError(f"usage endpoint returned an unreadable body for {email}")
            return summarize_usage(body)
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
          min_fable_percent: int | None = None, host: str | None = None,
          status_hint: str = "status --host local") -> dict[str, Any]:
    if min_fable_percent is not None and not 0 <= min_fable_percent <= 100:
        raise ResetError("minimum Fable percent must be between 0 and 100")
    receipt: dict[str, Any] = {"started_at": now_iso(), "account": email, "grant_id": grant_id, "dry_run": dry_run}
    if host is not None:
        receipt["host"] = host
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
        receipt["result"] = "sending"
        write_receipt(receipt_path, receipt)  # proves the receipt is writable before anything is spent
        try:
            status, body = proxy.upstream(email, "POST", CLAIM_URL.format(org=org),
                                          {"program": PROGRAM, "grant_id": grant_id, "request_id": request_id})
        except urllib.error.HTTPError as err:
            if 400 <= err.code < 500:
                # The management API rejects 4xx requests before forwarding them, so nothing was sent.
                receipt["result"] = "refused"
                receipt["claim_error"] = f"management HTTP {err.code}"
                receipt["finished_at"] = now_iso()
                write_receipt(receipt_path, receipt)
                raise ResetError(f"the proxy refused the claim before sending it (management HTTP {err.code}). "
                                 f"Receipt: {receipt_path}") from None
            indeterminate = f"management HTTP {err.code}"
        except ProxyConnectionError as err:
            indeterminate = str(err)
        else:
            indeterminate = None
        if indeterminate is not None:
            # The proxy may have forwarded the claim before failing, so its outcome is unknown.
            receipt["result"] = "indeterminate"
            receipt["claim_error"] = indeterminate
            receipt["finished_at"] = now_iso()
            write_receipt(receipt_path, receipt)
            raise ResetError(f"the claim may have been sent ({indeterminate}). Receipt: {receipt_path}. Check "
                             f"`{status_hint}` before anything else. If the grant is still usable, rerunning the "
                             f"same command on the same host reuses request {request_id}.")
        receipt["claim_http_status"] = status
        receipt["claim_response"] = body
        receipt["result"] = body.get("result") if isinstance(body, dict) else f"http_{status}"
        write_receipt(receipt_path, receipt)  # keep the claim outcome even if the after-check fails
        try:
            receipt["after"] = fetch_usage(proxy, email, sleep=sleep)
        except ResetError as err:
            receipt["after_error"] = str(err)
        except urllib.error.HTTPError as err:
            receipt["after_error"] = f"management HTTP {err.code}"
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


def proxy_view(credential: dict[str, Any] | None, error: str | None = None) -> dict[str, Any]:
    """Reduce a v8 credentials entry to its cooldowns and each model's latest failure, newest first.

    `available` is False when the proxy's view could not be read or has no
    entry for the account; `error` then says why. `failures` is None when the
    entry carries no `model_failures` (a proxy before fleet.5, or one running
    without an auth manager) and empty when the proxy recorded none.
    """
    if credential is None:
        return {"available": False, "error": error or "the proxy has no entry for this account",
                "status": None, "cooldowns": [], "failures": None}
    cooldowns = [{k: c.get(k) for k in ("scope", "model_key", "reason", "retry_at", "remaining_seconds")}
                 for c in credential.get("cooldowns") or [] if isinstance(c, dict)]
    raw = credential.get("model_failures")
    failures = None
    if isinstance(raw, dict):
        failures = sorted(({"model": model, "http_status": f.get("http_status"), "code": f.get("code"),
                            "type": f.get("type"), "at": f.get("at")}
                           for model, f in raw.items() if isinstance(f, dict)),
                          key=lambda f: str(f["at"] or ""), reverse=True)
    return {"available": True, "error": None, "status": credential.get("status"),
            "cooldowns": cooldowns, "failures": failures}


# Go writes 0-9 fractional digits; Python 3.9's fromisoformat accepts only 3 or 6.
FRACTION_RE = re.compile(r"(T\d{2}:\d{2}:\d{2})(?:\.(\d+))?")


def parse_go_time(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str):
        return None
    text = FRACTION_RE.sub(lambda m: m.group(1) + ("." + (m.group(2) + "000000")[:6] if m.group(2) else ""),
                           value.replace("Z", "+00:00"), count=1)
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def age_text(at: Any, now: datetime.datetime) -> str:
    then = parse_go_time(at)
    if then is None:
        return "at an unknown time"
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def format_proxy_view(view: dict[str, Any], now: datetime.datetime) -> str:
    if not view["available"]:
        return f"  proxy state unavailable: {view['error']}"
    lines = []
    if not view["cooldowns"]:
        lines.append("  proxy: no cooldown")
    for c in view["cooldowns"]:
        target = c["model_key"] if c["scope"] == "model" and c["model_key"] else "whole account"
        left = c["remaining_seconds"]
        left = f"{math.ceil(left / 60)}m left" if isinstance(left, (int, float)) else f"until {c['retry_at']}"
        lines.append(f"  proxy cooldown: {target} {c['reason']}, {left}")
    if view["failures"] is None:
        lines.append("  last failures: not reported by this proxy")
    for f in view["failures"] or []:
        kind = f["type"] or f["code"] or "error"
        lines.append(f"  last failure: {f['model']} HTTP {f['http_status']} {kind} {age_text(f['at'], now)} ({f['at']})")
    return "\n".join(lines)


def read_credentials(proxy: Proxy) -> tuple[dict[str, dict[str, Any]], str | None]:
    """The proxy's view of each Claude account, or an empty view and the reason it could not be read."""
    try:
        return proxy.claude_credentials(), None
    except urllib.error.HTTPError as err:
        return {}, f"/v8/management/credentials returned HTTP {err.code}"
    except (ProxyConnectionError, ValueError, AttributeError) as err:
        return {}, f"/v8/management/credentials could not be read ({type(err).__name__})"


def cmd_status(proxy: Proxy, args: argparse.Namespace) -> int:
    report = []
    credentials = None
    now = datetime.datetime.now(datetime.timezone.utc)
    for account in proxy.claude_accounts():
        email = account.get("email")
        if args.account and email != args.account:
            continue
        if credentials is None:
            credentials, credentials_error = read_credentials(proxy)
        status, profile = proxy.upstream(email, "GET", PROFILE_URL)
        tier = (profile.get("organization") or {}).get("rate_limit_tier") if status == 200 else None
        summary = fetch_usage(proxy, email)
        view = proxy_view(credentials.get(email), credentials_error)
        report.append({"account": email, "tier": tier, **summary, "proxy": view})
        if not args.json:
            print(format_status(email, tier, summary))
            print(format_proxy_view(view, now))
    if args.json:
        print(json.dumps({"captured_at": now_iso(), "accounts": report}, indent=2))
    return 0 if report else 1


def notify(message: str) -> None:
    subprocess.run(["osascript", "-e", f'display notification "{message}" with title "Claude usage watch"'],
                   check=False)


def cmd_watch(proxy: Proxy, args: argparse.Namespace) -> int:
    # Polls below count failures and retry, so check the proxy once first. An
    # unreachable proxy then exits PROXY_UNREACHABLE before any output, which
    # is what lets `--host auto` fall back.
    proxy.claude_accounts()
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
                    notify(f"{args.account}: {args.limit} at {percent}%")
                return 0
        except (ResetError, urllib.error.URLError, OSError) as err:
            failures += 1
            print(f"{now_iso()} poll failed ({failures}/{args.max_failures}): {err}", flush=True)
            if failures >= args.max_failures:
                return 2
        time.sleep(args.interval)


def status_hint(args: argparse.Namespace) -> str:
    """The `status` command that asks the same proxy this claim used."""
    if args.host == "will":
        return "status --host will"
    options = "".join(f"{flag} {value} " for flag, value in (("--root", args.root), ("--base-url", args.base_url))
                      if value)
    return f"{options}status --host local"


def cmd_claim(proxy: Proxy, args: argparse.Namespace) -> int:
    if not (args.yes or args.dry_run):
        print("claim consumes a reset grant; pass --yes to redeem or --dry-run to check", file=sys.stderr)
        return 2
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    receipt_path = args.receipt or STATE_DIR / f"claim-{args.grant}-{stamp}{'-dryrun' if args.dry_run else ''}.json"
    receipt = claim(proxy, args.account, args.grant, STATE_DIR, receipt_path, args.dry_run,
                    min_fable_percent=args.min_fable_percent, host=getattr(args, "label", None),
                    status_hint=status_hint(args))
    print(json.dumps(receipt, indent=2))
    print(f"receipt: {receipt_path}", file=sys.stderr)
    return 0 if receipt["result"] in ("reset", "dry_run") else 1


def fleet_hosts() -> dict[str, Any] | None:
    try:
        hosts = json.loads(HOSTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # Will's hand-copied ops subset has no fleet hosts file
    return hosts if isinstance(hosts, dict) else None


def will_entry() -> dict[str, Any] | None:
    """Return Will's fleet entry (ssh, instance_root, port), or None without a usable one."""
    hosts = fleet_hosts()
    if hosts is None:
        return None
    try:
        host = hosts["will"]
        destination, root, port = host["ssh"], str(pathlib.PurePosixPath(host["instance_root"])), int(host["port"])
    except (KeyError, TypeError, ValueError):
        destination = None
    if not isinstance(destination, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", destination):
        print(f"warning: no usable Will entry in {HOSTS_FILE}; asking only the local proxy", file=sys.stderr)
        return None
    return {"ssh": destination, "instance_root": root, "port": port}


def is_loopback(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        if (parts.scheme not in ("http", "https") or host is None
                or parts.username is not None or parts.password is not None):
            return False
        parts.port  # raises ValueError for a malformed port
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_root(command: str) -> pathlib.Path:
    """The instance root whose .env holds the local proxy's key, when --root is not given."""
    if command == "claim" or (ROOT / ".env").exists():
        return ROOT  # a claim keeps its request IDs under the checkout that sends it
    try:
        return pathlib.Path((fleet_hosts() or {})["m5"]["instance_root"])  # a worktree has no .env
    except (KeyError, TypeError):
        return ROOT


def remote_program(argv: list[str], host: dict[str, Any]) -> str:
    """Source for `python3 -` on Will: this file, run against Will's instance root, proxy and .env."""
    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    remote_file = str(pathlib.PurePosixPath(host["instance_root"]) / "ops" / "claude_usage_resets.py")
    call = (argv, "http://127.0.0.1:%d" % host["port"], "Will (%s)" % host["ssh"], host["instance_root"])
    return (
        "import sys, types\n"
        "module = types.ModuleType('claude_usage_resets')\n"
        f"module.__file__ = {remote_file!r}\n"
        "sys.modules[module.__name__] = module\n"
        f"exec(compile({source!r}, module.__file__, 'exec'), module.__dict__)\n"
        f"sys.exit(module.run_remote(*{call!r}))\n"
    )


def ask_will(argv: list[str], host: dict[str, Any], live: bool) -> tuple[int, str, bool]:
    """Run the command on Will. Returns its exit code, its stdout, and whether Will was unreachable.

    Will's stderr passes straight through. With `live`, stdout is also shown as
    it arrives, for `watch`. The management key never leaves Will: the remote
    process reads Will's own .env.
    """
    program = remote_program(argv, host)  # read before ssh starts, so a failure cannot strand it on stdin
    try:
        proc = subprocess.Popen([*SSH_ARGS, "--", host["ssh"], "python3", "-I", "-B", "-"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8")
    except OSError:
        return SSH_FAILED, "", True
    try:
        proc.stdin.write(program)
    except OSError:
        pass  # ssh exited early; its exit status says why
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
    lines = []
    for line in proc.stdout:
        lines.append(line)
        if live:
            sys.stdout.write(line)
            sys.stdout.flush()
    code = proc.wait()
    return code, "".join(lines), code in (SSH_FAILED, PROXY_UNREACHABLE)


def run_local(args: argparse.Namespace, label: str = "the local proxy") -> int:
    """Run the command against the proxy at `--base-url`, with the key from the instance root's .env."""
    root = args.root or local_root(args.command)
    base_url = args.base_url or DEFAULT_BASE_URL
    if not is_loopback(base_url):
        # The management key stays on its host, so only a loopback proxy may receive it.
        print(f"error: --base-url must be a loopback URL such as {DEFAULT_BASE_URL}", file=sys.stderr)
        return 2
    try:
        key = load_management_key(root)
    except FileNotFoundError:
        print(f"error: {root / '.env'} not found; pass --root with the instance root", file=sys.stderr)
        return 1
    except ResetError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    proxy = Proxy(base_url, key)
    args.label = label
    try:
        return {"status": cmd_status, "watch": cmd_watch, "claim": cmd_claim}[args.command](proxy, args)
    except ProxyConnectionError as err:
        if proxy.answered:
            # Something may already have happened, so this is not "unreachable".
            print(f"error: lost the connection to {label} at {base_url} after it answered ({err})",
                  file=sys.stderr)
            return 1
        print(f"error: {label} did not answer at {base_url} ({err})", file=sys.stderr)
        return PROXY_UNREACHABLE
    except ResetError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as err:
        print(f"error: {label} at {base_url} returned HTTP {err.code}", file=sys.stderr)
        return 1
    finally:
        if proxy.answered:
            print(f"answered by {label} at {base_url}", file=sys.stderr)


def run_remote(argv: list[str], base_url: str, label: str, root: str) -> int:
    """Entry point on Will: the command against Will's own proxy, .env and claim state.

    The root comes from the fleet hosts file, not from this module's path, so a
    bundle symlink on Will cannot move the key, request IDs or receipts.
    """
    global STATE_DIR
    STATE_DIR = pathlib.Path(root) / "ops" / "runtime" / "claude-resets"
    args = build_parser().parse_args(argv)
    args.root, args.base_url = pathlib.Path(root), base_url
    args.notify = False  # Will has no notification centre; the caller shows it
    return run_local(args, label)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=pathlib.Path,
                        help="local instance root holding .env (default: this checkout; for status and watch "
                             "from a worktree, the Mac instance root in fleet/hosts.json)")
    parser.add_argument("--base-url", help=f"the local proxy (default {DEFAULT_BASE_URL})")
    read_host = argparse.ArgumentParser(add_help=False)
    read_host.add_argument("--host", choices=("auto", "will", "local"), default="auto",
                           help="auto (default): ask Will over SSH, and use the local proxy only if SSH or "
                                "Will's proxy cannot be reached. will or local: that host only")
    claim_host = argparse.ArgumentParser(add_help=False)
    claim_host.add_argument("--host", choices=("will", "local"),
                            help="the only host this claim uses; required with --yes, and local for a dry run "
                                 "without it. It never falls back, because the request ID and receipt stay there")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", parents=[read_host], help="read-only usage, scoped limits, and reset grants")
    p.add_argument("--account", help="Claude account email; default all")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("watch", parents=[read_host], help="poll one limit and exit 0 when it reaches the threshold")
    p.add_argument("--account", required=True)
    p.add_argument("--limit", default="Fable", help="scoped model name, or 'all' for the weekly total")
    p.add_argument("--threshold", type=int, default=99)
    p.add_argument("--interval", type=int, default=300, help="seconds; 180 drew HTTP 429s on 2026-11-11")
    p.add_argument("--max-failures", type=int, default=10)
    p.add_argument("--notify", action="store_true", help="show a macOS notification on trigger")

    p = sub.add_parser("claim", parents=[claim_host], help="redeem one reset grant (consumes it)")
    p.add_argument("--account", required=True)
    p.add_argument("--grant", required=True, help="grant id from `status`, e.g. launch-2610-fable")
    p.add_argument("--receipt", type=pathlib.Path, help="receipt path; with --host will, a path on Will")
    p.add_argument("--min-fable-percent", type=int, metavar="0..100",
                   help="refuse a claim unless a fresh read reports a finite Fable usage of at least this percent")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--yes", action="store_true", help="really redeem the grant")
    mode.add_argument("--dry-run", action="store_true", help="run every check but do not send the claim")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_parser().parse_args(argv)
    if ".fleet-releases" in ROOT.parts:
        # The ops bundle ships this module only for the fleet monitor's imports.
        # Run as a command there, its .env lookup, receipts and request IDs would
        # resolve inside a release that the next ship replaces.
        print("error: run ops/claude_usage_resets.py from the checkout, not the ops bundle", file=sys.stderr)
        return 2
    if args.command == "claim" and args.host is None:
        if args.yes:
            # Spending a grant names its host, so a rerun cannot land elsewhere by default.
            print("error: claim --yes needs --host will or --host local", file=sys.stderr)
            return 2
        args.host = "local"
    if args.host == "will" and (args.root or args.base_url):
        print("error: --root and --base-url name the local proxy; drop them with --host will", file=sys.stderr)
        return 2
    will = None if args.host == "local" else will_entry()
    if args.host == "will" and will is None:
        print(f"error: --host will needs Will's entry in {HOSTS_FILE}", file=sys.stderr)
        return 2
    if will is not None and will["instance_root"] == str(ROOT):
        # Running in Will's own instance: Will is the local proxy.
        args.root = args.root or pathlib.Path(will["instance_root"])
        args.base_url = args.base_url or f"http://127.0.0.1:{will['port']}"
        return run_local(args, "Will (this host)")
    if will is not None:
        live = args.command == "watch"
        code, output, unreachable = ask_will(argv, will, live)
        # Fall back only before Will has shown anything, so a watch never
        # switches hosts halfway, and never when the host was named.
        if not unreachable or (live and output) or args.host == "will":
            if not live:
                sys.stdout.write(output)
            if code == SSH_FAILED:
                print(f"error: the SSH session to Will ({will['ssh']}) failed", file=sys.stderr)
                if args.command == "claim" and args.yes:
                    print("The claim may have been sent. Check `status --host will` and the receipt on Will "
                          "first. If the grant is still usable, rerun the same command with --host will: it "
                          "reuses the request ID stored on Will. Another host would use a new one.",
                          file=sys.stderr)
            if code == 0 and live and args.notify:
                notify(f"{args.account}: {args.limit} reached {args.threshold}%")
            return code
        reason = "the SSH session failed" if code == SSH_FAILED else "its proxy did not answer"
        print(f"Will ({will['ssh']}): {reason}; asking the local proxy", file=sys.stderr)
    return run_local(args)


if __name__ == "__main__":
    sys.exit(main())
