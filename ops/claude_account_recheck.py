#!/usr/bin/env python3
"""Recheck one upgraded Claude account before clearing its cached quota cooldown.

The default is read-only. --yes invokes the server's account-pinned inference
verification. A patched server selectively releases stale credential quota
after a completed allowed response. This command never calls blanket reset.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
from typing import Any

if __package__:
    from .claude_usage_resets import OAUTH_HEADERS, PROFILE_URL, ROOT, Proxy, load_management_key, now_iso
else:
    from claude_usage_resets import OAUTH_HEADERS, PROFILE_URL, ROOT, Proxy, load_management_key, now_iso

MODEL = "claude-opus-5-5"


class RecheckError(RuntimeError):
    """Revalidation failed; the command must not clear a cooldown."""


def credential(proxy: Proxy, auth_index: str) -> dict[str, Any]:
    matches = [a for a in proxy.mgmt("/v8/management/credentials")["files"]
               if a.get("auth_index") == auth_index]
    if len(matches) != 1 or matches[0].get("provider") != "claude":
        raise RecheckError("auth_index must identify exactly one Claude credential")
    account = matches[0]
    if account.get("disabled") or account.get("requires_sign_in"):
        raise RecheckError("credential is disabled or requires sign-in")
    if not account.get("email") or not account.get("id"):
        raise RecheckError("credential identity is incomplete")
    return account


def read_profile(proxy: Proxy, auth_index: str) -> dict[str, Any]:
    result = proxy.mgmt("/v8/management/requests/api-call", {
        "auth_index": auth_index, "method": "GET", "url": PROFILE_URL, "header": dict(OAUTH_HEADERS),
    })
    raw = result.get("body")
    try:
        body = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        body = None
    return {"status": result.get("status_code"), "body": body}


def recheck(proxy: Proxy, auth_index: str, expected_tier: str, model: str = MODEL,
            apply: bool = False, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    report = receipt if receipt is not None else {}
    report.update(started_at=now_iso(), auth_index=auth_index, model=model,
                  dry_run=not apply, cooldown_cleared=False, result="checking")
    account = credential(proxy, auth_index)
    cooldowns = [{k: v for k, v in c.items() if k != "remaining_seconds"}
                 for c in account.get("cooldowns") or []]
    report["before_cooldowns"] = cooldowns
    if account.get("cooldowns") is None:
        raise RecheckError("cooldown state is unavailable on this proxy")

    profile = read_profile(proxy, auth_index)
    body = profile["body"]
    if profile["status"] != 200 or not isinstance(body, dict):
        raise RecheckError(f"profile read failed (HTTP {profile['status']})")
    if (body.get("account") or {}).get("email") != account["email"]:
        raise RecheckError("profile account does not match the selected credential")
    tier = (body.get("organization") or {}).get("rate_limit_tier")
    report["tier"] = tier
    if not expected_tier or tier != expected_tier:
        raise RecheckError("current subscription tier does not match --expected-tier")
    if not apply:
        report["result"] = "dry_run"
        return report

    report["verification_attempted"] = True
    verified = proxy.mgmt("/v8/management/credentials/verify", {"auth_index": auth_index, "model": model})
    if verified.get("status") != "ok" or verified.get("verified") is not True:
        raise RecheckError("server did not confirm completed account-pinned inference")
    after = credential(proxy, auth_index)
    if after.get("id") != account["id"] or after.get("email") != account["email"]:
        raise RecheckError("credential identity changed during verification")
    remaining = after.get("cooldowns")
    if remaining is None or any(c.get("scope") == "credential" or c.get("model_key") == model for c in remaining):
        report["result"] = "still_blocked"
        raise RecheckError("verified model is still blocked; server may need the quota recheck fix")
    report["cooldown_cleared"] = True
    report["remaining_model_cooldowns"] = remaining
    report["result"] = "recovered"
    report["finished_at"] = now_iso()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=pathlib.Path, default=ROOT)
    parser.add_argument("--base-url", default="http://127.0.0.1:8317")
    parser.add_argument("--auth-index", required=True, help="exact Claude auth_index from v8 credentials")
    parser.add_argument("--expected-tier", required=True, help="expected upstream rate_limit_tier after upgrade")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--yes", action="store_true", help="authorize one server-side account-pinned verification")
    parser.add_argument("--receipt", type=pathlib.Path, help="sanitized JSON receipt; includes no account email or UUID")
    args = parser.parse_args(argv)
    report: dict[str, Any] = {}
    exit_code = 0
    try:
        proxy = Proxy(args.base_url, load_management_key(args.root))
        recheck(proxy, args.auth_index, args.expected_tier, args.model, args.yes, report)
    except (RecheckError, OSError, ValueError) as err:
        # Do not print an HTTP body or transport exception that may contain credentials.
        report["error"] = str(err) if isinstance(err, RecheckError) else type(err).__name__
        if isinstance(err, urllib.error.HTTPError):
            report["verification_http_status"] = err.code
        if report.get("result") in (None, "checking"):
            report["result"] = "refused"
        exit_code = 1
    report["finished_at"] = now_iso()
    output = json.dumps(report, indent=2) + "\n"
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(output)
    print(output, end="")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
