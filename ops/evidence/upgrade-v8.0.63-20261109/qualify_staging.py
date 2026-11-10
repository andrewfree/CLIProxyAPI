import argparse
import datetime
import hashlib
import json
import os
import pathlib
import secrets
import subprocess
import time

parser = argparse.ArgumentParser()
parser.add_argument('--arch', required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--plugins', required=True)
args = parser.parse_args()
out = pathlib.Path(args.output).resolve()
out.mkdir(parents=True, exist_ok=True)
plugin_dir = pathlib.Path(args.plugins).resolve()
libraries = list((plugin_dir / 'linux' / args.arch).glob('*.so'))
assert libraries, 'No plugin libraries discovered'
hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in libraries}
name = 'cliproxy-v8063-isolated-' + args.arch + '-20261109'
image = 'cliproxy-fleet:v8.0.63-fleet.1-' + args.arch
assert subprocess.run(['docker', 'inspect', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
key = secrets.token_hex(24)
client_key = secrets.token_hex(24)
config_file = out / 'synthetic-config.yaml'
config_file.write_text('''config-version: 8
server:
  host: "127.0.0.1"
  port: 8317
management:
  allow-remote: false
  disable-control-panel: true
  disable-auto-update-panel: true
access:
  api-keys: ["''' + client_key + '''"]
oauth:
  auth-dir: "/stage-empty-auth"
routing:
  strategy: "quota-aware"
  session-affinity: false
plugins:
  enabled: true
  dir: "/stage-plugins"
  configs:
    auth-cursor:
      enabled: true
    cliproxybar-quota:
      enabled: true
observability:
  logs:
    debug: false
    logging-to-file: false
    request-log: false
  usage:
    usage-statistics-enabled: false
''')
config_file.chmod(0o600)
if 'cliproxybar-quota.so' not in hashes:
    config_file.write_text(config_file.read_text().replace('    cliproxybar-quota:\n      enabled: true\n', ''))
env = dict(os.environ, MANAGEMENT_PASSWORD=key, DO_NOT_TRACK='1', GOTELEMETRY='off')
result = {'arch': args.arch, 'image': image, 'plugin_file_sha256': hashes,
          'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'failures': [], 'limitations': ['No upstream inference or quota fetch; no production credentials available.']}
started = False

def request(endpoint, token=None):
    headers = '' if token is None else 'Authorization: Bearer ' + token + '\r\n'
    wire = ('GET ' + endpoint + ' HTTP/1.0\r\nHost: 127.0.0.1\r\n' + headers + 'Connection: close\r\n\r\n').encode()
    response = subprocess.run(['docker', 'exec', '-i', name, 'bash', '-c',
                               'exec 3<>/dev/tcp/127.0.0.1/8317; cat >&3; cat <&3'],
                              input=wire, capture_output=True, timeout=8, check=True).stdout
    head, body = response.split(b'\r\n\r\n', 1)
    status = int(head.splitlines()[0].split()[1])
    response_headers = dict(line.decode().split(': ', 1) for line in head.splitlines()[1:] if b': ' in line)
    assert status == 200, endpoint + ': HTTP ' + str(status)
    return json.loads(body), response_headers

try:
    started = True
    subprocess.run(['docker', 'run', '-d', '--pull=never', '--name', name, '--network', 'none',
                    '-e', 'MANAGEMENT_PASSWORD', '-e', 'DO_NOT_TRACK', '-e', 'GOTELEMETRY',
                    '--mount', f'type=bind,source={config_file},target=/stage-config.yaml,readonly',
                    '--mount', f'type=bind,source={plugin_dir},target=/stage-plugins,readonly',
                    '--tmpfs', '/stage-empty-auth:rw,nosuid,noexec,size=16m',
                    image, '/CLIProxyAPI/CLIProxyAPI', '--config', '/stage-config.yaml', '--local-model'], env=env,
                   check=True, stdout=subprocess.DEVNULL)
    started = True
    for attempt in range(80):
        try:
            health, _ = request('/healthz')
            break
        except Exception:
            if attempt == 79:
                raise
            time.sleep(0.25)
    plugins, headers = request('/v8/management/plugins', key)
    loaded_config, _ = request('/v0/management/config', key)
    strategy, _ = request('/v0/management/routing/strategy', key)
    credentials, _ = request('/v8/management/credentials', key)
    models, _ = request('/v1/models', client_key)
    inspect = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
    result.update(health=health, catalog_status=200, catalog_model_count=len(models.get('data', [])),
                  registered_plugins=[{'id': p['id'], 'registered': p.get('registered'),
                                       'effective_enabled': p.get('effective_enabled'),
                                       'version': (p.get('metadata') or {}).get('version'),
                                       'supports_quota': p.get('supports_quota'),
                                       'error': p.get('error')} for p in plugins['plugins']],
                  plugins_enabled=plugins.get('plugins_enabled'), strategy=strategy,
                  loaded_routing=loaded_config.get('routing'),
                  session_affinity=loaded_config['routing'].get('session-affinity', False),
                  usage_statistics_enabled=loaded_config.get('usage-statistics-enabled'),
                  credential_count=len(credentials.get('files', [])),
                  version=next((v for k, v in headers.items() if k.lower() == 'x-cpa-version'), None),
                  revision=inspect['Config']['Labels'].get('org.opencontainers.image.revision'),
                  image_id=inspect['Image'], network_mode=inspect['HostConfig']['NetworkMode'],
                  published_ports=inspect['HostConfig']['PortBindings'],
                  mounts=[{'destination': m['Destination'], 'writable': m['RW']} for m in inspect['Mounts']],
                  do_not_track='DO_NOT_TRACK=1' in inspect['Config']['Env'])
    assert health['status'] == 'ok'
    assert strategy.get('strategy') == 'quota-aware', strategy
    assert loaded_config['routing']['strategy'] == 'quota-aware'
    assert result['session_affinity'] is False
    assert result['usage_statistics_enabled'] is False
    assert result['credential_count'] == 0
    assert result['version'] == 'v8.0.63-fleet.1'
    assert result['revision'] == '87e90c0c7629fd2d667ef5be13e2fc775005d5d8'
    assert result['network_mode'] == 'none' and not result['published_ports']
    assert result['do_not_track'] and result['plugins_enabled']
    existing = [p for p in result['registered_plugins'] if p['id'] != 'cliproxybar-quota' or 'cliproxybar-quota.so' in hashes]
    assert len(existing) == len(libraries)
    assert all(p['registered'] and p['effective_enabled'] for p in existing)
    cursor = next(p for p in result['registered_plugins'] if p['id'] == 'auth-cursor')
    assert cursor['version'] == '2.1.1'
    assert hashes['auth-cursor-v2.1.1.so'] == {'amd64': '75bfdcf6f9dd7b177d7b6e3970c9bdc66101dd2c67b98c1bfa399ef84e290894', 'arm64': '4a2bb071c75f1bd83df561168da2a5a667777d422c41a83ac0f95315adce809a'}[args.arch]
except Exception as error:
    result['failures'].append(str(error).replace(key, '[redacted]').replace(client_key, '[redacted]'))
finally:
    if started:
        logs = subprocess.check_output(['docker', 'logs', name], stderr=subprocess.STDOUT).decode()
        (out / (args.arch + '-startup.log')).write_text(logs.replace(key, '[synthetic management key]').replace(client_key, '[synthetic client key]'))
        subprocess.run(['docker', 'rm', '-f', name], check=True, stdout=subprocess.DEVNULL)
    result['isolated_container_removed'] = subprocess.run(['docker', 'inspect', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
    result['plugin_files_unchanged'] = hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in libraries}
    result['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    config_file.unlink(missing_ok=True)
    (out / (args.arch + '-qualification.json')).write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
raise SystemExit(bool(result['failures']))
