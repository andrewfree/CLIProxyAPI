import json
import logging
import tempfile
import unittest
from collections import deque
from pathlib import Path

from ops.cliproxy_watchdog import ProbeResult, Watchdog, WatchdogConfig


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
            engine_restart_cooldown=300,
            compose_repair_cooldown=120,
            container_restart_cooldown=120,
        )
        self.ok_probe = ProbeResult()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_engine_failure_restarts_desktop_then_continues(self) -> None:
        watchdog = FakeWatchdog(
            self.config,
            now=1_000,
            engine=[False, True],
            health=[True],
            probes=[self.ok_probe],
        )

        result = watchdog.run_once()

        self.assertEqual(result, 0)
        self.assertEqual(watchdog.desktop_restarts, 1)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_engine_restart"], 1_000)

    def test_engine_restart_is_rate_limited(self) -> None:
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text(
            json.dumps({"last_engine_restart": 900}), encoding="utf-8"
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
            json.dumps({"outbound_consecutive_failures": 1}), encoding="utf-8"
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

    def test_persistent_total_dns_failure_escalates_to_desktop_restart(self) -> None:
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text(
            json.dumps({"outbound_consecutive_failures": 1}), encoding="utf-8"
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

        self.assertEqual(result, 0)
        self.assertEqual(watchdog.container_restarts, 1)
        self.assertEqual(watchdog.desktop_restarts, 1)
        self.assertEqual(watchdog.compose_repairs, 1)

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


if __name__ == "__main__":
    unittest.main()
