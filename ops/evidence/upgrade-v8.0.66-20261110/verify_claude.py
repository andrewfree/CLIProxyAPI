import argparse
import datetime
import json
import pathlib
import urllib.error
import urllib.request


parser = argparse.ArgumentParser()
parser.add_argument('root', type=pathlib.Path)
parser.add_argument('--generate', action='store_true')
args = parser.parse_args()
values = {}
for line in (args.root / '.env').read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        name, value = line.split('=', 1)
        values[name] = value.strip().strip('"').strip("'")
base = 'http://127.0.0.1:8317'


def request(route, key, payload=None):
    req = urllib.request.Request(base + route,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as response:
        return json.load(response)


management_key = values['MANAGEMENT_PASSWORD']
config = request('/v0/management/config', management_key)
credentials = request('/v0/management/auth-files', management_key)['files']
accounts = []
report = {'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'accounts': accounts, 'generation_attempted': args.generate}
for credential in credentials:
    if credential.get('provider') != 'claude':
        continue
    row = {'email': credential.get('email'), 'disabled': credential.get('disabled', False)}
    for resource in ('profile', 'usage'):
        result = request('/v8/management/requests/api-call', management_key, {
            'auth_index': credential['auth_index'], 'method': 'GET',
            'url': 'https://api.anthropic.com/api/oauth/' + resource,
            'header': {'Authorization': 'Bearer $TOKEN$', 'anthropic-beta': 'oauth-2025-04-20',
                       'User-Agent': 'claude-cli/2.1.319 (external, cli)', 'x-app': 'cli'}})
        row[resource + '_http_status'] = result.get('status_code')
        assert result.get('status_code') == 200, resource + ' unavailable'
        body = result['body']
        body = json.loads(body) if isinstance(body, str) else body
        if resource == 'profile':
            assert body['account']['email'] == row['email'], 'account identity mismatch'
            row['tier'] = body['organization'].get('rate_limit_tier')
            row['subscription_status'] = body['organization'].get('subscription_status')
        else:
            row['quota'] = {key: {'used_percent': body[key].get('utilization'),
                                  'resets_at': body[key].get('resets_at')}
                            for key in ('five_hour', 'seven_day') if isinstance(body.get(key), dict)}
    accounts.append(row)
assert len(accounts) == 3 and len({row['email'] for row in accounts}) == 3
if args.generate:
    key = config['api-keys'][0]
    catalog = request('/v1/models', key)
    model = 't3-gmail/claude-fable-5-2'
    assert model in {item['id'] for item in catalog['data']}, 'pinned Claude smoke model absent'
    try:
        result = request('/v1/messages', key, {
            'model': model, 'max_tokens': 128, 'stream': False, 'thinking': {'type': 'disabled'},
            'messages': [{'role': 'user', 'content': 'Reply exactly CLIPROXY_UPGRADE_OK.'}]})
        text = ''.join(block.get('text', '') for block in result.get('content', []) if block.get('type') == 'text')
        report['generation'] = {'requested_model': model, 'response_model': result.get('model'),
                                'http_status': 200, 'response_matches': text.strip() == 'CLIPROXY_UPGRADE_OK',
                                'response_text': text, 'content_types': [block.get('type') for block in result.get('content', [])],
                                'stop_reason': result.get('stop_reason'), 'usage': result.get('usage')}
    except urllib.error.HTTPError as error:
        report['generation'] = {'requested_model': model, 'http_status': error.code}
        print(json.dumps(report, indent=2))
        raise SystemExit(1)
print(json.dumps(report, indent=2))
raise SystemExit(0 if not args.generate or report['generation']['response_matches'] else 1)
