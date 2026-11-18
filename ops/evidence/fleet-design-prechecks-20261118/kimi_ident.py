#!/usr/bin/env python3
"""Print hashed identity fields of Kimi auth files. argv[1]: auths dir. No secret values."""
import base64, hashlib, json, os, sys
d = os.path.expanduser(sys.argv[1])
def h(v): return hashlib.sha256(str(v).encode()).hexdigest()[:10]
def jwt_claims(tok):
    try:
        p = tok.split('.')[1]; return json.loads(base64.urlsafe_b64decode(p + '=' * (-len(p) % 4)))
    except Exception: return {}
for f in sorted(os.listdir(d)):
    if 'kimi' not in f.lower() or not f.endswith('.json'): continue
    try: j = json.load(open(os.path.join(d, f)))
    except Exception as e: print(h(f), 'unreadable', type(e).__name__); continue
    ident = {}
    for k in ('email', 'user_id', 'account_id', 'sub', 'id', 'label', 'phone'):
        if j.get(k): ident[k] = h(j[k])
    for tk in ('access_token', 'id_token', 'refresh_token'):
        c = jwt_claims(j.get(tk) or '') if isinstance(j.get(tk), str) else {}
        for k in ('sub', 'user_id', 'uid', 'email', 'account_id'):
            if c.get(k): ident[f'{tk[:2]}.{k}'] = h(c[k])
    print('file', h(f), 'type', j.get('type'), 'prefix', j.get('prefix'), 'disabled', j.get('disabled'), 'keys', sorted(k for k in j if 'token' not in k and 'key' not in k)[:12], 'ident', ident)
