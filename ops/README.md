# CLIProxy recovery operations

This directory owns the host-side recovery and archive-before-prune paths for the localhost CLIProxy service. It does not contain credentials. Runtime state and the jobs' bounded logs live in ignored `ops/runtime/`.

The primary Linux instance has its own secret-free owner contract under
[`instances/will/`](instances/will/README.md). That contract is separate from
the M5 Compose contract and gives PiSec a stable source for Will's image,
loopback exposure, health, and configuration-policy expectations.

## Choosing a Claude account in T3 Code

Use a **Claude - CLIProxy pool** profile when any subscribed Claude account
may handle the request. Set `CLIPROXY_CLAUDE_ROUTE=shared-pool` and leave
`CLIPROXY_ACCOUNT_PREFIX` empty. The launcher forwards unprefixed Claude
model IDs to CLIProxy, which selects eligible credentials and handles
credential retries. The launcher does not retry requests. It switches hosts
only when its profile sets `CLIPROXY_FALLBACK_URL` (see
[Host failover](#host-failover-for-the-shared-pool)). Its model list contains only unprefixed Claude models. Account-qualified
models are rejected in pool mode.

Use a named profile when a specific account must handle the request.
`CLIPROXY_CLAUDE_ROUTE=account` is the default. A valid account prefix remains
required in this mode. Pool mode rejects a nonempty account prefix.

The native **Claude - Direct login** profile uses the local Claude login.
It does not use CLIProxy. A turn rejected by that login cannot execute an
instruction to find another account. Select a pool profile before resuming
that thread. Existing running sessions keep their launch environment.

`claude_account_launcher.mjs` lets separate T3 Claude provider entries use
specific CLIProxy accounts. Each entry runs the normal Claude binary through
the same launcher. The launcher starts an ephemeral loopback listener for that
Claude process and adds the selected account's model prefix before forwarding
requests to the existing proxy. It streams replies and forwards errors without
trying a different account. The listener closes when Claude exits.

The launcher sends `x-should-retry: false` so Claude Code can stop retrying
when CLIProxy returns HTTP 429 without `Retry-After` and a JSON body of at
most 64 KiB reports a cooldown reset more than 180 seconds away. Shorter
cooldowns keep Claude Code's normal retries, which absorb waits of up to about
three minutes; CLIProxy itself waits up to 75 seconds before it answers.

The launcher accepts only `POST /v1/messages`, `POST /v1/messages/count_tokens`,
and `GET /v1/models`. Message requests require an `application/json` body.
Unsupported routes are rejected before they reach the shared account pool.

### Configure a shared pool profile

Run `configure_claude_pool.mjs` on the T3 host. It uses the running server's
settings API, stores the proxy key in T3's secret store, and revokes its
temporary configuration session. It preserves existing profiles and labels
the native login explicitly. An existing pool profile can be updated by
running the command again with a new backup directory.

On the Mac, new profiles use the launcher under the M5 `bundle_root` in
[`fleet/hosts.json`](fleet/hosts.json). Follow the
[Mac ops bundle cutover](FLEET.md#mac-ops-bundle-cutover) before creating a Mac
pool profile. A rerun keeps the profile's saved absolute launcher path.
On Will, new profiles use the launcher next to the configuration script.

```sh
node ops/configure_claude_pool.mjs \
  --base-dir <T3-base-directory> \
  --profile-id claude_cliproxy \
  --label 'Claude - CLIProxy pool (Will primary)' \
  --upstream <proxy-origin> \
  --claude-binary <absolute-Claude-binary> \
  --api-key-file <private-file-containing-only-the-proxy-key> \
  --backup-dir <new-private-backup-directory>
```

The key file must have mode `0600`. Delete that temporary file after setup.
The backup contains redacted settings before and after the change. The tool
uses the installed T3 runtime and its `undici` package. It requires
orchestration protocol 2. It does not restart T3 or either proxy.

On the Mac, configure `claude_cliproxy` for Will and
`claude_cliproxy_mac` for the local proxy. On Will, use its own loopback
proxy. Pass `--fallback <origin>` to give a pool profile a second host. The
tool rewrites the profile's whole environment, so pass `--fallback` again on
every rerun or the fallback is removed. A rerun clears a pool `launchArgs` value that equals
the direct profile's (an inherited `--chrome`); if the direct profile's
arguments changed after a pool copied them, clear the pool's value once by
hand in T3 settings. No route can produce a reply while
every eligible account is at its upstream limit.

To roll back, use `server.updateSettings` to remove only the added profile
IDs and restore the prior native display name from the backup. Preserve
later edits to other settings. Restore the previous launcher from the host's
private deployment backup after confirming no task-owned client uses it.

### Host failover for the shared pool

`CLIPROXY_FALLBACK_URL` names a second CLIProxy origin for a shared-pool
profile. On the Mac, `claude_cliproxy` uses it for Will-to-Mac failover,
and `codex_cliproxy` has used it for session-start failover since November 18, and `codex_kimi_mac` since November 21 (see [Codex failover](#codex-failover)).
The Claude launcher refuses it on a named account route, because a pinned
account must never move to another host. Without the variable, the Claude
launcher keeps the default connection agent and starts no timers or probes.

With the variable set, the launcher:

- Gives each upstream connection 3 seconds to connect
  (`CLIPROXY_CONNECT_TIMEOUT_MS`, 100 to 60000). No timer ends a client
  request after its connection exists; only health probes are bounded after
  connect.
- Checks a host with `GET /healthz`, which must return 200 with
  `{"status":"ok"}` within 3 seconds. It checks the primary at startup.
- Replays any request on the fallback when the primary refused it, could not
  be resolved, or did not connect in time. Nothing reached the primary.
- Replays any request on the fallback when the primary answers 5xx with a dial
  error: CLIProxy's last attempt could not connect to its upstream. An earlier
  attempt in the same retry round can still have reached the provider if
  egress failed in the middle of it, so in that moment a message can run
  twice. Claude Code would usually resend the message after that 5xx
  anyway, and the replay sends that retry to the host that can serve it; it
  adds a run only when Claude Code had no retries left. New requests
  then stay on the fallback for 120 seconds. Requests that hit the same
  outage together share that hold; a repeat after it ends doubles the next
  hold, up to 600 seconds. A 2xx or 3xx from the primary returns the next
  hold to 120 seconds without ending one that is running.
- Passes 401, 403, 429, 529 and other 5xx responses through unchanged. Both
  hosts hold the same accounts, so the fallback would answer the same way.
- Probes the host once when a connection closes with no response bytes.
  `count_tokens` and the model list replay on the other host. A message
  request gets a retryable 502 (`x-should-retry: true`) instead, because the
  primary may already be generating; Claude Code's retry takes the fallback
  if the probe failed.
- Destroys the client response, without replay, when a stream breaks after
  it started.
- Probes each host with open requests every 20 seconds. Three failed probes of
  the primary in a row take it out of service for new requests. An open
  request on either host is ended only after three failed probes of its host
  during which it received no bytes, counting only probes it was open for a
  whole interval before, so a stream that is still delivering is never cut.
- Returns the primary to service after two passing probes 15 seconds apart,
  and no sooner than 30 seconds after it left.
- Fails over at most once per request. A request that starts on the fallback
  while the primary is out of service tries the primary once if the fallback
  cannot be reached. A request that has already moved from the primary gets a
  502.

Every response from a CLIProxy host carries `X-CLIProxy-Host: primary` or
`fallback`. The launcher's own 502s after a failed attempt carry it too,
naming the host last tried; its 400, 404 and 415 rejections do not. A launcher
without `CLIPROXY_FALLBACK_URL` sends no such header. Host
transitions are appended to `ops/runtime/fleet/failover.jsonl` (ignored by git;
`CLIPROXY_FAILOVER_LOG` overrides the path) as one JSON line each, with no
request bodies, URLs or keys.

Turn failover on or off for one pool profile with `set_pool_fallback.mjs`.
It changes only `CLIPROXY_BASE_URL` and `CLIPROXY_FALLBACK_URL`. T3 returns
the profile's key redacted, so the tool stores the key passed on stdin again
as a sensitive variable. That key must be the client key the profile already
uses. Before it opens a T3 session, the tool sends `GET /v1/models` with the
key to the primary and, when `--fallback` is given, to the fallback, and
stops unless each returns HTTP 200. Pass `--no-key-check` to skip that check,
for example while one host is down. The tool keeps private before and after
settings (mode `0600`) in the backup directory. They hold no value for the
key or any sensitive variable: T3 returns those empty, and the tool replaces
any it did not with `<redacted>`. To restore, rerun the tool with the
previous URLs and the key. It also checks that the profile's
`config` block and every other profile are unchanged. Its report lists only
an allowlisted set of variables, with tailnet names masked; the key is never
printed. New sessions use the change; running sessions keep their launch
environment until they end.

```sh
node ops/set_pool_fallback.mjs --base-dir ~/.t3 --profile-id claude_cliproxy \
  --primary <primary-origin> --fallback http://127.0.0.1:8317 \
  --backup-dir <new-private-backup-directory> < <private-key-file>
node ops/set_pool_fallback.mjs --base-dir ~/.t3 --profile-id claude_cliproxy \
  --primary <primary-origin> --no-fallback \
  --backup-dir <new-private-backup-directory> < <private-key-file>
```

To check failover end to end without touching T3, run the drill. It starts
the launcher once per primary with a stub in place of Claude Code, sends one
`count_tokens` request, and prints one JSON line per primary with the status,
the answering host and the transitions. The key comes from
`~/.local/bin/cliproxy-codex-token`. When `CLIPROXY_DRILL_KEY` is set, its
value is used as the key instead, and `--key-command <path>` overrides both.
The key is never printed.

A closed port, an unresolvable name and a blackholed address should all
answer from the fallback. A closed port and an unresolvable name answered in
under 0.5 seconds on November 18. An address that silently drops SYNs
answers from the fallback after about 3.4 seconds, because the launcher's
3-second startup health probe must fail first. Will's tailnet name should answer from the primary.

```sh
node ops/claude_failover_drill.mjs --primary http://127.0.0.1:1 \
  --primary http://will.invalid:8317 \
  --primary http://<address-that-drops-SYNs>:8317 \
  --primary <primary-origin>
```

Exit 0 means only that every run returned 200. It does not say which side
answered, so check each line's `host` (`primary` or `fallback`) against the
expectation above. Exit 1 means at least one run did not return 200 or the
drill could not run, for example because the key command failed. Exit 2
means a usage error, such as no `--primary`. On `SIGINT`, `SIGTERM`,
`SIGHUP` or `SIGQUIT` the drill stops the running launcher and its stub, removes its
temporary directory, and exits with 128 plus the signal number.

On `SIGTERM`, `SIGINT`, or `SIGHUP`, the launcher closes its listener and
connections, then forwards the signal to its child. If the child has not exited
after five seconds, the launcher terminates that child with `SIGKILL`.

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

Verify the launcher with
`node --test ops/claude_account_launcher.test.mjs ops/claude_account_launcher_process.test.mjs ops/claude_account_failover.test.mjs ops/claude_failover_drill.test.mjs ops/set_pool_fallback.test.mjs ops/configure_claude_pool.test.mjs`.
For live verification, check a streamed response through the chosen account and
an unavailable account's error. The `X-CPA-TRACE-ID` header identifies which
proxy auth entry served the response. To undo this setup, remove the two T3
provider entries through Settings, then clear their auth-file prefixes; existing
unprefixed clients continue to use the shared pool.

## Codex failover

On the Mac, the Will-primary `codex_cliproxy` profile has used session-start
failover since the approved rollout on November 18, after the Mac bundle cutover.
To enable it again after a rollback, set:

| Profile field | Value |
| --- | --- |
| `config.binaryPath` | `/Users/rever/cliproxy-live/ops/codex_fallback_launcher.mjs` (enable only after the Mac bundle cutover) |
| Profile environment `CLIPROXY_FALLBACK_URL` | `http://127.0.0.1:8317` |
| Profile environment `CODEX_REAL_BINARY` | `/opt/homebrew/bin/codex` |

Keep the existing launch arguments and auth command, including the
`-c` argument followed by the single string
`model_providers.cliproxy.base_url="http://will.<tailnet>.ts.net:8317/v1"`.
The executable needs Node.js on T3's PATH and
`claude_account_launcher.mjs` beside it for the shared health probe.

The launcher probes both hosts in parallel. A host passes if a fresh
connection to `GET /healthz` returns HTTP 200 with JSON `{"status":"ok"}`.
The probes share a startup budget of about three seconds. Will wins if
healthy, and the launcher cancels any pending Mac probe. If Will fails and
the Mac passes, only the base URL changes, to `http://127.0.0.1:8317/v1`.
If both fail, Codex starts with its original arguments so it can report
its usual connection error. A running session stays on its selected host
until its Codex process ends.

Each invocation with the fallback variable and a config override decides
and logs independently. This includes sessions, provider status probes,
and `exec` and `mcp` helpers. The launcher skips both probing and logging
when the fallback variable is absent or the invocation has no config
overrides, as with `--version`. Each decision appends one JSON line to
`ops/runtime/fleet/codex-failover.jsonl` under the instance root (the
launcher's checkout, or `/Users/rever/cliproxy` when it runs from the Mac
bundle), or to the file selected by `CLIPROXY_CODEX_FAILOVER_LOG`. Records contain the
timestamp, process ID, subcommand (`app-server`, `exec`, `mcp`, or `none`
when there is no recognized subcommand), selected route, and reason.
The log omits URLs, hostnames, and credentials. If the launcher cannot
identify a single supported base URL, the fallback URL is invalid, or the
probe module cannot load, it keeps the original arguments and records
`invalid_configuration`.

The launcher rewrites only a quoted URL assigned directly to the bare key
`model_providers.cliproxy.base_url`. Quoted key paths and whole provider
table overrides count when checking for ambiguity and pass through unchanged.

See the [fleet runbook](FLEET.md#enable-or-roll-back-codex-failover) for the
rollout, closed-port drill and rollback.

## Recovery behavior

`com.rever.cliproxy-watchdog` runs at login and every 120 seconds. While
`ops/runtime/fleet/mac-paused.json` exists under the Mac instance root, the
watchdog skips every check and recovery action. The marker stays in place
until `mac resume` confirms the Docker engine is up. A cycle that starts while
`mac pause` or `mac resume` holds the transition lock is skipped. See
[Quitting Docker on the Mac](FLEET.md#quitting-docker-on-the-mac).

During normal operation the watchdog performs these checks in order:

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
- Healthy engine but dead CLIProxy port: try guarded Compose repair from the
  instance root. If the activation receipt is missing or the inputs have
  changed, try starting the existing container.
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

The Mac's Compose guard requires the running container and Compose inputs
to match the approved activation receipt before running `compose up`. The
[Mac ops bundle cutover](FLEET.md#mac-ops-bundle-cutover) covers recording
that receipt and using `ops/runtime/watchdog.pause` before an approved Compose
activation. That maintenance pause suppresses repairs for up to 30 minutes
while checks continue. Remove the pause file after checking the refreshed
receipt.

Factory reset, Docker volume deletion, and deleting `Docker.raw` are never watchdog actions.

`com.rever.cliproxy-log-archive` runs at login and every 300 seconds. It handles only closed `.log` and `.log.gz` files older than one hour; the active `main.log` and recent request logs are never candidates. Each candidate is copied to the archive drive (Innie since November 18, 2026; see below), SHA-256 checked, recorded in the archive manifest, and fingerprinted again before the local copy is unlinked. A missing or unwritable archive mount means zero pruning. Lumberjack's closed `main-<timestamp>.log` rotations are eligible, so the active `main.log` remains local while completed rotations are preserved in the archive.

## Install and inspect

Follow the [Mac ops bundle cutover](FLEET.md#mac-ops-bundle-cutover) to
ship a reviewed bundle and install its LaunchAgents. The installer checks both
script paths before touching either job. The fleet monitor's LaunchAgent,
`com.rever.cliproxy-fleet-monitor`, installs with the block in
[Install or resume on the Mac](FLEET.md#install-or-resume-on-the-mac) and logs to
`~/Library/Logs/cliproxy-fleet-monitor.log`. Inspect the installed jobs and their
logs from the instance checkout.

```bash
cd /Users/rever/cliproxy
launchctl print gui/$(id -u)/com.rever.cliproxy-watchdog
launchctl print gui/$(id -u)/com.rever.cliproxy-log-archive
launchctl print gui/$(id -u)/com.rever.cliproxy-fleet-monitor
tail -n 100 ops/runtime/watchdog.log
tail -n 100 ops/runtime/archive.log
tail -n 100 ~/Library/Logs/cliproxy-fleet-monitor.log
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

The command prints an authorization URL and waits up to 5 minutes. Open that URL manually; do not have automation preview or fetch it. When the browser and login process run on the same host, the callback to `http://localhost:54545/callback` reaches the login process through the existing loopback-only Compose port. A successful login saves the matching credential under `auths/` with mode `0600`.

Do not restart CLIProxy while testing or waiting on a token refresh. Claude refresh tokens rotate; killing the process after the provider consumes the old token but before CLIProxy saves the response can strand the old token on disk.

#### Recover a Claude login on Will from the Mac

Use the management API on Will through SSH. Preserve the existing credential's
prefix and account identity. Keep rotating tokens local to each proxy instance.

1. Create a targeted session with `POST /v8/management/oauth/auth-url`. Set
   `provider` to `claude` and `name` to the existing credential filename. Set
   `verify` to `true` and `verify_model` to the exact model you need to restore.
   Retain the returned `state` privately. Have Andrew open the authorization
   URL and approve the intended account.
2. Ensure the completed callback reaches Will. With `is_webui: true`, the callback
   forwarder redirects the browser to `127.0.0.1:8317/anthropic/callback`. On the
   Mac, that address belongs to the Mac proxy. Forwarding port `54545` alone
   does not correct this second hop. The legacy callback page displays
   "Authentication successful" even when that instance has no matching session.
3. If the callback landed on the Mac, submit its response to Will with
   `POST /v8/management/oauth/callback`. Put `provider: claude`, the exact session
   `state`, and the returned `code` in the request body. Keep the code out of
   logs. Do not reopen the authorization URL or copy credentials between hosts.
4. Poll `GET /v8/management/oauth/status?state=...` on Will. Confirm `saved`,
   `adopted`, and `inference_verified` are `true`. Then call
   `POST /v8/management/credentials/refresh` with the same credential `name`.
   Confirm `renewal_verified: true`, `status: active`, `unavailable: false`, and
   no `refresh_error`. Check that the saved file still has mode `0600`.

The targeted session expires after five minutes. If the session has expired,
create a new one. Verify recovery on Will before resuming the affected T3 chat.

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

## Claude usage resets

`claude_usage_resets.py` reads each Claude account's weekly and per-model
limits, plus its Anthropic reset grants. It can also watch a limit or redeem a
grant. Every request goes through a proxy's management API, so tokens never
leave CLIProxy. `status` and `watch` are read-only and ask Will first over SSH,
falling back to the local proxy only when Will is unreachable. `claim` consumes
a grant, runs only on the host it names, and needs Andrew's approval for that
account and grant.

```sh
python3 ops/claude_usage_resets.py status
python3 ops/claude_usage_resets.py watch --account <email> --limit Fable --threshold 99 --notify
python3 ops/claude_usage_resets.py claim --host will --account <email> --grant <grant-id> --dry-run
python3 -m unittest -v ops.test_claude_usage_resets
```

[`CLAUDE_USAGE_RESETS.md`](CLAUDE_USAGE_RESETS.md) documents the endpoints, the
claim request copied from Claude Code, and the 2026-11-11 Fable reset record.

## Log retention

`config.yaml` deliberately sets `logs-max-total-size-mb: 0`, disabling CLIProxy's delete-based cleaner. Do not set a positive cap: the cleaner deletes immediately on config reload and cannot verify an archive first.

The durable archive is:

```text
/Volumes/Innie/backups/llm-logs/cliproxy/m5/live/
```

Until November 18, 2026 it was `/Users/rever/Volumes/TheCloud/backups/llm-logs/cliproxy/m5/live/`. That NAS share filled up (0 bytes free), so no log was archived or pruned and the Mac data volume fell to about 6 GiB free. Andrew chose Innie, a local drive with about 600 GiB free. The T9 drive had just thrown a disk error. Logs archived before that date stay on TheCloud. To switch destinations, update the two archive variables in the tracked `ops/com.rever.cliproxy-log-archive.plist` and `ops/test_launchd_plist.py` in one PR. Ship the reviewed commit and reinstall the LaunchAgents from the bundle using the [Mac ops bundle cutover](FLEET.md#mac-ops-bundle-cutover).

Closed logs are grouped by UTC modification date under `requests/YYYY/MM/DD/`; `SHA256SUMS` is append-only evidence for archived content. The local copy is pruned only after content verification. If the archive drive is unavailable, local files accumulate and `ops/runtime/archive.log` records the failure. `main.log` itself is never pruned while active and rolls at 10 MB; its closed rotations then follow the same archive path.

To archive without pruning, use:

```bash
python3 ops/archive_logs.py --archive-only --verbose
```

The August 27 recovery set and downstream reconstruction are described in `ops/RECOVERY-2026-08-27.md`.
