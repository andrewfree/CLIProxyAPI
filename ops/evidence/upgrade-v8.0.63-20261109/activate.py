import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('architecture', choices=['arm64', 'amd64'])
parser.add_argument('stage', type=Path)
parser.add_argument('backup', type=Path)
parser.add_argument('--activate', action='store_true')
args = parser.parse_args()
root = Path.cwd()
work = root / 'ops/runtime/upgrade-v8.0.63-20261109'
manifest = json.loads((args.stage / 'ops/fleet-release.json').read_text())
previous = json.loads((args.backup / 'ops/fleet-release.json').read_text())
image = manifest['images'][args.architecture]
container = json.loads(subprocess.check_output(['docker', 'inspect', 'cli-proxy-api']))[0]
assert container['Image'] == previous['images'][args.architecture], 'Running image changed since backup'
info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
assert info['Architecture'] == args.architecture
assert info['Config']['Labels']['org.opencontainers.image.revision'] == manifest['source_commit']
assert info['Config']['Labels']['org.opencontainers.image.version'] == manifest['version']
assert (root / 'config.yaml').read_bytes() == (args.backup / 'config.yaml').read_bytes(), 'Configuration changed since backup'
environment = (root / '.env').read_text()
assert environment == (args.backup / '.env').read_text(), 'Environment changed since backup'
updated_environment, replacements = re.subn(r'(?m)^CLI_PROXY_IMAGE=.*$', 'CLI_PROXY_IMAGE=' + image, environment)
assert replacements == 1, 'Expected one existing image selection'
mapping = {
    'ops/fleet-release.json': 'ops/fleet-release.json',
    'ops/verify_fleet_instance.py': 'ops/verify_fleet_instance.py',
    'ops/fleet_routing.py': 'ops/fleet_routing.py',
}
if args.architecture == 'amd64':
    mapping.update({
        'ops/instances/will/docker-compose.yml': 'docker-compose.yml',
        'ops/instances/will/workload-contract.json': 'ops/instances/will/workload-contract.json',
        'ops/instances/will/config-policy.json': 'ops/instances/will/config-policy.json',
        'ops/instances/will/verify_live.py': 'ops/instances/will/verify_live.py',
    })
else:
    for name in ['docker-compose.yml', 'ops/workload-contract.json', 'ops/instances/will/docker-compose.yml', 'ops/instances/will/workload-contract.json', 'ops/instances/will/config-policy.json', 'ops/workload_contract_test.go', 'ops/will_instance_contract_test.go']:
        mapping[name] = name
for source in mapping:
    assert (args.stage / source).is_file(), source
receipt = {
    'architecture': args.architecture,
    'version': manifest['version'],
    'source_commit': manifest['source_commit'],
    'image': image,
    'previous_image': container['Image'],
    'previous_container_id': container['Id'],
    'backup': str(args.backup),
    'authorization': 'Andrew: ok finish/deploy commit etc',
    'config_sha256': hashlib.sha256((root / 'config.yaml').read_bytes()).hexdigest(),
    'config_inode': (root / 'config.yaml').stat().st_ino,
}
if not args.activate:
    print(json.dumps(dict(receipt, preflight='passed'), indent=2))
    raise SystemExit(0)
receipt['started_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
subprocess.run(['docker', 'stop', '--time', '20', 'cli-proxy-api'], check=True, stdout=subprocess.DEVNULL)
for source, destination in mapping.items():
    target = root / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    shutil.copyfile(args.stage / source, target)
    os.chmod(target, mode)
with (root / '.env').open('r+') as output:
    output.write(updated_environment)
    output.truncate()
command = ['docker', 'compose', 'up', '-d', '--no-deps', '--pull', 'never', '--no-build', '--wait', '--wait-timeout', '60', 'cli-proxy-api']
result = subprocess.run(command, capture_output=True, text=True)
(work / ('compose-' + args.architecture + '.log')).write_text(result.stdout + result.stderr)
receipt['compose_exit_code'] = result.returncode
receipt['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
(work / ('activation-' + args.architecture + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt, indent=2))
raise SystemExit(result.returncode)
