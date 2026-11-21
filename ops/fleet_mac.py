"""Pause and resume the Mac's Docker Desktop fallback."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


DESKTOP = ["docker", "--context", "desktop-linux"]
DOCKER_TIMEOUT = 45
ENGINE_WAIT = 120
HEALTH_WAIT = 240
# Only node running the launcher carries its routing environment. T3 also runs a /bin/sh
# wrapper per session whose arguments name the launcher but whose environment ps cannot show.
CLAUDE_LAUNCHER = re.compile(r"^(?:\S*/)?node\s+(?:\S*/)?claude_account_launcher\.mjs(?=\s|$)")
CODEX = re.compile(r"^(?:\S*/)?codex(?:-[\w.-]+)?\s")


class MacError(Exception):
    pass


def marker_path(root):
    return root / "ops/runtime/fleet/mac-paused.json"


def read_marker(root):
    try:
        with marker_path(root).open(encoding="utf-8") as source:
            marker = json.load(source)
        datetime.fromisoformat(marker["paused_at"])
        return marker
    except FileNotFoundError:
        return None
    except (ValueError, TypeError, KeyError):
        # The marker's presence still pauses recovery if its metadata is unreadable.
        return {"paused_at": "unknown"}


def write_marker(root, reason, containers):
    path = marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = {"paused_at": datetime.now(timezone.utc).isoformat(), "containers": containers}
    if reason is not None:
        marker["reason"] = reason
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
            staged = Path(out.name)
            os.fchmod(out.fileno(), 0o600)
            json.dump(marker, out)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        staged.replace(path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


@contextmanager
def transition_lock(root, *, blocking=True):
    path = marker_path(root).with_name("mac-transition.lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600), "wb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
        else:
            yield True


def run(argv, timeout):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, errors="replace", timeout=timeout, check=False)


def is_loopback_url(value):
    try:
        hostname = urlsplit(value).hostname
        return hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def mac_only_sessions(port):
    processes = run(["ps", "-axww", "-o", "pid=", "-o", "etime=", "-o", "command="], 10)
    if processes.returncode:
        raise MacError("Could not list processes")
    base_url = re.escape(f"http://127.0.0.1:{port}/v1")
    mac_codex = re.compile(r'\bbase_url\s*=\s*(["\']?)' + base_url + r'\1(?=\s|$)')
    sessions = []
    for line in processes.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3:
            continue
        pid, age, command = fields
        if not pid.isdecimal() or not re.fullmatch(r"[\d:-]+", age):
            continue
        if CODEX.match(command) and "app-server" in command.split() and mac_codex.search(command):
            sessions.append((pid, age, "Codex via Mac proxy"))
        if CLAUDE_LAUNCHER.search(command):
            detail = run(["ps", "eww", "-p", pid, "-o", "command="], 5)
            if detail.returncode == 1 and not detail.stdout and not detail.stderr:
                continue  # The process exited after the initial listing.
            prefix, _, environment = detail.stdout.strip().partition(command)
            if detail.returncode or prefix or not environment.startswith(" "):
                raise MacError(f"Could not inspect the Claude launcher at PID {pid}")
            primary = re.search(r"(?:^|\s)CLIPROXY_BASE_URL=(\S*)", environment)
            fallback = re.search(r"(?:^|\s)CLIPROXY_FALLBACK_URL=(\S*)", environment)
            if primary and is_loopback_url(primary[1]) and not (fallback and fallback[1]):
                sessions.append((pid, age, "Claude via Mac proxy"))
    return sessions


def engine_up():
    return run([*DESKTOP, "info", "--format", "{{.ServerVersion}}"], 5).returncode == 0


def wait_for(check, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(2, remaining))
    return False


def desktop(action, *flags):
    try:
        result = run(["docker", "desktop", action, *flags], DOCKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise MacError(f"Docker Desktop {action} timed out") from None
    if result.returncode:
        raise MacError(f"Docker Desktop {action} failed (exit {result.returncode})")


def pause(root, port, health, args):
    if health("will") != "ok":
        print("Will proxy is unhealthy.")
        if not args.force:
            raise MacError("Pause refused. Use --force to pause without a healthy Will proxy")
    try:
        sessions = mac_only_sessions(port)
    except (MacError, OSError, subprocess.TimeoutExpired):
        print("Could not verify the routes of running sessions.")
        if not args.force:
            raise MacError("Pause refused. Use --force to bypass session verification") from None
        sessions = []
    if sessions:
        print("Mac-only sessions (PID, age, route):")
        for pid, age, route in sessions:
            print(f"  {pid} {age} {route}")
        if not args.force:
            raise MacError("Pause refused. Use --force to stop Docker with Mac-only sessions open")

    with transition_lock(root):
        if read_marker(root) is not None and not engine_up():
            print("Mac is already paused. Docker engine is down.")
            return 0
        result = run([*DESKTOP, "ps", "--format", "{{.Names}}"], 10)
        if result.returncode:
            raise MacError("Could not list running Docker Desktop containers")
        containers = sorted(result.stdout.split())
        print("Quitting Docker Desktop stops all running containers:")
        for name in containers or ["(none)"]:
            print(f"  {name}")
        if not args.yes:
            try:
                answer = input("Quit Docker Desktop? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("Pause cancelled.")
                return 1
        write_marker(root, args.reason, containers)
        try:
            desktop("stop")
            if not wait_for(lambda: not engine_up(), DOCKER_TIMEOUT):
                raise MacError("Docker engine is still up")
        except MacError as error:
            raise MacError(f"{error}. The pause marker remains in place") from None
        except (OSError, subprocess.TimeoutExpired):
            raise MacError("Could not verify Docker stopped. The pause marker remains in place") from None
    print("Mac paused. Docker engine is down.")
    return 0


def resume(root, health):
    with transition_lock(root):
        desktop("start", "--detach")
        if not wait_for(engine_up, ENGINE_WAIT):
            raise MacError("Docker engine did not come up. Any pause marker remains in place")
        marker_path(root).unlink(missing_ok=True)
    if not wait_for(lambda: health("m5") == "ok", HEALTH_WAIT):
        raise MacError("Docker engine is up and the watchdog is enabled. Mac proxy is still unhealthy")
    print("Mac resumed. Docker engine is up and the Mac proxy is healthy.")
    return 0


def command(args, host, health):
    root = Path(host["instance_root"])
    try:
        if sys.platform != "darwin" or not root.is_dir():
            raise MacError("Run this command on the Mac with the configured M5 instance root")
        if args.mac_command == "pause":
            return pause(root, host["port"], health, args)
        if args.mac_command == "resume":
            return resume(root, health)
        marker = read_marker(root)
        print(f"Mac paused since {marker['paused_at']}." if marker is not None else "Mac is not paused.")
        print("Docker engine: " + ("up" if engine_up() else "down"))
        print(f"Mac proxy: {health('m5')}")
        print(f"Will proxy: {health('will')}")
        return 0
    except MacError as error:
        print(f"{error}.", file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired):
        print("Could not complete the Mac operation. Check Docker and the pause marker with mac status.",
              file=sys.stderr)
    return 1
