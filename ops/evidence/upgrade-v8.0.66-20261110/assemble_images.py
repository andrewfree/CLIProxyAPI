import argparse
import json
import os
import pathlib
import subprocess


parser = argparse.ArgumentParser()
parser.add_argument('--arch', choices=('amd64', 'arm64'))
parser.add_argument('--artifacts', type=pathlib.Path,
                    default=pathlib.Path('/srv/share/drives/disk12/cliproxy-v8.0.66-fleet.1-20261110/artifacts'))
args = parser.parse_args()
root = args.artifacts
report = json.loads((root / 'build-receipt.json').read_text())
for arch in (args.arch,) if args.arch else ('amd64', 'arm64'):
    tag = 'cliproxy-fleet:' + report['version'] + '-' + arch
    assert subprocess.run(['docker', 'image', 'inspect', tag], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode != 0
    with (root / (arch + '-image-build-local.log')).open('x') as log:
        subprocess.run(
            ['docker', 'build', '--pull=false', '--network=none', '--platform', 'linux/' + arch,
             '--label', 'org.opencontainers.image.revision=' + report['source_commit'],
             '--label', 'org.opencontainers.image.version=' + report['version'],
             '-t', tag, str(root / ('runtime-' + arch))],
            env=dict(os.environ, DOCKER_BUILDKIT='0', DO_NOT_TRACK='1'),
            stdout=log, stderr=subprocess.STDOUT, check=True)
    image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', tag]))[0]
    assert image['Architecture'] == arch
    report[arch + '_image'] = {'id': image['Id'], 'architecture': image['Architecture'],
                             'labels': image['Config']['Labels']}
    (root / 'build-receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    print(arch + ' image ' + image['Id'], flush=True)
