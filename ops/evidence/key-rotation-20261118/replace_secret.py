#!/usr/bin/env python3
"""Replace one exact secret value with another in a file, in place (same inode), with a private backup.
stdin: JSON {"old": "...", "new": "..."}. argv[1]: file. argv[2]: backup dir. Prints counts and hashes only."""
import hashlib, json, os, sys, time

spec = json.loads(sys.stdin.read())
old, new = spec['old'].encode(), spec['new'].encode()
path, backup = os.path.expanduser(sys.argv[1]), os.path.expanduser(sys.argv[2])
st = os.stat(path)
data = open(path, 'rb').read()
n = data.count(old)
if n != 1 or new in data:
    sys.exit(f'refusing: old value occurs {n} times, new present {new in data}; nothing changed')
os.makedirs(backup, mode=0o700, exist_ok=True)
stamp = time.strftime('%Y%m%dT%H%M%S')
fd = os.open(os.path.join(backup, f'{os.path.basename(path)}.{stamp}.bak'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.write(fd, data)
os.close(fd)
out = data.replace(old, new)
with open(path, 'r+b') as f:
    f.seek(0); f.write(out); f.truncate(len(out)); f.flush(); os.fsync(f.fileno())
st2 = os.stat(path)
print(json.dumps({'file': sys.argv[1], 'replaced': 1, 'inode_same': st2.st_ino == st.st_ino, 'mode': oct(st2.st_mode & 0o777),
                  'sha_before': hashlib.sha256(data).hexdigest()[:12], 'sha_after': hashlib.sha256(out).hexdigest()[:12]}))
