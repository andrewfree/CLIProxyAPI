#!/usr/bin/env python3
"""Insert new client keys before the current first client key, in place, keeping the inode.

stdin: JSON {"new": [k1, k2], "old_sha256": [h1, h2]}. argv[1]: config path. argv[2]: backup dir.
Prints only counts, hashes of the file, and inode facts. Never prints a key.
"""
import hashlib, json, os, re, sys, time

spec = json.loads(sys.stdin.read())
cfg_path, backup_dir = sys.argv[1], sys.argv[2]
new = spec['new']
old_hashes = spec['old_sha256']
st = os.stat(cfg_path)
data = open(cfg_path, 'rb').read()
lines = data.decode().splitlines(keepends=True)
pattern = re.compile(r'^(\s*-\s*)(["\']?)([^"\'\s#]+)\2(\s*(#.*)?)$')
first_idx = None
present = set()
for i, line in enumerate(lines):
    m = pattern.match(line.rstrip('\n'))
    if not m:
        continue
    h = hashlib.sha256(m.group(3).encode()).hexdigest()
    if h in old_hashes:
        present.add(h)
        if h == old_hashes[0] and first_idx is None:
            first_idx = i
    if m.group(3) in new:
        sys.exit('refusing: a new key is already present; nothing changed')
if first_idx is None or present != set(old_hashes):
    sys.exit(f'refusing: old keys found {len(present)} of 2, first key line found {first_idx is not None}; nothing changed')
m = pattern.match(lines[first_idx].rstrip('\n'))
indent, quote = m.group(1), m.group(2)
inserted = [f'{indent}{quote}{k}{quote}\n' for k in new]
out = ''.join(lines[:first_idx] + inserted + lines[first_idx:]).encode()
os.makedirs(backup_dir, mode=0o700, exist_ok=True)
stamp = time.strftime('%Y%m%dT%H%M%S')
fd = os.open(os.path.join(backup_dir, f'config.yaml.pre-add-keys.{stamp}.bak'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'wb') as b:
    b.write(data)
with open(cfg_path, 'r+b') as f:
    f.seek(0); f.write(out); f.truncate(len(out)); f.flush(); os.fsync(f.fileno())
st2 = os.stat(cfg_path)
print(json.dumps({'inserted_lines': len(inserted), 'at_line': first_idx + 1, 'inode_same': st2.st_ino == st.st_ino,
                  'mode': oct(st2.st_mode & 0o777), 'sha_before': hashlib.sha256(data).hexdigest()[:12],
                  'sha_after': hashlib.sha256(out).hexdigest()[:12], 'time': stamp}))
