#!/usr/bin/env python3
"""Remove client-key list lines whose value hashes match old_sha256, in place (same inode), with a backup.
stdin: JSON {"old_sha256": [...], "keep_sha256": [...]}. argv[1]: config. argv[2]: backup dir. argv[3]: --dry-run optional.
Refuses unless every old hash is found exactly once and every keep hash remains. Prints counts only."""
import hashlib, json, os, re, sys, time

spec = json.loads(sys.stdin.read())
path, backup = sys.argv[1], sys.argv[2]
dry = len(sys.argv) > 3 and sys.argv[3] == '--dry-run'
pat = re.compile(r'^(\s*-\s*)(["\']?)([^"\'\s#]+)\2(\s*(#.*)?)$')
st = os.stat(path)
data = open(path, 'rb').read()
lines = data.decode().splitlines(keepends=True)
drop, keep_seen = [], set()
for i, line in enumerate(lines):
    m = pat.match(line.rstrip('\n'))
    if not m:
        continue
    h = hashlib.sha256(m.group(3).encode()).hexdigest()
    if h in spec['old_sha256']:
        drop.append((i, h))
    if h in spec['keep_sha256']:
        keep_seen.add(h)
if sorted(h for _, h in drop) != sorted(spec['old_sha256']) or keep_seen != set(spec['keep_sha256']):
    sys.exit(f'refusing: old lines found {len(drop)}, keep keys found {len(keep_seen)}; nothing changed')
out = ''.join(l for i, l in enumerate(lines) if i not in {i for i, _ in drop}).encode()
if dry:
    print(json.dumps({'dry_run': True, 'would_remove': len(drop), 'keep_present': len(keep_seen)}))
    sys.exit(0)
os.makedirs(backup, mode=0o700, exist_ok=True)
fd = os.open(os.path.join(backup, f'config.yaml.pre-remove-keys.{time.strftime("%Y%m%dT%H%M%S")}.bak'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, data)
os.close(fd)
with open(path, 'r+b') as f:
    f.seek(0); f.write(out); f.truncate(len(out)); f.flush(); os.fsync(f.fileno())
print(json.dumps({'removed': len(drop), 'keep_present': len(keep_seen), 'inode_same': os.stat(path).st_ino == st.st_ino,
                  'sha_after': hashlib.sha256(out).hexdigest()[:12]}))
