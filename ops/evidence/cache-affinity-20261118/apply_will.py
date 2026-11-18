#!/usr/bin/env python3
"""Run on Will via ssh stdin: in-place, inode-preserving session-affinity edit.

Usage: python3 - apply|revert|dry-run
Prints only the changed line, file identity and non-secret loaded values.
"""
import hashlib, json, os, shutil, subprocess, sys, time, urllib.request

ROOT = '/home/andrew/cliproxy'
CFG = f'{ROOT}/config.yaml'
OUT = f'{ROOT}/ops/runtime/cache-affinity-apply-20261118'
OLD, NEW = '  session-affinity: false\n', '  session-affinity: true\n'
TZ_OLD, TZ_NEW = '  runtime-version: "v26.12.1"\n', '  runtime-version: "v26.12.1"\n  timezone: "America/Los_Angeles"\n'


def mgmt(path):
    key = [l.split('=', 1)[1].strip().strip('"').strip("'") for l in open(f'{ROOT}/.env')
           if l.startswith('MANAGEMENT_PASSWORD=')][0]
    req = urllib.request.Request('http://127.0.0.1:8317' + path, headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def loaded():
    c = mgmt('/v0/management/config')
    rt = c.get('routing') or {}
    return {'session-affinity': rt.get('session-affinity'), 'rebind-on': rt.get('session-affinity-rebind-on'),
            'subagents': rt.get('session-affinity-subagents'), 'ttl': rt.get('session-affinity-ttl'),
            'transient-error-cooldown-seconds': c.get('transient-error-cooldown-seconds'),
            'max-retry-interval': c.get('max-retry-interval'),
            'timezone': (c.get('claude-header-defaults') or {}).get('timezone')}


MODES = ('dry-run', 'apply', 'revert', 'timezone', 'revert-timezone', 'dry-run-timezone')


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in MODES:
        sys.exit(f'usage: python3 - {"|".join(MODES)} (got {sys.argv[1:]!r}); nothing was changed')
    mode = sys.argv[1]
    if mode in ('timezone', 'dry-run-timezone'):
        old, new = TZ_OLD, TZ_NEW
    elif mode == 'revert-timezone':
        old, new = TZ_NEW, TZ_OLD
    else:
        old, new = (NEW, OLD) if mode == 'revert' else (OLD, NEW)
    st = os.stat(CFG)
    before = open(CFG, 'rb').read()
    text = before.decode()
    n = text.count(old)
    info = {'mode': mode, 'inode': st.st_ino, 'perm': oct(st.st_mode & 0o777), 'anchor_count': n,
            'sha_before': hashlib.sha256(before).hexdigest()[:16], 'free_gib': shutil.disk_usage(ROOT).free >> 30,
            'loaded_before': loaded()}
    if n != 1:
        print(json.dumps(info)); sys.exit('refusing: anchor count is not 1')
    if mode in ('dry-run', 'dry-run-timezone'):
        print(json.dumps(info)); return
    os.makedirs(OUT, mode=0o700, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%S')
    fd = os.open(f'{OUT}/config.yaml.{mode}.{stamp}.bak', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as b:
        b.write(before)
    data = text.replace(old, new).encode()
    with open(CFG, 'r+b') as f:
        f.seek(0); f.write(data); f.truncate(len(data)); f.flush(); os.fsync(f.fileno())
    time.sleep(8)
    st2 = os.stat(CFG)
    csha = subprocess.check_output(['docker', 'exec', 'cli-proxy-api', 'sha256sum', '/CLIProxyAPI/config.yaml'],
                                   text=True).split()[0]
    info.update({'time': stamp, 'inode_after': st2.st_ino, 'perm_after': oct(st2.st_mode & 0o777),
                 'sha_after': hashlib.sha256(data).hexdigest()[:16], 'container_sha': csha[:16],
                 'loaded_after': loaded()})
    want = (mode == 'apply')
    checks = {
        'inode_unchanged': st2.st_ino == st.st_ino,
        'perm_600': (st2.st_mode & 0o777) == 0o600,
        'container_matches_host': csha[:16] == info['sha_after'],
    }
    if mode in ('apply', 'revert'):
        checks['loaded_affinity'] = bool(info['loaded_after']['session-affinity']) == want
    else:
        checks['loaded_timezone'] = (info['loaded_after']['timezone'] == 'America/Los_Angeles') == (mode == 'timezone')
    info['checks'] = checks
    with open(f'{OUT}/receipt-{mode}-{stamp}.json', 'w') as r:
        json.dump(info, r, indent=1)
    print(json.dumps(info))
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        sys.exit(f'POST-CHECK FAILED: {failed}; backup is in {OUT}')


main()
