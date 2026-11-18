"""Native build and local image assembly adapted from fleet.1 and fleet.3."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


VERSION = "v8.0.66-fleet.4"
COMMIT = "4a1d49c3c839d97411a6a29b5daa18e16212c3bf"
TAG = "cliproxy-fleet:" + VERSION


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def image_info(image):
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))[0]
    return {
        "id": info["Id"], "architecture": info["Architecture"], "os": info["Os"],
        "labels": info["Config"].get("Labels") or {}, "size_bytes": info["Size"],
        "descriptor": info.get("Descriptor"), "repo_digests": info.get("RepoDigests") or [],
    }


parser = argparse.ArgumentParser()
parser.add_argument("arch", choices=["arm64", "amd64"])
parser.add_argument("builder")
parser.add_argument("source_sha256")
parser.add_argument("build_date")
args = parser.parse_args()
root = Path(__file__).resolve().parent
source = root / "source"
arch = args.arch
mac = arch == "arm64"
disk = "/System/Volumes/Data" if mac else "/"
min_bytes = 15 * 1024**3 if mac else 150_000_000_000
report_file = root / (arch + "-build.json")
env = dict(os.environ, DO_NOT_TRACK="1", GOTELEMETRY="off", DOCKER_BUILDKIT="0")

free = shutil.disk_usage(disk).free
assert free >= min_bytes, f"Free bytes beneath required threshold on {disk}: {free}"
assert not report_file.exists()
assert subprocess.run(
    ["docker", "image", "inspect", TAG], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
).returncode != 0, "Release tag already exists"
archive_sha = hashlib.file_digest((root / "source.tar").open("rb"), "sha256").hexdigest()
assert archive_sha == args.source_sha256
with (root / "source.tar").open("rb") as archive:
    archive_commit = subprocess.check_output(["git", "get-tar-commit-id"], stdin=archive, text=True).strip()
assert archive_commit == COMMIT
with tarfile.open(root / "source.tar", "r") as archive:
    archive_paths = set()
    for member in archive:
        archive_paths.add(member.name)
        exported = source / member.name
        if member.isfile():
            assert exported.is_file() and not exported.is_symlink(), member.name
            assert exported.read_bytes() == archive.extractfile(member).read(), member.name
            assert exported.stat().st_mode & 0o100 == member.mode & 0o100, member.name
        elif member.issym():
            assert exported.is_symlink() and os.readlink(exported) == member.linkname, member.name
        else:
            assert member.isdir() and exported.is_dir() and not exported.is_symlink(), member.name
    assert archive_paths == {p.relative_to(source).as_posix() for p in source.rglob("*")}

manifest = json.loads((source / "ops/fleet-release.json").read_text())
parent = manifest["images"][arch]
parent_info = image_info(parent)
builder_info = image_info(args.builder)
assert parent_info["architecture"] == builder_info["architecture"] == arch
assert parent_info["os"] == builder_info["os"] == "linux"
report = {
    "version": VERSION, "upstream": "v8.0.66", "source_commit": COMMIT,
    "source_archive_sha256": archive_sha, "archive_commit": archive_commit,
    "export_entries_verified": len(archive_paths),
    "build_date": args.build_date, "started_at": now(), "build_only": True,
    "disk_pre_build_free_bytes": free, "runtime_parent": parent_info, "builder": builder_info,
    "binaries": {}, "checks": {}, "commands": {}
}


def save():
    report_file.write_text(json.dumps(report, indent=2) + "\n")


def run(name, command):
    report["commands"][name] = command
    save()
    print(f"{arch}: {name}", flush=True)
    log = root / (arch + "-" + name + ".log")
    with log.open("x") as out:
        proc = subprocess.run(command, env=env, stdout=out, stderr=subprocess.STDOUT)
    report["checks"][name] = {"exit_code": proc.returncode, "log": log.name}
    save()
    proc.check_returncode()
    return log


work = root / ("runtime-" + arch)
work.mkdir()
(work / "tmp").mkdir()
cpus = "6" if mac else "8"
cache_mount = ("type=volume,source=cliproxy-fleet4-arm64-buildcache,target=/build"
               if mac else "type=bind,source=/home/andrew/.cache/cliproxy-upgrade-20261102,target=/build")
mods = "cliproxy-fleet4-arm64-modcache" if mac else "cliproxy-go126-modcache"
base = [
    "docker", "run", "--rm", "--pull=never", "--network", "none",
    "--platform", "linux/" + arch, "--name", "cliproxy-fleet4-" + arch + "-builder", "--cpus", cpus,
    "-e", "DO_NOT_TRACK=1", "-e", "GOTELEMETRY=off", "-e", "GOMAXPROCS=" + cpus,
    "-e", "GOCACHE=/build/cache", "-e", "GOTMPDIR=/out/tmp",
    "-e", "GOPROXY=off", "-e", "GOTOOLCHAIN=local", "-e", "CGO_ENABLED=1",
    "--mount", f"type=bind,source={source},target=/app,readonly",
    "--mount", cache_mount, "--mount", f"type=volume,source={mods},target=/go/pkg/mod",
    "--mount", f"type=bind,source={work},target=/out", "-w", "/app", builder_info["id"],
]
try:
    toolchain = run("toolchain", base + ["bash", "-ec", "go version; gcc -dumpfullversion; getconf GNU_LIBC_VERSION"])
    assert toolchain.read_text().splitlines()[0] == f"go version go1.26.10 linux/{arch}"
    flags = f"-s -w -X main.Version={VERSION} -X main.Commit={COMMIT} -X main.BuildDate={args.build_date}"
    run("required-compile", base + ["go", "build", "-buildvcs=false", "-ldflags=" + flags,
                                    "-o", "/out/CLIProxyAPI", "./cmd/server/"])
    report["required_compilation"] = "passed"
    with (work / "CLIProxyAPI").open("rb") as binary:
        report["binaries"][arch] = hashlib.file_digest(binary, "sha256").hexdigest()
    shutil.copyfile(source / "config.example.yaml", work / "config.example.yaml")
    (work / "Dockerfile").write_text(
        f"FROM {parent}\nCOPY CLIProxyAPI /CLIProxyAPI/CLIProxyAPI\n"
        "COPY config.example.yaml /CLIProxyAPI/config.example.yaml\n"
    )
    run("image-build", ["docker", "build", "--pull=false", "--network=none", "--platform", "linux/" + arch,
                        "--label", "org.opencontainers.image.revision=" + COMMIT,
                        "--label", "org.opencontainers.image.version=" + VERSION,
                        "-t", TAG, str(work)])
    image = image_info(TAG)
    assert image["architecture"] == arch and image["os"] == "linux"
    assert image["labels"]["org.opencontainers.image.revision"] == COMMIT
    assert image["labels"]["org.opencontainers.image.version"] == VERSION
    assert image["id"] != parent
    report[arch + "_image"] = image
    save()
    for name, test_args, expected, minimum in [
        ("executor-date-pin", ["./internal/runtime/executor/", "-run", "TestClaudeCloakedDateReminder|TestClaudeDatePinSessionID"],
         r"TestClaudeCloakedDateReminder", 5),
        ("helps", ["./internal/runtime/executor/helps/"], r"TestClaudeDatePinSessionID", 1),
    ]:
        log = run(name, base + ["go", "test", "-json", "-count=1"] + test_args)
        events = [json.loads(line) for line in log.read_text().splitlines() if line.startswith("{")]
        passed = [e["Test"] for e in events if e.get("Action") == "pass" and "Test" in e]
        named = sorted({test for test in passed if test.startswith(expected) and "/" not in test})
        report["checks"][name].update(passed_test_events=len(passed), date_pin_tests_passed=named)
        assert len(named) >= minimum, f"{name}: required tests were not executed"
        save()
finally:
    report["finished_at"] = now()
    report["disk_post_build_free_bytes"] = shutil.disk_usage(disk).free
    save()
