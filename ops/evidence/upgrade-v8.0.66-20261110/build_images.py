import hashlib
import json
import pathlib
import shutil
import subprocess
import tarfile

root = pathlib.Path('/srv/share/drives/disk12/cliproxy-v8.0.66-fleet.1-20261110')
artifacts = root / 'artifacts'
meta = json.loads((artifacts / 'build.json').read_text())
assert meta['source_commit'] == '815987ae113117253b37c7c0755e209fadd01372'
assert shutil.disk_usage('/').free >= 150_000_000_000
cache = pathlib.Path('/home/andrew/.cache/cliproxy-upgrade-20261102')
base = [
    'docker', 'run', '--rm', '--network', 'none', '--name', 'cliproxy-v8066-build-20261110',
    '--cpus', '8', '-e', 'DO_NOT_TRACK=1', '-e', 'GOTELEMETRY=off',
    '-e', 'GOMAXPROCS=8', '-e', 'GOCACHE=/build/cache', '-e', 'GOTMPDIR=/build/tmp-v8066',
    '-e', 'GOPROXY=off', '-e', 'GOTOOLCHAIN=local',
    '--mount', f'type=bind,source={root / "source"},target=/app,readonly',
    '--mount', f'type=bind,source={cache},target=/build',
    '--mount', 'type=volume,source=cliproxy-go126-modcache,target=/go/pkg/mod',
    '-w', '/app', 'cliproxy-cross-builder:20261102',
]
result = dict(meta, build_only=True, source_archive_sha256=hashlib.sha256((artifacts / 'source.tar').read_bytes()).hexdigest(), binaries={})
result['required_compilation'] = 'passed: go build -o /build/test-output-v8066 ./cmd/server'
result['validation'] = json.loads((artifacts / 'validation.json').read_text())
assert result['validation']['checks']['remainder-go-suite']['exit_code'] == 0
assert result['validation']['checks']['quota-race']['exit_code'] == 0
flags = '-s -w -X main.Version=' + meta['version'] + ' -X main.Commit=' + meta['source_commit'] + ' -X main.BuildDate=' + meta['build_date']
for arch, cc in [('amd64', 'gcc'), ('arm64', 'aarch64-linux-gnu-gcc')]:
    binary_name = 'CLIProxyAPI-v8066-' + arch
    assert not (cache / binary_name).exists(), 'Do not overwrite prior binary'
    with (artifacts / (arch + '-build.log')).open('x') as log:
        subprocess.run(base + ['env', 'CGO_ENABLED=1', 'GOOS=linux', 'GOARCH=' + arch, 'CC=' + cc,
                              'go', 'build', '-buildvcs=false', '-ldflags=' + flags,
                              '-o', '/build/' + binary_name, './cmd/server/'],
                       check=True, stdout=log, stderr=subprocess.STDOUT)
    context = artifacts / ('runtime-' + arch)
    context.mkdir()
    shutil.copyfile(cache / binary_name, context / 'CLIProxyAPI')
    (context / 'CLIProxyAPI').chmod(0o755)
    shutil.copyfile(root / 'source/config.example.yaml', context / 'config.example.yaml')
    shutil.copyfile(artifacts / ('Dockerfile.runtime.' + arch), context / 'Dockerfile')
    result['binaries'][arch] = hashlib.sha256((context / 'CLIProxyAPI').read_bytes()).hexdigest()
    (artifacts / 'build-receipt.json').write_text(json.dumps(result, indent=2) + '\n')
    print(arch + ' binary built', flush=True)
for arch in ('amd64', 'arm64'):
    tag = 'cliproxy-fleet:' + meta['version'] + '-' + arch
    assert subprocess.run(['docker', 'image', 'inspect', tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
    with (artifacts / (arch + '-image-build.log')).open('x') as log:
        subprocess.run(['docker', 'build', '--pull=false', '--network=none', '--platform', 'linux/' + arch,
                        '--label', 'org.opencontainers.image.revision=' + meta['source_commit'],
                        '--label', 'org.opencontainers.image.version=' + meta['version'],
                        '-t', tag, str(artifacts / ('runtime-' + arch))], check=True, stdout=log, stderr=subprocess.STDOUT)
    info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', tag]))[0]
    result[arch + '_image'] = {'id': info['Id'], 'architecture': info['Architecture'], 'labels': info['Config']['Labels']}
    result['will_root_free_bytes'] = shutil.disk_usage('/').free
    (artifacts / 'build-receipt.json').write_text(json.dumps(result, indent=2) + '\n')
    print(arch + ' image built', flush=True)
print(json.dumps(result, indent=2), flush=True)
