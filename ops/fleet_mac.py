"""Pause and resume the Mac's Docker Desktop fallback."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


DESKTOP = ["docker", "--context", "desktop-linux"]
DOCKER_TIMEOUT = 45
ENGINE_WAIT = 120
HEALTH_WAIT = 240
WILL_PROBE_TIMEOUT = 3
MAX_HEALTH_BODY = 4096
MONITOR_MAX_AGE = timedelta(minutes=20)
# Only node running the launcher carries its routing environment. T3 also runs a /bin/sh
# wrapper per session whose arguments name the launcher but whose environment ps cannot show.
CLAUDE_LAUNCHER = re.compile(r"^(?:\S*/)?node\s+(?:\S*/)?claude_account_launcher\.mjs(?=\s|$)")
CODEX = re.compile(r"^(?:\S*/)?codex(?:-[\w.-]+)?\s")
CODEX_BASE_URL = re.compile(r"""\bbase_url\s*=\s*(["']?)([^\s"']+)\1(?=\s|$)""")


class MacError(Exception):
    pass


def runtime_dir(root):
    return root / "ops/runtime/fleet"


def marker_path(root):
    return runtime_dir(root) / "mac-paused.json"


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
    except ValueError:
        return False
    if hostname is None:
        return False
    hostname = hostname.rstrip(".")
    if hostname == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        # The system resolver also accepts shortened and integer IPv4 addresses.
        try:
            address = ipaddress.IPv4Address(socket.inet_aton(hostname))
        except OSError:
            return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def codex_uses_mac(command, port):
    if not CODEX.match(command) or "app-server" not in command.split():
        return False
    for match in CODEX_BASE_URL.finditer(command):
        value = match[2]
        if not is_loopback_url(value):
            continue
        try:
            url = urlsplit(value)
            if url.scheme in ("http", "https") and url.port == port and url.path in ("/v1", "/v1/"):
                return True
        except ValueError:
            continue
    return False


def mac_only_sessions(port):
    processes = run(["ps", "-axww", "-o", "pid=", "-o", "etime=", "-o", "command="], 10)
    if processes.returncode:
        raise MacError("Could not list processes")
    sessions = []
    for line in processes.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3:
            continue
        pid, age, command = fields
        if not pid.isdecimal() or not re.fullmatch(r"[\d:-]+", age):
            continue
        if codex_uses_mac(command, port):
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
    try:
        return run([*DESKTOP, "info", "--format", "{{.ServerVersion}}"], 5).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


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


def will_client_origin(override, settings_path):
    value = override
    if value is None:
        try:
            with settings_path.open(encoding="utf-8") as source:
                environment = json.load(source)["providerInstances"]["claude_cliproxy"]["environment"]
            value = next((item.get("value") for item in environment
                          if item.get("name") == "CLIPROXY_BASE_URL" and not item.get("sensitive")), None)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
    if not isinstance(value, str) or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme in ("http", "https") and url.hostname and url.port != 0
                and url.username is None and url.password is None and url.path in ("", "/")
                and "?" not in value and "#" not in value and not is_loopback_url(value)):
            return url
    except ValueError:
        pass
    return None


def client_health_ok(origin):
    connect = http.client.HTTPSConnection if origin.scheme == "https" else http.client.HTTPConnection
    connection = connect(origin.hostname, origin.port, timeout=WILL_PROBE_TIMEOUT)
    try:
        connection.request("GET", "/healthz", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return False
        body = response.read(MAX_HEALTH_BODY + 1)
        if len(body) > MAX_HEALTH_BODY:
            return False
        payload = json.loads(body)
        return isinstance(payload, dict) and payload.get("status") == "ok"
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def monitor_will_ok(root):
    try:
        with (runtime_dir(root) / "health.json").open(encoding="utf-8") as source:
            report = json.load(source)
        checked_at = datetime.fromisoformat(report["checked_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - checked_at > MONITOR_MAX_AGE:
            print("Note: Fleet monitor health report is stale.")
            return True
    except FileNotFoundError:
        print("Note: Fleet monitor health report is missing.")
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("Could not verify Will in the fleet monitor health report.")
        return False
    try:
        healthy = report["hosts"]["will"]["status"] == "ok"
    except (KeyError, TypeError):
        healthy = False
    print("Fleet monitor Will status: " + ("ok" if healthy else "unhealthy"))
    return healthy


def check_pause(root, port, health, args, settings_path):
    try:
        ssh_ok = health("will") == "ok"
    except (OSError, subprocess.TimeoutExpired):
        ssh_ok = False
    print("Will proxy over SSH: " + ("ok" if ssh_ok else "unhealthy"))
    origin = will_client_origin(args.will_origin, settings_path)
    client_ok = origin is not None and client_health_ok(origin)
    if origin is None:
        print("Will client origin unavailable or invalid. Pass a non-loopback HTTP(S) --will-origin.")
    else:
        print("Will proxy at client origin: " + ("ok" if client_ok else "unhealthy"))
    monitor_ok = monitor_will_ok(root)
    try:
        sessions = mac_only_sessions(port)
    except (MacError, OSError, subprocess.TimeoutExpired):
        print("Could not verify the routes of running sessions.")
        sessions = None
    if sessions:
        print("Mac-only sessions (PID, age, route):")
        for pid, age, route in sessions:
            print(f"  {pid} {age} {route}")
    elif sessions is not None:
        print("Mac-only sessions: none.")
    if not args.force and not (ssh_ok and client_ok and monitor_ok and sessions == []):
        raise MacError("Pause refused. Use --force to bypass failed Will or session checks")


def bundle_supports_pause(bundle_root):
    try:
        return ((bundle_root / "fleet_mac.py").is_file()
                and all("fleet_mac.read_marker" in (bundle_root / name).read_text(encoding="utf-8")
                        for name in ("cliproxy_watchdog.py", "fleet_monitor.py")))
    except (OSError, ValueError):
        return False


def pause(root, port, health, args, *, settings_path, bundle_root):
    check_pause(root, port, health, args, settings_path)
    containers = None
    if engine_up():
        result = run([*DESKTOP, "ps", "--format", "{{.Names}}"], 10)
        if result.returncode:
            raise MacError("Could not list running Docker Desktop containers")
        containers = sorted(result.stdout.split())
        print("Quitting Docker Desktop stops all running containers:")
        for name in containers or ["(none)"]:
            print(f"  {name}")
    elif read_marker(root) is not None:
        print("Mac is already paused. Docker engine is down.")
        return 0
    if not args.yes:
        prompt = ("Pause Mac fallback while Docker is down?" if containers is None else "Quit Docker Desktop?")
        try:
            answer = input(prompt + " [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Pause cancelled.")
            return 1

    with transition_lock(root):
        running = engine_up()
        if not running and read_marker(root) is not None:
            print("Mac is already paused. Docker engine is down.")
            return 0
        if running and containers is None:
            raise MacError("Docker engine started during preflight. Run mac pause again to review its containers")
        if not bundle_supports_pause(bundle_root):
            message = "Ship the bundle first so the Mac watchdog and monitor honor the pause marker"
            if not args.force:
                raise MacError(f"Pause refused. {message}")
            print(f"Warning: {message}.")
        print("Rechecking Will and session routes before pausing.")
        check_pause(root, port, health, args, settings_path)
        write_marker(root, args.reason, containers if running else None)
        if not running:
            print("Mac paused. Docker engine was already down.")
            return 0
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
        if not engine_up():
            desktop("start", "--detach")
            if not wait_for(engine_up, ENGINE_WAIT):
                raise MacError("Docker engine did not come up. Any pause marker remains in place")
        marker_path(root).unlink(missing_ok=True)
    if not wait_for(lambda: health("m5") == "ok", HEALTH_WAIT):
        raise MacError("Docker engine is up and the watchdog is enabled. Mac proxy is still unhealthy")
    print("Mac resumed. Docker engine is up and the Mac proxy is healthy.")
    return 0


def command(args, host, health, *, settings_path):
    root = Path(host["instance_root"])
    try:
        if sys.platform != "darwin" or not root.is_dir():
            raise MacError("Run this command on the Mac with the configured M5 instance root")
        if args.mac_command == "pause":
            return pause(root, host["port"], health, args, settings_path=settings_path,
                         bundle_root=Path(host["bundle_root"]))
        if args.mac_command == "resume":
            return resume(root, health)
        marker = read_marker(root)
        print(f"Mac paused since {marker['paused_at']}." if marker is not None else "Mac is not paused.")
        running = engine_up()
        print("Docker engine: " + ("up" if running else "down"))
        if marker is not None and running:
            print("Warning: Docker is running while paused; run mac resume or mac pause again")
        print(f"Mac proxy: {health('m5')}")
        print(f"Will proxy: {health('will')}")
        return 0
    except MacError as error:
        print(f"{error}.", file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired):
        print("Could not complete the Mac operation. Check Docker and the pause marker with mac status.",
              file=sys.stderr)
    return 1
