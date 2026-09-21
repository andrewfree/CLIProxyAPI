# CLIProxy recovery operations

This directory owns the host-side recovery and archive-before-prune paths for the localhost CLIProxy service. It does not contain credentials. Runtime state and the jobs' bounded logs live in ignored `ops/runtime/`.

The primary Linux instance has its own secret-free owner contract under
[`instances/will/`](instances/will/README.md). That contract is separate from
the M5 Compose contract and gives PiSec a stable source for Will's image,
loopback exposure, health, and configuration-policy expectations.

## Choosing a Claude account in T3 Code

`claude_account_launcher.mjs` lets separate T3 Claude provider entries use
specific CLIProxy accounts. Each entry runs the normal Claude binary through
the same launcher. The launcher starts an ephemeral loopback listener for that
Claude process and adds the selected account's model prefix before forwarding
requests to the existing proxy. It streams replies and forwards errors without
trying a different account. The listener closes when Claude exits.

Give each Claude auth file a unique `prefix` through the management API. The
local iCloud and Gmail accounts use `t3-icloud` and `t3-gmail`. Keep
`force-model-prefix` false so existing applications can continue using the
unprefixed shared pool.

Add one T3 Claude provider instance per account, with this launcher's absolute
path as its binary. Set these instance environment variables:

| Variable | Value |
| --- | --- |
| `CLIPROXY_ACCOUNT_PREFIX` | The account's unique prefix |
| `CLIPROXY_BASE_URL` | The existing proxy origin |
| `CLIPROXY_CLAUDE_BINARY` | Absolute path to the real Claude executable |
| `ANTHROPIC_API_KEY` | Proxy API key, stored as a sensitive T3 variable |
| `ANTHROPIC_AUTH_TOKEN` | Empty |
| `CLAUDE_CODE_OAUTH_TOKEN` | Empty |

Use the same Claude config directory for both entries to keep them compatible
with existing Claude threads. Choose the named account entry in T3's model
picker. A selected account's cooldown is returned to that chat; choosing another
account is a manual action. The launcher's model list only includes its chosen
account, with ordinary model names. Give subagents the same provider entry to
keep their requests on that account too.

Apply T3 provider changes through its settings UI or `server.updateSettings`
API. Do not edit the running instance's database or settings file directly.
The launcher needs Node.js on the provider process's PATH and has no additional
package dependencies. No separate long-running service or fixed port is needed.

Verify the launcher with `node --test ops/claude_account_launcher.test.mjs`.
For live verification, check a streamed response through the chosen account and
an unavailable account's error. The `X-CPA-TRACE-ID` header identifies which
proxy auth entry served the response. To undo this setup, remove the two T3
provider entries through Settings, then clear their auth-file prefixes; existing
unprefixed clients continue to use the shared pool.

## Recovery behavior

`com.rever.cliproxy-watchdog` runs at login and every 120 seconds. One run performs these checks in order:

1. A direct HTTP `GET /_ping` on the configured Docker Unix socket must return
   `200` and `OK`. The socket timeout is three seconds. This does not start the
   Docker CLI or its plugins.
2. `curl -fsS --max-time 3 http://127.0.0.1:8317/healthz` must succeed.
3. From inside `cli-proxy-api`, Docker DNS and a verified TLS handshake must work for `chatgpt.com`, `platform.claude.com`, and `auth.kimi.com`.
4. OAuth metadata and the latest refresh outcome are summarized without reading token values into output. Warnings (for example `expires_soon`) are logged but do not fail the job; only critical auth failures exit non-zero, so launchd keeps running recovery on schedule.

Recovery is deliberately narrow:

- Docker socket and CLIProxy health must both fail on three consecutive runs
  before Desktop recovery is considered. Either recovering clears the count,
  and failures more than five minutes apart do not accumulate. Both checks
  are repeated immediately before `docker desktop stop --force --timeout 30`
  and `docker desktop start --detach`. Recovery polls the socket for up to two
  minutes.
- Healthy engine but dead CLIProxy port: `docker compose up -d --no-deps cli-proxy-api` from `/Users/rever/cliproxy`.
- Total container DNS failure twice in a row: restart only `cli-proxy-api`.
  Persistent DNS failure is logged for investigation. CLI execution errors
  and timeouts are reported separately and do not justify a container or
  Desktop restart. Desktop recovery requires the repeated socket and service
  failures above.

Desktop restarts are limited to once per 30 minutes by default, Compose
repairs once per 2 minutes, and container restarts once per 2 minutes. The
state file is `ops/runtime/state.json`. `--check-only` does not change that
state or perform recovery actions. The watchdog log rotates at 1 MB with
three backups and records bounded socket failure diagnostics and completed
checks.

Socket discovery honors `CLIPROXY_DOCKER_SOCKET`, then Docker's
`DOCKER_CONTEXT`/`DOCKER_HOST` selection and active context metadata under
`DOCKER_CONFIG` (default `~/.docker`). The default context uses
`/var/run/docker.sock`. Remote Docker endpoints are rejected because this job
recovers the local Desktop installation. A socket override must point to the
same engine used by the configured Docker CLI. The September 7 false-restart
diagnosis and validation are recorded in `RECOVERY-2026-09-07.md`.

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
