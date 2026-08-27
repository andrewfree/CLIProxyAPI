# CLIProxy recovery operations

This directory owns the host-side recovery and archive-before-prune paths for the localhost CLIProxy service. It does not contain credentials. Runtime state and the jobs' bounded logs live in ignored `ops/runtime/`.

## Recovery behavior

`com.rever.cliproxy-watchdog` runs at login and every 120 seconds. One run performs these checks in order:

1. `docker info` must finish within 10 seconds.
2. `curl -fsS --max-time 3 http://127.0.0.1:8317/healthz` must succeed.
3. From inside `cli-proxy-api`, Docker DNS and a verified TLS handshake must work for `chatgpt.com`, `platform.claude.com`, and `auth.kimi.com`.
4. OAuth metadata and the latest refresh outcome are summarized without reading token values into output.

Recovery is deliberately narrow:

- Dead Docker engine: `docker desktop stop --force --timeout 30`, then `docker desktop start --detach`; poll `docker info` for up to two minutes.
- Healthy engine but dead CLIProxy port: `docker compose up -d --no-deps cli-proxy-api` from `/Users/rever/cliproxy`.
- Total container DNS failure twice in a row: restart only `cli-proxy-api`; if all three DNS probes still fail, restart Docker Desktop and reconcile Compose.

Desktop restarts are limited to once per 5 minutes, Compose repairs once per 2 minutes, and container restarts once per 2 minutes. The state file is `ops/runtime/state.json`. The watchdog log rotates at 1 MB with three backups.

Factory reset, Docker volume deletion, and deleting `Docker.raw` are never watchdog actions.

`com.rever.cliproxy-log-archive` runs at login and every 300 seconds. It handles only closed `.log` and `.log.gz` files older than one hour; the active `main.log` and recent request logs are never candidates. Each candidate is copied to TheCloud, SHA-256 checked, recorded in the NAS manifest, and fingerprinted again before the local copy is unlinked. A missing or unwritable NAS mount means zero pruning. Lumberjack's closed `main-<timestamp>.log` rotations are eligible, so the active `main.log` remains local while completed rotations are preserved in the cloud archive.

## Install and inspect

```bash
cd /Users/rever/cliproxy
./ops/install_watchdog.sh
launchctl print gui/$(id -u)/com.rever.cliproxy-watchdog
launchctl print gui/$(id -u)/com.rever.cliproxy-log-archive
tail -n 100 ops/runtime/watchdog.log
tail -n 100 ops/runtime/archive.log
```

Run the infrastructure checks without recovery actions or auth gating:

```bash
python3 ops/cliproxy_watchdog.py --check-only --skip-auth --verbose
python3 ops/archive_logs.py --check-only --verbose
```

Run the full test suite and validate the launchd property list:

```bash
python3 -m unittest -v \
  ops.test_auth_health \
  ops.test_archive_logs \
  ops.test_cliproxy_watchdog \
  ops.test_launchd_plist
plutil -lint \
  ops/com.rever.cliproxy-watchdog.plist \
  ops/com.rever.cliproxy-log-archive.plist
```

A controlled container-recovery drill is:

```bash
docker stop cli-proxy-api
launchctl kickstart -k gui/$(id -u)/com.rever.cliproxy-watchdog
curl -fsS --max-time 3 http://127.0.0.1:8317/healthz
docker inspect cli-proxy-api --format '{{.State.Status}} {{.State.Health.Status}}'
```

A controlled engine-recovery drill uses the same non-reset command as the incident recovery:

```bash
docker desktop stop --force --timeout 30
launchctl kickstart -k gui/$(id -u)/com.rever.cliproxy-watchdog
```

Then poll `docker info`, healthz, and the container health. This briefly interrupts every local Docker workload, so do it only as an intentional maintenance test.

## OAuth health and reauthentication

The reporter prints only provider, email when present, expiry, disabled state, refresh result, file name, and permissions:

```bash
cd /Users/rever/cliproxy
python3 ops/auth_health.py
```

It alerts on `disabled`, expired or long-lived tokens within 3 days of expiry, repeated refresh failures, `invalid_grant`, and 401/403-style grant failures. Kimi access tokens are intentionally short-lived; a recent successful refresh is reported as `short_lived_auto_refresh_ok` instead of a false alarm.

### Claude

Use the supported callback flow from the running container:

```bash
docker exec -it cli-proxy-api \
  /CLIProxyAPI/CLIProxyAPI \
  -config /CLIProxyAPI/config.yaml \
  -claude-login \
  -no-browser
```

The command prints an authorization URL and waits up to 5 minutes. Open that URL manually; do not have automation preview or fetch it. The browser callback to `http://localhost:54545/callback` reaches the login process through the existing loopback-only Compose port. A successful login replaces `auths/claude-<email>.json` with mode 0600 data.

Do not restart CLIProxy while testing or waiting on a token refresh. Claude refresh tokens rotate; killing the process after the provider consumes the old token but before CLIProxy saves the response can strand the old token on disk.

### Kimi

Kimi currently refreshes automatically. Only reauthenticate after the reporter shows a real post-network refresh failure:

```bash
docker exec -it cli-proxy-api \
  /CLIProxyAPI/CLIProxyAPI \
  -config /CLIProxyAPI/config.yaml \
  -kimi-login \
  -no-browser
```

Visit the printed device URL and enter the printed code. Do not preview the one-time URL with automation.

### Codex

Do not reauthenticate a Codex account merely because Docker or 8317 failed. A real post-recovery OpenAI/Codex 401 or `invalid_grant` for that auth file is required. If proven, use the supported device flow:

```bash
docker exec -it cli-proxy-api \
  /CLIProxyAPI/CLIProxyAPI \
  -config /CLIProxyAPI/config.yaml \
  -codex-device-login \
  -no-browser
```

## Log retention

`config.yaml` deliberately sets `logs-max-total-size-mb: 0`, disabling CLIProxy's delete-based cleaner. Do not set a positive cap: the cleaner deletes immediately on config reload and cannot verify an archive first.

The durable archive is:

```text
/Users/rever/Volumes/TheCloud/backups/llm-logs/cliproxy/m5/live/
```

Closed logs are grouped by UTC modification date under `requests/YYYY/MM/DD/`; `SHA256SUMS` is append-only evidence for archived content. The local copy is pruned only after content verification. If TheCloud is unavailable, local files accumulate and `ops/runtime/archive.log` records the failure. `main.log` itself is never pruned while active and rolls at 10 MB; its closed rotations then follow the same archive path.

To archive without pruning, use:

```bash
python3 ops/archive_logs.py --archive-only --verbose
```

The August 27 recovery set and downstream reconstruction are described in `ops/RECOVERY-2026-08-27.md`.
