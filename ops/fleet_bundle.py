"""Export and publish the managed ops files without replacing the instance's ops directory."""

import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile


MANIFEST = "bundle-manifest.json"
CURRENT = ".fleet-current"
RELEASES = ".fleet-releases"
BUNDLE_MARKER = "ops/fleet_bundle.py"
LAUNCHER = "ops/claude_account_launcher.mjs"
JOBS = {
    "com.rever.cliproxy-watchdog": "cliproxy_watchdog.py",
    "com.rever.cliproxy-log-archive": "archive_logs.py",
}


class BundleError(Exception):
    pass


def bundle_paths(paths):
    if not paths or len({name.casefold() for name in paths}) != len(paths):
        raise BundleError("Empty or duplicate bundle path list")
    for name in paths:
        parts = name.split("/")
        if (not re.fullmatch(r"ops/[A-Za-z0-9_./-]+", name)
                or any(not part or part.startswith(".") for part in parts)
                or parts[1] in ("runtime", MANIFEST)
                or any(part in ("auths", "config.yaml") for part in parts)):
            raise BundleError("Invalid or private path in bundle allowlist")
    return paths


def git(repo, *args):
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(repo), *args],
        capture_output=True, check=False,
    )
    if result.returncode:
        raise BundleError("Git could not read the requested commit or bundle file")
    return result.stdout


def file_record(data, mode):
    return {"sha256": hashlib.sha256(data).hexdigest(), "mode": mode}


def export_bundle(repo, revision, *, require_clean=False):
    if require_clean and git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise BundleError("Ship requires a clean source checkout")
    if revision is not None and not re.fullmatch(r"[0-9a-fA-F]{7,64}", revision):
        raise BundleError("Use a known commit SHA")
    commit = git(repo, "rev-parse", "--verify", (revision or "HEAD") + "^{commit}").decode().strip()
    if revision is not None and not commit.startswith(revision.lower()):
        raise BundleError("Use a known commit SHA")
    allowlist = "ops/fleet/bundle.txt"
    # Read Git blobs directly so checkout filters and archive attributes cannot change bytes.
    tree = {}
    for entry in git(repo, "ls-tree", "-rz", commit, "--", "ops/").split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        mode, kind, oid = meta.split()
        tree[os.fsdecode(name)] = (mode, kind, oid)

    def blob(name):
        mode, kind, oid = tree.get(name, (None, None, None))
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise BundleError("Bundle entries must be regular files in the requested commit")
        return git(repo, "cat-file", "blob", oid.decode()), int(mode, 8) & 0o777

    names = bundle_paths(blob(allowlist)[0].decode().splitlines())
    manifest = {"commit": commit, "files": {}}
    contents = {}
    for name in names:
        data, mode = blob(name)
        manifest["files"][name] = file_record(data, mode)
        contents[name] = base64.b64encode(data).decode()
    require_bundle_launcher(names)
    return manifest, contents


def require_bundle_launcher(names):
    if BUNDLE_MARKER not in names or LAUNCHER not in names:
        raise BundleError("Commit does not support the ops bundle launcher. Use the checkout paths for pre-bundle commits.")


def smoke_launcher(stage):
    # Reproduce the public file alias and pointer without moving the active pointer.
    node = shutil.which("node")
    if not node:
        raise BundleError("Node.js is required on the destination to check the bundle launcher")
    aliases = stage / "smoke"
    aliases.mkdir()
    (aliases / CURRENT).symlink_to("../ops")
    name = Path(LAUNCHER).name
    alias = aliases / name
    alias.symlink_to(f"{CURRENT}/{name}")
    marker = "bundle-launcher-smoke"
    try:
        result = subprocess.run(
            [node, str(alias), marker], cwd=aliases, capture_output=True, timeout=10,
            env={"CLIPROXY_CLAUDE_BINARY": "/bin/echo", "CLIPROXY_CLAUDE_ROUTE": "shared-pool",
                 "CLIPROXY_BASE_URL": "http://127.0.0.1:1"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BundleError("Staged bundle launcher smoke test could not run") from None
    if result.returncode != 0 or result.stdout.strip() != marker.encode():
        raise BundleError("Staged bundle launcher did not run the fake Claude binary")
    shutil.rmtree(aliases)


def read_regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise BundleError("Bundle contains a non-regular file")
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


def write_regular(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def relative_name(name):
    return Path(name).relative_to("ops")


def visible_file(root, name):
    relative = relative_name(name)
    for parent in relative.parents:
        if (root / parent).is_symlink():
            raise BundleError("Bundle path has a symlinked parent")
    return root / relative


def current_release(root):
    pointer = root / CURRENT
    if not pointer.is_symlink():
        if pointer.exists():
            raise BundleError("Invalid active bundle pointer")
        return None
    target = Path(os.readlink(pointer))
    if (len(target.parts) != 3 or target.parts[0] != RELEASES
            or not re.fullmatch(r"[A-Za-z0-9_-]+", target.parts[1]) or target.parts[2] != "ops"
            or not (root / target).is_dir() or (root / target).resolve() != root / target):
        raise BundleError("Invalid active bundle pointer")
    return root / target


def observed_file(root, current, name):
    path = visible_file(root, name)
    if path.is_symlink():
        relative = relative_name(name)
        if current is None or os.readlink(path) != os.path.relpath(root / CURRENT / relative, path.parent):
            raise BundleError("Bundle path has an unmanaged symlink")
        path = visible_file(current, name)
    try:
        return read_regular(path)
    except FileNotFoundError:
        return None


def active_manifest(root, current):
    path = root / MANIFEST
    if not path.is_symlink():
        if path.exists():
            raise BundleError("Bundle manifest path is already in use")
        return {}
    if current is None or os.readlink(path) != f"{CURRENT}/{MANIFEST}":
        raise BundleError("Invalid bundle manifest link")
    try:
        result = json.loads(read_regular(current / MANIFEST)[0])
    except FileNotFoundError:
        # First ship may have stopped before publishing its staged files.
        return {}
    if (not isinstance(result, dict) or not isinstance(result.get("files"), dict)
            or not isinstance(result.get("retained"), list)
            or not all(isinstance(name, str) for name in result["retained"])):
        raise BundleError("Invalid active bundle manifest")
    return result


def ship_to(root, manifest, contents, apply):
    root = Path(root)
    if root.is_symlink() or not root.is_absolute():
        raise BundleError("Invalid bundle root")
    root = root.resolve()
    if not apply:
        return publish(root, manifest, contents, False)
    root.mkdir(parents=True, exist_ok=True)
    # Keep a stable lock inode for concurrent or retried ships.
    fd = os.open(root / ".fleet-lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    with os.fdopen(fd, "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return publish(root, manifest, contents, True)


def publish(root, manifest, contents, apply):
    names = bundle_paths(list(manifest["files"]))
    require_bundle_launcher(names)
    current = current_release(root)
    previous = active_manifest(root, current)
    managed = bundle_paths(list(set(names) | set(previous.get("files", {}))
                                | set(previous.get("retained", []))))
    old_files = {name: observed_file(root, current, name) for name in sorted(managed)}
    result = {"commit": manifest["commit"], "unchanged": [], "changed": [], "added": []}
    new_files = {}
    for name in sorted(names):
        record = manifest["files"][name]
        data = base64.b64decode(contents[name], validate=True)
        mode = record["mode"]
        if mode not in (0o644, 0o755) or file_record(data, mode) != record:
            raise BundleError("Transfer does not match bundle manifest")
        new_files[name] = (data, mode)
        old = old_files[name]
        category = "added" if old is None else "unchanged" if old == (data, mode) else "changed"
        result[category].append(name)
    retained = sorted(name for name in old_files if name not in new_files and old_files[name] is not None)
    manifest = dict(manifest, retained=retained)
    result["status"] = "dry-run"
    if not apply:
        return result
    result["status"] = "unchanged"
    if not result["added"] and not result["changed"] and previous == manifest:
        return result

    releases, backups = root / RELEASES, root.parent / "prev"
    for parent in (releases, backups):
        if parent.is_symlink():
            raise BundleError("Unexpected symlink for bundle storage")
        parent.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    stage = Path(tempfile.mkdtemp(prefix=stamp + "-", dir=releases))
    rollback = backups / stage.name
    converted = []
    try:
        candidate = stage / "ops"
        for name in retained:
            write_regular(candidate / relative_name(name), *old_files[name])
        for name, entry in new_files.items():
            write_regular(candidate / relative_name(name), *entry)
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        write_regular(candidate / MANIFEST, manifest_bytes, 0o644)
        smoke_launcher(stage)

        rollback.mkdir(mode=0o700)
        for name, old in old_files.items():
            if old is not None:
                write_regular(rollback / relative_name(name), *old)
        if previous:
            write_regular(rollback / MANIFEST, *read_regular(current / MANIFEST))

        if current is None:
            # The seed has the same layout as a published release.
            seed = stage.with_name(stage.name + "-previous")
            current = seed / "ops"
            current.mkdir(parents=True)
            (root / CURRENT).symlink_to(current.relative_to(root))
        # Introduce aliases against the old release first, preserving every existing
        # file's bytes and mode until the single pointer rename publishes the stage.
        for name in sorted(set(new_files) | set(retained)):
            path = visible_file(root, name)
            if path.is_symlink():
                continue
            old = old_files[name]
            relative = relative_name(name)
            if old is not None:
                write_regular(visible_file(current, name), *old)
            path.parent.mkdir(parents=True, exist_ok=True)
            link = stage / "link"
            link.symlink_to(os.path.relpath(root / CURRENT / relative, path.parent))
            converted.append((path, old))
            os.replace(link, path)
        if not (root / MANIFEST).is_symlink():
            (root / MANIFEST).symlink_to(f"{CURRENT}/{MANIFEST}")
        pointer = stage / "publish"
        pointer.symlink_to(candidate.relative_to(root))
        os.replace(pointer, root / CURRENT)
    except BaseException:
        if current_release(root) != stage / "ops":
            for path, old in reversed(converted):
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    restored = stage / "restore"
                    write_regular(restored, *old)
                    os.replace(restored, path)
            shutil.rmtree(stage)
        raise
    result.update(status="shipped", previous=str(rollback))
    return result


def verify_bundle(host, manifest, launchers):
    root = Path(host["bundle_root"]).resolve()
    current = current_release(root)
    installed = active_manifest(root, current)
    issues = []
    if BUNDLE_MARKER not in manifest["files"]:
        issues.append("P0/bundle-compatibility")
    if any(installed.get(key) != manifest[key] for key in ("commit", "files")):
        issues.append("P0/manifest")
    for name, expected in manifest["files"].items():
        try:
            path = visible_file(root, name)
            actual = observed_file(root, current, name)
            if (not path.resolve(strict=True).is_relative_to(root)
                    or actual is None or file_record(*actual) != expected):
                issues.append("P0/file:" + name)
        except (OSError, BundleError):
            issues.append("P0/file:" + name)

    launcher = root / "claude_account_launcher.mjs"
    count = 5 if host["name"] == "m5" else 1
    if ("ops/" + launcher.name not in manifest["files"]
            or not isinstance(launchers, list) or len(launchers) != count
            or any(not isinstance(path, str) or Path(path) != launcher for path in launchers)):
        issues.append("P0/t3-launchers")
    if host["name"] == "m5":
        home = root.parent.parent
        for label, script in JOBS.items():
            try:
                with (home / "Library/LaunchAgents" / (label + ".plist")).open("rb") as stream:
                    job = plistlib.load(stream)
                if ("ops/" + script not in manifest["files"] or job["Label"] != label
                        or job["ProgramArguments"] != ["/opt/homebrew/bin/python3", str(root / script)]
                        or job["EnvironmentVariables"]["CLIPROXY_ROOT"] != host["instance_root"]
                        or job["WorkingDirectory"] != host["instance_root"]):
                    issues.append("P0/" + label)
            except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
                issues.append("P0/" + label)

        token = home / ".local/bin/cliproxy-codex-token"
        try:
            if token.is_symlink():
                if token.resolve(strict=True) != (root / token.name).resolve(strict=True):
                    raise BundleError("Unexpected token helper target")
                token = token.resolve()
            token_hash = hashlib.sha256(read_regular(token)[0]).hexdigest()
        except (OSError, BundleError):
            token_hash = None
        expected_hash = manifest["files"].get("ops/cliproxy-codex-token", {}).get("sha256")
        if expected_hash is None or token_hash != expected_hash:
            issues.append("P0/token-helper")
    return {"status": "drift" if issues else "ok", "commit": manifest["commit"], "issues": issues}


def on_host(host, operation, *args, runner=subprocess.run):
    if host["ssh"] is None:
        return operation(*args)
    program = Path(__file__).read_text(encoding="utf-8")
    program += "\ntry:\n print(json.dumps(" + operation.__name__ + "(*" + repr(args) + ")))\n"
    program += ("except BundleError as error:\n print(json.dumps({'status':'error','error':str(error)}))\n"
                "except Exception:\n print(json.dumps({'status':'error','error':'Bundle operation failed on host'}))\n")
    result = runner(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
         "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes",
         "--", host["ssh"], "python3", "-I", "-B", "-"],
        input=program, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise BundleError("SSH bundle operation failed")
    response = json.loads(result.stdout)
    if not isinstance(response, dict) or "status" not in response:
        raise BundleError("Invalid SSH bundle result")
    return response
