import json
import pathlib
import shutil
import subprocess


root = pathlib.Path('/srv/share/drives/disk12/cliproxy-v8.0.66-fleet.1-20261110')
artifacts = root / 'artifacts'
cache = pathlib.Path('/home/andrew/.cache/cliproxy-upgrade-20261102')
assert shutil.disk_usage('/').free >= 150_000_000_000
source = root / 'source'
base = [
    'docker', 'run', '--rm', '--network', 'host', '--cpus', '8',
    '-e', 'DO_NOT_TRACK=1', '-e', 'GOTELEMETRY=off', '-e', 'GOMAXPROCS=8',
    '-e', 'GOCACHE=/build/cache', '-e', 'GOTMPDIR=/build/tmp-v8066',
    '-e', 'GOPROXY=off', '-e', 'GOTOOLCHAIN=local',
    '--mount', f'type=bind,source={source},target=/app',
    '--mount', f'type=bind,source={cache},target=/build',
    '--mount', 'type=volume,source=cliproxy-go126-modcache,target=/go/pkg/mod',
    '-w', '/app', 'cliproxy-cross-builder:20261102',
]
subprocess.run(base + ['mkdir', '-p', '/build/tmp-v8066'], check=True)
report = json.loads((artifacts / 'build.json').read_text())
report['checks'] = {}
commands = [
    ('required-compile', ['go', 'build', '-o', '/build/test-output-v8066', './cmd/server']),
    ('full-go-suite', ['go', 'test', '-json', '-count=1', './...']),
    ('quota-race', ['go', 'test', '-race', '-count=1', './sdk/cliproxy/auth',
                    './sdk/api/handlers/claude', './internal/registry',
                    '-run', 'Quota|Cooldown|SchedulerReject|RequestRetry|Preferred|Upstream|Transient']),
]
for name, command in commands:
    print('Starting ' + name, flush=True)
    with (artifacts / (name + '.log')).open('w') as log:
        process = subprocess.run(base + command, stdout=log, stderr=subprocess.STDOUT)
    report['checks'][name] = {'exit_code': process.returncode}
    if name == 'full-go-suite':
        failed = []
        passed = 0
        for line in (artifacts / (name + '.log')).read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('Action') == 'fail':
                failed.append({'package': event.get('Package'), 'test': event.get('Test')})
            elif event.get('Action') == 'pass' and event.get('Test'):
                passed += 1
        report['checks'][name].update(failed=failed, passed_tests=passed)
    (artifacts / 'validation.json').write_text(json.dumps(report, indent=2) + '\n')
    print(name + ' exit ' + str(process.returncode), flush=True)
    if name == 'required-compile':
        if process.returncode:
            print((artifacts / (name + '.log')).read_text()[-4000:], flush=True)
            break
        subprocess.run(base + ['rm', '-f', '/build/test-output-v8066'], check=True)
print(json.dumps(report, indent=2), flush=True)
raise SystemExit(any(check['exit_code'] for check in report['checks'].values()))
