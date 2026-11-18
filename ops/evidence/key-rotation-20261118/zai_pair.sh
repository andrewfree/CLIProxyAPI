#!/bin/zsh
# Emit {"old": <current Z.ai key from Mac config>, "new": <zai-new.key>} on stdout for piping only.
cd "${0:A:h}" && uv run -q --no-project --with pyyaml python -I -c "
import yaml,json,os
c=yaml.safe_load(open(os.path.expanduser('~/cliproxy/config.yaml')))
old=[e['api-key'].strip() for p in c['api-keys']['openai-compatibility'] if p['name']=='zai-coding-plan' for e in p['keys']][0]
print(json.dumps({'old':old,'new':open('zai-new.key').read().strip()}))"
