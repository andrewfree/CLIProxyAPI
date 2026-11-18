#!/usr/bin/env python3
"""Fleet.4 inference gate: Claude Code-shaped requests through one CLIProxy host.

Usage: inference_gate.py <base-url> <label> [<log-dir> | ssh:<host>]
Sends a pool turn (stream), a second pool turn (non-stream), a named t3-gmail turn (stream),
and a subagent turn (stream) in the same client session. Checks HTTP 200, end_turn, exact text and
cache reads, then reads the injected date for each request from the host's request logs.
Prints no key, account identity or prompt text beyond the fixed probe strings.
"""
import json, os, re, subprocess, sys, time, urllib.request, urllib.error, uuid

BASE, LABEL = sys.argv[1].rstrip('/'), sys.argv[2]
LOGS = sys.argv[3] if len(sys.argv) > 3 else None
KEY = subprocess.check_output([os.path.expanduser('~/.local/bin/cliproxy-codex-token')], text=True).strip()
UA = 'claude-cli/2.1.322 (external, t3-code, agent-sdk/0.3.321)'
SESSION = str(uuid.uuid4())
MODEL = 'claude-haiku-5-5'


def call(model, messages, stream, agent=None):
    body = {'model': model, 'max_tokens': 64, 'stream': stream, 'messages': messages}
    headers = {'x-api-key': KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json',
               'user-agent': UA, 'x-claude-code-session-id': SESSION, 'x-app': 'cli'}
    if agent:
        headers['x-claude-code-agent-id'] = agent
    req = urllib.request.Request(BASE + '/v1/messages', data=json.dumps(body).encode(), headers=headers)
    started = time.time()
    out = {'stream': stream, 'model': model, 'agent': agent}
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            out['status'] = r.status
            raw = r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        out['status'] = e.code
        out['error'] = e.read().decode('utf-8', 'replace')[:300]
        return out
    out['seconds'] = round(time.time() - started, 1)
    text, usage, stop = '', {}, None
    if stream:
        for line in raw.splitlines():
            if not line.startswith('data:'):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            if ev.get('type') == 'content_block_delta':
                text += ev.get('delta', {}).get('text', '')
            elif ev.get('type') == 'message_start':
                usage.update(ev.get('message', {}).get('usage', {}))
            elif ev.get('type') == 'message_delta':
                stop = ev.get('delta', {}).get('stop_reason') or stop
                usage.update({k: v for k, v in ev.get('usage', {}).items() if v})
    else:
        d = json.loads(raw)
        text = ''.join(b.get('text', '') for b in d.get('content', []) if b.get('type') == 'text')
        usage, stop = d.get('usage', {}), d.get('stop_reason')
    out.update({'text': text.strip(), 'stop_reason': stop,
                'cache_read': usage.get('cache_read_input_tokens'), 'cache_write': usage.get('cache_creation_input_tokens')})
    return out


PAD = ''.join(f'Fleet gate reference line {i}: the quick brown fox jumps over the lazy dog.\n' for i in range(1200))
u1 = {'role': 'user', 'content': PAD + 'Reply with exactly: FLEET4_OK'}
results = []
results.append(('pool turn 1 stream', 'FLEET4_OK', call(MODEL, [u1], True)))
turn2 = [u1, {'role': 'assistant', 'content': 'FLEET4_OK'}, {'role': 'user', 'content': 'Reply with exactly: FLEET4_TWO'}]
results.append(('pool turn 2 non-stream', 'FLEET4_TWO', call(MODEL, turn2, False)))
results.append(('named t3-gmail stream', 'FLEET4_NAMED', call('t3-gmail/' + MODEL, [{'role': 'user', 'content': 'Reply with exactly: FLEET4_NAMED'}], True)))
results.append(('subagent stream', 'FLEET4_AGENT', call(MODEL, [{'role': 'user', 'content': 'Reply with exactly: FLEET4_AGENT'}], True, agent='gate-worker')))

passed = True
for name, want, r in results:
    ok = r.get('status') == 200 and r.get('stop_reason') == 'end_turn' and r.get('text') == want
    passed &= ok
    print(json.dumps({'check': name, 'ok': ok, **{k: v for k, v in r.items() if k != 'text'}, 'text_matches': r.get('text') == want}))

if LOGS:
    time.sleep(3)
    script = r'''
import os, re, sys, json
d, session = sys.argv[1], sys.argv[2]
pat = re.compile(r"Today's date is (\d{4}-\d{2}-\d{2})")
rows = []
for f in sorted((n for n in os.listdir(d) if re.match(r"v1-messages-\d", n)), reverse=True)[:400]:
    data = open(os.path.join(d, f), "rb").read().decode("utf-8", "replace")
    head = data.split("=== REQUEST BODY ===", 1)[0]
    if session not in head: continue
    api = data.split("=== API REQUEST 1 ===", 1)[-1]
    m = pat.search(api)
    agent = re.search(r"(?mi)^X-Claude-Code-Agent-Id:\s*(\S+)", head)
    model = re.search(r'"model"\s*:\s*"([^"]+)"', data.split("=== REQUEST BODY ===", 1)[-1])
    rows.append({"file_ts": f[12:29], "model": model.group(1) if model else None, "agent": agent.group(1) if agent else None, "date": m.group(1) if m else None})
print(json.dumps(sorted(rows, key=lambda r: r["file_ts"])))
'''
    if LOGS.startswith('ssh:'):
        cmd = ['ssh', '-o', 'BatchMode=yes', LOGS[4:], 'sudo -n python3 - /home/andrew/cliproxy/logs ' + SESSION]
        rows = json.loads(subprocess.run(cmd, input=script, capture_output=True, text=True).stdout or '[]')
    else:
        rows = json.loads(subprocess.run([sys.executable, '-I', '-c', script, LOGS, SESSION], capture_output=True, text=True).stdout or '[]')
    dates = {r['date'] for r in rows}
    print(json.dumps({'check': 'injected dates', 'requests_logged': len(rows), 'rows': rows,
                      'single_date_for_session': len(dates) == 1 and None not in dates}))
    passed &= len(rows) >= 4 and len(dates) == 1 and None not in dates
cache_ok = (results[1][2].get('cache_read') or 0) > 0
print(json.dumps({'check': 'turn 2 cache read', 'ok': cache_ok, 'cache_read': results[1][2].get('cache_read')}))
print(json.dumps({'gate': LABEL, 'passed': bool(passed and cache_ok)}))
sys.exit(0 if passed and cache_ok else 1)
