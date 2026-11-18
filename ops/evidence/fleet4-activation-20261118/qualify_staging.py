"""Credential-free staging adapted from fleet.1 qualify_staging.py."""
import argparse
import datetime
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


parser = argparse.ArgumentParser()
parser.add_argument("--arch", required=True, choices=["amd64", "arm64"])
args = parser.parse_args()
arch = args.arch
root = Path(__file__).resolve().parent
build = json.loads((root / (arch + "-build.json")).read_text())
manifest = json.loads((root / "source/ops/fleet-release.json").read_text())
tag = "cliproxy-fleet:" + build["version"]
image = build[arch + "_image"]["id"]
revision = build["source_commit"]
name = "cliproxy-fleet4-" + arch + "-stage"
plugins = root / "plugins"
pin = manifest["cursor_plugin"]["libraries"][arch]
plugin = plugins / "linux" / arch / pin["filename"]
assert sorted(p.name for p in plugin.parent.iterdir()) == [pin["filename"]]

# This public value is valid only within this disposable, network-none test.
probe_token = "fleet4-isolated-synthetic-probe"
env = dict(os.environ, MANAGEMENT_PASSWORD=probe_token, DO_NOT_TRACK="1", GOTELEMETRY="off")
result = {
    "arch": arch, "image": tag, "image_id": image, "started_utc": now(),
    "failures": [], "checks": {}, "requests": [],
    "limitations": ["No upstream model inference or quota fetch. No production credentials or mounts."],
}


def sha(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, capture_output=True, timeout=15, **kwargs).stdout


def exists():
    return subprocess.run(
        ["docker", "container", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


def check(label, condition):
    result["checks"][label] = bool(condition)
    if not condition:
        result["failures"].append(label)


def request(endpoint, token=None):
    auth = "" if token is None else "Authorization: Bearer " + token + "\r\n"
    wire = (f"GET {endpoint} HTTP/1.0\r\nHost: 127.0.0.1\r\n{auth}Connection: close\r\n\r\n").encode()
    response = docker("exec", "-i", name, "bash", "-ec", 'exec 3<>"/dev/tcp/127.0.0.1/$1"; cat >&3; cat <&3',
                      "stage-probe", str(port), input=wire)
    head, body = response.split(b"\r\n\r\n", 1)
    status = int(head.splitlines()[0].split()[1])
    headers = dict(line.decode().lower().split(": ", 1) for line in head.splitlines()[1:] if b": " in line)
    result["requests"].append({"method": "GET", "endpoint": endpoint, "status": status})
    if status != 200:
        raise ValueError(f"{endpoint} returned HTTP {status}")
    return json.loads(body), headers


class HealthRelay(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path != "/healthz":
            self.send_error(404)
            return
        try:
            health, _ = request("/healthz")
        except (ValueError, OSError, subprocess.SubprocessError):
            self.send_error(503)
            return
        body = json.dumps(health).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


assert not exists(), "Staging name already in use"
assert docker("image", "inspect", "--format", "{{.Id}}", tag).decode().strip() == image
owned = False
relay = HTTPServer(("127.0.0.1", 0), HealthRelay)
port = relay.server_port
assert port != 8317
thread = threading.Thread(target=relay.serve_forever, daemon=True)
thread.start()

try:
    with tempfile.TemporaryDirectory(prefix=arch + "-staging-", dir=root) as scratch:
        config = Path(scratch) / "synthetic-config.yaml"
        config.write_text(f"""config-version: 8
server:
  host: "127.0.0.1"
  port: {port}
  kv-store:
    path: "/stage-kv/kv.db"
management:
  allow-remote: false
  disable-control-panel: true
  disable-auto-update-panel: true
oauth:
  auth-dir: "/stage-empty-auth"
routing:
  strategy: "quota-aware"
  session-affinity: false
plugins:
  enabled: true
  dir: "/stage-plugins"
  configs:
    auth-cursor:
      enabled: true
observability:
  logs:
    debug: false
    logging-to-file: false
    request-log: false
  usage:
    usage-statistics-enabled: false
""")
        config.chmod(0o600)
        result["plugin_file_sha256"] = {plugin.name: sha(plugin)}
        if sha(plugin) != pin["sha256"]:
            raise ValueError("Staging plugin does not match fleet pin")
        command = [
            "run", "-d", "--pull=never", "--name", name, "--network", "none",
            "-e", "MANAGEMENT_PASSWORD", "-e", "DO_NOT_TRACK", "-e", "GOTELEMETRY",
            "--mount", f"type=bind,source={config},target=/stage-config.yaml,readonly",
            "--mount", f"type=bind,source={plugins},target=/stage-plugins,readonly",
            "--tmpfs", "/stage-empty-auth:rw,nosuid,noexec,size=16m",
            "--tmpfs", "/stage-kv:rw,nosuid,noexec,size=16m",
            image, "/CLIProxyAPI/CLIProxyAPI", "--config", "/stage-config.yaml", "--local-model",
        ]
        result["command"] = ["docker", *command]
        docker(*command, env=env)
        owned = True
        for attempt in range(60):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                if response.status == 200:
                    health = json.loads(response.read())
                    break
                if attempt == 59:
                    raise ValueError(f"Staging loopback health returned HTTP {response.status}")
            finally:
                connection.close()
            time.sleep(0.25)

        plugin_state, headers = request("/v8/management/plugins", probe_token)
        loaded_config, _ = request("/v0/management/config", probe_token)
        credentials, _ = request("/v8/management/credentials", probe_token)
        strategy, _ = request("/v0/management/routing/strategy", probe_token)
        info = json.loads(docker("container", "inspect", name))[0]
        runtime_hash = docker("exec", name, "sha256sum", f"/stage-plugins/linux/{arch}/{plugin.name}").decode().split()[0]
        runtime_binary_hash = docker("exec", name, "sha256sum", "/CLIProxyAPI/CLIProxyAPI").decode().split()[0]
        docker("exec", name, "test", "-s", "/stage-kv/kv.db")
        auth_files = docker("exec", name, "find", "/stage-empty-auth", "-type", "f", "-print")
        listeners = {
            line.split()[1]
            for path in ("/proc/net/tcp", "/proc/net/tcp6")
            for line in docker("exec", name, "cat", path).decode().splitlines()[1:]
            if line.split()[3] == "0A" and line.split()[1].endswith(f":{port:04X}")
        }
        cursor = next(p for p in plugin_state["plugins"] if p["id"] == "auth-cursor")
        result.update(
            health=health, health_http=200, host_health_relay=f"127.0.0.1:{port}",
            container_listener=f"127.0.0.1:{port}", version=headers.get("x-cpa-version"),
            binary_commit=headers.get("x-cpa-commit"),
            revision=info["Config"]["Labels"].get("org.opencontainers.image.revision"),
            registered_plugins=[{"id": cursor["id"], "registered": cursor["registered"],
                                 "version": cursor["metadata"]["version"],
                                 "effective_enabled": cursor["effective_enabled"]}],
            runtime_plugin_sha256=runtime_hash, runtime_binary_sha256=runtime_binary_hash,
            auth_regular_file_count=len(auth_files.splitlines()), bbolt_initialized_on_separate_tmpfs=True,
            usage_statistics_enabled=loaded_config.get("usage-statistics-enabled"),
            do_not_track="DO_NOT_TRACK=1" in info["Config"]["Env"],
            gotelemetry_off="GOTELEMETRY=off" in info["Config"]["Env"],
            credential_count=len(credentials["files"] or []),
            network_mode=info["HostConfig"]["NetworkMode"],
            published_ports=info["HostConfig"]["PortBindings"],
            mounts=[{"type": m["Type"], "destination": m["Destination"], "writable": m["RW"]} for m in info["Mounts"]],
            tmpfs_auth="/stage-empty-auth" in info["HostConfig"]["Tmpfs"],
            loaded_routing=loaded_config["routing"],
        )
        check("loopback_health", health.get("status") == "ok" and listeners == {f"0100007F:{port:04X}"})
        check("version_header", result["version"] == build["version"])
        check("source_label_and_binary_commit", result["revision"] == result["binary_commit"] == revision)
        check("immutable_image", info["Image"] == image and runtime_binary_hash == build["binaries"][arch])
        check("cursor_plugin_loaded", plugin_state["plugins_enabled"] and cursor["registered"]
              and cursor["effective_enabled"] and cursor["metadata"]["version"] == manifest["cursor_plugin"]["version"])
        check("plugin_library_hash", sha(plugin) == runtime_hash == pin["sha256"])
        check("analytics_and_usage_off", result["do_not_track"] and result["gotelemetry_off"]
              and result["usage_statistics_enabled"] is False)
        check("no_credentials", result["credential_count"] == 0 and not auth_files and result["tmpfs_auth"])
        check("isolated_mounts_and_network", result["network_mode"] == "none" and not result["published_ports"]
              and {m["Source"] for m in info["Mounts"] if m["Type"] == "bind"} == {str(config), str(plugins)}
              and all(not m["RW"] for m in info["Mounts"] if m["Type"] == "bind"))
        check("staging_routing", strategy["strategy"] == result["loaded_routing"]["strategy"] == "quota-aware"
              and result["loaded_routing"].get("session-affinity", False) is False)
except Exception as error:
    result["failures"].append(str(error).replace(probe_token, "[synthetic]"))
finally:
    relay.shutdown()
    relay.server_close()
    thread.join()
    if owned:
        try:
            logs = subprocess.run(["docker", "logs", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=15, check=True).stdout.decode(errors="replace")
            (root / (arch + "-startup.log")).write_text(logs.replace(probe_token, "[synthetic]"))
        finally:
            docker("container", "rm", "-f", name)
    check("staging_container_removed", not exists())
    result["finished_utc"] = now()
    (root / (arch + "-qualification.json")).write_text(json.dumps(result, indent=2) + "\n")

print(f"{arch} staging: " + (", ".join(result["failures"]) or "passed"))
raise SystemExit(bool(result["failures"]))
