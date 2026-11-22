from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ops import fleet_bundle as bundle
from ops import fleetctl


class FleetBundleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.destination = self.root / "live/ops"
        self.git("init", "--quiet")
        names = ["ops/task", "ops/fleet/bundle.txt", "ops/nested/second",
                 bundle.BUNDLE_MARKER, bundle.LAUNCHER]
        self.write("ops/fleet/bundle.txt", "\n".join(names) + "\n")
        self.write("ops/nested/second", "old")
        self.write("ops/task", "same")
        self.write("ops/ignored-by-allowlist", "unshipped")
        self.write(bundle.BUNDLE_MARKER, "# Bundle-aware commit")
        launcher = Path(__file__).resolve().parent / Path(bundle.LAUNCHER).name
        self.write(bundle.LAUNCHER, launcher.read_text(), mode=0o755)
        self.first = self.commit()
        self.write("ops/task", "same", mode=0o755)
        self.write("ops/nested/second", "new")
        self.write("ops/third", "added")
        self.write("ops/fleet/bundle.txt", "\n".join([*names, "ops/third"]) + "\n")
        self.second = self.commit()

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-c", "core.filemode=true", "-c", "core.hooksPath=/dev/null",
             "-c", "user.name=Fixture", "-c", "user.email=fixture",
             "-C", str(self.repo), *args],
            stderr=subprocess.PIPE, text=True,
        ).strip()

    def write(self, name, content, mode=0o644):
        file = self.repo / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        file.chmod(mode)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def ship(self, revision=None, apply=True):
        manifest, files = bundle.export_bundle(self.repo, revision, require_clean=True)
        return bundle.ship_to(str(self.destination), manifest, files, apply)

    def test_dry_run_uses_requested_commit_and_writes_nothing(self):
        report = self.ship(self.first[:8], apply=False)
        self.assertEqual(report["commit"], self.first)
        self.assertEqual(report["added"], sorted([
            "ops/fleet/bundle.txt", "ops/nested/second", "ops/task", bundle.BUNDLE_MARKER, bundle.LAUNCHER,
        ]))
        self.assertFalse(self.destination.parent.exists())

    def test_cli_requires_apply_for_writes(self):
        self.write("ops/fleet/hosts.json", json.dumps({"m5": {
            "name": "m5", "ssh": None, "bundle_root": str(self.destination)
        }}))
        commit = self.commit()
        with patch.object(fleetctl, "OPS", self.repo / "ops"):
            base = ["ship", "--host", "m5", "--commit", commit]
            for extra, expected in [([], "dry-run"), (["--dry-run"], "dry-run"),
                                    (["--apply"], "shipped")]:
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(fleetctl.main([*base, *extra]), 0)
                self.assertEqual(json.loads(output.getvalue())["status"], expected)
                self.assertEqual(self.destination.exists(), expected == "shipped")

    def test_dirty_and_unknown_commits_are_refused_before_touching_destination(self):
        with self.assertRaisesRegex(bundle.BundleError, "requested commit"):
            self.ship("1" * 40)
        self.write("ops/untracked", "dirty")
        with self.assertRaisesRegex(bundle.BundleError, "clean source"):
            self.ship(self.first)
        self.assertFalse(self.destination.parent.exists())

    def test_pre_bundle_commit_is_rejected_by_ship_and_p0(self):
        self.write("ops/fleet/hosts.json", json.dumps({"m5": {
            "name": "m5", "ssh": None, "bundle_root": str(self.destination)
        }}))
        self.write("ops/fleet/bundle.txt", f"{bundle.LAUNCHER}\n")
        commit = self.commit()
        commands = [
            ["ship", "--host", "m5", "--commit", commit, "--dry-run"],
            ["ship", "--host", "m5", "--commit", commit, "--apply"],
            ["check", "--host", "m5", "--bundle-commit", commit, "--launcher-paths", "-"],
        ]
        with patch.object(fleetctl, "OPS", self.repo / "ops"):
            for args in commands:
                with self.subTest(args=args), redirect_stdout(io.StringIO()) as output:
                    self.assertNotEqual(fleetctl.main(args), 0)
                    self.assertIn("does not support the ops bundle launcher", output.getvalue())
        self.assertFalse(self.destination.parent.exists())

    def test_launcher_smoke_failure_leaves_initial_files_and_active_release_intact(self):
        self.destination.mkdir(parents=True)
        launcher = self.destination / Path(bundle.LAUNCHER).name
        launcher.write_text("original")
        self.write(bundle.LAUNCHER, "// Exits successfully without launching Claude.", mode=0o755)
        broken = self.commit()
        pointer = self.destination / bundle.CURRENT

        with self.assertRaisesRegex(bundle.BundleError, "did not run the fake Claude binary"):
            self.ship(broken)
        self.assertFalse(pointer.is_symlink())
        self.assertFalse(launcher.is_symlink())
        self.assertEqual(launcher.read_text(), "original")
        self.assertEqual(list((self.destination / bundle.RELEASES).iterdir()), [])

        self.ship(self.first)
        active = os.readlink(pointer)
        releases = set((self.destination / bundle.RELEASES).iterdir())
        manifest = (self.destination / bundle.MANIFEST).read_bytes()
        with self.assertRaisesRegex(bundle.BundleError, "did not run the fake Claude binary"):
            self.ship(broken)
        self.assertEqual(os.readlink(pointer), active)
        self.assertEqual(set((self.destination / bundle.RELEASES).iterdir()), releases)
        self.assertEqual((self.destination / bundle.MANIFEST).read_bytes(), manifest)
        self.assertEqual(launcher.read_bytes(), (self.destination / active / launcher.name).read_bytes())

    def test_ships_committed_bytes_and_modes_and_retains_previous_files(self):
        self.destination.mkdir(parents=True)
        private = self.destination / "runtime"
        private.mkdir()
        state = private / "state"
        state.write_text("unmanaged")
        inode = private.stat().st_ino
        first = self.ship(self.first)
        self.assertEqual(first["status"], "shipped")
        self.assertEqual((self.destination / "nested/second").read_text(), "old")
        self.assertFalse((self.destination / "ignored-by-allowlist").exists())
        second = self.ship()
        self.assertEqual(second["changed"], ["ops/fleet/bundle.txt", "ops/nested/second", "ops/task"])
        self.assertEqual(second["added"], ["ops/third"])
        previous = Path(second["previous"])
        self.assertEqual(previous.parent, self.destination.parent / "prev")
        self.assertEqual((previous / "nested/second").read_text(), "old")
        self.assertEqual((previous / "task").stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.destination / "task").stat().st_mode & 0o777, 0o755)
        manifest = json.loads((self.destination / bundle.MANIFEST).read_text())
        self.assertEqual(manifest["commit"], self.second)
        self.assertEqual(manifest["files"]["ops/task"], bundle.file_record(b"same", 0o755))
        self.assertEqual(self.ship()["unchanged"], sorted(manifest["files"]))
        self.assertEqual(self.ship()["status"], "unchanged")
        self.ship(self.first)
        self.assertEqual((self.destination / "third").read_text(), "added")
        self.assertEqual((self.destination / "nested/second").read_text(), "old")
        self.assertEqual(private.stat().st_ino, inode)
        self.assertEqual(state.read_text(), "unmanaged")

    def test_pointer_is_the_publication_boundary_and_failure_keeps_previous_files(self):
        self.destination.mkdir(parents=True)
        (self.destination / "task").write_text("first unmanaged version")
        replace = os.replace

        def fail_publish(source, destination):
            if destination == self.destination / bundle.CURRENT:
                self.assertEqual((self.destination / "task").read_text(), "first unmanaged version")
                self.assertFalse((self.destination / "nested/second").exists())
                self.assertFalse((self.destination / bundle.MANIFEST).exists())
                raise OSError("fixture publication failure")
            return replace(source, destination)

        with patch.object(bundle.os, "replace", side_effect=fail_publish):
            with self.assertRaisesRegex(OSError, "publication failure"):
                self.ship(self.first)
        self.assertEqual((self.destination / "task").read_text(), "first unmanaged version")
        self.assertFalse((self.destination / "task").is_symlink())
        self.assertEqual(self.ship(self.first)["status"], "shipped")
        self.assertEqual((self.destination / "task").read_text(), "same")

    def test_rejects_private_and_nonregular_git_entries(self):
        self.write("ops/fleet/bundle.txt", "ops/runtime/private\n")
        with self.assertRaisesRegex(bundle.BundleError, "private"):
            self.ship(self.commit())
        self.write("ops/fleet/bundle.txt", "ops/alias\n")
        (self.repo / "ops/alias").symlink_to("task")
        with self.assertRaisesRegex(bundle.BundleError, "regular files"):
            self.ship(self.commit())

    def test_ssh_worker_uses_injected_runner_and_never_touches_other_paths(self):
        host = {"name": "will", "ssh": "will.example.invalid", "bundle_root": str(self.destination)}
        manifest, files = bundle.export_bundle(self.repo, self.first)
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.run([sys.executable, "-I", "-B", "-"], **kwargs)

        for apply, expected in [(False, "dry-run"), (True, "shipped")]:
            report = bundle.on_host(host, bundle.ship_to, host["bundle_root"], manifest, files,
                                    apply, runner=run)
            self.assertEqual(report["status"], expected)
        self.assertEqual(calls[0][-5:], ["will.example.invalid", "python3", "-I", "-B", "-"])
        self.assertEqual((self.destination / "task").read_text(), "same")

    def test_p0_requires_hashes_and_configured_launch_paths(self):
        self.destination = self.root / "cliproxy-live/ops"
        ops = Path(__file__).resolve().parent
        fixture_names = ["ops/fleet/bundle.txt", bundle.BUNDLE_MARKER, bundle.LAUNCHER,
                         "ops/cliproxy-codex-token", *("ops/" + name for name in bundle.JOBS.values()),
                         "ops/" + bundle.MONITOR + ".plist"]
        for name in fixture_names[3:]:
            self.write(name, "fixture " + name, mode=0o755)
        self.write(fixture_names[0], "\n".join(fixture_names) + "\n")
        commit = self.commit()
        self.ship(commit)
        agents = self.root / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        host = {"name": "m5", "bundle_root": str(self.destination),
                "instance_root": str(self.root / "cliproxy")}
        for label, script in bundle.JOBS.items():
            plist = plistlib.loads((ops / (label + ".plist")).read_bytes())
            plist["ProgramArguments"][1] = str(self.destination / script)
            plist["WorkingDirectory"] = host["instance_root"]
            plist["EnvironmentVariables"]["CLIPROXY_ROOT"] = host["instance_root"]
            (agents / (label + ".plist")).write_bytes(plistlib.dumps(plist))
        helper = self.root / ".local/bin/cliproxy-codex-token"
        helper.parent.mkdir(parents=True)
        shutil.copy2(self.destination / helper.name, helper)
        manifest, _ = bundle.export_bundle(self.repo, commit)
        launchers = [str(self.destination / "claude_account_launcher.mjs")] * 5
        # The monitor LaunchAgent is optional until activated.
        self.assertEqual(bundle.verify_bundle(host, manifest, launchers)["issues"], [])
        monitor = agents / (bundle.MONITOR + ".plist")
        shutil.copyfile(self.destination / monitor.name, monitor)
        self.assertEqual(bundle.verify_bundle(host, manifest, launchers)["issues"], [])
        archive_label = "com.rever.cliproxy-log-archive"
        archive_plist = agents / (archive_label + ".plist")
        stale = plistlib.loads(archive_plist.read_bytes())
        self.assertEqual(stale["ProgramArguments"].pop(), "--archive-only")
        archive_plist.write_bytes(plistlib.dumps(stale))
        self.assertEqual(bundle.verify_bundle(host, manifest, launchers)["issues"], ["P0/" + archive_label])
        launchers[0] = str(self.root / "cliproxy/ops/claude_account_launcher.mjs")
        (self.destination / "archive_logs.py").write_text("edited")
        helper.write_text("different")
        monitor.write_text("stale")
        self.assertEqual(set(bundle.verify_bundle(host, manifest, launchers)["issues"]), {
            "P0/t3-launchers", "P0/file:ops/archive_logs.py", "P0/token-helper",
            "P0/" + bundle.MONITOR, "P0/" + archive_label,
        })
