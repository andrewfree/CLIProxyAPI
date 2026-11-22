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

Install only after Andrew approves `promote fleet <id>` for this pilot.
Keep `logs-max-total-size-mb: 1024` throughout. The mover reads closed request
logs and writes verified zstd level 3 copies on disk12. The existing CLIProxy
cleaner continues to apply the cap. Pre-existing files and files seen only
during a scan have no close evidence and are counted as unconfirmed. A close
can also result from an interrupted writer. Treat move-in events as publication
of a closed file and record them separately in the independent event count.
The current CLIProxy producer writes directly to the final pathname.

Disk12 is unencrypted. Andrew approved plaintext storage for this 24-hour
pilot. The Samba `[will]` share exports `/srv/share` and maps its authorized
identity to `andrew`, which can read private files. Apply the separately
approved Samba veto before the first archive write. Under `[will]`, add
`/cliproxy-request-archive/` to `veto files` and keep `delete veto files = no`.
Preserve existing veto patterns and check alternate shares and aliases. After
provisioning the private tree, confirm the authorized SMB client can list the
parent but cannot see or traverse the archive or read a synthetic marker by
explicit path. Keep the veto in place while the archive holds sensitive data.
Keep request names, hashes and contents out of terminal output, receipts and
aggregate status. Private manifests carry the encoded basename and both hashes
for verification.

### Install and verify

From the reviewed checkout on the workstation, copy the script and units to a
private upload directory on Will. Install the code on Will's executable disk.

```sh
ssh will.local 'umask 077 && mkdir -p /home/andrew/cliproxy/ops/runtime/will-log-mover-upload'
scp ops/will_log_mover.py ops/instances/will/cliproxy-will-log-mover@.service \
  ops/instances/will/cliproxy-will-log-mover-watchdog@.service \
  ops/instances/will/cliproxy-will-log-mover-watchdog@.timer \
  will.local:/home/andrew/cliproxy/ops/runtime/will-log-mover-upload/
ssh will.local
```

On Will, confirm the disk's identity against the reviewed preflight. That
preflight observed `/dev/sdc1` with ext4. If the mount differs, verify the
device and filesystem UUID before proceeding. Pin the UUID so the mover can
reject a replacement disk. The subshell pins its working directory on disk12
while creating the private archive tree.

```sh
set -eu
umask 077
instance=/home/andrew/cliproxy
upload="$instance/ops/runtime/will-log-mover-upload"
mountpoint=/srv/share/drives/disk12
archive_root=cliproxy-request-archive
archive_dir="$mountpoint/$archive_root/will/requests"
pilot_service="cliproxy-will-log-mover@$archive_root.service"
pilot_timer="cliproxy-will-log-mover-watchdog@$archive_root.timer"
pilot_watchdog="cliproxy-will-log-mover-watchdog@$archive_root.service"
pilot_path=/usr/local/bin:/usr/bin:/bin:/home/andrew/.local/bin
sudo testparm -s --section-name=will --parameter-name='veto files' | grep -F "/$archive_root/"
sudo testparm -s --section-name=will --parameter-name='delete veto files' | grep -ix no
env PATH="$pilot_path" python3.14 -I --version
findmnt --mountpoint "$mountpoint" -o TARGET,SOURCE,FSTYPE,UUID,OPTIONS
disk_uuid=$(findmnt -rn --mountpoint "$mountpoint" -t ext4 -o UUID)
test -n "$disk_uuid"
(
  cd -P -- "$mountpoint"
  test "$(findmnt -rn --mountpoint "$mountpoint" -o UUID)" = "$disk_uuid"
  test "$(stat -c %d .)" = "$(stat -Lc %r "/dev/disk/by-uuid/$disk_uuid")"
  for directory in "$archive_root" "$archive_root/will" "$archive_root/will/requests"; do
    test ! -L "$directory"
    if [ ! -d "$directory" ]; then
      mkdir -m 0700 -- "$directory"
    fi
    test "$(stat -c '%a:%U:%G' "$directory")" = 700:andrew:andrew
  done
)
install -d -m 0700 "$instance/ops/runtime/will-log-mover"
install -m 0600 "$upload/will_log_mover.py" "$instance/ops/will_log_mover.py"
printf 'WILL_LOG_MOVER_DISK_UUID=%s\nPATH=%s\n' "$disk_uuid" "$pilot_path" > "$upload/disk.env"
sudo install -m 0600 "$upload/disk.env" /etc/cliproxy-will-log-mover.env
sudo install -m 0644 "$upload/cliproxy-will-log-mover@.service" \
  "$upload/cliproxy-will-log-mover-watchdog@.service" \
  "$upload/cliproxy-will-log-mover-watchdog@.timer" /etc/systemd/system/
sudo systemd-analyze verify "$pilot_service" "$pilot_watchdog" "$pilot_timer"
sudo systemctl daemon-reload
```

The systemd instance name supplies the archive root to both the mover and its
writable path. Use a single instance for this pilot. Finish the SMB client check
with a synthetic marker before starting it.

Start the independent, aggregate-only close counter before starting the service.
Record the current image digest and publisher revision first. Start the pilot
without enabling it at boot. A fresh run gets its own status counters.
Record the manifest count immediately before and after the run, with the mover
stopped. This counts published manifests without printing archive identifiers.
Check the expected mount before each count and keep any traversal failure out of
the acceptance comparison.

```sh
set -o pipefail
find "$archive_dir" \
  -type d -name '.pending-*' -prune -o -type f -name manifest.json -printf '.' \
  2>/dev/null | wc -c
```

```sh
sudo systemctl start "$pilot_service" "$pilot_timer"
systemctl is-active "$pilot_service" "$pilot_timer"
systemctl list-timers "$pilot_timer"
python3.14 -m json.tool /home/andrew/cliproxy/ops/runtime/will-log-mover/status.json
sudo journalctl -u "$pilot_watchdog" --since '-2 minutes' --no-pager
```

The service stops itself after 24 hours. It has neither automatic restart nor
boot enablement. The watchdog warns if the service exits early. The mover
pauses capture below 200 GiB
available, when an item would cross that floor or when it cannot use the
expected mount. It resumes with a writable archive and at least 250 GiB free.
The watcher still counts completed and pending records while copying is paused.
The held-descriptor budget is 512 files and 2 GiB. Backlog beyond that budget
keeps completion metadata for reconciliation, but the cleaner can remove those
files before they are reopened. Staged work survives a restart. Check the
watchdog and status during the run, including peak activity.

### Acceptance and rollback

- Compare an independent matching close and move-in count with the durable
  manifests over the 24-hour interval and a representative busy period. Record
  boundary counts, any restarts, and any files recovered from an earlier stage
  so they do not enter the new-event total. Report missed records, captures
  after cleaner unlink, unconfirmed records, and any watch gaps.
- Require zero integrity failures, misses, late captures, watch gaps and
  queue overflows. Check that only CLIProxy's existing cap can delete source
  files. Verify a timed restore by comparing bytes and SHA-256 to the
  manifest and report only the aggregate result.
- Record oldest pending age, peak byte ingress per minute, verified drain
  throughput and disk12 I/O. Run a synthetic drain at twice the busiest
  measured minute, at least 13 MiB/s, before accepting the pilot. Confirm
  headroom during live peaks and record disk12's remaining free space.
- Record the planned and actual pilot end times. At the recorded end, or for
  rollback, stop the watchdog timer and the mover. Preserve all archived
  copies, the Samba veto and the final status for comparison.

```sh
sudo systemctl stop "$pilot_timer" "$pilot_service" "$pilot_watchdog"
sudo systemctl disable "$pilot_timer"
systemctl show -p ActiveState -p UnitFileState "$pilot_service" "$pilot_timer"
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
