import io
import json
import logging
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

from ops.cliproxy_watchdog import (
    CommandResult,
    ProbeResult,
    Watchdog,
    WatchdogConfig,
    docker_socket_from_environment,
)


class FakeWatchdog(Watchdog):
    def __init__(
        self,
        config: WatchdogConfig,
        *,
        now: float,
        engine: list[bool],
        health: list[bool],
        probes: list[ProbeResult],
    ) -> None:
        super().__init__(config, logger=logging.getLogger("watchdog-test"), clock=lambda: now)
        self.engine_results = deque(engine)
        self.health_results = deque(health)
        self.probe_results = deque(probes)
        self.desktop_restarts = 0
        self.compose_repairs = 0
        self.container_restarts = 0
        self.auth_ok = True

    @staticmethod
    def _next(values: deque, fallback):
        if len(values) > 1:
            return values.popleft()
        if values:
            return values[0]
        return fallback

    def engine_ok(self) -> bool:
        return bool(self._next(self.engine_results, False))

    def health_ok(self) -> bool:
        return bool(self._next(self.health_results, False))

    def outbound_probe(self) -> ProbeResult:
        return self._next(
            self.probe_results,
            ProbeResult(dns_failures=self.config.probe_hosts),
        )

    def restart_docker_desktop(self) -> bool:
        self.desktop_restarts += 1
        return True

    def compose_up(self) -> bool:
        self.compose_repairs += 1
        return True

    def restart_container(self) -> bool:
        self.container_restarts += 1
        return True

    def wait_for_engine(self) -> bool:
        return self.engine_ok()

    def wait_for_health(self) -> bool:
        return self.health_ok()

    def log_auth_health(self) -> bool:
        return self.auth_ok


class WatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_path = self.root / "runtime" / "state.json"
        self.config = WatchdogConfig(
            root=self.root,
            state_path=self.state_path,
            docker_socket=self.root / "docker.sock",
            compose_repair_cooldown=120,
            container_restart_cooldown=120,
        )
        self.ok_probe = ProbeResult()
        commands = patch.object(
            Watchdog, "run_command", side_effect=AssertionError("Unexpected real command")
        )
        commands.start()
        self.addCleanup(commands.stop)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_failed_engine_probe_cannot_restart_a_healthy_proxy(self) -> None:
        watchdog = FakeWatchdog(
            self.config,
            now=1_000,
            engine=[False, True],
            health=[True],
            probes=[self.ok_probe],
        )

        result = watchdog.run_once()

        self.assertEqual(result, 1)
        self.assertEqual(watchdog.desktop_restarts, 0)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("last_engine_restart", state)

    def test_first_combined_failure_does_not_restart_desktop(self) -> None:
        watchdog = FakeWatchdog(
            self.config,
            now=1_000,
            engine=[False],
            health=[False],
            probes=[self.ok_probe],
        )

        self.assertEqual(watchdog.run_once(), 1)
        self.assertEqual(watchdog.desktop_restarts, 0)

    def test_engine_restart_is_rate_limited(self) -> None:
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text(
            json.dumps({
                "last_engine_restart": 100,
                "engine_consecutive_failures": 2,
                "last_engine_failure": 900,
            }), encoding="utf-8"
        )
        watchdog = FakeWatchdog(
            self.config,
            now=1_000,
            engine=[False],
            health=[False],
            probes=[self.ok_probe],
        )

        result = watchdog.run_once()

        self.assertEqual(result, 1)
        self.assertEqual(watchdog.desktop_restarts, 0)

    def test_three_combined_failures_restart_then_recover(self) -> None:
        for now in (1_000, 1_120):
            watchdog = FakeWatchdog(
                self.config, now=now, engine=[False], health=[False], probes=[]
            )
            self.assertEqual(watchdog.run_once(), 1)
            self.assertEqual(watchdog.desktop_restarts, 0)

        watchdog = FakeWatchdog(
            self.config,
            now=1_240,
            engine=[False, False, True],
            health=[False, False, True],
            probes=[self.ok_probe],
        )
        self.assertEqual(watchdog.run_once(), 0)
        self.assertEqual(watchdog.desktop_restarts, 1)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["last_engine_restart"], 1_240)
        self.assertEqual(state["engine_consecutive_failures"], 0)

    def test_recovery_before_final_confirmation_prevents_restart(self) -> None:
        for engine, health in (([False, True], [False]), ([False], [False, True])):
            with self.subTest(engine=engine, health=health):
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps({
                    "engine_consecutive_failures": 2, "last_engine_failure": 900
                }))
                watchdog = FakeWatchdog(
                    self.config, now=1_000, engine=engine, health=health, probes=[]
                )
                self.assertEqual(watchdog.run_once(), 1)
                self.assertEqual(watchdog.desktop_restarts, 0)
                self.assertEqual(json.loads(self.state_path.read_text())["engine_consecutive_failures"], 0)

    def test_old_or_future_failure_history_does_not_count(self) -> None:
        for previous in (100, 1_100):
            with self.subTest(previous=previous):
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps({
                    "engine_consecutive_failures": 2, "last_engine_failure": previous
                }))
                watchdog = FakeWatchdog(
                    self.config, now=1_000, engine=[False], health=[False], probes=[]
                )
                self.assertEqual(watchdog.run_once(), 1)
                self.assertEqual(watchdog.desktop_restarts, 0)
                self.assertEqual(json.loads(self.state_path.read_text())["engine_consecutive_failures"], 1)

    def test_healthy_engine_or_proxy_resets_failure_history(self) -> None:
        for engine in (True, False):
            with self.subTest(engine=engine):
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps({
                    "engine_consecutive_failures": 2, "last_engine_failure": 900
                }))
                watchdog = FakeWatchdog(
                    self.config, now=1_000, engine=[engine], health=[True], probes=[self.ok_probe]
                )
                watchdog.run_once()
                self.assertEqual(watchdog.desktop_restarts, 0)
                state = json.loads(self.state_path.read_text())
                self.assertEqual(state["engine_consecutive_failures"], 0)
                self.assertNotIn("last_engine_failure", state)

    def test_check_only_never_changes_state_or_runs_recovery(self) -> None:
        original = json.dumps({
            "engine_consecutive_failures": 2,
            "last_engine_failure": 900,
            "outbound_consecutive_failures": 2,
        })
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text(original)
        for engine, health, probe in (
            (False, False, self.ok_probe),
            (False, True, self.ok_probe),
            (True, False, self.ok_probe),
            (True, True, self.ok_probe),
            (True, True, ProbeResult(dns_failures=self.config.probe_hosts)),
        ):
            with self.subTest(engine=engine, health=health, probe=probe):
                watchdog = FakeWatchdog(
                    self.config, now=1_000, engine=[engine], health=[health], probes=[probe]
                )
                watchdog.run_once(check_only=True)
                self.assertEqual(self.state_path.read_text(), original)
                self.assertEqual(
                    (watchdog.desktop_restarts, watchdog.container_restarts, watchdog.compose_repairs),
                    (0, 0, 0),
                )

    def test_health_failure_runs_scoped_compose_repair(self) -> None:
        watchdog = FakeWatchdog(
            self.config,
            now=2_000,
            engine=[True],
            health=[False, True],
            probes=[self.ok_probe],
        )

        result = watchdog.run_once()

        self.assertEqual(result, 0)
        self.assertEqual(watchdog.compose_repairs, 1)
        self.assertEqual(watchdog.desktop_restarts, 0)

    def test_first_outbound_failure_alerts_without_restart(self) -> None:
        failed = ProbeResult(dns_failures=self.config.probe_hosts)
        watchdog = FakeWatchdog(
            self.config,
            now=3_000,
            engine=[True],
            health=[True],
            probes=[failed],
        )

        result = watchdog.run_once()

        self.assertEqual(result, 1)
        self.assertEqual(watchdog.container_restarts, 0)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["outbound_consecutive_failures"], 1)

    def test_second_total_dns_failure_restarts_only_container_first(self) -> None:
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text(
            json.dumps({"outbound_consecutive_failures": 1, "dns_consecutive_failures": 1}), encoding="utf-8"
        )
        failed = ProbeResult(dns_failures=self.config.probe_hosts)
        watchdog = FakeWatchdog(
            self.config,
            now=4_000,
            engine=[True],
            health=[True, True],
            probes=[failed, self.ok_probe],
        )

        result = watchdog.run_once()

        self.assertEqual(result, 0)
        self.assertEqual(watchdog.container_restarts, 1)
        self.assertEqual(watchdog.desktop_restarts, 0)

    def test_persistent_dns_failure_does_not_restart_healthy_engine(self) -> None:
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text(
            json.dumps({"outbound_consecutive_failures": 1, "dns_consecutive_failures": 1}), encoding="utf-8"
        )
        failed = ProbeResult(dns_failures=self.config.probe_hosts)
        watchdog = FakeWatchdog(
            self.config,
            now=4_500,
            engine=[True, True],
            health=[True, True, True],
            probes=[failed, failed, self.ok_probe],
        )

        result = watchdog.run_once()

        self.assertEqual(result, 1)
        self.assertEqual(watchdog.container_restarts, 1)
        self.assertEqual(watchdog.desktop_restarts, 0)
        self.assertEqual(watchdog.compose_repairs, 0)

    def test_repeated_cli_probe_errors_do_not_restart_anything(self) -> None:
        failed = ProbeResult(probe_errors=("chatgpt.com:dns:rc=124",))
        for now in (1_000, 1_120, 1_240):
            watchdog = FakeWatchdog(
                self.config, now=now, engine=[True], health=[True], probes=[failed]
            )
            self.assertEqual(watchdog.run_once(), 1)
            self.assertEqual(
                (watchdog.desktop_restarts, watchdog.container_restarts, watchdog.compose_repairs),
                (0, 0, 0),
            )

    def test_cli_error_does_not_count_toward_repeated_dns_failure(self) -> None:
        for now, probe in (
            (1_000, ProbeResult(probe_errors=("chatgpt.com:dns:rc=124",))),
            (1_120, ProbeResult(dns_failures=self.config.probe_hosts)),
        ):
            watchdog = FakeWatchdog(
                self.config, now=now, engine=[True], health=[True], probes=[probe]
            )
            self.assertEqual(watchdog.run_once(), 1)
            self.assertEqual(watchdog.container_restarts, 0)
            self.assertEqual(watchdog.desktop_restarts, 0)

    def test_fully_healthy_run_performs_no_mutation(self) -> None:
        watchdog = FakeWatchdog(
            self.config,
            now=5_000,
            engine=[True],
            health=[True],
            probes=[self.ok_probe],
        )

        result = watchdog.run_once()

        self.assertEqual(result, 0)
        self.assertEqual(watchdog.desktop_restarts, 0)
        self.assertEqual(watchdog.compose_repairs, 0)
        self.assertEqual(watchdog.container_restarts, 0)

    def test_skip_auth_isolates_infrastructure_check(self) -> None:
        watchdog = FakeWatchdog(
            self.config,
            now=6_000,
            engine=[True],
            health=[True],
            probes=[self.ok_probe],
        )
        watchdog.auth_ok = False

        self.assertEqual(watchdog.run_once(skip_auth=True), 0)
        self.assertEqual(watchdog.run_once(skip_auth=False), 1)

    def test_auth_warnings_do_not_fail_watchdog_exit(self) -> None:
        class WarningOnlyWatchdog(FakeWatchdog):
            def log_auth_health(self) -> bool:  # type: ignore[override]
                self.logger.warning("OAuth health alert: expires_soon")
                return True

        watchdog = WarningOnlyWatchdog(
            self.config,
            now=7_000,
            engine=[True],
            health=[True],
            probes=[self.ok_probe],
        )
        self.assertEqual(watchdog.run_once(), 0)


class SocketProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        config = WatchdogConfig(
            root=Path("/unused"), state_path=Path("/unused/state.json"),
            docker_socket=Path("/configured/docker.sock"),
        )
        self.watchdog = Watchdog(config, logger=logging.getLogger("socket-probe-test"))
        self.socket = MagicMock()
        sockets = patch("ops.cliproxy_watchdog.socket.socket", return_value=self.socket)
        sockets.start()
        self.addCleanup(sockets.stop)
        commands = patch.object(self.watchdog, "run_command", side_effect=AssertionError("Unexpected CLI call"))
        commands.start()
        self.addCleanup(commands.stop)

    def test_ping_uses_configured_socket_without_docker_cli(self) -> None:
        self.socket.makefile.return_value = io.BytesIO(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"
        )
        self.assertTrue(self.watchdog.engine_ok())
        self.socket.connect.assert_called_once_with("/configured/docker.sock")
        self.socket.settimeout.assert_called_once_with(3)
        self.assertIn(b"GET /_ping HTTP/1.1", self.socket.sendall.call_args.args[0])
        self.socket.close.assert_called()

    def test_error_or_invalid_ping_response_is_not_healthy(self) -> None:
        for payload in (
            b"HTTP/1.1 503 Unavailable\r\nContent-Length: 2\r\n\r\nOK",
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nBAD",
            b"not an HTTP response\r\n",
        ):
            with self.subTest(payload=payload):
                self.socket.makefile.return_value = io.BytesIO(payload)
                self.assertFalse(self.watchdog.engine_ok())
        self.socket.close.assert_called()

    def test_socket_timeout_logs_bounded_diagnostics_without_error_text(self) -> None:
        self.socket.connect.side_effect = TimeoutError("do-not-log-this-payload")
        with patch("ops.cliproxy_watchdog.time.monotonic", side_effect=[100.0, 103.0]):
            with self.assertLogs("socket-probe-test", level="WARNING") as logs:
                self.assertFalse(self.watchdog.engine_ok())
        message = "\n".join(logs.output)
        self.assertIn("error=TimeoutError", message)
        self.assertIn("elapsed=3.000s", message)
        self.assertNotIn("do-not-log-this-payload", message)
        self.socket.close.assert_called()

    def test_cli_execution_errors_are_not_classified_as_dns_failure(self) -> None:
        for code in (1, 124, 125, 127):
            with self.subTest(code=code):
                with patch.object(self.watchdog, "run_command", return_value=CommandResult(code)):
                    probe = self.watchdog.outbound_probe()
                self.assertFalse(probe.ok)
                self.assertFalse(probe.all_dns_failed(self.watchdog.config.probe_hosts))
                self.assertFalse(probe.dns_failures)
                self.assertEqual(len(probe.probe_errors), 3)

    def test_getent_not_found_is_a_dns_failure(self) -> None:
        with patch.object(self.watchdog, "run_command", return_value=CommandResult(2)):
            probe = self.watchdog.outbound_probe()
        self.assertTrue(probe.all_dns_failed(self.watchdog.config.probe_hosts))
        self.assertFalse(probe.probe_errors)


class SocketConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.config_dir = Path(temp_dir.name)
        self.environment = {"DOCKER_CONFIG": str(self.config_dir)}

    def test_active_context_socket_is_read_without_running_docker(self) -> None:
        (self.config_dir / "config.json").write_text(json.dumps({"currentContext": "desktop-linux"}))
        context_dir = self.config_dir / "contexts" / "meta" / "context-id"
        context_dir.mkdir(parents=True)
        (context_dir / "meta.json").write_text(json.dumps({
            "Name": "desktop-linux", "Endpoints": {"docker": {"Host": "unix:///configured/docker.sock"}}
        }))
        self.assertEqual(docker_socket_from_environment(self.environment), Path("/configured/docker.sock"))

    def test_explicit_socket_and_host_overrides(self) -> None:
        self.environment["DOCKER_HOST"] = "unix:///host/docker.sock"
        self.assertEqual(docker_socket_from_environment(self.environment), Path("/host/docker.sock"))
        self.environment["CLIPROXY_DOCKER_SOCKET"] = "/override/docker.sock"
        self.assertEqual(docker_socket_from_environment(self.environment), Path("/override/docker.sock"))

    def test_explicit_default_context_takes_priority_over_host(self) -> None:
        self.environment.update(DOCKER_CONTEXT="default", DOCKER_HOST="tcp://remote:2375")
        self.assertEqual(docker_socket_from_environment(self.environment), Path("/var/run/docker.sock"))

    def test_missing_config_uses_standard_local_socket(self) -> None:
        self.assertEqual(docker_socket_from_environment(self.environment), Path("/var/run/docker.sock"))

    def test_remote_or_unknown_context_cannot_trigger_local_recovery(self) -> None:
        for extra in ({"DOCKER_HOST": "tcp://remote:2375"}, {"DOCKER_CONTEXT": "unknown"}):
            with self.subTest(extra=extra), self.assertRaises((ValueError, RuntimeError)):
                docker_socket_from_environment({**self.environment, **extra})


if __name__ == "__main__":
    unittest.main()
