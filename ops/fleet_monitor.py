#!/usr/bin/env python3
"""Record CLIProxy fleet health and quota history, without inference requests."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, time as local_time, timedelta, timezone
import fcntl
import http.client
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import tempfile

if __package__:
    from . import fleet_monitor_probe as probes, fleetctl
else:
    import fleet_monitor_probe as probes
    import fleetctl

OPS = Path(__file__).resolve().parent
HOST_TIMEOUT = 70
REMOTE_DEADLINE = 63  # Ends Will's worker even if SSH is interrupted.
PARITY_TIMEOUT = 105
TEXT_TIMEOUT = 12
PARITY_INTERVAL = 3600
DAILY_AT = local_time(9)
CLAUDE_MIN_INTERVAL = 300
CLAUDE_RATE_LIMIT_BACKOFF = 1200
# Claude is read by elapsed time, so the first tick after the Mac sleeps reads it.
# The slack keeps a launchd start that lands a little early on the cadence.
CLAUDE_POLL_DUE = probes.TICK_INTERVAL * probes.CLAUDE_EVERY_TICKS - 60
CLAUDE_SUMMARY_MAX_AGE = timedelta(minutes=35)
COOLDOWN_QUIET = timedelta(hours=1)
TEXT_PORT = 8787
TEXT_SENT, TEXT_NOT_SENT, TEXT_UNCONFIRMED = 0, 20, 21
SSH_ARGS = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
            "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes"]


def now_utc():
    return datetime.now(timezone.utc)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, data):
    staging = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
            staging = Path(out.name)
            json.dump(data, out, sort_keys=True, indent=2, allow_nan=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        staging.replace(path)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def append_history(path, records):
    if not records:
        return
    data = b"".join((json.dumps(row, sort_keys=True, allow_nan=False) + "\n").encode() for row in records)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_APPEND | os.O_RDWR, 0o600), "a+b") as out:
        end = out.seek(0, os.SEEK_END)
        while end:
            start = max(0, end - 4096)
            out.seek(start)
            chunk = out.read(end - start)
            boundary = chunk.rfind(b"\n")
            if boundary >= 0:
                end = start + boundary + 1
                break
            end = start
        out.truncate(end)
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def remote_program(data):
    # Send the secret-free worker source over SSH stdin, as fleetctl does.
    # The only private argument is the monitor's pseudonym salt.
    sources = {name: (OPS / (name + ".py")).read_text(encoding="utf-8")
               for name in ("fleetctl", "claude_usage_resets", "fleet_monitor_probe")}
    return (
        "import io, sys, types\n"
        f"for name, source in {sources!r}.items():\n"
        "    module = types.ModuleType(name)\n"
        "    module.__file__ = name + '.py'\n"
        "    sys.modules[name] = module\n"
        "    exec(compile(source, module.__file__, 'exec'), module.__dict__)\n"
        f"sys.stdin = io.StringIO({data!r})\n"
        "sys.exit(sys.modules['fleet_monitor_probe'].worker())\n"
    )


def run_host(host, salt, visited, not_before):
    name = host["name"]
    fallback = {"reachability": "unknown", "egress": {}, "accounts": [],
                "quota_errors": ["host-probe"], "uncertain": False}
    destination = host["ssh"]
    if not fleetctl.valid_ssh_host(name, destination):
        return fallback
    endpoint = {name: host[name] for name in ("port", "instance_root")}
    payload = json.dumps([endpoint, salt, sorted(visited), not_before], allow_nan=False)
    if destination is None:
        argv = [sys.executable, "-B", str(OPS / "fleet_monitor.py"), "--worker"]
    else:
        argv = [*SSH_ARGS, "--", destination, "timeout", "-k", "1s", f"{REMOTE_DEADLINE}s",
                "python3", "-I", "-B", "-"]
        payload = remote_program(payload)
    try:
        proc = subprocess.run(argv, input=payload, capture_output=True, text=True,
                              timeout=HOST_TIMEOUT, check=False)
        return host_output(proc.stdout, proc.returncode, fallback, destination)
    except subprocess.TimeoutExpired as error:
        return host_output(error.stdout, None, fallback, destination)
    except (OSError, UnicodeError):
        return fallback


def host_output(stdout, returncode, fallback, destination):
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="replace")
    report = None
    for line in (stdout or "").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict) and value.get("reachability") in ("ok", "unreachable"):
                report = value
        except ValueError:
            continue
    if returncode == 0 and report is not None:
        return report
    if report is not None:
        return dict(report, uncertain=report.get("stage") == probes.WORKER_POLLING)
    if returncode in (255, None) and destination is not None:
        return dict(fallback, reachability="unreachable", ssh_unreachable=True, uncertain=True)
    return fallback


def run_parity():
    try:
        proc = subprocess.run([sys.executable, "-B", str(OPS / "fleetctl.py"), "parity"], capture_output=True,
                              text=True, timeout=PARITY_TIMEOUT, check=False)
        report = json.loads(proc.stdout)
        status = {0: {"equal"}, 1: {"drift"}, 2: {"unreachable", "error"}}
        if isinstance(report, dict) and report.get("status") in status.get(proc.returncode, set()):
            return report
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {"status": "error", "error": "Parity probe did not complete"}


def notify_message(message, command):
    argv = command or [sys.executable, "-B", str(OPS / "fleet_monitor.py"), "--text-me-worker"]
    try:
        result = subprocess.run(argv, input=message + ("\n" if command else ""), text=True,
                                timeout=10 if command else TEXT_TIMEOUT, check=False,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, PermissionError):
        return TEXT_NOT_SENT
    except (OSError, subprocess.SubprocessError):
        return TEXT_UNCONFIRMED
    if result.returncode == TEXT_SENT:
        return TEXT_SENT
    if not command and result.returncode == TEXT_NOT_SENT:
        return TEXT_NOT_SENT
    return TEXT_UNCONFIRMED


def text_me_worker():
    connection = http.client.HTTPConnection("127.0.0.1", TEXT_PORT, timeout=10)
    try:
        body = json.dumps({"text": sys.stdin.read()})
        try:
            connection.connect()
        except OSError:
            return TEXT_NOT_SENT
        # Once writing starts, the request may reach Text Me even if we get an error.
        connection.request("POST", "/text", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return TEXT_NOT_SENT
        result = json.load(response)
        if isinstance(result, dict):
            if result.get("ok") is True:
                return TEXT_SENT
            if result.get("ok") is False:
                return TEXT_NOT_SENT
        return TEXT_UNCONFIRMED
    except (OSError, http.client.HTTPException, ValueError):
        return TEXT_UNCONFIRMED
    finally:
        connection.close()


def host_health(host):
    egress = host["egress"]
    reachability = host["reachability"]
    if reachability != "ok":
        status = reachability
    elif "down" in egress.values():
        status = "egress-down"
    else:
        status = "ok" if egress and all(value == "ok" for value in egress.values()) else "unknown"
    return {"status": status, "reachability": reachability, "egress": egress,
            "quota_errors": host["quota_errors"], "ssh_unreachable": host.get("ssh_unreachable", False)}


def quota_records(observed, previous, captured_at):
    by_account = {}
    for host, report in observed.items():
        for row in report["accounts"]:
            by_account.setdefault(row["account_id"], {})[host] = row
    # During incomplete inventory keep the affected accounts visible as unknown.
    for tag, account in previous.items():
        for host in account["hosts"]:
            if observed[host]["quota_errors"]:
                by_account.setdefault(tag, {})
    records, inventory = [], {}
    for tag, members in sorted(by_account.items()):
        old = previous.get(tag, {})
        sample = next((row for row in members.values() if row["status"] == "ok"),
                      next((row for row in members.values() if row["attempted_at"] is not None),
                           next(iter(members.values()), old)))
        aliases = [row["label"] for row in members.values() if not row["label"].startswith("account-")]
        if not aliases and old.get("label") and not old["label"].startswith("account-"):
            aliases.append(old["label"])
        label = aliases[0] if aliases else sample["label"]
        unknown = [host for host in old.get("hosts", [])
                   if host not in members and observed[host]["quota_errors"]]
        status = sample["status"] if members else "quota-unavailable"
        cooldowns = [dict(entry, host=host) for host, row in members.items() for entry in row["cooldowns"]]
        hosts = sorted(set(members) | set(unknown))
        records.append({"captured_at": captured_at, "provider": sample["provider"], "account_id": tag,
                        "label": label, "status": status, "source_host": next((host for host, row in members.items()
                                                                               if row is sample), None),
                        "hosts": hosts, "windows": sample.get("windows", []),
                        "cooldowns": cooldowns, "grants": sample.get("grants")})
        inventory[tag] = {"provider": sample["provider"], "label": label, "hosts": hosts}
    return records, inventory


def status_alerts(state, hosts, parity):
    alerts = []
    previous = state.setdefault("health_alerts", {})
    will, mac = hosts["will"], hosts["m5"]
    egress = will["egress"].values()
    affected = ", ".join(name for name, value in sorted(will["egress"].items()) if value == "down")
    egress_down = (True if "down" in egress else False if egress and all(s == "ok" for s in egress) else None)
    # The Codex wrapper checks only /healthz at session start, so it cannot see egress loss.
    fallback = "Select the _mac profile for Codex-family. claude_cliproxy fails over on network faults."
    if will.get("ssh_unreachable"):
        will_opening = "Will is not reachable over SSH from the Mac."
    elif unreachable(mac):
        will_opening = "Will proxy unreachable."  # With both down, the wrapper stays on Will.
    else:
        # The wrapper picks a host only when a Codex session starts.
        will_opening = ("Will proxy unreachable. New codex_cliproxy sessions start on the Mac; restart open "
                        "ones or use codex_cliproxy_mac. claude_cliproxy fails over per request.")
    issues = [
        ("will-unreachable", unreachable(will), will_opening, "Will health checks recovered."),
        ("will-egress", egress_down if will["reachability"] == "ok" else None,
         f"Will egress down for {affected}. {fallback}", "Will egress recovered."),
        ("mac-unreachable", unreachable(mac),
         "Mac proxy down. Check the local fallback.", "Mac proxy reachable again."),
    ]
    if parity is not None:
        issues.append(("parity-drift", {"drift": True, "equal": False}.get(parity.get("status")),
                       "Fleet parity drift appeared. Check the latest parity report.", "Fleet parity is equal again."))
    for key, failing, opening, closing in issues:
        current = previous.get(key, False)
        if isinstance(current, bool):
            current = {"active": current, "streak": 0}
        if failing is None or failing == current["active"]:
            current["streak"] = 0
        elif current["streak"] == 1:
            alerts.append(opening if failing else closing)
            current = {"active": failing, "streak": 0}
        else:
            current["streak"] = 1
        previous[key] = current
    return alerts


def unreachable(host):
    return {"unreachable": True, "ok": False}.get(host["reachability"])


def account_name(row):
    return row["provider"] + " " + row["label"]


def display_time(value):
    stamp = probes.timestamp(value)
    return stamp.astimezone().strftime("%b %d %H:%M %Z") if stamp else "unknown"


def quota_alerts(state, records, now):
    alerts = []
    all_states = state.setdefault("quota_alerts", {})
    for row in records:
        name = account_name(row)
        account = all_states.setdefault(row["account_id"], {})
        for window in row["windows"]:
            previous = account.setdefault(window["window"], {})
            percent = window["used_percent"]
            above = window["period"] in ("weekly", "monthly") and percent >= 90
            reset = probes.timestamp(window["reset_at"])
            capped = window["capped"] and (reset is None or reset > now)
            old_reset = probes.timestamp(previous.get("reset_at"))
            new_window = old_reset is not None and old_reset <= now and reset is not None and reset > now
            if capped and (not previous.get("capped") or new_window):
                alerts.append(f"{name} {window['window']} capped. Reset {display_time(window['reset_at'])}.")
            elif above and not capped and (not previous.get("above") or new_window):
                alerts.append(f"{name} {window['window']} is {percent:g}% used. Reset {display_time(window['reset_at'])}.")
            previous.update(above=above, capped=capped, reset_at=window["reset_at"])

        deadline = max((entry["until"] for entry in row["cooldowns"]
                        if entry["scope"] == "credential" and entry["reason"] == "quota"), default=None)
        cooldown = account.setdefault("_credential_cooldown", {})
        observed_until = probes.timestamp(deadline)
        last_cooldown = probes.timestamp(cooldown.get("until"))
        if observed_until is not None and observed_until > now:
            reported_cap = any(
                window.get("capped")
                and ((reset := probes.timestamp(window.get("reset_at"))) is None or reset > now)
                for window in account.values()
            )
            if not reported_cap and (last_cooldown is None or now - last_cooldown >= COOLDOWN_QUIET):
                alerts.append(f"{name} in proxy quota cooldown until {display_time(deadline)}.")
            cooldown["until"] = probes.iso(max(last_cooldown or now, observed_until))
    return alerts


def outages(hosts):
    found = []
    for name, report in hosts.items():
        label = "Will" if name == "will" else "Mac"
        if report.get("ssh_unreachable"):
            found.append("Will unreachable over SSH from the Mac")
        elif report["reachability"] == "unreachable":
            found.append(label + " proxy unreachable")
        if any(value == "down" for value in report["egress"].values()):
            found.append(label + " upstream egress down")
    return found


def summary_records(state, records, now):
    previous = state.get("claude_summary_readings", {})
    recent = {}
    displayed = []
    for row in records:
        tag = row["account_id"]
        if row["provider"] == "claude":
            if row["status"] == "ok":
                recent[tag] = {"sampled_at": row["captured_at"], "windows": row["windows"]}
            elif tag in previous and now - probes.timestamp(previous[tag]["sampled_at"]) < CLAUDE_SUMMARY_MAX_AGE:
                recent[tag] = previous[tag]
                if row["status"] == "quota-deferred":
                    row = dict(row, **recent[tag])
        displayed.append(row)
    state["claude_summary_readings"] = recent
    return displayed


def daily_message(day, records, hosts, parity, seen_outages):
    lines = [f"CLIProxy daily summary {day}"]
    for row in sorted(records, key=account_name):
        relevant = [window for window in row["windows"] if window["period"] in ("weekly", "monthly", "unknown")]
        windows = "; ".join(f"{w['window']} {w['used_percent']:g}%, resets {display_time(w['reset_at'])}"
                            for w in relevant)
        if not windows:
            windows = "weekly/monthly quota unavailable" if row["status"] == "ok" else row["status"]
        if row.get("sampled_at"):
            windows += f" (as of {display_time(row['sampled_at'])})"
        lines.append(f"{account_name(row)}: {windows}")
    if not records:
        lines.append("No quota accounts available.")
    for name, report in hosts.items():
        if report["quota_errors"]:
            lines.append(f"{'Will' if name == 'will' else 'Mac'} quota inventory incomplete.")
        if report["status"] == "unknown":
            lines.append(f"{'Will' if name == 'will' else 'Mac'} health incomplete.")
    lines.extend([f"Parity: {parity.get('status', 'unknown')}.",
                  "Outages since last summary: " + (", ".join(seen_outages) if seen_outages else "none observed") + "."])
    return "\n".join(lines)


def collect_hosts(hosts, salt, not_before, host_guards=None):
    host_guards = host_guards or {}
    mac = run_host(hosts["m5"], salt, set(), dict(not_before, _all=host_guards.get("m5", 0)))
    visited = {row["account_id"] for row in mac["accounts"] if row["attempted_at"] is not None}
    will_due = dict(not_before, _all=host_guards.get("will", 0))
    if mac.get("uncertain"):
        # An interrupted Mac worker may already have polled shared accounts.
        will_due["_skip_claude"] = True
    will = run_host(hosts["will"], salt, visited, will_due)
    return {"m5": mac, "will": will}


def collect_with_claude(hosts, state, state_file):
    now = now_utc().timestamp()
    account_due = {tag: attempt + CLAUDE_MIN_INTERVAL
                   for tag, attempt in state.get("claude_attempts", {}).items()}
    account_due.update(state.get("claude_not_before", {}))
    host_guards = state.get("claude_host_guards", {})
    # Both workers have deadlines. Reserve through those deadlines in case the
    # monitor exits before it learns which accounts they attempted.
    reserve = now + 2 * HOST_TIMEOUT + 10 + CLAUDE_MIN_INTERVAL
    reserved = {name: max(host_guards.get(name, 0), reserve) for name in hosts}
    state["claude_host_guards"] = reserved
    write_json(state_file, state)
    observed = collect_hosts(hosts, state["salt"], account_due, host_guards)

    for name, report in observed.items():
        host_guards[name] = reserved[name] if report.get("uncertain") else host_guards.get(name, 0)
        for row in report["accounts"]:
            if row["provider"] == "claude" and row["attempted_at"] is not None:
                interval = (CLAUDE_RATE_LIMIT_BACKOFF if row["status"] == "quota-rate-limited"
                            else CLAUDE_MIN_INTERVAL)
                account_due[row["account_id"]] = row["attempted_at"] + interval
    state["claude_host_guards"] = {name: due for name, due in host_guards.items() if due > now}
    state["claude_not_before"] = {tag: due for tag, due in account_due.items() if due > now}
    state.pop("claude_attempts", None)
    state.pop("claude_guard_until", None)
    write_json(state_file, state)
    return observed


def tick(root, hosts, runtime, command, no_notify):
    now = now_utc()
    captured_at = probes.iso(now)
    state_file = runtime / "monitor-state.json"
    state = read_json(state_file, {"salt": secrets.token_hex(32)})
    latest_path = runtime / "latest.json"
    latest = read_json(latest_path, {})
    checked = probes.timestamp(latest.get("checked_at"))
    parity_due = checked is None or (now - checked).total_seconds() >= PARITY_INTERVAL

    hosts["m5"]["instance_root"] = str(root)
    state.pop("claude_tick", None)
    last_poll = state.get("claude_polled_at")
    claude_due = last_poll is None or now.timestamp() - last_poll >= CLAUDE_POLL_DUE
    with ThreadPoolExecutor(max_workers=1) as pool:
        parity = pool.submit(run_parity) if parity_due else None
        if claude_due:
            state["claude_polled_at"] = now.timestamp()
            observed = collect_with_claude(hosts, state, state_file)
        else:
            observed = collect_hosts(hosts, state["salt"], {"_skip_claude": True})

    records, state["inventory"] = quota_records(observed, state.get("inventory", {}), captured_at)
    daily_records = summary_records(state, records, now)
    append_history(runtime / f"quota-history-{now:%Y-%m}.jsonl", records)

    health = {name: host_health(report) for name, report in observed.items()}
    if parity_due:
        latest = dict(parity.result(), checked_at=captured_at, health=health)
        write_json(latest_path, latest)

    seen_outages = sorted(set(state.get("outages", [])) | set(outages(health)))
    state["outages"] = seen_outages
    delivery = state.get("last_notification")
    if not no_notify:
        candidate = deepcopy(state)
        # A confirmed failure can leave an alert pending across ticks with no quota reading.
        pending = candidate.pop("pending_alerts", {})
        alerts = pending.pop("messages", [])
        candidate.update(pending)
        alerts += status_alerts(candidate, health, latest if parity_due else None)
        alerts += quota_alerts(candidate, records, now)
        messages = list(alerts)
        local = now.astimezone()
        day = local.date().isoformat()
        summary_due = local.time() >= DAILY_AT and day > state.get("summary_day", "")
        if summary_due:
            messages.append(daily_message(day, daily_records, health, latest, seen_outages))
            candidate["summary_day"] = day
        if not messages:
            state = candidate
        else:
            # Save the claim before sending so a crash cannot repeat an ambiguous delivery.
            delivery = {"at": captured_at, "status": "attempting"}
            candidate["last_notification"] = delivery
            write_json(state_file, candidate)
            outcome = notify_message("\n".join(messages), command)
            if outcome == TEXT_NOT_SENT:
                delivery["status"] = "not-sent"
                state["last_notification"] = delivery
                alert_state = {key: candidate[key] for key in ("health_alerts", "quota_alerts")}
                if alerts:
                    state["pending_alerts"] = {"messages": alerts, **alert_state}
                else:
                    state.update(alert_state)
                print("Fleet monitor notification failed. Pending alerts will be retried.", file=sys.stderr)
            else:
                state = candidate
                delivery["status"] = "sent" if outcome == TEXT_SENT else "unconfirmed"
                if outcome == TEXT_SENT and summary_due:
                    state["outages"] = []
                if outcome != TEXT_SENT:
                    print("Fleet monitor notification unconfirmed.", file=sys.stderr)

    write_json(state_file, state)
    write_json(runtime / "health.json", {"checked_at": captured_at, "hosts": health,
                                        "quota_accounts": len(records), "parity": latest.get("status"),
                                        "last_notification": delivery})


def main(argv=None):
    args_in = sys.argv[1:] if argv is None else argv
    if args_in == ["--worker"]:
        return probes.worker()
    if args_in == ["--text-me-worker"]:
        return text_me_worker()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("CLIPROXY_ROOT", "~/cliproxy")).expanduser(),
                        help="instance root for local management and private runtime files")
    parser.add_argument("--no-notify", action="store_true", help="collect without sending or consuming alert transitions")
    args = parser.parse_args(args_in)
    try:
        command = shlex.split(os.environ.get("CLIPROXY_FLEET_NOTIFY_COMMAND", ""))
        runtime = args.root / "ops/runtime/fleet"
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(runtime / "monitor.lock", os.O_CREAT | os.O_WRONLY, 0o600), "w") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            hosts = read_json(OPS / "fleet/hosts.json", None)
            tick(args.root, hosts, runtime, command, args.no_notify)
    except Exception:
        print("Fleet monitor could not complete the tick. Check runtime state and installation.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
