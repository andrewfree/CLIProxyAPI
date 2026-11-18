#!/bin/zsh
# Emit {"new": [...], "old": [...]} from the Mac config and the private new-key file. Pipe only; never print to a terminal.
cd "${0:A:h}" && uv run -q --no-project --with pyyaml python -I -c "
import yaml,json,os
c=yaml.safe_load(open(os.path.expanduser('~/cliproxy/config.yaml')))
ks=[k.strip() for k in c['access']['api-keys']]
n=json.load(open('new-client-keys.json'))['new']
print(json.dumps({'new':n,'old':[k for k in ks if k not in n]}))"
