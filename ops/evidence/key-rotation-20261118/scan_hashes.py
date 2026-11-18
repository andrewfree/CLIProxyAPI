import json, sys, hashlib, os
lab = json.loads(sys.stdin.read())
roots = ['~/.config', '~/.local/bin', '~/.local/share/opencode', '~/.t3/userdata/secrets', '~/.t3/userdata/settings.json', '~/.t3/service.env',
         '~/.codex/config.toml', '~/.claude/settings.json', '~/.zshenv', '~/.zshrc', '~/cliproxy/config.yaml', '~/cliproxy/.env', '~/.grok', '~/.opencode']
for r in roots:
    p = os.path.expanduser(r)
    paths = [p] if os.path.isfile(p) else [os.path.join(d, f) for d, _, fs in os.walk(p) for f in fs if '.git' not in d and 'node_modules' not in d] if os.path.isdir(p) else []
    for q in paths:
        try:
            data = open(q, 'rb').read()
        except Exception:
            continue
        if len(data) > 5_000_000:
            continue
        seen = set()
        for tok in data.replace(b'\x00', b' ').replace(b'=', b' ').replace(b':', b' ').split():
            h = hashlib.sha256(tok.strip(b'"\',;')).hexdigest()
            if h in lab and h not in seen:
                seen.add(h); print(os.uname().nodename.split('.')[0], q.replace(os.path.expanduser('~'), '~'), lab[h])
