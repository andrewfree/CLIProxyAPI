import collections
import hashlib
import json
import pathlib
import subprocess
import urllib.request

root = pathlib.Path.cwd()
release = json.loads((root / 'ops/fleet-release.json').read_text())
container = json.loads(subprocess.check_output(['docker', 'inspect', 'cli-proxy-api'], text=True))[0]
arch = json.loads(subprocess.check_output(['docker', 'image', 'inspect', container['Image']], text=True))[0]['Architecture']
values = {}
for line in (root / '.env').read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        key, value = line.split('=', 1)
        values[key] = value.strip().strip('"').strip("'")
key = values['MANAGEMENT_PASSWORD']
def get(path):
    req = urllib.request.Request('http://127.0.0.1:8317' + path, headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response), dict(response.headers)
plugins, headers = get('/v0/management/plugins')
auths, _ = get('/v0/management/auth-files')
config, _ = get('/v0/management/config')
plugin = next(p for p in plugins['plugins'] if p['id'] == 'auth-cursor')
filename = root / 'plugins/linux' / arch / release['cursor_plugin']['libraries'][arch]['filename']
observed = {
    'image_id': container['Image'],
    'image_ref': container['Config']['Image'],
    'source_commit': container['Config']['Labels'].get('org.opencontainers.image.revision'),
    'version': next((v for k, v in headers.items() if k.lower() == 'x-cpa-version'), None),
    'health': container['State']['Health']['Status'],
    'plugins_enabled': plugins['plugins_enabled'],
    'cursor_registered': plugin['registered'],
    'cursor_enabled': plugin['effective_enabled'],
    'cursor_version': plugin['metadata']['version'],
    'cursor_sha256': hashlib.sha256(filename.read_bytes()).hexdigest(),
    'usage_statistics_enabled': config.get('usage-statistics-enabled'),
    'quota_cooldown_max_seconds': config.get('quota-cooldown-max-seconds'),
    'save_cooldown_status': config.get('save-cooldown-status'),
    'do_not_track': 'DO_NOT_TRACK=1' in container['Config']['Env'],
    'account_counts': dict(collections.Counter(a.get('provider', 'unknown') for a in auths['files'])),
}
expected = {
    'image_id': release['images'][arch],
    'image_ref': release['images'][arch],
    'source_commit': release['source_commit'],
    'version': release['version'],
    'health': 'healthy',
    'plugins_enabled': True,
    'cursor_registered': True,
    'cursor_enabled': True,
    'cursor_version': release['cursor_plugin']['version'],
    'cursor_sha256': release['cursor_plugin']['libraries'][arch]['sha256'],
    'usage_statistics_enabled': False,
    'quota_cooldown_max_seconds': release['runtime_policy']['quota-cooldown-max-seconds'],
    'save_cooldown_status': release['runtime_policy']['save-cooldown-status'],
    'do_not_track': True,
}
failures = [name for name, value in expected.items() if observed.get(name) != value]
print(json.dumps({'platform': arch, 'observed': observed, 'failures': failures}, indent=2))
raise SystemExit(bool(failures))
