#!/usr/bin/env python3
"""Watch the Mac fleet monitor and sync its heartbeat to Mint over SSH."""

import argparse
from datetime import datetime
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

MAX_AGE = 20 * 60
POLL_INTERVAL = 300
MINT = "olerever@mint.tail98889d.ts.net"
MINT_SCRIPT = "/usr/bin/python3 -I -B ~/.local/lib/cliproxy-fleet-heartbeat/fleet_heartbeat.py"
MINT_STATE = "~/.local/state/cliproxy-fleet-heartbeat"
STATE_ERRORS = (OSError, ValueError, RecursionError)
SSH_ERRORS = (*STATE_ERRORS, subprocess.SubprocessError)


def read_state(path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    if not isinstance(data, dict):
        raise ValueError("Invalid heartbeat state")
    return data


def write_state(path, data):
    staging = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
            staging = Path(out.name)
            json.dump(data, out)
            out.flush()
            os.fsync(out.fileno())
        staging.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def mint_status(state, now, sample=None, *, sync=False):
    path = state / "heartbeat.json"
    current = read_state(path, {"first_seen": now, "checked_at": None, "synced_at": None})
    checked_at = current["checked_at"]
    if (type(current["first_seen"]) is not int
            or (checked_at is not None and type(checked_at) is not int)
            or (current.get("synced_at") is not None and type(current["synced_at"]) is not int)):
        raise ValueError("Invalid Mint heartbeat state")
    if sync:
        if sample is not None and sample > now + 60:
            raise ValueError("Mac heartbeat is ahead of Mint")
        if sample is not None and sample > (checked_at or 0):
            current["checked_at"] = sample
        current["synced_at"] = now
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_state(path, current)
    age = now - (current["checked_at"] or current["first_seen"])
    synced_at = current.get("synced_at")
    status = "waiting" if synced_at is None else "stale" if age > MAX_AGE else "fresh"
    return {"status": status, "checked_at": current["checked_at"], "synced_at": synced_at}


def ask_mint(snapshot, now):
    result = subprocess.run(
        ["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlias=mint.local",
         "-o", "UpdateHostKeys=no", "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
         MINT, MINT_SCRIPT + " sync"],
        input=f"{snapshot or '-'}\n", text=True, capture_output=True, check=True, timeout=15,
    )
    status = json.loads(result.stdout)
    if (not isinstance(status, dict) or status.get("status") not in ("fresh", "stale")
            or type(status.get("synced_at")) is not int):
        raise ValueError("Mint returned an invalid heartbeat status")
    if abs(status["synced_at"] - now) > 60:
        raise ValueError("Mint and Mac heartbeat clocks differ")
    if snapshot is not None and status.get("checked_at") != snapshot:
        raise ValueError("Mint did not record the Mac heartbeat")
    return status["status"]


def snapshot_epoch(runtime):
    try:
        value = read_state(runtime / "health.json", {}).get("checked_at")
        if isinstance(value, str):
            stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
            if stamp.tzinfo is not None:
                return int(stamp.timestamp())
    except (*STATE_ERRORS, OverflowError):
        pass
    return None


def awake_since():
    output = subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.boottime", "kern.waketime"],
                                     text=True, timeout=3, stderr=subprocess.DEVNULL)
    values = re.findall(r"\bsec = (\d+)", output)
    if len(values) != 2:
        raise ValueError("Missing Mac wake time")
    return max(map(int, values))


def post_text(message, key):
    connection = http.client.HTTPConnection("127.0.0.1", 8787, timeout=10)
    try:
        connection.request("POST", "/text", json.dumps({"text": message}),
                           {"Content-Type": "application/json", "Idempotency-Key": key})
        response = connection.getresponse()
        if response.status == 200:
            result = json.load(response)
            return isinstance(result, dict) and result.get("ok") is True
    except (*STATE_ERRORS, http.client.HTTPException):
        pass
    finally:
        connection.close()
    return False


def report_failure(message, *, desktop=False):
    print(message, file=sys.stderr)
    if not desktop:
        return False
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", "on run argv",
             "-e", 'display notification (item 1 of argv) with title "CLIProxy fleet heartbeat"',
             "-e", "end run", message], check=True, timeout=5,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        print("Fleet heartbeat could not post a macOS notification.", file=sys.stderr)
        return False


def check_heartbeat(runtime, now):
    watch_path = runtime / "heartbeat-watch.json"
    receipt_path = runtime / "heartbeat-alert.json"
    watch = read_state(watch_path, {})
    observed = snapshot_epoch(runtime)
    if observed is None:
        report_failure("Fleet heartbeat cannot read a timestamp from health.json.")
    elif observed > now + 60:
        report_failure("Fleet heartbeat health.json timestamp is ahead of the Mac clock.")
        observed = None
    # Keep the last valid observation if health.json is missing, corrupt or older.
    snapshot = max(observed or 0, watch.get("checked_at") or 0) or None
    first_seen = watch.setdefault("first_seen", now)
    try:
        awake = awake_since()
        if not 0 < awake <= now:
            raise ValueError("Invalid Mac wake time")
    except SSH_ERRORS:
        report_failure("Fleet heartbeat cannot read Mac wake time. Using the relay poll interval.")
        awake = watch.get("awake_since", now)
        if not 0 <= now - watch.get("polled_at", now) <= 2 * POLL_INTERVAL:
            awake = now

    watch.update(checked_at=snapshot, awake_since=awake, polled_at=now)
    try:
        status = ask_mint(snapshot, now)
    except SSH_ERRORS as error:
        status = None
        peer_since = max(watch.setdefault("peer_failed_at", now), awake)
        # Wall clock corrections or overnight sleep cannot age a Mint failure.
        if peer_since > now:
            peer_since = now
        watch["peer_failed_at"] = peer_since
        message = f"Fleet heartbeat could not sync with Mint ({type(error).__name__})."
        desktop = now - peer_since > MAX_AGE and not watch.get("peer_warned")
        if report_failure(message, desktop=desktop):
            watch["peer_warned"] = True
    else:
        watch.pop("peer_failed_at", None)
        watch.pop("peer_warned", None)
    write_state(watch_path, watch)

    age = now - (snapshot or first_seen)
    if now - max(snapshot or first_seen, awake) <= MAX_AGE:
        return int(status is None)
    if status == "fresh" or (status is None and now - peer_since <= MAX_AGE):
        return int(status is None)

    episode = str(snapshot) if snapshot is not None else f"missing-{first_seen}"
    key = f"cliproxy-fleet-heartbeat-{episode}"
    receipt = read_state(receipt_path, {})
    if receipt.get("episode") == episode and receipt.get("status") == "acknowledged":
        return int(status is None)
    if receipt.get("episode") != episode or "message" not in receipt:
        age_text = f"The last monitor snapshot is {age // 60} minutes old." if snapshot else (
            f"No monitor snapshot has been readable for {age // 60} minutes.")
        peer_text = " The heartbeat check could not reach or validate its Mint peer." if status is None else ""
        message = "CLIProxy fleet monitor heartbeat is stale. " + age_text + peer_text
        receipt = {"episode": episode, "status": "pending", "message": message}
        write_state(receipt_path, receipt)
    if not post_text(receipt["message"], key):
        if report_failure("Fleet heartbeat has no Text Me acknowledgment. The relay will retry.",
                          desktop=not receipt.get("text_warned")):
            receipt["text_warned"] = True
            write_state(receipt_path, receipt)
        return 1
    receipt["status"] = "acknowledged"
    write_state(receipt_path, receipt)
    return int(status is None)


def relay(runtime):
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(os.open(runtime / "heartbeat-alert.lock", os.O_CREAT | os.O_WRONLY, 0o600), "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            report_failure("Fleet heartbeat relay is already running.")
            return 1
        return check_heartbeat(runtime, int(time.time()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("watch", "sync", "relay"))
    parser.add_argument("--state", type=Path, default=Path(MINT_STATE).expanduser())
    parser.add_argument("--runtime", type=Path, default=Path("~/cliproxy/ops/runtime/fleet").expanduser())
    args = parser.parse_args(argv)
    try:
        if args.mode == "relay":
            return relay(args.runtime)
        sample = None
        if args.mode == "sync":
            value = sys.stdin.readline(32).strip()
            if value != "-":
                sample = int(value)
                if sample <= 0:
                    raise ValueError("Invalid Mac heartbeat")
        print(json.dumps(mint_status(args.state, int(time.time()), sample, sync=args.mode == "sync")))
    except (*SSH_ERRORS, KeyError, TypeError) as error:
        report_failure(f"Fleet heartbeat {args.mode} failed ({type(error).__name__}).",
                       desktop=args.mode == "relay")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
