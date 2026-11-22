"""Manage the Mac's Docker Desktop fallback and Docker-use leases."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


DESKTOP = ["docker", "--context", "desktop-linux"]
DOCKER_TIMEOUT = 45
ENGINE_WAIT = 120
HEALTH_WAIT = 240
WILL_PROBE_TIMEOUT = 3
MAX_HEALTH_BODY = 4096
MONITOR_MAX_AGE = timedelta(minutes=20)
LEASE_ID = re.compile(r"[0-9a-f]{16}")
LEASE_FIELDS = ("thread", "project", "purpose", "why_mac")
LEASE_LOCK_WAIT = 30
PROXY_CONTAINER = "cli-proxy-api"
RESTART_POLICY = re.compile(r"no|always|unless-stopped|on-failure(?::[0-9]+)?")
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


def write_json(path, value, *, create=False):
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".tmp-", delete=False) as out:
            staged = Path(out.name)
            os.fchmod(out.fileno(), 0o600)
            json.dump(value, out)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        if create:
            # Publish the complete file without replacing an existing lease ID.
            os.link(staged, path)
        else:
            staged.replace(path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def write_marker(root, reason, containers, *, restart_policy=None, broken_leases=()):
    path = marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = {"paused_at": datetime.now(timezone.utc).isoformat(), "containers": containers}
    if reason is not None:
        marker["reason"] = reason
    if restart_policy is not None:
        marker["restart_policy"] = restart_policy
    if broken_leases:
        marker["broken_leases"] = list(broken_leases)
    write_json(path, marker)


@contextmanager
def transition_lock(root, *, blocking=True, timeout=None):
    path = marker_path(root).with_name("mac-transition.lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600), "wb") as lock:
        deadline = None if timeout is None else time.monotonic() + timeout
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking or deadline is not None else 0)
        while True:
            try:
                fcntl.flock(lock, flags)
            except BlockingIOError:
                remaining = 0 if deadline is None else deadline - time.monotonic()
                if remaining <= 0:
                    yield False
                    return
                time.sleep(min(0.25, remaining))
                if time.monotonic() >= deadline:
                    yield False
                    return
            else:
                yield True
                return


def lease_dir(root):
    return runtime_dir(root) / "docker-leases"


def lease_text(value):
    return isinstance(value, str) and 0 < len(value) <= 200 and value.isprintable() and bool(value.strip())


def lease_time(value):
    timestamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if timestamp.utcoffset() is None:
        raise ValueError("Lease timestamp has no time zone")
    return timestamp


def read_leases(root, *, now=None):
    """Return live and expired leases, including unknown live entries for read failures."""
    now = datetime.now(timezone.utc) if now is None else now
    try:
        paths = sorted(lease_dir(root).iterdir())
    except FileNotFoundError:
        return []
    except OSError:
        return [{"id": None, "live": True, "unknown": True}]
    leases = []
    for path in paths:
        lease_id = path.stem
        if path.suffix != ".json" or not LEASE_ID.fullmatch(lease_id):
            continue
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("Lease is not a regular file")
            record = json.loads(path.read_text(encoding="utf-8"))
            if (type(record["version"]) is not int or record["version"] != 1 or record["id"] != lease_id
                    or not all(lease_text(record[field]) for field in LEASE_FIELDS)):
                raise ValueError("Invalid lease metadata")
            expires = lease_time(record["expires_at"])
            if expires <= lease_time(record["created_at"]):
                raise ValueError("Invalid lease interval")
            leases.append({**record, "live": now < expires, "unknown": False})
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            leases.append({"id": lease_id, "live": True, "unknown": True})
    return leases


def live_leases(root):
    return [lease for lease in read_leases(root) if lease["live"]]


def expiry_after(now, hours):
    duration = timedelta(hours=hours) if 0 < hours <= 12 else timedelta(0)
    if not duration:
        raise MacError("--hours must be greater than 0 and at most 12")
    return now + duration


def acquire_lease(root, bundle_root, *, thread, project, purpose, why_mac, hours=3, approved=None):
    fields = {"thread": thread, "project": project, "purpose": purpose, "why_mac": why_mac}
    for field, value in fields.items():
        if not lease_text(value):
            raise MacError(f"--{field.replace('_', '-')} requires 1 to 200 printable characters")
    with transition_lock(root, timeout=LEASE_LOCK_WAIT) as acquired:
        if not acquired:
            raise MacError("Mac transition in progress; retry shortly")
        now = datetime.now(timezone.utc)
        expires = expiry_after(now, hours)
        if not bundle_supports_leases(bundle_root):
            raise MacError("Ship the bundle first so the Mac watchdog honors Docker leases")
        directory = lease_dir(root)
        directory.mkdir(mode=0o700, exist_ok=True)
        record = {"version": 1, **fields, "created_at": now.isoformat(), "expires_at": expires.isoformat()}
        if approved is not None:
            record["approved"] = approved
        while True:
            lease_id = secrets.token_hex(8)
            filename = f"{lease_id}.json"
            if (directory / "broken" / filename).exists():
                continue
            record["id"] = lease_id
            try:
                write_json(directory / filename, record, create=True)
            except FileExistsError:
                continue
            return record


def valid_lease_id(lease_id):
    if not LEASE_ID.fullmatch(lease_id):
        raise MacError("Lease ID must have 16 lowercase hexadecimal characters")


def renew_lease(root, lease_id, hours):
    valid_lease_id(lease_id)
    with transition_lock(root):
        now = datetime.now(timezone.utc)
        expires = expiry_after(now, hours)
        for lease in read_leases(root, now=now):
            if lease["id"] == lease_id and lease["live"] and not lease["unknown"]:
                lease.pop("live")
                lease.pop("unknown")
                lease["expires_at"] = max(expires, lease_time(lease["expires_at"])).isoformat()
                write_json(lease_dir(root) / f"{lease_id}.json", lease)
                return lease
    raise MacError(f"Lease {lease_id} is missing, expired, or unreadable")


def release_lease(root, lease_id):
    valid_lease_id(lease_id)
    with transition_lock(root):
        if any(lease["id"] == lease_id and lease["unknown"] for lease in read_leases(root)):
            raise MacError(f"Lease {lease_id} is unreadable; use mac pause --break-lease with a reason")
        try:
            (lease_dir(root) / f"{lease_id}.json").unlink()
        except FileNotFoundError:
            raise MacError(f"Lease {lease_id} does not exist") from None
        return not live_leases(root) and read_marker(root) is not None and engine_up()


def break_leases(root, leases, reason, containers):
    """Archive the named live leases while the caller holds the transition lock."""
    if not leases:
        return []
    directory = lease_dir(root)
    archive = directory / "broken"
    archive.mkdir(mode=0o700, exist_ok=True)
    broken_at = datetime.now(timezone.utc).isoformat()
    records = []
    for lease in leases:
        filename = f"{lease['id']}.json"
        record = {key: value for key, value in lease.items() if key not in ("live", "unknown")}
        record.update(broken_at=broken_at, broken_by_reason=reason, containers=containers)
        if lease["unknown"]:
            record["owner"] = "unknown"
        write_json(archive / filename, record)
        (directory / filename).unlink()
        records.append(record)
    return records


def pacific(timestamp, *, date=False):
    local = lease_time(timestamp).astimezone(ZoneInfo("America/Los_Angeles"))
    return local.strftime(("%Y-%m-%d " if date else "") + "%-I:%M %p %Z")


def lease_description(lease, *, date=False):
    if lease["id"] is None:
        return "Docker lease directory is unreadable (unknown owner)"
    if lease.get("unknown"):
        return f"Docker lease {lease['id']} is unreadable (unknown owner)"
    expires = pacific(lease['expires_at'], date=date)
    return (f"Docker lease {lease['id']} ({lease['project']}, thread {lease['thread']}) "
            f"\"{lease['purpose']}\" until {expires}")


def print_expired_lease(lease, *, details=False):
    expiry = pacific(lease["expires_at"], date=True)
    print(f"Lease {lease['id']} expired at {expiry}; ask thread {lease['thread']} to renew or release it.")
    if details:
        print(f"  {lease['project']}: {lease['purpose']}")


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


ENGINE_UP, ENGINE_DOWN, ENGINE_UNKNOWN = "up", "down", "unknown"


def engine_state():
    # A docker info that does not answer in time is "unknown", never "down": under memory
    # pressure a running engine can be slow, and pause must not mistake it for a stopped one.
    try:
        result = run([*DESKTOP, "info", "--format", "{{.ServerVersion}}"], 5)
    except (subprocess.TimeoutExpired, OSError):
        return ENGINE_UNKNOWN
    return ENGINE_UP if result.returncode == 0 else ENGINE_DOWN


def engine_up():
    return engine_state() == ENGINE_UP


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


def docker_restart_policy():
    result = run([*DESKTOP, "inspect", "--type", "container", "--format",
                  "{{json .HostConfig.RestartPolicy}}", PROXY_CONTAINER], 10)
    if result.returncode:
        if re.search(r"No such (?:container|object): cli-proxy-api$", result.stderr.strip()):
            return None
        raise MacError(f"Could not inspect the restart policy for {PROXY_CONTAINER}")
    try:
        policy = json.loads(result.stdout)
        name = policy["Name"] or "no"
        retry_count = policy["MaximumRetryCount"]
        if not RESTART_POLICY.fullmatch(name) or type(retry_count) is not int or retry_count < 0:
            raise ValueError("Invalid restart policy")
        return f"on-failure:{retry_count}" if name == "on-failure" and retry_count else name
    except (ValueError, KeyError, TypeError):
        raise MacError(f"Could not read the restart policy for {PROXY_CONTAINER}") from None


def update_restart_policy(policy):
    if not isinstance(policy, str) or not RESTART_POLICY.fullmatch(policy):
        raise MacError("The pause marker contains an invalid restart policy")
    try:
        result = run([*DESKTOP, "update", f"--restart={policy}", PROXY_CONTAINER], DOCKER_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        raise MacError(f"Could not set {PROXY_CONTAINER} restart policy; the pause marker remains in place") from None
    if result.returncode:
        raise MacError(f"Could not set {PROXY_CONTAINER} restart policy; the pause marker remains in place")


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


def check_pause_leases(root, ids):
    for lease_id in ids:
        valid_lease_id(lease_id)
    leases = live_leases(root)
    blocking = [lease for lease in leases if lease["id"] not in ids]
    if blocking:
        for lease in blocking:
            action = " Ask that thread to release it." if not lease["unknown"] else ""
            print(f"Pause refused: {lease_description(lease)}.{action}")
        raise MacError("Pause refused because Docker has live leases")
    if set(ids) - {lease["id"] for lease in leases}:
        raise MacError("Pause refused: each --break-lease must name a live lease")
    for lease in leases:
        print(f"Will break {lease_description(lease)}.")
    return leases


def check_pause(root, port, health, args, settings_path):
    leases = check_pause_leases(root, args.break_lease)
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
    return leases


def bundle_supports_pause(bundle_root):
    try:
        return ((bundle_root / "fleet_mac.py").is_file()
                and all("fleet_mac.read_marker" in (bundle_root / name).read_text(encoding="utf-8")
                        for name in ("cliproxy_watchdog.py", "fleet_monitor.py")))
    except (OSError, ValueError):
        return False


def bundle_supports_leases(bundle_root):
    try:
        return ("def read_leases(" in (bundle_root / "fleet_mac.py").read_text(encoding="utf-8")
                and "fleet_mac.read_leases" in (bundle_root / "cliproxy_watchdog.py").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


def pause(root, port, health, args, *, settings_path, bundle_root):
    if (args.break_lease or args.break_reason is not None) and not (
            args.break_lease and args.break_reason and args.break_reason.strip()):
        raise MacError("--break-lease and a nonempty --break-reason must be used together")
    check_pause(root, port, health, args, settings_path)
    containers = None
    state = engine_state()
    already_paused = state == ENGINE_DOWN and read_marker(root) is not None and not args.break_lease
    if state != ENGINE_DOWN:
        # An engine that did not answer docker info in time is treated as running.
        result = run([*DESKTOP, "ps", "--format", "{{.Names}}"], 10)
        if result.returncode:
            raise MacError("Could not list running Docker Desktop containers")
        containers = sorted(result.stdout.split())
        print("Quitting Docker Desktop stops all running containers:")
        for name in containers or ["(none)"]:
            print(f"  {name}")
    if not args.yes and not already_paused:
        prompt = ("Pause Mac fallback while Docker is down?" if containers is None else "Quit Docker Desktop?")
        try:
            answer = input(prompt + " [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Pause cancelled.")
            return 1

    with transition_lock(root):
        state = engine_state()
        if state == ENGINE_UNKNOWN:
            raise MacError("Could not tell whether the Docker engine is running. Run mac pause again")
        running = state == ENGINE_UP
        marker = read_marker(root) or {}
        if not running and marker and not args.break_lease:
            check_pause_leases(root, args.break_lease)
            print("Mac is already paused. Docker engine is down.")
            return 0
        if running and containers is None:
            raise MacError("Docker engine started during preflight. Run mac pause again to review its containers")
        if not running and containers is not None:
            raise MacError("Docker engine stopped during preflight. Run mac pause again")
        if not bundle_supports_pause(bundle_root):
            message = "Ship the bundle first so the Mac watchdog and monitor honor the pause marker"
            if not args.force:
                raise MacError(f"Pause refused. {message}")
            print(f"Warning: {message}.")
        print("Rechecking Will and session routes before pausing.")
        # Acquire uses this same lock, so a successful recheck covers the stop.
        leases = check_pause(root, port, health, args, settings_path)
        current_policy = docker_restart_policy() if running else None
        previous_policy = marker.get("restart_policy", current_policy)
        broken = marker.get("broken_leases", [])
        # Persist the original policy before changing Docker, even if stopping fails.
        write_marker(root, args.reason, containers, restart_policy=previous_policy, broken_leases=broken)
        if current_policy is not None:
            update_restart_policy("no")
        if leases:
            broken = [*broken, *break_leases(root, leases, args.break_reason, containers)]
            write_marker(root, args.reason, containers, restart_policy=previous_policy, broken_leases=broken)
        if not running:
            print("Mac paused. Docker engine was already down.")
            return 0
        try:
            desktop("stop")
            if not wait_for(lambda: engine_state() == ENGINE_DOWN, DOCKER_TIMEOUT):
                raise MacError("Docker engine is still up")
        except MacError as error:
            raise MacError(f"{error}. The pause marker remains in place") from None
        except (OSError, subprocess.TimeoutExpired):
            raise MacError("Could not verify Docker stopped. The pause marker remains in place") from None
    print("Mac paused. Docker engine is down.")
    return 0


def resume(root, health, *, yes_leases=False):
    with transition_lock(root):
        leases = live_leases(root)
        for lease in leases:
            print(lease_description(lease) + ".")
        if leases and not yes_leases:
            raise MacError("Resume refused because Docker has live leases. Pass --yes-leases to enable Mac recovery")
        marker = read_marker(root) or {}
        state = engine_state()
        if state == ENGINE_DOWN:
            desktop("start", "--detach")
        # A slow engine ("unknown") is already starting or running, so it gets the wait but no second start.
        if state != ENGINE_UP and not wait_for(engine_up, ENGINE_WAIT):
            raise MacError("Docker engine did not come up. Any pause marker remains in place")
        if "restart_policy" in marker:
            # Updating the policy needs the engine. Keep the proxy watchdog paused until it is restored.
            update_restart_policy(marker["restart_policy"])
        marker_path(root).unlink(missing_ok=True)
    if not wait_for(lambda: health("m5") == "ok", HEALTH_WAIT):
        raise MacError("Docker engine is up and the watchdog is enabled. Mac proxy is still unhealthy")
    print("Mac resumed. Docker engine is up and the Mac proxy is healthy.")
    return 0


def lease_command(root, args, bundle_root, will_docker_up):
    if args.lease_command == "acquire":
        lease = acquire_lease(root, bundle_root, thread=args.thread, project=args.project,
                              purpose=args.purpose, why_mac=args.why_mac, hours=args.hours, approved=args.approved)
        print(lease_description(lease, date=True) + ".")
        if will_docker_up():
            print("Docker work defaults to Will; this lease is a Mac failover.")
    elif args.lease_command == "renew":
        lease = renew_lease(root, args.id, args.hours)
        print(lease_description(lease, date=True) + ".")
    elif args.lease_command == "release":
        released_last_while_paused = release_lease(root, args.id)
        print(f"Released Docker lease {args.id}.")
        if released_last_while_paused:
            print("No live leases. Docker is still running while the Mac is paused; "
                  "quit it with `mac pause` when your work is done.")
    else:
        leases = read_leases(root)
        if not leases:
            print("No Docker leases.")
        for lease in leases:
            if lease["live"]:
                print(lease_description(lease, date=True) + ".")
            else:
                print_expired_lease(lease, details=True)
    return 0


def command(args, host, health, *, settings_path, will_docker_up):
    root = Path(host["instance_root"])
    try:
        if sys.platform != "darwin" or not root.is_dir():
            raise MacError("Run this command on the Mac with the configured M5 instance root")
        if args.mac_command == "pause":
            return pause(root, host["port"], health, args, settings_path=settings_path,
                         bundle_root=Path(host["bundle_root"]))
        if args.mac_command == "resume":
            return resume(root, health, yes_leases=args.yes_leases)
        if args.mac_command == "lease":
            return lease_command(root, args, Path(host["bundle_root"]), will_docker_up)
        marker = read_marker(root)
        print(f"Mac paused since {marker['paused_at']}." if marker is not None else "Mac is not paused.")
        state = engine_state()
        print(f"Docker engine: {state}")
        leases = read_leases(root)
        if marker is not None and state == ENGINE_UP and not any(lease["live"] for lease in leases):
            print("Docker is running while paused with no live lease. Its owner may still need it: "
                  "check /Users/rever/omxlapi/out/coordination/THREADS.md and the running containers "
                  "before stopping it. mac pause stops every container.")
        for lease in leases:
            if not lease["live"]:
                print_expired_lease(lease)
            elif marker is not None and state == ENGINE_UP and not lease["unknown"]:
                print(f"Docker is running for lease {lease['id']} ({lease['project']}, thread {lease['thread']}, "
                      f"\"{lease['purpose']}\") until {pacific(lease['expires_at'])}. "
                      "The Mac CLIProxy fallback stays paused.")
            else:
                print(lease_description(lease) + ".")
        print(f"Mac proxy: {health('m5')}")
        print(f"Will proxy: {health('will')}")
        return 0
    except MacError as error:
        print(f"{error}.", file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired):
        print("Could not complete the Mac operation. Check Docker and the pause marker with mac status.",
              file=sys.stderr)
    return 1
