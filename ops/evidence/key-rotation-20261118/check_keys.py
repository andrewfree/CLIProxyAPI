#!/usr/bin/env python3
"""Check which client keys a CLIProxy host accepts. stdin: JSON {"new": [...]} plus optional "old": [...].
argv[1]: base URL. Prints only labels and HTTP status codes."""
import json, sys, urllib.request, urllib.error

spec = json.loads(sys.stdin.read())
base = sys.argv[1].rstrip('/')
labels = [(f'new{i + 1}', k) for i, k in enumerate(spec.get('new', []))] + [(f'old{i + 1}', k) for i, k in enumerate(spec.get('old', []))]
out = {}
for label, key in labels:
    req = urllib.request.Request(base + '/v1/models', headers={'Authorization': 'Bearer ' + key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            out[label] = r.status
    except urllib.error.HTTPError as e:
        out[label] = e.code
    except Exception as e:
        out[label] = type(e).__name__
print(json.dumps(out))
