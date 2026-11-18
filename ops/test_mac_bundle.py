import json
import logging
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ops import archive_logs, cliproxy_watchdog as watchdog


class BundleLaunchAgentsTest(unittest.TestCase):
    def test_bundle_plists_match_portable_fleet_contract(self):
        ops = Path(__file__).resolve().parent
        host = json.loads((ops / "fleet/hosts.json").read_text())["m5"]
        allowlist = (ops / "fleet/bundle.txt").read_text().splitlines()
        for job, script, seconds in (
            ("watchdog", "cliproxy_watchdog.py", 120),
            ("log-archive", "archive_logs.py", 300),
        ):
            label = "com.rever.cliproxy-" + job
            with self.subTest(label=label):
                self.assertIn(f"ops/{label}.plist", allowlist)
                self.assertIn(f"ops/{script}", allowlist)
                with (ops / (label + ".plist")).open("rb") as stream:
                    plist = plistlib.load(stream)
                self.assertEqual(plist["Label"], label)
                self.assertEqual(plist["ProgramArguments"],
                                 ["/opt/homebrew/bin/python3", host["bundle_root"] + "/" + script])
                self.assertEqual(plist["WorkingDirectory"], host["instance_root"])
                self.assertEqual(plist["EnvironmentVariables"]["CLIPROXY_ROOT"], host["instance_root"])
                self.assertTrue(plist["RunAtLoad"])
                self.assertEqual(plist["StartInterval"], seconds)


class BundleJobsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        self.runtime = self.root / "ops/runtime"
        self.runtime.mkdir(parents=True)
        self.logger = logging.getLogger("bundle-watchdog-fixture")
        self.config = watchdog.WatchdogConfig(
            root=self.root, state_path=self.runtime / "state.json", docker="docker", guard_compose=True
        )
        self.now = 10000
        self.job = watchdog.Watchdog(self.config, logger=self.logger, clock=lambda: self.now)

    def test_watchdog_root_override_and_archive_instance_locations(self):
        env = {"CLIPROXY_ROOT": str(self.root), "DOCKER_CONTEXT": "default",
               "CLIPROXY_DOCKER": "fake-docker", "CLIPROXY_CURL": "fake-curl"}
        with patch.dict(os.environ, env, clear=True):
            config = watchdog.WatchdogConfig.from_environment()
            self.assertEqual(config.root, self.root)
            self.assertEqual(config.state_path, self.runtime / "state.json")
            self.assertTrue(config.guard_compose)
            with (patch.object(sys, "argv", ["archive_logs.py", "--check-only"]),
                  patch.object(archive_logs, "build_logger") as logger,
                  patch.object(archive_logs, "LogArchiver") as archiver):
                archiver.return_value.run.return_value.failures = 0
                self.assertEqual(archive_logs.main(), 0)
                self.assertEqual(logger.call_args.args[0], self.runtime / "archive.log")
                self.assertTrue((self.runtime / "archive.lock").exists())
                self.assertEqual(archiver.call_args.args[0].source_dir, self.root / "logs")

    def test_pause_suppresses_all_repair_routes_once_per_cycle_and_expires(self):
        pause = self.runtime / "watchdog.pause"
        pause.touch()
        os.utime(pause, (self.now, self.now))
        routes = [
            (False, False, watchdog.ProbeResult()),
            (True, False, watchdog.ProbeResult()),
            (True, True, watchdog.ProbeResult(dns_failures=self.config.probe_hosts)),
        ]
        for engine, health, outbound in routes:
            self.job.save_state({"engine_consecutive_failures": 2, "last_engine_failure": self.now,
                                 "dns_consecutive_failures": 2})
            with (patch.object(self.job, "engine_ok", return_value=engine),
                  patch.object(self.job, "health_ok", return_value=health),
                  patch.object(self.job, "outbound_probe", return_value=outbound),
                  patch.object(self.job, "run_command") as command,
                  self.assertLogs(self.logger, level="INFO") as logs):
                self.assertEqual(self.job.run_once(skip_auth=True), 1)
            command.assert_not_called()
            self.assertEqual(sum("repairs paused" in line for line in logs.output), 1)
        self.now += watchdog.PAUSE_SECONDS
        with (patch.object(self.job, "engine_ok", return_value=True),
              patch.object(self.job, "health_ok", return_value=False),
              patch.object(self.job, "compose_up", return_value=False) as repair):
            self.job.run_once(skip_auth=True)
        repair.assert_called_once()

    def test_compose_uses_explicit_project_and_matches_activation_before_up(self):
        (self.root / ".env").write_text("CLI_PROXY_IMAGE=fixture:env-pin\n")
        (self.root / "docker-compose.yml").write_text("image: fixture:default\n")
        (self.root / "docker-compose.override.yml").write_text("image: fixture:override-pin\n")
        config_hash = "a" * 64
        resolved_image = "fixture:override-pin"
        running = False

        def fake_docker(argv, **kwargs):
            if argv[1] == "inspect":
                return watchdog.CommandResult(0, " ".join(
                    json.dumps(value) for value in ("fixture:override-pin", config_hash, str(self.root), running)
                ))
            if argv[1:] == ["start", self.config.container]:
                return watchdog.CommandResult(0)
            self.assertEqual(kwargs["cwd"], self.root)
            self.assertNotIn("CLI_PROXY_IMAGE", kwargs["env"])
            self.assertEqual(argv[1:10], [
                "compose", "--project-directory", str(self.root), "--env-file", str(self.root / ".env"),
                "-f", str(self.root / "docker-compose.yml"), "-f", str(self.root / "docker-compose.override.yml")
            ])
            if argv[10:12] == ["config", "--images"]:
                self.assertIn("env-pin", Path(argv[5]).read_text())
                return watchdog.CommandResult(0, resolved_image + "\n")
            if argv[10:12] == ["config", "--hash"]:
                return watchdog.CommandResult(0, "cli-proxy-api " + config_hash + "\n")
            self.assertEqual(argv[10:], ["up", "-d", "--no-deps", "--pull", "never", "--no-build",
                                         "cli-proxy-api"])
            return watchdog.CommandResult(0)

        with (patch.dict(os.environ, {"CLI_PROXY_IMAGE": "fixture:shell-override"}),
              patch.object(self.job, "run_command", side_effect=fake_docker) as run):
            self.assertFalse(self.job.record_compose_activation())
            self.assertFalse(self.job.compose_activation_path.exists())
            running = True
            self.assertTrue(self.job.record_compose_activation())
            self.assertEqual(json.loads(self.job.compose_activation_path.read_text())["image"],
                             "fixture:override-pin")
            self.assertTrue(self.job.compose_up())
            self.assertEqual(run.call_args.args[0][10], "up")
            resolved_image = "fixture:drifted-pin"
            run.reset_mock()
            self.assertTrue(self.job.compose_up())
            self.assertEqual(run.call_args.args[0], ["docker", "start", "cli-proxy-api"])
            resolved_image = "fixture:override-pin"
            (self.root / "docker-compose.override.yml").write_text("changed")
            run.reset_mock()
            self.assertTrue(self.job.compose_up())
            self.assertEqual(run.call_args.args[0], ["docker", "start", "cli-proxy-api"])

    def test_compose_refuses_unexpected_working_directory_without_running_docker(self):
        os.chdir(self.runtime)
        with patch.object(self.job, "run_command") as run:
            self.assertFalse(self.job.compose_up())
            self.assertFalse(self.job.record_compose_activation())
        run.assert_not_called()

    def test_bundled_watchdog_imports_its_dependency_from_the_bundle(self):
        ops = Path(__file__).resolve().parent
        live = self.root / "release/ops"
        live.mkdir(parents=True)
        for name in ("cliproxy_watchdog.py", "auth_health.py"):
            (live / name).write_bytes((ops / name).read_bytes())
        result = subprocess.run([sys.executable, "-E", "-B", str(live / "cliproxy_watchdog.py"), "--help"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--record-compose-activation", result.stdout)
