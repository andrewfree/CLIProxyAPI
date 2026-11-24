#!/usr/bin/env python3
"""Check the Mac fleet monitor on Mint and relay stale alerts through Mac Text Me."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

MAX_AGE = 20 * 60
MINT = "olerever@mint.tail98889d.ts.net"
MINT_COMMAND = "/usr/bin/python3 -B ~/.local/lib/cliproxy-fleet-heartbeat/fleet_heartbeat.py sync"
MINT_STATE = "~/.local/state/cliproxy-fleet-heartbeat"
STALE_ALERT = "CLIProxy fleet monitor health is over 20 minutes old. Check the fleet monitor LaunchAgent on the Mac."


def mint_status(state, now, sample=None, *, sync=False):
    heartbeat = state / "heartbeat.json"
    try:
        previous = json.loads(heartbeat.read_text(encoding="ascii"))
        if not isinstance(previous, dict):
            raise ValueError("Invalid Mint heartbeat state")
    except FileNotFoundError:
        if not sync:
            return "waiting"
        previous = None

    if sync and sample is not None and sample > now + 60:
        raise ValueError("Mac heartbeat is ahead of Mint")
    current = {"first_seen": now, "checked_at": None} if previous is None else previous
    # A missing or older Mac snapshot cannot renew the last heartbeat.
    if sync and sample is not None and sample > (current["checked_at"] or 0):
        current = dict(current, checked_at=sample)
    if sync and current != previous:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=state, delete=False) as out:
            staging = Path(out.name)
            try:
                json.dump(current, out)
                out.close()
                staging.replace(heartbeat)
            finally:
                staging.unlink(missing_ok=True)
    checked_at = current["checked_at"] or current["first_seen"]
    return "stale" if now - checked_at > MAX_AGE else "fresh"


def ask_mint(heartbeat):
    result = subprocess.run(
        ["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=yes", "-o", "HostKeyAlias=mint.local",
         "-o", "UpdateHostKeys=no", "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
         MINT, MINT_COMMAND],
        input=f"{heartbeat or '-'}\n", text=True, capture_output=True, check=True, timeout=15,
    )
    status = result.stdout.strip()
    if status not in {"fresh", "stale"}:
        raise ValueError("Mint returned an invalid status")
    return status


def snapshot_epoch(monitor, runtime):
    try:
        snapshot = monitor.read_json(runtime / "health.json", {})
        checked_at = monitor.probes.timestamp(snapshot.get("checked_at")) if isinstance(snapshot, dict) else None
    except (ValueError, OSError):
        return None
    return int(checked_at.timestamp()) if checked_at else None


def relay(runtime):
    if __package__:
        from . import fleet_monitor as monitor
    else:
        import fleet_monitor as monitor

    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = runtime / "heartbeat-alert.json"
    with os.fdopen(os.open(runtime / "heartbeat-alert.lock", os.O_CREAT | os.O_WRONLY, 0o600), "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        status = ask_mint(snapshot_epoch(monitor, runtime))
        if status == "fresh":
            receipt.unlink(missing_ok=True)
            return 0
        if receipt.exists():
            return 0

        delivery = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": "attempting"}
        monitor.write_json(receipt, delivery)
        outcome = monitor.notify_message(STALE_ALERT, None)
        if outcome == monitor.TEXT_NOT_SENT:
            receipt.unlink()
            print("Fleet heartbeat text failed. The relay will retry.", file=sys.stderr)
            return 1
        delivery["status"] = "sent" if outcome == monitor.TEXT_SENT else "unconfirmed"
        monitor.write_json(receipt, delivery)
        if outcome != monitor.TEXT_SENT:
            print("Fleet heartbeat text delivery is unconfirmed.", file=sys.stderr)
            return 1
    return 0


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
        print(mint_status(args.state, int(time.time()), sample, sync=args.mode == "sync"))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(f"Fleet heartbeat {args.mode} failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
