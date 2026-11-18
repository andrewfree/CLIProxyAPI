#!/usr/bin/env python3
"""In-place, inode-preserving edits of the Mac CLIProxy config for the cache-affinity change.

Usage: apply_mac.py routing|alias|revert-routing|revert-alias [--dry-run]
Prints only the changed lines and non-secret loaded values. Never prints keys.
"""
import hashlib, json, os, shutil, subprocess, sys, time, urllib.request

ROOT = '/Users/rever/cliproxy'
CFG = f'{ROOT}/config.yaml'
OUT = os.path.dirname(os.path.abspath(__file__))

EDITS = {
    'routing': [
        ('    session-affinity: false\n', '    session-affinity: true\n'),
        ('        quota-cooldown-max-seconds: 3600\n    retry:\n',
         '        quota-cooldown-max-seconds: 3600\n        transient-error-cooldown-seconds: -1\n    retry:\n'),
        ('        max-retry-credentials: 0\nquota-exceeded:\n',
         '        max-retry-credentials: 0\n        max-retry-interval: 75\nquota-exceeded:\n'),
    ],
    'alias': [
        ('            - name: "glm-5.3-flash"\n              alias: "glm-5.3-flash"\nconfig-version: 8\n',
         '            - name: "glm-5.3-flash"\n              alias: "glm-5.3-flash"\n'
         '            - name: "glm-5.3-flash"\n              alias: "glm-5p3-flash"\nconfig-version: 8\n'),
    ],
}
EDITS['timezone'] = [
    ('            runtime-version: "v26.12.1"\n',
     '            runtime-version: "v26.12.1"\n            timezone: "America/Los_Angeles"\n'),
]
EDITS['revert-timezone'] = [(new, old) for old, new in EDITS['timezone']]
EDITS['revert-routing'] = [(new, old) for old, new in EDITS['routing']]
EDITS['revert-alias'] = [(new, old) for old, new in EDITS['alias']]


def sha(b):
    return hashlib.sha256(b).hexdigest()


def mgmt(path):
    key = None
    for line in open(f'{ROOT}/.env'):
        if line.startswith('MANAGEMENT_PASSWORD='):
            key = line.split('=', 1)[1].strip().strip('"').strip("'")
    req = urllib.request.Request('http://127.0.0.1:8317' + path, headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def loaded():
    c = mgmt('/v0/management/config')
    routing = c.get('routing') or {}
    return {
        'session-affinity': routing.get('session-affinity'),
        'rebind-on': routing.get('session-affinity-rebind-on'),
        'subagents': routing.get('session-affinity-subagents'),
        'ttl': routing.get('session-affinity-ttl'),
        'transient-error-cooldown-seconds': c.get('transient-error-cooldown-seconds'),
        'max-retry-interval': c.get('max-retry-interval'),
        'request-log': c.get('request-log'),
        'debug': c.get('debug'),
        'timezone': (c.get('claude-header-defaults') or {}).get('timezone'),
    }


def main():
    args = sys.argv[1:]
    if not args or args[0] not in EDITS or any(a != '--dry-run' for a in args[1:]):
        sys.exit(f'usage: apply_mac.py {"|".join(EDITS)} [--dry-run] (got {args!r}); nothing was changed')
    step = args[0]
    dry = '--dry-run' in args[1:]
    st = os.stat(CFG)
    free = shutil.disk_usage(ROOT).free
    if free < 1 << 30:
        sys.exit(f'refusing: only {free >> 20} MiB free')
    with open(CFG, 'rb') as f:
        before = f.read()
    text = before.decode()
    after = text
    for old, new in EDITS[step]:
        n = after.count(old)
        if n != 1:
            sys.exit(f'refusing: anchor found {n} times: {old.splitlines()[0]!r}')
        after = after.replace(old, new)
    changed = [l for l in after.splitlines() if l not in text.splitlines()] + \
              ['-' + l for l in text.splitlines() if l not in after.splitlines()]
    print(json.dumps({'step': step, 'dry_run': dry, 'inode': st.st_ino, 'mode': oct(st.st_mode & 0o777),
                      'sha_before': sha(before), 'changed_lines': changed}, indent=1))
    if dry:
        return
    stamp = time.strftime('%Y%m%dT%H%M%S')
    backup = f'{OUT}/config.yaml.{step}.{stamp}.bak'
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as b:
        b.write(before)
    data = after.encode()
    with open(CFG, 'r+b') as f:
        f.seek(0)
        f.write(data)
        f.truncate(len(data))
        f.flush()
        os.fsync(f.fileno())
    st2 = os.stat(CFG)
    container_sha = subprocess.check_output(
        ['docker', 'exec', 'cli-proxy-api', 'sha256sum', '/CLIProxyAPI/config.yaml'], text=True).split()[0]
    time.sleep(8)
    receipt = {'step': step, 'time': stamp, 'inode_before': st.st_ino, 'inode_after': st2.st_ino,
               'mode_after': oct(st2.st_mode & 0o777), 'sha_before': sha(before), 'sha_after': sha(data),
               'container_sha': container_sha, 'loaded': loaded(), 'backup': backup}
    receipt['checks'] = {
        'inode_unchanged': st2.st_ino == st.st_ino,
        'perm_600': (st2.st_mode & 0o777) == 0o600,
        'container_matches_host': container_sha == receipt['sha_after'],
    }
    with open(f'{OUT}/receipt-{step}-{stamp}.json', 'w') as r:
        json.dump(receipt, r, indent=1)
    print(json.dumps(receipt, indent=1))
    failed = [k for k, ok in receipt['checks'].items() if not ok]
    if failed:
        sys.exit(f'POST-CHECK FAILED: {failed}; backup is {backup}')


if __name__ == '__main__':
    main()
