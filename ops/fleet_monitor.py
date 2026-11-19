#!/usr/bin/env python3
"""Record CLIProxy fleet health and quota history, without inference requests."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as local_time, timezone
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
PARITY_TIMEOUT = 105
TEXT_TIMEOUT = 12
PARITY_INTERVAL = 3600
DAILY_AT = local_time(9)
TEXT_PORT = 8787
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
        # The remote deadline also ends the worker if SSH is interrupted.
        argv = [*SSH_ARGS, "--", destination, "timeout", "-k", "1s", "63s", "python3", "-I", "-B", "-"]
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
        return dict(fallback, reachability="unreachable")
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
    if command:
        subprocess.run(command, input=message + "\n", text=True, timeout=10,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run([sys.executable, "-B", str(OPS / "fleet_monitor.py"), "--text-me-worker"],
                       input=message, text=True, timeout=TEXT_TIMEOUT, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def text_me_worker():
    connection = http.client.HTTPConnection("127.0.0.1", TEXT_PORT, timeout=10)
    try:
        connection.request("POST", "/text", json.dumps({"text": sys.stdin.read()}),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        return 0 if response.status == 200 and json.load(response).get("ok") is True else 1
    except Exception:
        return 1
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
            "quota_errors": host["quota_errors"]}


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
    issues = [
        ("will-unreachable", will["reachability"] == "unreachable" if will["reachability"] != "unknown" else None,
         "Will unreachable. Select the _mac profile for Codex-family. claude_cliproxy fails over on network faults.",
         "Will proxy reachable again."),
        ("will-egress", egress_down if will["reachability"] == "ok" else None,
         f"Will egress down for {affected}. Select the _mac profile for Codex-family. claude_cliproxy fails over on network faults.",
         "Will egress recovered."),
        ("mac-unreachable", mac["reachability"] == "unreachable" if mac["reachability"] != "unknown" else None,
         "Mac proxy down. Check the local fallback.", "Mac proxy reachable again."),
        ("parity-drift", {"drift": True, "equal": False}.get(parity.get("status")),
         "Fleet parity drift appeared. Check the latest parity report.", "Fleet parity is equal again."),
    ]
    for key, active, opening, closing in issues:
        if active is None:
            continue
        if active != previous.get(key, False):
            alerts.append(opening if active else closing)
        previous[key] = active
    return alerts


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
        old_deadline = probes.timestamp(cooldown.get("until"))
        if deadline:
            if ((old_deadline is None or old_deadline <= now)
                    and not any(window["capped"] for window in row["windows"])):
                alerts.append(f"{name} in proxy quota cooldown until {display_time(deadline)}.")
            cooldown["until"] = deadline
        elif row["status"] == "ok":
            cooldown["until"] = None
    return alerts


def outages(hosts):
    found = []
    for name, report in hosts.items():
        label = "Will" if name == "will" else "Mac"
        if report["reachability"] == "unreachable":
            found.append(label + " proxy unreachable")
        if any(value == "down" for value in report["egress"].values()):
            found.append(label + " upstream egress down")
    return found


def daily_message(day, records, hosts, parity, seen_outages):
    lines = [f"CLIProxy daily summary {day}"]
    for row in sorted(records, key=account_name):
        relevant = [window for window in row["windows"] if window["period"] in ("weekly", "monthly", "unknown")]
        windows = "; ".join(f"{w['window']} {w['used_percent']:g}%, resets {display_time(w['reset_at'])}"
                            for w in relevant)
        if not windows:
            windows = "weekly/monthly quota unavailable" if row["status"] == "ok" else row["status"]
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


def tick(root, hosts, runtime, command, no_notify):
    now = now_utc()
    captured_at = probes.iso(now)
    state_file = runtime / "monitor-state.json"
    state = read_json(state_file, {"salt": secrets.token_hex(32)})
    latest_path = runtime / "latest.json"
    latest = read_json(latest_path, {})
    checked = probes.timestamp(latest.get("checked_at"))
    parity_due = checked is None or (now - checked).total_seconds() >= PARITY_INTERVAL

    # If the last collector died after dispatching a request, reserve its entire
    # possible request window before allowing another Claude usage read.
    guard = state.get("claude_guard_until", 0)
    not_before = {tag: attempted + probes.CLAUDE_INTERVAL
                  for tag, attempted in state.get("claude_attempts", {}).items()}
    state["claude_guard_until"] = max(guard, now.timestamp() + 2 * HOST_TIMEOUT + 10 + probes.CLAUDE_INTERVAL)
    write_json(state_file, state)

    hosts["m5"]["instance_root"] = str(root)
    with ThreadPoolExecutor(max_workers=1) as pool:
        parity = pool.submit(run_parity) if parity_due else None
        # A guard from a killed worker applies even to accounts we could not inventory.
        not_before = dict(not_before, _all=guard)
        mac = run_host(hosts["m5"], state["salt"], set(), not_before)
        visited = {row["account_id"] for row in mac["accounts"] if row["attempted_at"] is not None}
        if mac.get("uncertain"):
            not_before["_all"] = max(guard, now_utc().timestamp() + probes.CLAUDE_INTERVAL)
        will = run_host(hosts["will"], state["salt"], visited, not_before)
        observed = {"m5": mac, "will": will}
    state["claude_guard_until"] = not_before["_all"] if not will.get("uncertain") else max(
        not_before["_all"], now_utc().timestamp() + probes.CLAUDE_INTERVAL)
    if state["claude_guard_until"] <= now.timestamp():
        state["claude_guard_until"] = 0

    attempts = {tag: attempted for tag, attempted in state.get("claude_attempts", {}).items()
                if attempted > now.timestamp() - probes.CLAUDE_INTERVAL}
    for report in observed.values():
        for row in report["accounts"]:
            if row["provider"] == "claude" and row["attempted_at"] is not None:
                attempts[row["account_id"]] = row["attempted_at"]
    state["claude_attempts"] = attempts
    records, state["inventory"] = quota_records(observed, state.get("inventory", {}), captured_at)
    append_history(runtime / f"quota-history-{now:%Y-%m}.jsonl", records)

    health = {name: host_health(report) for name, report in observed.items()}
    if parity_due:
        latest = dict(parity.result(), checked_at=captured_at, health=health)
        write_json(latest_path, latest)

    seen_outages = sorted(set(state.get("outages", [])) | set(outages(health)))
    state["outages"] = seen_outages
    delivery = state.get("last_notification")
    if not no_notify:
        messages = status_alerts(state, health, latest) + quota_alerts(state, records, now)
        local = now.astimezone()
        day = local.date().isoformat()
        summary_due = local.time() >= DAILY_AT and day > state.get("summary_day", "")
        if summary_due:
            messages.append(daily_message(day, records, health, latest, seen_outages))
            state["summary_day"] = day
        if messages:
            # Text Me has no idempotency key. Persist the delivery claim before
            # sending, since a lost response can follow a successful text.
            delivery = {"at": captured_at, "status": "attempting"}
            state["last_notification"] = delivery
            write_json(state_file, state)
            try:
                notify_message("\n".join(messages), command)
                delivery["status"] = "sent"
                if summary_due:
                    state["outages"] = []
            except (OSError, subprocess.SubprocessError):
                delivery["status"] = "unconfirmed"
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
