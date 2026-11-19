"""Host-side, read-only probes for the fleet monitor."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import http.client
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit

if __package__:
    from . import claude_usage_resets as claude, fleetctl
else:
    import claude_usage_resets as claude
    import fleetctl

ZAI_HOSTS = frozenset(("api.z.ai", "open.bigmodel.cn", "dev.bigmodel.cn"))
QUOTA_HOSTS = {
    "claude": "api.anthropic.com",
    "codex": "chatgpt.com",
    "kimi": "api.kimi.com",
    "kimi-ai": "api.kimi.ai",
    "zai": "api.z.ai",
}
CREDENTIALS = "/v8/management/credentials"
COMPATIBILITY = "/v8/management/config/api-keys/openai-compatibility"
TICK_INTERVAL = 300  # The LaunchAgent's StartInterval.
CLAUDE_EVERY_TICKS = 3
WORKER_POLLING = "quota-polling"
CONTAINER = "cli-proxy-api"
CLAUDE_MAX_WAIT = 15  # Longer waits defer the Claude account to a later tick.
HTTP_TIMEOUT = 20  # Kimi's usage endpoint answered in about 10.5 s on 2026-11-19.
# Quota reads end this many seconds after a host collection starts, so a slow
# provider or a longer account list cannot run past Will's remote deadline.
POLL_DEADLINE = 55


def obj(value):
    return value if isinstance(value, dict) else {}


def rows(value):
    return (entry for entry in value if isinstance(entry, dict)) if isinstance(value, list) else ()


def number(value):
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        try:
            result = float(value)
            if math.isfinite(result):
                return result
        except (ValueError, OverflowError):
            pass
    return None


def timestamp(value):
    try:
        if isinstance(value, str) and "T" in value:
            result = fleetctl.parse_time(value)
            if result.tzinfo is None:
                return None
        else:
            seconds = number(value)
            if seconds is None or seconds <= 0:
                return None
            if seconds >= 1e12:
                seconds /= 1000
            result = datetime.fromtimestamp(seconds, timezone.utc)
        if result.year >= 2000:
            return result.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def iso(value):
    return value.isoformat(timespec="seconds") if value is not None else None


def quota_window(name, period, percent, reset=None):
    used = number(percent)
    if used is None or not 0 <= used <= 100:
        return None
    return {"window": name, "period": period, "used_percent": used,
            "reset_at": iso(timestamp(reset)), "capped": used >= 100}


def claude_windows(body, summary):
    windows = [quota_window("weekly", "weekly", summary["weekly_percent"], summary["weekly_resets_at"])]
    for name, entry in summary["scoped"].items():
        scope = name.lower()
        # Persist familiar model names, with a fingerprint for other provider labels.
        if scope not in {"fable", "opus", "sonnet", "haiku"}:
            scope = "scope-" + hashlib.sha256(name.encode()).hexdigest()[:10]
        windows.append(quota_window("weekly-" + scope, "weekly", entry["percent"], entry["resets_at"]))
    for key, name, period in (("five_hour", "session", "session"), ("seven_day", "weekly", "weekly"),
                              ("seven_day_sonnet", "weekly-sonnet", "weekly"),
                              ("seven_day_opus", "weekly-opus", "weekly")):
        entry = obj(body.get(key))
        windows.append(quota_window(name, period, entry.get("utilization"), entry.get("resets_at")))
    # Only counts and timestamps from the reset-grant program are useful history.
    reset = obj(summary.get("resets"))
    counts = [number(grant.get("resets_left")) for grant in rows(reset.get("grants"))]
    available = any(grant.get("usable_now") is True for grant in rows(reset.get("grants")))
    grants = ({"resets_left": sum(n for n in counts if n is not None and n >= 0) if any(
                    n is not None and n >= 0 for n in counts) else None,
               "usable_now": available, "cooldown_until": iso(timestamp(reset.get("cooldown_until")))}
              if reset else None)
    return windows, grants


def codex_windows(body, now):
    rate = obj(body.get("rate_limit"))
    windows = []
    for key, fallback in (("primary_window", "session"), ("secondary_window", "weekly")):
        entry = obj(rate.get(key))
        seconds = number(entry.get("limit_window_seconds"))
        if seconds is not None and seconds <= 0:
            continue
        period = fallback if seconds is None else "weekly" if seconds >= 518400 else "session"
        reset = entry.get("reset_at") or relative_reset(now, entry.get("reset_after_seconds"))
        windows.append(quota_window(period, period, entry.get("used_percent"), reset))
    return windows


def relative_reset(now, value):
    seconds = number(value)
    if seconds is not None and 0 <= seconds < 366 * 86400:
        return iso(now + timedelta(seconds=seconds))
    return None


def kimi_windows(body, now):
    windows = []
    for key, name, period in (("limit_5h", "session", "session"), ("limit_7d", "weekly", "weekly"),
                              ("limit_month_total", "monthly-total", "monthly"),
                              ("limit_month_code", "monthly-code", "monthly")):
        entry = obj(obj(body.get("usages")).get(key))
        ratio = number(entry.get("used_ratio"))
        if ratio is not None and ratio >= 0:
            windows.append(quota_window(name, period, min(ratio, 1) * 100, kimi_reset(entry, now)))
    # The old usage shape can accompany the ratios. First reading per window wins.
    windows.append(kimi_count_window(obj(body.get("usage")), "weekly", now))
    for limit in rows(body.get("limits")):
        detail = obj(limit.get("detail")) or limit
        window = obj(limit.get("window"))
        duration = number(window.get("duration", limit.get("duration", detail.get("duration"))))
        unit = window.get("timeUnit", limit.get("timeUnit", detail.get("timeUnit")))
        unit = str(unit).upper().removeprefix("TIME_UNIT_").rstrip("S")
        multiplier = {"SECOND": 1, "MINUTE": 60, "HOUR": 3600, "DAY": 86400, "WEEK": 604800}.get(unit)
        if duration and multiplier:
            name = {18000: "session", 604800: "weekly"}.get(duration * multiplier)
            if name:
                windows.append(kimi_count_window(detail, name, now))
    return windows


def kimi_count_window(entry, name, now):
    limit = number(entry.get("limit"))
    used = number(entry.get("used"))
    remaining = number(entry.get("remaining"))
    if limit is not None and limit > 0:
        if remaining is not None and 0 <= remaining <= limit:
            used = limit - remaining
        if used is not None and used >= 0:
            return quota_window(name, name, min(used / limit, 1) * 100, kimi_reset(entry, now))
    return None


def kimi_reset(entry, now):
    for key in ("reset_at", "resetAt", "reset_time", "resetTime"):
        if timestamp(entry.get(key)):
            return entry[key]
    for key in ("reset_in", "resetIn", "ttl"):
        reset = relative_reset(now, entry.get(key))
        if reset:
            return reset
    return None


# The Coding Plan reports CREDIT_LIMIT windows since November 2026. Window
# lengths and the used share follow CodexBar's Z.ai plugin (steipete/CodexBar,
# Plugins/zai.js), which agrees with the resets observed on 2026-11-19.
ZAI_PREFIXES = {"TOKENS_LIMIT": "tokens", "TIME_LIMIT": "mcp", "CREDIT_LIMIT": "credits"}
ZAI_UNITS = {1: (1440, "d"), 3: (60, "h"), 5: (1, "min"), 6: (10080, "w")}


def zai_used(entry):
    usage, current = number(entry.get("usage")), number(entry.get("currentValue"))
    remaining = number(entry.get("remaining"))
    used = None
    if usage is not None and usage > 0:
        if remaining is not None:
            used = max(usage - remaining, usage - remaining if current is None else current)
        elif current is not None:
            used = current
    if used is not None:
        return min(max(used, 0), usage) / usage * 100
    percent = number(entry.get("percentage"))
    # Charges can land after a request ends, so a reading can pass 100%.
    return None if percent is None else min(max(percent, 0), 100)


def zai_window(kind, entry):
    unit, count = number(entry.get("unit")), number(entry.get("number"))
    if kind == "TIME_LIMIT" and (unit, count) == (5, 1):
        return "mcp-monthly", "monthly"  # CodexBar's monthly MCP marker.
    if unit not in ZAI_UNITS or not count or count < 0 or count != int(count):
        identity = hashlib.sha256(repr((unit, count)).encode()).hexdigest()[:8]
        return ZAI_PREFIXES[kind] + "-" + identity, "unknown"
    minutes, suffix = ZAI_UNITS[unit]
    minutes *= count
    period = ("session" if minutes <= 1440 else "monthly" if minutes >= 40320
              else "weekly" if minutes >= 10080 else "unknown")
    return f"{ZAI_PREFIXES[kind]}-{int(count)}{suffix}", period


def zai_windows(body):
    payload = obj(body["data"]) if "data" in body else body
    occurrences = Counter()
    windows = []
    for entry in rows(payload.get("limits")):
        kind = entry.get("type")
        if kind not in ZAI_PREFIXES:
            continue
        key, period = zai_window(kind, entry)
        occurrences[key] += 1
        name = key if occurrences[key] == 1 else f"{key}-{occurrences[key]}"
        windows.append(quota_window(name, period, zai_used(entry), entry.get("nextResetTime")))
    return windows


def quota_request(item):
    provider = item["provider"]
    target = QUOTA_HOSTS[provider]
    header = {"Authorization": "Bearer $TOKEN$", "Accept": "application/json"}
    if provider == "claude":
        url, header = claude.USAGE_URL, dict(claude.OAUTH_HEADERS)
    elif provider == "codex":
        url = "https://" + target + "/backend-api/wham/usage"
        if item.get("codex_id"):
            header["ChatGPT-Account-Id"] = item["codex_id"]
    elif provider in {"kimi", "kimi-ai"}:
        url = "https://" + target + "/coding/v1/usages"
    else:
        url = "https://" + item["zai_host"] + "/api/monitor/usage/quota/limit"
        header = {"Authorization": "$TOKEN$", "Accept-Language": "en-US,en",
                  "Content-Type": "application/json"}
    return {"auth_index": item["index"], "method": "GET", "url": url, "header": header}


def fetch_windows(api, item, now, timeout=HTTP_TIMEOUT):
    result = obj(api.quota(quota_request(item), timeout))
    status = result.get("status_code")
    if not isinstance(status, int) or not 200 <= status < 300:
        return "quota-rate-limited" if status == 429 else "quota-unavailable", [], None
    raw = result.get("body")
    body = obj(json.loads(raw) if isinstance(raw, str) else raw)
    if body.get("error") is not None or body.get("success") is False:
        return "quota-unavailable", [], None
    provider = item["provider"]
    grants = None
    if provider == "claude":
        summary = claude.summarize_usage(body)
        windows, grants = claude_windows(body, summary)
    elif provider == "codex":
        windows = codex_windows(body, now)
    elif provider in {"kimi", "kimi-ai"}:
        windows = kimi_windows(body, now)
    else:
        windows = zai_windows(body)
    unique = {}
    for window in windows:
        if window:
            kept = unique.setdefault(window["window"], window)
            # Kimi can put a window's reset only in its second, count-based reading.
            if kept["reset_at"] is None:
                kept["reset_at"] = window["reset_at"]
    return ("ok" if unique else "quota-unavailable"), list(unique.values()), grants


class QuotaAPI(fleetctl.LocalAPI):
    def quota(self, payload, timeout=HTTP_TIMEOUT):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.request("POST", "/v8/management/requests/api-call", json.dumps(payload), {
                "Authorization": "Bearer " + self.management_key(),
                "Content-Type": "application/json",
            })
            response = connection.getresponse()
            if response.status != 200:
                raise fleetctl.ProbeError("Management quota request failed")
            return json.load(response)
        finally:
            connection.close()


def zai_destination(value):
    if not isinstance(value, str) or "\\" in value:
        return None
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and url.hostname in ZAI_HOSTS and url.port in (None, 443)
                and url.username is None and url.password is None and not url.query and not url.fragment):
            return url.hostname
    except ValueError:
        pass
    return None


def account_id(salt, provider, identity):
    return hmac.new(bytes.fromhex(salt), (provider + "\0" + identity).encode(), "sha256").hexdigest()[:16]


def quota_accounts(files, groups, salt):
    accounts = {}
    by_index = {item.get("auth_index"): item for item in files if isinstance(item.get("auth_index"), str)}

    def add(provider, index, identity, item, **details):
        tag = account_id(salt, provider, identity)
        entry = {"provider": provider, "account_id": tag, "index": index, "item": item, **details}
        if tag not in accounts or accounts[tag]["item"].get("disabled") is True:
            accounts[tag] = entry

    for index, item in by_index.items():
        provider = item.get("provider")
        if provider not in ("claude", "codex", "kimi", "kimi-ai", "glm"):
            continue
        if not index or item.get("account_type") not in (None, "", "oauth"):
            continue
        if item.get("runtime_only") and not item.get("account_type"):
            continue
        codex_id = obj(item.get("id_token")).get("chatgpt_account_id")
        email = item.get("email")
        identity = (codex_id if provider == "codex" and isinstance(codex_id, str) and codex_id
                    else email.strip().lower() if isinstance(email, str) and email.strip() else index)
        if provider == "glm":
            add("zai", index, identity, item, zai_host="api.z.ai")
        else:
            add(provider, index, identity, item, codex_id=codex_id)

    # Configured compatibility group names do not authorize sending a token.
    for group in groups:
        origin = zai_destination(group.get("base-url"))
        if not origin:
            continue
        for key in rows(group.get("keys")):
            index, api_key = key.get("auth_index"), key.get("api-key")
            if not isinstance(index, str) or not index or not isinstance(api_key, str) or not api_key:
                continue
            item = {**by_index.get(index, {})}
            if group.get("disabled") is True or key.get("disabled") is True:
                item["disabled"] = True
            add("zai", index, origin + "\0" + api_key, item, zai_host=origin,
                group_label="zai-coding-plan" if group.get("name") == "zai-coding-plan" else None)
    return accounts


def route_label(api, item):
    if item.get("group_label"):
        return item["group_label"] + "-" + item["account_id"][:6]
    route = None
    # A label from the credentials API usually contains the account email.
    try:
        query = urlencode({"name": item["item"]["id"]})
        payload, _ = api.get(CREDENTIALS + "/models?" + query)
        ids = {model["id"] for model in rows(obj(payload).get("models")) if isinstance(model.get("id"), str)}
        from_ids = fleetctl.registered_prefix(ids, forced=True)
        if from_ids and re.fullmatch(r"(?:t3-[a-z0-9-]{1,24}|camoff-astra)", from_ids):
            route = from_ids
    except Exception:
        pass
    return route or "account-" + item["account_id"][:8]


def local_cooldowns(item, now):
    result = {}
    for entry in rows(item.get("cooldowns")):
        if entry.get("reason") not in ("credential_quota", "quota", "transient_rate_limit"):
            continue
        scope = entry.get("scope")
        if scope not in ("credential", "model"):
            continue
        retry = timestamp(entry.get("retry_at"))
        if retry is not None and retry > now:
            key = (scope, "rate-limit" if entry["reason"] == "transient_rate_limit" else "quota")
            result[key] = max(retry, result.get(key, now))
    if not result and item.get("quota_blocked") is True:
        retry = timestamp(item.get("next_retry_after"))
        if retry is not None and retry > now:
            result["credential", "quota"] = retry
    return [{"scope": scope, "reason": reason, "until": iso(retry)}
            for (scope, reason), retry in sorted(result.items())]


def collect_account(api, account, now, visited, not_before, egress, deadline=None):
    tag = account["account_id"]
    item = account["item"]
    provider = account["provider"]
    target = account.get("zai_host") or QUOTA_HOSTS[provider]
    result = {"provider": provider, "account_id": tag, "label": route_label(api, account),
              "cooldowns": local_cooldowns(item, now), "windows": [], "grants": None,
              "attempted_at": None}
    if item.get("disabled") is True or item.get("status") == "disabled":
        result["status"] = "disabled"
    elif item.get("requires_sign_in") is True:
        result["status"] = "sign-in-required"
    elif tag in visited:
        result["status"] = "polled-on-m5"
    elif provider == "claude" and not_before.get("_skip_claude"):
        result["status"] = "quota-deferred"
    elif egress.get(target) == "down":
        result["status"] = "egress-down"
    else:
        due = max(not_before.get(tag, 0), not_before.get("_all", 0))
        delay = max(0, due - time.time()) if provider == "claude" else 0
        left = math.inf if deadline is None else deadline - time.monotonic() - delay
        if delay > CLAUDE_MAX_WAIT or left < 1:
            result["status"] = "quota-deferred"
            return result
        if delay:
            time.sleep(delay)
        result["attempted_at"] = time.time()
        try:
            result["status"], result["windows"], result["grants"] = fetch_windows(
                api, account, now, min(HTTP_TIMEOUT, left))
        except Exception:
            result["status"] = "quota-unavailable"
    return result


EGRESS_SCRIPT = '''
command -v timeout >/dev/null || exit 1
for upstream do
    (
        if timeout -k 1s 4s bash -c 'exec 3<>"/dev/tcp/$1/443"' bash "$upstream" 2>/dev/null; then
            printf '%s ok\n' "$upstream"
        else
            printf '%s down\n' "$upstream"
        fi
    ) &
done
wait
'''


def probe_egress(targets):
    try:
        process = subprocess.run(["docker", "exec", CONTAINER, "bash", "-c", EGRESS_SCRIPT, "bash", *targets],
                                 capture_output=True, text=True, timeout=10, check=False)
        if process.returncode == 0:
            result = {}
            for line in process.stdout.splitlines():
                target, _, status = line.partition(" ")
                if target in targets and status in ("ok", "down"):
                    result[target] = status
            if set(result) == set(targets):
                return result
    except (OSError, subprocess.SubprocessError):
        pass
    return dict.fromkeys(targets, "unknown")


def collect_host(host, salt, visited, not_before, progress=lambda report: None):
    deadline = time.monotonic() + POLL_DEADLINE
    now = datetime.now(timezone.utc)
    api = QuotaAPI(host["port"], Path(host["instance_root"]) / ".env")
    with ThreadPoolExecutor(max_workers=2) as pool:
        health = pool.submit(fleetctl.collect, host, None, None, None, "health", api)
        egress = pool.submit(probe_egress, sorted(set(QUOTA_HOSTS.values()) - {QUOTA_HOSTS["kimi-ai"]}))
        health = health.result()["status"]
        targets = egress.result()
    result = {"reachability": health, "egress": targets, "accounts": [], "quota_errors": []}
    progress(dict(result, quota_errors=["inventory-incomplete"]))
    if health != "ok":
        result["quota_errors"] = ["proxy"]
        return result

    try:
        api.management_key()  # Fail closed before any worker uses local credentials.
    except Exception:
        result["quota_errors"] = ["management-key"]
        return result
    files, groups = [], []
    for route, field in ((CREDENTIALS, "credentials"), (COMPATIBILITY, "zai-config")):
        try:
            payload, _ = api.get(route)
            if field == "credentials" and isinstance(obj(payload).get("files"), list):
                files = list(rows(payload["files"]))
            elif field == "zai-config" and isinstance(payload, list):
                groups = list(rows(payload))
            else:
                result["quota_errors"].append(field)
        except Exception:
            result["quota_errors"].append(field)
    accounts = quota_accounts(files, groups, salt)
    additional = {account.get("zai_host") or QUOTA_HOSTS[account["provider"]]
                  for account in accounts.values()} - targets.keys()
    if additional:
        targets.update(probe_egress(sorted(additional)))
    progress(dict(result, quota_errors=["inventory-incomplete"], stage=WORKER_POLLING))
    with ThreadPoolExecutor(max_workers=6) as pool:
        pending = [pool.submit(collect_account, api, account, now, visited, not_before, targets, deadline)
                   for account in accounts.values()]
        result["accounts"] = [job.result() for job in pending]
    return result


def worker():
    try:
        data = json.load(sys.stdin)
        output = collect_host(*data, progress=lambda report: print(json.dumps(report), flush=True))
    except Exception:
        # Tracebacks can contain raw API responses, labels and local paths.
        return 1
    print(json.dumps(output, allow_nan=False))
    return 0
