#!/usr/bin/env python3
"""Report which client key each T3 provider-env secret file holds. stdin: JSON {"new": [...], "old": [...]}.
argv[1]: secrets dir. Prints file instance names and key labels only."""
import base64, json, os, sys

spec = json.loads(sys.stdin.read())
labels = {k.encode(): f'new{i + 1}' for i, k in enumerate(spec['new'])}
labels.update({k.encode(): f'old{i + 1}' for i, k in enumerate(spec.get('old', []))})
d = os.path.expanduser(sys.argv[1])
for f in sorted(os.listdir(d)):
    if not f.startswith('provider-env-') or 'QU5USFJPUElDX0FQSV9LRVk' not in f:
        continue
    inst = f.split('-')[2]
    try:
        name = base64.urlsafe_b64decode(inst + '=' * (-len(inst) % 4)).decode()
    except Exception:
        name = inst
    data = open(os.path.join(d, f), 'rb').read()
    found = [lab for k, lab in labels.items() if k in data]
    print(name, found or ['no known key (possibly encrypted)'])
