#!/usr/bin/env python3
"""Recover Docker Desktop and CLIProxy from engine, health, or DNS failures."""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import logging.handlers
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

try:
    from ops.auth_health import format_text, scan_auth_directory
except ModuleNotFoundError:  # Direct execution from ops/.
    from auth_health import format_text, scan_auth_directory


DEFAULT_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def docker_socket_from_environment(environment: Mapping[str, str]) -> Path:
    override = environment.get("CLIPROXY_DOCKER_SOCKET")
    if override:
        socket_path = Path(override).expanduser()
    else:
        config_dir = Path(environment.get("DOCKER_CONFIG", "~/.docker")).expanduser()
        context = environment.get("DOCKER_CONTEXT")
        endpoint = environment.get("DOCKER_HOST") if not context else None
        if not context and not endpoint:
            try:
                config = json.loads((config_dir / "config.json").read_text())
            except FileNotFoundError:
                config = {}
            context = config.get("currentContext") or "default"
        if not endpoint:
            if context == "default":
                endpoint = "unix:///var/run/docker.sock"
            else:
                for path in sorted((config_dir / "contexts" / "meta").glob("*/meta.json")):
                    metadata = json.loads(path.read_text())
                    if metadata.get("Name") == context:
                        endpoint = metadata.get("Endpoints", {}).get("docker", {}).get("Host")
                        break
        if not endpoint:
            raise RuntimeError("Configured Docker context has no endpoint")
        address = urlsplit(endpoint)
        if address.scheme != "unix" or address.netloc or address.query or address.fragment:
            raise ValueError("Desktop recovery requires a local Unix Docker endpoint")
        socket_path = Path(unquote(address.path))
    if not socket_path.is_absolute():
        raise ValueError("Docker socket path must be absolute")
    return socket_path


class DockerSocketConnection(http.client.HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("localhost", timeout=3)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


@dataclass(frozen=True)
class ProbeResult:
    dns_failures: tuple[str, ...] = ()
    tls_failures: tuple[str, ...] = ()
    probe_errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.dns_failures and not self.tls_failures and not self.probe_errors

    def all_dns_failed(self, hosts: Sequence[str]) -> bool:
        return not self.probe_errors and bool(hosts) and set(self.dns_failures) == set(hosts)


@dataclass(frozen=True)
class WatchdogConfig:
    root: Path
    state_path: Path
    docker: str = "/usr/local/bin/docker"
    docker_socket: Path = Path("/var/run/docker.sock")
    curl: str = "/usr/bin/curl"
    container: str = "cli-proxy-api"
    service: str = "cli-proxy-api"
    health_url: str = "http://127.0.0.1:8317/healthz"
    probe_hosts: tuple[str, ...] = (
        "chatgpt.com",
        "platform.claude.com",
        "auth.kimi.com",
    )
    engine_restart_cooldown: int = 1_800
    engine_failure_threshold: int = 3
    engine_failure_window: int = 300
    compose_repair_cooldown: int = 120
    container_restart_cooldown: int = 120
    engine_start_timeout: int = 120
    health_start_timeout: int = 90
    auth_warn_days: int = 3
    compose_pull_policy: str | None = None

    @classmethod
    def from_environment(cls, root: Path | None = None) -> "WatchdogConfig":
        project_root = root or Path(__file__).resolve().parent.parent
        search_path = os.environ.get("PATH", DEFAULT_PATH)
        docker = os.environ.get("CLIPROXY_DOCKER") or shutil.which(
            "docker", path=search_path
        )
        curl = os.environ.get("CLIPROXY_CURL") or shutil.which(
            "curl", path=search_path
        )
        if not docker:
            raise RuntimeError("docker CLI not found in watchdog PATH")
        if not curl:
            raise RuntimeError("curl not found in watchdog PATH")
        compose_pull_policy = os.environ.get("CLIPROXY_COMPOSE_PULL_POLICY") or None
        if compose_pull_policy not in {None, "always", "missing", "never"}:
            raise ValueError(
                "CLIPROXY_COMPOSE_PULL_POLICY must be always, missing, or never"
            )
        runtime_dir = project_root / "ops" / "runtime"
        return cls(
            root=project_root,
            state_path=runtime_dir / "state.json",
            docker=docker,
            docker_socket=docker_socket_from_environment(os.environ),
            curl=curl,
            engine_restart_cooldown=int(
                os.environ.get("CLIPROXY_ENGINE_RESTART_COOLDOWN", "1800")
            ),
            compose_repair_cooldown=int(
                os.environ.get("CLIPROXY_COMPOSE_REPAIR_COOLDOWN", "120")
            ),
            container_restart_cooldown=int(
                os.environ.get("CLIPROXY_CONTAINER_RESTART_COOLDOWN", "120")
            ),
            auth_warn_days=int(os.environ.get("CLIPROXY_AUTH_WARN_DAYS", "3")),
            compose_pull_policy=compose_pull_policy,
        )


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Watchdog:
    def __init__(
        self,
        config: WatchdogConfig,
        *,
        logger: logging.Logger,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.logger = logger
        self.clock = clock

    def run_command(
        self,
        args: Sequence[str],
        *,
        timeout: int,
        cwd: Path | None = None,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(args),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
                env={**os.environ, "PATH": os.environ.get("PATH", DEFAULT_PATH)},
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, stderr=f"timed out after {timeout}s")
        except OSError as error:
            return CommandResult(127, stderr=str(error))
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def engine_ok(self) -> bool:
        started = time.monotonic()
        connection = DockerSocketConnection(self.config.docker_socket)
        try:
            connection.request("GET", "/_ping")
            response = connection.getresponse()
            if response.status == 200 and response.read(3) == b"OK":
                return True
            self.logger.warning(
                "Docker socket ping rejected response status=%d elapsed=%.3fs",
                response.status,
                time.monotonic() - started,
            )
        except (OSError, http.client.HTTPException) as error:
            self.logger.warning(
                "Docker socket ping failed error=%s errno=%s elapsed=%.3fs",
                type(error).__name__,
                getattr(error, "errno", None),
                time.monotonic() - started,
            )
        finally:
            connection.close()
        return False

    def health_ok(self) -> bool:
        result = self.run_command(
            [
                self.config.curl,
                "-fsS",
                "--max-time",
                "3",
                self.config.health_url,
            ],
            timeout=5,
        )
        return result.returncode == 0

    def outbound_probe(self) -> ProbeResult:
        dns_failures: list[str] = []
        tls_failures: list[str] = []
        probe_errors: list[str] = []
        for host in self.config.probe_hosts:
            dns = self.run_command(
                [
                    self.config.docker,
                    "exec",
                    self.config.container,
                    "getent",
                    "ahostsv4",
                    host,
                ],
                timeout=6,
            )
            if dns.returncode == 2:  # GNU getent: key not found; rc=1 is an invocation error.
                dns_failures.append(host)
                continue
            if dns.returncode != 0 or not dns.stdout.strip():
                probe_errors.append(f"{host}:dns:rc={dns.returncode}")
                continue
            tls = self.run_command(
                [
                    self.config.docker,
                    "exec",
                    self.config.container,
                    "openssl",
                    "s_client",
                    "-brief",
                    "-verify_return_error",
                    "-connect",
                    f"{host}:443",
                    "-servername",
                    host,
                ],
                timeout=12,
            )
            if tls.returncode != 0:
                tls_failures.append(host)
        return ProbeResult(tuple(dns_failures), tuple(tls_failures), tuple(probe_errors))

    def restart_docker_desktop(self) -> bool:
        self.logger.warning(
            "recovery action: docker desktop stop --force --timeout 30"
        )
        stop = self.run_command(
            [self.config.docker, "desktop", "stop", "--force", "--timeout", "30"],
            timeout=45,
        )
        if stop.returncode != 0:
            self.logger.warning("Docker Desktop stop returned rc=%d", stop.returncode)
        self.logger.warning("recovery action: docker desktop start --detach")
        start = self.run_command(
            [self.config.docker, "desktop", "start", "--detach"], timeout=45
        )
        if start.returncode != 0:
            self.logger.error("Docker Desktop start returned rc=%d", start.returncode)
            return False
        return True

    def compose_up(self) -> bool:
        command = [
            self.config.docker,
            "compose",
            "up",
            "-d",
            "--no-deps",
        ]
        if self.config.compose_pull_policy:
            command.extend(["--pull", self.config.compose_pull_policy])
        command.append(self.config.service)
        self.logger.warning(
            "recovery action: %s",
            " ".join(command),
        )
        result = self.run_command(
            command,
            timeout=150,
            cwd=self.config.root,
        )
        if result.returncode != 0:
            self.logger.error("Compose repair returned rc=%d", result.returncode)
        return result.returncode == 0

    def restart_container(self) -> bool:
        self.logger.warning("recovery action: docker compose restart %s", self.config.service)
        result = self.run_command(
            [self.config.docker, "compose", "restart", self.config.service],
            timeout=120,
            cwd=self.config.root,
        )
        if result.returncode != 0:
            self.logger.error("Container restart returned rc=%d", result.returncode)
        return result.returncode == 0

    def _wait_until(self, check: Callable[[], bool], timeout: int) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if check():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(2)

    def wait_for_engine(self) -> bool:
        return self._wait_until(self.engine_ok, self.config.engine_start_timeout)

    def wait_for_health(self) -> bool:
        return self._wait_until(self.health_ok, self.config.health_start_timeout)

    def load_state(self) -> dict[str, float | int]:
        try:
            data = json.loads(self.config.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            self.logger.warning("Ignoring unreadable watchdog state: %s", error)
            return {}
        return data if isinstance(data, dict) else {}

    def save_state(self, state: dict[str, float | int]) -> None:
        parent = self.config.state_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="state.", suffix=".tmp", dir=parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True)
                handle.write("\n")
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self.config.state_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def cooldown_elapsed(
        state: dict[str, float | int], key: str, now: float, cooldown: int
    ) -> bool:
        previous = float(state.get(key, 0))
        return previous <= 0 or now - previous >= cooldown

    def log_auth_health(self) -> bool:
        statuses = scan_auth_directory(
            self.config.root / "auths",
            self.config.root / "logs" / "main.log",
            warn_days=self.config.auth_warn_days,
        )
        warnings = [status for status in statuses if status.level == "warning"]
        critical = [status for status in statuses if status.level == "critical"]
        if warnings:
            self.logger.warning("OAuth health alert:\n%s", format_text(warnings))
        if critical:
            self.logger.error("OAuth health critical:\n%s", format_text(critical))
            return False
        # Warnings stay in the log; only critical auth fails the process.
        # Non-zero exits make launchd throttle StartInterval recovery.
        return True

    def _restart_engine_if_allowed(
        self, state: dict[str, float | int], now: float, *, check_only: bool
    ) -> bool:
        if check_only:
            return False
        if int(state.get("engine_consecutive_failures", 0)) < self.config.engine_failure_threshold:
            return False
        if not self.cooldown_elapsed(
            state,
            "last_engine_restart",
            now,
            self.config.engine_restart_cooldown,
        ):
            self.logger.error("Docker engine restart suppressed by cooldown")
            return False
        if self.engine_ok() or self.health_ok():
            state["engine_consecutive_failures"] = 0
            state.pop("last_engine_failure", None)
            self.logger.warning("Docker or CLIProxy recovered; suppressing Desktop restart")
            return False
        state["last_engine_restart"] = now
        self.save_state(state)
        return self.restart_docker_desktop() and self.wait_for_engine()

    def run_once(
        self, *, check_only: bool = False, skip_auth: bool = False
    ) -> int:
        now = self.clock()
        state = self.load_state()

        engine_ok = self.engine_ok()
        health_ok = self.health_ok()
        if not engine_ok:
            if health_ok:
                state["engine_consecutive_failures"] = 0
                state.pop("last_engine_failure", None)
                self.logger.warning("Docker socket probe failed but CLIProxy is healthy; no recovery")
                if not check_only:
                    self.save_state(state)
                return 1
            previous = float(state.get("last_engine_failure", 0))
            consecutive = int(state.get("engine_consecutive_failures", 0))
            if not 0 <= now - previous <= self.config.engine_failure_window:
                consecutive = 0
            state["engine_consecutive_failures"] = consecutive + 1
            state["last_engine_failure"] = now
            self.logger.error(
                "Docker socket and CLIProxy health failed count=%d required=%d",
                consecutive + 1,
                self.config.engine_failure_threshold,
            )
            if not self._restart_engine_if_allowed(state, now, check_only=check_only):
                if not check_only:
                    self.save_state(state)
                return 1
            self.logger.info("Docker engine recovered")
            health_ok = self.health_ok()
        state["engine_consecutive_failures"] = 0
        state.pop("last_engine_failure", None)

        if not health_ok:
            self.logger.error("CLIProxy health check failed: %s", self.config.health_url)
            if check_only or not self.cooldown_elapsed(
                state,
                "last_compose_repair",
                now,
                self.config.compose_repair_cooldown,
            ):
                if not check_only:
                    self.save_state(state)
                return 1
            state["last_compose_repair"] = now
            self.save_state(state)
            if not self.compose_up() or not self.wait_for_health():
                self.save_state(state)
                return 1
            self.logger.info("CLIProxy health recovered")

        probe = self.outbound_probe()
        dns_consecutive = (
            int(state.get("dns_consecutive_failures", 0)) + 1
            if probe.all_dns_failed(self.config.probe_hosts)
            else 0
        )
        state["dns_consecutive_failures"] = dns_consecutive
        if probe.ok:
            state["outbound_consecutive_failures"] = 0
        else:
            consecutive = int(state.get("outbound_consecutive_failures", 0)) + 1
            state["outbound_consecutive_failures"] = consecutive
            self.logger.error(
                "Outbound probe failed count=%d dns=%s tls=%s probe_errors=%s",
                consecutive,
                ",".join(probe.dns_failures) or "none",
                ",".join(probe.tls_failures) or "none",
                ",".join(probe.probe_errors) or "none",
            )
            should_restart_container = (
                not check_only
                and dns_consecutive >= 2
                and probe.all_dns_failed(self.config.probe_hosts)
                and self.cooldown_elapsed(
                    state,
                    "last_container_restart",
                    now,
                    self.config.container_restart_cooldown,
                )
            )
            if should_restart_container:
                state["last_container_restart"] = now
                self.save_state(state)
                if self.restart_container() and self.wait_for_health():
                    probe = self.outbound_probe()
                    if probe.ok:
                        state["outbound_consecutive_failures"] = 0
                        state["dns_consecutive_failures"] = 0
                        self.logger.info("Container DNS/TLS recovered")
                    elif probe.all_dns_failed(self.config.probe_hosts):
                        self.logger.error(
                            "Container restart did not restore DNS; Desktop recovery requires "
                            "repeated socket and service failures"
                        )
                    else:
                        state["dns_consecutive_failures"] = 0

        auth_ok = True if skip_auth else self.log_auth_health()
        if not check_only:
            self.save_state(state)
        outbound_ok = int(state.get("outbound_consecutive_failures", 0)) == 0
        self.logger.info(
            "Watchdog check complete engine=ok service=ok outbound=%s auth=%s",
            "ok" if outbound_ok else "failed",
            "skipped" if skip_auth else "ok" if auth_ok else "failed",
        )
        return 0 if outbound_ok and auth_ok else 1


def build_logger(path: Path, *, verbose: bool = False) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cliproxy-watchdog")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(handler)
    if verbose:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(console)
    logger.propagate = False
    return logger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only", action="store_true",
        help="Observe without recovery actions or state writes",
    )
    parser.add_argument(
        "--skip-auth",
        action="store_true",
        help="Check only engine, service health, DNS, and TLS",
    )
    parser.add_argument("--verbose", action="store_true", help="Also write status to stderr")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    logger = build_logger(root / "ops" / "runtime" / "watchdog.log", verbose=args.verbose)
    try:
        config = WatchdogConfig.from_environment(root)
        return Watchdog(config, logger=logger).run_once(
            check_only=args.check_only, skip_auth=args.skip_auth
        )
    except Exception:
        logger.exception("Unhandled watchdog failure")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
