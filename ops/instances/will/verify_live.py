#!/usr/bin/env python3
"""Verify Will's live CLIProxy instance without exposing credential content."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


INSTANCE_DIR = Path(__file__).resolve().parent
DEFAULT_TARGET = "will.local"
SAFE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")

REMOTE_PROBE = r'''set -euo pipefail
root=/home/andrew/cliproxy
service=cli-proxy-api
cd "$root"

compose=$(
  docker compose config --format json |
    jq -c '.services["cli-proxy-api"] |
      {image,pull_policy,restart,ports,volumes,healthcheck,stop_grace_period,logging}'
)
container=$(
  docker inspect "$service" |
    jq -c '.[0] | {
      image_ref: .Config.Image,
      image_id: .Image,
      state: .State.Status,
      health: (.State.Health.Status // "none"),
      restart_policy: .HostConfig.RestartPolicy.Name,
      ports: .HostConfig.PortBindings,
      mounts: [.Mounts[] | {
        type: .Type,
        source: .Source,
        target: .Destination
      }]
    }'
)

config_file="$root/config.yaml"
request_retry=$(awk -F: '$1 == "request-retry" {gsub(/[[:space:]\"'\''#]/, "", $2); print $2}' "$config_file")
max_retry_credentials=$(awk -F: '$1 == "max-retry-credentials" {gsub(/[[:space:]\"'\''#]/, "", $2); print $2}' "$config_file")
max_retry_interval=$(awk -F: '$1 == "max-retry-interval" {gsub(/[[:space:]\"'\''#]/, "", $2); print $2}' "$config_file")
routing_strategy=$(
  awk '
    /^routing:[[:space:]]*$/ { in_routing = 1; next }
    in_routing && /^[^[:space:]]/ { in_routing = 0 }
    in_routing && /^[[:space:]]+strategy:/ {
      value = $0
      sub(/^[^:]*:[[:space:]]*/, "", value)
      gsub(/[[:space:]\"'\''#]/, "", value)
      print value
    }
  ' "$config_file"
)
config_mode=$(stat -c '%a' "$config_file")
config_owner=$(stat -c '%U:%G' "$config_file")
host_sha256=$(sha256sum "$config_file" | awk '{print $1}')
container_sha256=$(docker exec "$service" sha256sum /CLIProxyAPI/config.yaml | awk '{print $1}')
listeners=$(
  ss -H -ltn |
    awk '$4 ~ /:(8317|8085|1455|54545|51121|11451)$/ {print $4}' |
    sort -u |
    jq -Rsc 'split("\n") | map(select(length > 0))'
)
health_status=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8317/healthz)

jq -n \
  --argjson compose "$compose" \
  --argjson container "$container" \
  --arg request_retry "$request_retry" \
  --arg max_retry_credentials "$max_retry_credentials" \
  --arg max_retry_interval "$max_retry_interval" \
  --arg routing_strategy "$routing_strategy" \
  --arg config_mode "$config_mode" \
  --arg config_owner "$config_owner" \
  --arg host_sha256 "$host_sha256" \
  --arg container_sha256 "$container_sha256" \
  --argjson listeners "$listeners" \
  --argjson health_status "$health_status" \
  '{
    compose: $compose,
    container: $container,
    config: {
      "request-retry": $request_retry,
      "max-retry-credentials": $max_retry_credentials,
      "max-retry-interval": $max_retry_interval,
      "routing.strategy": $routing_strategy,
      mode: $config_mode,
      owner: $config_owner,
      host_sha256: $host_sha256,
      container_sha256: $container_sha256
    },
    listeners: $listeners,
    health_status: $health_status
  }'
'''


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"unsupported policy document: {path.name}")
    return data


def load_contracts() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        _load_json(INSTANCE_DIR / "workload-contract.json"),
        _load_json(INSTANCE_DIR / "config-policy.json"),
    )


def _expected_ports(workload: dict[str, Any]) -> set[int]:
    return {
        exposure["port"]
        for exposure in workload["expected_lan_exposure"]
        if exposure["protocol"] == "tcp" and exposure["scope"] == "loopback"
    }


def evaluate_observation(
    contract: dict[str, Any],
    policy: dict[str, Any],
    observation: dict[str, Any],
) -> list[str]:
    """Return stable, secret-free drift codes for one sanitized observation."""
    findings: set[str] = set()
    workload = contract["workload"]
    runtime = workload["runtime"]
    compose = observation.get("compose", {})
    container = observation.get("container", {})
    config = observation.get("config", {})
    ports = _expected_ports(workload)

    if compose.get("image") != runtime["image"]:
        findings.add("compose_image_drift")
    compose_policy = policy["compose_policy"]
    if compose.get("pull_policy") != compose_policy["pull_policy"]:
        findings.add("compose_pull_policy_drift")
    if compose.get("restart") != compose_policy["restart"]:
        findings.add("compose_restart_policy_drift")
    if compose.get("stop_grace_period") != compose_policy["stop_grace_period"]:
        findings.add("compose_stop_grace_period_drift")
    logging = compose.get("logging", {})
    if (
        logging.get("driver") != compose_policy["logging_driver"]
        or logging.get("options", {}).get("max-size")
        != compose_policy["logging_max_size"]
        or logging.get("options", {}).get("max-file")
        != compose_policy["logging_max_file"]
    ):
        findings.add("compose_logging_policy_drift")
    if not isinstance(compose.get("healthcheck"), dict):
        findings.add("compose_healthcheck_missing")

    expected_compose_ports = {
        ("127.0.0.1", port, str(port), "tcp") for port in ports
    }
    observed_compose_ports: set[tuple[Any, Any, Any, Any]] = set()
    for entry in compose.get("ports", []):
        if isinstance(entry, dict):
            observed_compose_ports.add(
                (
                    entry.get("host_ip"),
                    entry.get("target"),
                    entry.get("published"),
                    entry.get("protocol"),
                )
            )
    if observed_compose_ports != expected_compose_ports:
        findings.add("compose_port_exposure_drift")

    required_mounts = {
        (entry["source"], entry["target"])
        for entry in policy["required_mounts"]
    }
    compose_mounts = {
        (entry.get("source"), entry.get("target"))
        for entry in compose.get("volumes", [])
        if isinstance(entry, dict) and entry.get("type") == "bind"
    }
    container_mounts = {
        (entry.get("source"), entry.get("target"))
        for entry in container.get("mounts", [])
        if isinstance(entry, dict) and entry.get("type") == "bind"
    }
    if compose_mounts != required_mounts:
        findings.add("compose_mount_drift")
    if container_mounts != required_mounts:
        findings.add("container_mount_drift")

    if container.get("image_ref") != runtime["image"]:
        findings.add("container_image_drift")
    expected_image_id = "sha256:" + runtime["image"].rsplit("sha256:", 1)[-1]
    if container.get("image_id") != expected_image_id:
        findings.add("container_image_id_drift")
    if container.get("state") != "running":
        findings.add("container_not_running")
    if container.get("health") != "healthy":
        findings.add("container_not_healthy")
    if container.get("restart_policy") != runtime["restart_policy"]:
        findings.add("container_restart_policy_drift")

    expected_bindings = {
        f"{port}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]
        for port in ports
    }
    if container.get("ports") != expected_bindings:
        findings.add("container_port_exposure_drift")
    expected_listeners = {f"127.0.0.1:{port}" for port in ports}
    if set(observation.get("listeners", [])) != expected_listeners:
        findings.add("listener_exposure_drift")

    for key, value in policy["required_config"].items():
        if config.get(key) != str(value):
            findings.add("config_policy_drift")
    if config.get("mode") != policy["config_mode"]:
        findings.add("config_mode_drift")
    if config.get("owner") != policy["config_owner"]:
        findings.add("config_owner_drift")
    if (
        not config.get("host_sha256")
        or config.get("host_sha256") != config.get("container_sha256")
    ):
        findings.add("config_mount_identity_drift")
    if observation.get("health_status") != workload["health"]["http"]["expected_status"]:
        findings.add("healthz_drift")

    return sorted(findings)


def collect_observation(
    target: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if not SAFE_TARGET.fullmatch(target):
        raise ValueError("target must be a hostname from trusted local configuration")
    result = runner(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            target,
            "/bin/bash",
            "-s",
        ],
        input=REMOTE_PROBE,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("remote observation failed")
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise ValueError("remote observation is not an object")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify Will CLIProxy runtime, loopback exposure, and config policy."
    )
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contract, policy = load_contracts()
        observation = collect_observation(args.target)
        findings = evaluate_observation(contract, policy, observation)
    except Exception as error:
        payload = {
            "schema_version": 1,
            "target": args.target,
            "status": "blocked",
            "findings": ["collection_failed"],
            "error_type": type(error).__name__,
        }
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"{args.target}: blocked (collection_failed)")
        return 2

    payload = {
        "schema_version": 1,
        "target": args.target,
        "status": "drift" if findings else "ok",
        "findings": findings,
    }
    if args.as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{args.target}: {payload['status']}")
        for finding in findings:
            print(f"  {finding}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
