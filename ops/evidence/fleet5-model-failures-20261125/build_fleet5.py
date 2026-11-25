"""Build v8.0.66-fleet.5 on Will: both Linux binaries in the offline cross-builder,
focused tests, and the amd64 image. The arm64 image is assembled on the Mac.

Adapted from ops/evidence/upgrade-v8.0.66-20261110/build_images.py and
ops/evidence/fleet4-activation-20261118/build_images.py.

Usage: python3 build_fleet5.py <source-sha256> <build-date>
"""
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tarfile

VERSION = "v8.0.66-fleet.5"
COMMIT = "e0f4810785231d68e0eb62fb6d17d1adcc4b3647"
BUILDER = "sha256:420b7ecff28ef9fd9e87ea50d53de965740c065da8bfa06807d5a6db364c0ad1"  # cliproxy-cross-builder:20261102
PARENT_AMD64 = "sha256:9e9da16e283e20da4a534d1397d4b6433a5708f4f04b6faf2d03b1367d357af6"  # fleet.4 amd64
ROOT = pathlib.Path("/srv/share/drives/disk12/cliproxy-v8.0.66-fleet.5-20261125")
CACHE = pathlib.Path("/home/andrew/.cache/cliproxy-upgrade-20261102")
ART = ROOT / "artifacts"
SOURCE = ROOT / "source"
REPORT = ART / "build.json"

source_sha, build_date = sys.argv[1], sys.argv[2]
assert shutil.disk_usage("/").free >= 150_000_000_000, "Will has under 150 GB free"
assert not REPORT.exists(), "build.json already exists"
archive = ART / "source.tar"
assert hashlib.sha256(archive.read_bytes()).hexdigest() == source_sha
assert subprocess.check_output(["git", "get-tar-commit-id"], stdin=archive.open("rb"), text=True).strip() == COMMIT
with tarfile.open(archive) as tar:
    names = set()
    for member in tar:
        names.add(member.name)
        exported = SOURCE / member.name
        if member.isfile():
            assert exported.read_bytes() == tar.extractfile(member).read(), member.name
assert names == {p.relative_to(SOURCE).as_posix() for p in SOURCE.rglob("*")}, "export differs from archive"

report = {"version": VERSION, "source_commit": COMMIT, "source_archive_sha256": source_sha, "build_date": build_date,
          "builder": BUILDER, "parent_amd64": PARENT_AMD64, "binaries": {}, "checks": {}}


def save():
    REPORT.write_text(json.dumps(report, indent=2) + "\n")


base = ["docker", "run", "--rm", "--pull=never", "--network", "none", "--name", "cliproxy-fleet5-build",
        "--cpus", "8", "-e", "DO_NOT_TRACK=1", "-e", "GOTELEMETRY=off", "-e", "GOMAXPROCS=8",
        "-e", "GOCACHE=/build/cache", "-e", "GOTMPDIR=/build/tmp-fleet5",
        "-e", "GOPROXY=off", "-e", "GOTOOLCHAIN=local",
        "--mount", f"type=bind,source={SOURCE},target=/app,readonly",
        "--mount", f"type=bind,source={CACHE},target=/build",
        "--mount", "type=volume,source=cliproxy-go126-modcache,target=/go/pkg/mod",
        "-w", "/app", BUILDER]


def run(name, command):
    print(name, flush=True)
    with (ART / (name + ".log")).open("x") as log:
        code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
    report["checks"][name] = {"exit_code": code, "log": name + ".log"}
    save()
    assert code == 0, f"{name} failed with exit {code}"


(CACHE / "tmp-fleet5").mkdir(exist_ok=True)
save()
run("toolchain", base + ["bash", "-ec", "go version; aarch64-linux-gnu-gcc -dumpfullversion; gcc -dumpfullversion"])
assert (ART / "toolchain.log").read_text().splitlines()[0] == "go version go1.26.10 linux/amd64"
run("focused-tests", base + ["go", "test", "-count=1", "./sdk/cliproxy/auth/", "./internal/api/handlers/management/",
                             "./internal/runtime/executor/"])
flags = f"-s -w -X main.Version={VERSION} -X main.Commit={COMMIT} -X main.BuildDate={build_date}"
for arch, cc in (("amd64", "gcc"), ("arm64", "aarch64-linux-gnu-gcc")):
    name = f"CLIProxyAPI-v8066-fleet5-{arch}"
    assert not (CACHE / name).exists(), "do not overwrite an earlier binary"
    run(f"{arch}-compile", base + ["env", "CGO_ENABLED=1", "GOOS=linux", "GOARCH=" + arch, "CC=" + cc,
                                   "go", "build", "-buildvcs=false", "-ldflags=" + flags,
                                   "-o", "/build/" + name, "./cmd/server/"])
    context = ART / ("runtime-" + arch)
    context.mkdir()
    shutil.copyfile(CACHE / name, context / "CLIProxyAPI")
    (context / "CLIProxyAPI").chmod(0o755)
    shutil.copyfile(SOURCE / "config.example.yaml", context / "config.example.yaml")
    report["binaries"][arch] = hashlib.sha256((context / "CLIProxyAPI").read_bytes()).hexdigest()
    save()

context = ART / "runtime-amd64"
(context / "Dockerfile").write_text(f"FROM {PARENT_AMD64}\nCOPY CLIProxyAPI /CLIProxyAPI/CLIProxyAPI\n"
                                    "COPY config.example.yaml /CLIProxyAPI/config.example.yaml\n")
tag = "cliproxy-fleet:" + VERSION
for existing in (tag, tag + "-amd64"):
    assert subprocess.run(["docker", "image", "inspect", existing], capture_output=True).returncode != 0, existing
with (ART / "amd64-image-build.log").open("x") as log:
    subprocess.run(["docker", "build", "--pull=false", "--network=none", "--platform", "linux/amd64",
                    "--label", "org.opencontainers.image.revision=" + COMMIT,
                    "--label", "org.opencontainers.image.version=" + VERSION,
                    "-t", tag, "-t", tag + "-amd64", str(context)],
                   check=True, stdout=log, stderr=subprocess.STDOUT, env={"DOCKER_BUILDKIT": "0", "PATH": "/usr/bin:/bin"})
info = json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]
assert info["Architecture"] == "amd64" and info["Config"]["Labels"]["org.opencontainers.image.revision"] == COMMIT
report["amd64_image"] = {"id": info["Id"], "architecture": info["Architecture"], "labels": info["Config"]["Labels"]}
report["will_root_free_bytes"] = shutil.disk_usage("/").free
save()
print(json.dumps(report, indent=2), flush=True)
