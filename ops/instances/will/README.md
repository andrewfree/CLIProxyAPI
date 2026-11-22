# Will CLIProxy instance

This directory is the secret-free owner contract for the primary CLIProxy
instance on `will.local`. The application repository owns the Compose shape,
image pin, configuration invariants, and live verifier. PiSec owns the host
catalog, expected exposure, and cross-workload coordination.

The mutable runtime remains under `/home/andrew/cliproxy` on Will. Never commit
or copy its `config.yaml`, `auths/`, `logs/`, or `plugins/` into this repository.
The tracked configuration policy contains only non-secret field names and
expected operational values.

## Verify source and live state

The Go owner-contract tests reject image, restart-policy, healthcheck, mount,
or published-port drift in the tracked files. The live verifier uses fixed,
read-only SSH probes and returns only stable drift codes; it never prints API
keys, OAuth tokens, request bodies, or model responses.

```sh
go test ./ops -count=1
python3 -m unittest -v ops.test_will_instance
python3 ops/instances/will/verify_live.py --json
```

The verifier checks all six Docker bindings against `127.0.0.1` and permits
only the two explicitly pinned Tailscale listeners in addition to loopback.
It verifies that Tailscale Serve forwards port 8317 to the local proxy,
verifies the running image and restart policy, compares the host
and in-container configuration hashes, enforces the retry/routing policy, and
requires `/healthz` HTTP 200. A clean verifier result is still not proof that a
provider can generate. After an update, also run an authenticated models check,
a real `/v1/responses` request, and the affected Codex/T3 thread.

## Request logging

The owner policy requires normal request logging and file logging, with a
1,024 MB total log-directory cap. Usage aggregation stays disabled. Logs survive
container replacement through the existing bind mount, but the size cleaner
can remove older files. This is bounded local retention, not an archival copy.

For a request-log setting change, preserve the mounted config file's inode and
permissions, then let the config watcher reload it. No container recreation is
needed. Confirm the container start time is unchanged and a new successful
`HEAD /healthz` request produces a normal request-log file. Ordinary HTTP GET
requests are skipped by the request logger. This check exercises logging
without a model generation and does not qualify a provider or client update.

## Copy-only request-log pilot

Before activation, obtain Andrew's explicit acceptance of **unencrypted pilot
archives on disk12** and the `promote fleet <id>` approval for the reviewed
installation. Keep `logs-max-total-size-mb: 1024` throughout. The mover writes
verified zstd level 3 copies and has read-only access to the source. A close
confirms that the writer released the same observed inode. It can also result
from an interrupted writer. Startup files, ambiguous closes and files found
after a watch gap can be copied after 60 seconds without an mtime change.
Those copies are marked `unconfirmed` in their manifests and reported
separately. A later close adds a `confirmation.json` only when a fresh read
matches the archived size, mtime and SHA-256. Changed bytes receive a new
archive and the earlier copy keeps its original evidence. Move-in events do
not establish completion.

The Samba `[will]` share exports `/srv/share` and maps its authorized identity
to `andrew`, which can read private files. Obtain separate approval for the
Samba veto and apply it before the first archive write. Under `[will]`, add
`/cliproxy-request-archive/` to `veto files` and keep `delete veto files = no`.
Preserve existing veto patterns and check alternate shares and aliases. Finish
the authorized SMB client probe after provisioning the tree and before starting
the mover. Confirm that the client can list the parent but cannot see or
traverse the archive or read a synthetic marker by explicit path. Keep the veto
in place while the archive holds sensitive data. Keep request names, hashes
and contents out of terminal output, receipts and aggregate status.

### Install and verify

From the reviewed checkout, upload the script and units to a private temporary
directory on Will. The installed script is outside the fleet bundle and owned
by root.

```sh
ssh will.local 'umask 077 && mkdir -p /home/andrew/.cache/cliproxy-will-log-mover-upload'
scp ops/will_log_mover.py ops/instances/will/cliproxy-will-log-mover@.service \
  ops/instances/will/cliproxy-will-log-mover-watchdog@.service \
  will.local:/home/andrew/.cache/cliproxy-will-log-mover-upload/
ssh will.local
```

On Will, confirm the disk identity against the reviewed preflight. It observed
`/dev/sdc1` with ext4. If the mount differs, verify the replacement device and
UUID before proceeding. The subshell holds its working directory on the
verified mount while it creates the private archive tree.

```sh
set -euo pipefail
umask 077
upload=/home/andrew/.cache/cliproxy-will-log-mover-upload
mountpoint=/srv/share/drives/disk12
archive_root=cliproxy-request-archive
archive_dir="$mountpoint/$archive_root/will/requests"
pilot_service="cliproxy-will-log-mover@${archive_root}.service"
pilot_watchdog="cliproxy-will-log-mover-watchdog@${archive_root}.service"
status_file=/run/cliproxy-will-log-mover/status.json
sudo testparm -s --section-name=will --parameter-name='veto files' | grep -F "/$archive_root/"
sudo testparm -s --section-name=will --parameter-name='delete veto files' | grep -ix no
/usr/bin/python3.14 -I -c 'from compression import zstd; assert zstd.ZstdFile'
findmnt --mountpoint "$mountpoint" -o TARGET,SOURCE,FSTYPE,UUID,OPTIONS
df -B1 --output=avail / "$mountpoint"
disk_uuid=$(findmnt -rn --mountpoint "$mountpoint" -t ext4 -o UUID)
test -n "$disk_uuid"
(
  cd -P -- "$mountpoint"
  test "$(findmnt -rn --mountpoint "$mountpoint" -o UUID)" = "$disk_uuid"
  test "$(stat -c %d .)" = "$(stat -Lc %r "/dev/disk/by-uuid/$disk_uuid")"
  for directory in "$archive_root" "$archive_root/will" "$archive_root/will/requests" "$archive_root/meta"; do
    test ! -L "$directory"
    if [ ! -d "$directory" ]; then
      mkdir -m 0700 -- "$directory"
    fi
    test "$(stat -c '%a:%U:%G' "$directory")" = 700:andrew:andrew
  done
)
sudo install -d -o root -g root -m 0755 /usr/local/lib/cliproxy-will-log-mover
sudo install -o root -g root -m 0755 "$upload/will_log_mover.py" \
  /usr/local/lib/cliproxy-will-log-mover/will_log_mover.py
printf 'WILL_LOG_MOVER_DISK_UUID=%s\n' "$disk_uuid" > "$upload/disk.env"
sudo install -o root -g root -m 0600 "$upload/disk.env" /etc/cliproxy-will-log-mover.env
sudo install -o root -g root -m 0644 "$upload/cliproxy-will-log-mover@.service" \
  "$upload/cliproxy-will-log-mover-watchdog@.service" /etc/systemd/system/
sudo systemd-analyze verify "$pilot_service" "$pilot_watchdog"
sudo systemctl daemon-reload
```

Use the literal instance name from the install block. It selects the archive
root for both the mover and the systemd write paths. Use one instance.
Finish the SMB client probe, record the current image digest and publisher
revision, and start the independent aggregate-only close counter before the
mover. Record the count of published manifests before and after the run while
the mover is stopped.

```sh
test "$(findmnt -rn --mountpoint "$mountpoint" -o UUID)" = "$disk_uuid"
find "$archive_dir" \
  -type d -name '.pending-*' -prune -o -type f -name manifest.json -printf '.' \
  2>/dev/null | wc -c
```

```sh
start_and_verify_pilot() {
  start_time=$(date +%s.%N) || return
  sudo systemctl start "$pilot_service" "$pilot_watchdog" || return
  /usr/bin/python3.14 -I - "$status_file" "$start_time" <<'PYTHON' || return
import json
from pathlib import Path
import sys
import time

path = Path(sys.argv[1])
start_time = float(sys.argv[2])
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        status = json.loads(path.read_text())
        if (status["heartbeat_unix"] > start_time
                and status["started_at_unix"] >= start_time
                and status["paused_reason"] is None
                and status["phase"] == "capturing" and status["source_watch_active"]):
            print(json.dumps(status, indent=2))
            break
    except (OSError, ValueError, KeyError):
        pass
    time.sleep(1)
else:
    raise SystemExit("Fresh heartbeat, successful destination probe, and armed watch did not appear")
PYTHON
  systemctl is-active "$pilot_service" && systemctl is-active "$pilot_watchdog"
}
if ! start_and_verify_pilot; then
  sudo systemctl stop "$pilot_watchdog" "$pilot_service" ||
    printf '%s\n' 'Could not stop both pilot services' >&2
  exit 1
fi
sudo journalctl -u "$pilot_watchdog" --since '-2 minutes' --no-pager || true
```

The mover pauses below 200 GiB free, when an item would cross that floor, or
when the expected mount is unavailable. It resumes with at least 250 GiB free
and a writable archive. The watcher retains up to 512 source descriptors
covering 2 GiB and continues reading events independently of disk12. Pending
entries beyond that budget can disappear before they are reopened. A writer
that closes after unlink cannot supply a capture pathname. Old staging
directories stay private and untouched. The status reports their startup count.

The unprivileged watchdog polls systemd and the mount every 30 seconds. It
journals state changes for an early or failed mover exit, stale heartbeat, or
missing mount. It uses systemd timestamps and the service result to judge an
exit. The heartbeat is only a liveness check. The watchdog has its own systemd
limit of 24 hours 5 minutes.
Will's existing root-space alarm uses a privileged Wazuh notification relay.
The pilot watchdog has no access to that relay's configuration. Andrew or his
designated pilot operator checks the watchdog journal and aggregate status
after startup and every 15 minutes through the recorded end, including pause,
miss, watch gap, and integrity counts in the status.

### Acceptance and rollback

- Check the 24-hour pilot window with a representative busy period. The
  admission cutoff is 23 hours 55 minutes after startup. Already-admitted
  work drains until the publication deadline at 23 hours 59 minutes. If work
  is unresolved at the deadline, the mover records a failed phase and the
  unfinished count and exits nonzero. Compare independent matching closes
  through the admission cutoff with confirmed archives, including archives
  with matching confirmation receipts. Report the boundary separately, along
  with missed, unconfirmed, later-confirmed, recovered-after-unlink, abandoned
  pending and watch gap counts. Any restart fails acceptance.
- Require zero integrity failures, misses, late captures, watch gaps and
  queue overflows. Verify a timed restore by comparing bytes and SHA-256 to the
  manifest, and report the aggregate result.
- Read the once-a-minute counter history under `$mountpoint/$archive_root/meta/`.
  Record peak pending files, bytes and age, the p50, p90 and maximum
  observed close-to-publish latency, minute ingress, verified drain rate,
  disk12 I/O and free space. The percentiles use 0.1-second buckets below one
  minute and one-second buckets after that. Run a synthetic drain of at least
  13 MiB/s, twice the busiest measured minute, before accepting the pilot.
- At the recorded end, check that the mover is inactive with a successful
  result, then stop the independent watchdog. The final status remains under
  `/run` until reboot. A mover `RuntimeMaxSec` timeout means the deadline logic
  failed. Both services are static. Preserve the archive, Samba veto and
  final status.

```sh
systemctl show -p ActiveState -p Result "$pilot_service" "$pilot_watchdog"
sudo systemctl stop "$pilot_watchdog"
```

For rollback, stop both services before reading diagnostics.

```sh
sudo systemctl stop "$pilot_watchdog" "$pilot_service"
systemctl show -p ActiveState -p Result "$pilot_service" "$pilot_watchdog" || true
if [ -r "$status_file" ]; then
  /usr/bin/python3.14 -I -m json.tool "$status_file" || true
fi
```

## Update boundary

1. Confirm no OAuth refresh or protected T3 request is in flight.
2. Update the image digest in this directory's Compose and workload contract in
   one focused commit. PiSec should report owner-contract drift until its
   inventory is updated to the same digest.
3. Back up Will's current Compose file, copy the reviewed tracked Compose file
   to `/home/andrew/cliproxy/docker-compose.yml`, and preserve mode `0600` and
   owner `andrew:andrew`.
4. Build or load the image pinned by `ops/fleet-release.json`, then recreate
   only `cli-proxy-api` with `--pull never --no-build`. Never delete the
   bind-mounted state directories.
5. Run the live verifier, then the authenticated model, generation, and exact
   client/thread checks. Roll back the Compose file and previous digest if any
   layer fails.

Docker engine/package updates and host reboots do not replace the bind-mounted
runtime state. A CLIProxy image change is an explicit contract change and must
pass the same verification sequence.
