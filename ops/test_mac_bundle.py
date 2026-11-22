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
        self.assertIn("ops/fleet_mac.py", allowlist)
        for job, script, seconds, args in (
            ("watchdog", "cliproxy_watchdog.py", 120, []),
            ("log-archive", "archive_logs.py", 300, ["--archive-only"]),
        ):
            label = "com.rever.cliproxy-" + job
            with self.subTest(label=label):
                self.assertIn(f"ops/{label}.plist", allowlist)
                self.assertIn(f"ops/{script}", allowlist)
                with (ops / (label + ".plist")).open("rb") as stream:
                    plist = plistlib.load(stream)
                self.assertEqual(plist["Label"], label)
                self.assertEqual(plist["ProgramArguments"],
                                 ["/opt/homebrew/bin/python3", host["bundle_root"] + "/" + script, *args])
                self.assertEqual(plist["WorkingDirectory"], host["instance_root"])
                self.assertEqual(plist["EnvironmentVariables"]["CLIPROXY_ROOT"], host["instance_root"])
                self.assertTrue(plist["RunAtLoad"])
                self.assertEqual(plist["StartInterval"], seconds)

    @unittest.skipUnless(sys.platform == "darwin", "needs macOS plutil and zsh")
    def test_installer_checks_both_scripts_before_touching_launch_agents(self):
        ops = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = root / "install_watchdog.sh"
            installer.write_text((ops / installer.name).read_text().replace("/bin/launchctl", "/usr/bin/false"))
            existing = root / "watchdog.py"
            existing.touch()
            for job, script in [("watchdog", existing), ("log-archive", root / "missing.py")]:
                label = f"com.rever.cliproxy-{job}"
                plist = {"Label": label, "ProgramArguments": [sys.executable, str(script)]}
                (root / (label + ".plist")).write_bytes(plistlib.dumps(plist))
            result = subprocess.run(
                ["/bin/zsh", "-f", str(installer), str(root)], env={"HOME": str(root)},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 1, result)
            self.assertIn("Cannot install com.rever.cliproxy-log-archive", result.stderr)
            self.assertFalse((root / "Library").exists())

    @unittest.skipUnless(sys.platform == "darwin", "needs macOS plutil and zsh")
    def test_installer_requires_archive_only_before_installing_either_job(self):
        ops = Path(__file__).resolve().parent
        for args, allowed in (
            ([], False), (["--prune"], False), (["--archive-only\n"], False),
            (["--archive-only", "--prune"], False),
            (["--prune", "--archive-only"], False), (["--verbose", "--archive-only"], True),
        ):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                installer = root / "install_watchdog.sh"
                installer.write_text((ops / installer.name).read_text().replace("/bin/launchctl", "/usr/bin/true"))
                for job, arguments in (("watchdog", []), ("log-archive", args)):
                    label = f"com.rever.cliproxy-{job}"
                    script = root / f"{job}.py"
                    script.touch()
                    plist = {"Label": label, "ProgramArguments": [sys.executable, str(script), *arguments]}
                    (root / f"{label}.plist").write_bytes(plistlib.dumps(plist))

                result = subprocess.run(
                    ["/bin/zsh", "-f", str(installer), str(root)], env={"HOME": str(root)},
                    capture_output=True, text=True, check=False,
                )

                self.assertEqual(result.returncode, 0 if allowed else 1, result)
                self.assertEqual((root / "Library/LaunchAgents").exists(), allowed)
                if not allowed:
                    self.assertIn("requires --archive-only and forbids --prune", result.stderr)

    @unittest.skipUnless(sys.platform == "darwin", "needs macOS plutil and zsh")
    def test_installer_uses_validated_bytes_after_bundle_switch(self):
        ops = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tmp = root / "tmp"
            tmp.mkdir()
            installer = root / "install_watchdog.sh"
            installer.write_text((ops / installer.name).read_text()
                                 .replace("/bin/launchctl", "/usr/bin/true")
                                 .replace("/usr/bin/install", '"$HOME/switch-and-install"'))
            switch = root / "switch-and-install"
            switch.write_text('#!/bin/zsh\n/bin/ln -sfn "$HOME/older" "$HOME/current"\n'
                              'exec /usr/bin/install "$@"\n')
            switch.chmod(0o700)
            for generation in ("newer", "older"):
                bundle = root / generation
                bundle.mkdir()
                for job in ("watchdog", "log-archive"):
                    label = f"com.rever.cliproxy-{job}"
                    script = root / f"{job}.py"
                    script.touch()
                    arguments = ["--archive-only"] if job == "log-archive" and generation == "newer" else []
                    plist = {"Label": label, "ProgramArguments": [sys.executable, str(script), *arguments],
                             "RunAtLoad": True, "Generation": generation}
                    (bundle / f"{label}.plist").write_bytes(plistlib.dumps(plist))
            current = root / "current"
            current.symlink_to(root / "newer")

            result = subprocess.run(
                ["/bin/zsh", "-f", str(installer), str(current)],
                env={"HOME": str(root), "TMPDIR": str(tmp)}, capture_output=True, text=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result)
            self.assertEqual(current.resolve(), (root / "older").resolve())
            for original in (root / "newer").iterdir():
                self.assertEqual((root / "Library/LaunchAgents" / original.name).read_bytes(), original.read_bytes())
            self.assertEqual(list(tmp.iterdir()), [])


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
        for name in ("cliproxy_watchdog.py", "auth_health.py", "fleet_mac.py", "instance_root.py"):
            (live / name).write_bytes((ops / name).read_bytes())
        result = subprocess.run([sys.executable, "-E", "-B", str(live / "cliproxy_watchdog.py"), "--help"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--record-compose-activation", result.stdout)

    def test_bundle_aliases_require_instance_root_before_touching_state(self):
        ops = Path(__file__).resolve().parent
        live = self.root / "published/ops"
        release = live / ".fleet-releases/fixture/ops"
        release.mkdir(parents=True)
        (live / ".fleet-current").symlink_to(release.relative_to(live))
        jobs = {
            "cliproxy_watchdog.py": ["--check-only", "--skip-auth"],
            "archive_logs.py": ["--check-only", "--mount-point", str(self.root / "not-mounted")],
            "auth_health.py": ["--json"],
        }
        for name in (*jobs, "fleet_mac.py", "instance_root.py"):
            (release / name).write_bytes((ops / name).read_bytes())
            (live / name).symlink_to(Path(".fleet-current") / name)
        fake_environment = {"CLIPROXY_DOCKER": "/usr/bin/false", "CLIPROXY_CURL": "/usr/bin/false",
                            "CLIPROXY_DOCKER_SOCKET": str(self.root / "missing.sock")}
        for name, args in jobs.items():
            with self.subTest(name=name):
                result = subprocess.run([sys.executable, "-E", "-B", str(live / name), *args],
                                        env=fake_environment, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 2, result)
                self.assertIn("CLIPROXY_ROOT is required", result.stderr)
        self.assertFalse((release / "runtime").exists())

        auth = self.root / "auths/fixture.json"
        auth.parent.mkdir()
        auth.write_text('{"type":"fixture","expired":"2099-01-01T00:00:00Z"}')
        auth.chmod(0o600)
        result = subprocess.run([sys.executable, "-E", "-B", str(live / "auth_health.py"), "--json"],
                                env={"CLIPROXY_ROOT": str(self.root)},
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result)
        self.assertEqual([entry["provider"] for entry in json.loads(result.stdout)], ["fixture"])
