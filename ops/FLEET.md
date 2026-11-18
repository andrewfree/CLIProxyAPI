# CLIProxy fleet

## Fleet contract

Will is the primary and the M5 Mac is the fallback. The fleet target is the
same release, effective config, model catalog, and accounts under the same
prefixes on both hosts. Keep each host's OAuth files, cooldown state, secrets,
and logs local. Change both hosts together or record the exact difference,
reason, decision reference, and review date in [exceptions.json](fleet/exceptions.json).

Run these checks from a worktree before and after a fleet change, login, or
logout:

```sh
python3 ops/fleetctl.py check
python3 ops/fleetctl.py health
python3 ops/fleetctl.py fingerprint --host m5
python3 ops/fleetctl.py parity
```

`check` checks tracked pins against Will's owner policy offline. `health`
probes each host's `/healthz`. `fingerprint` checks one instance against the
available pins and reports counts and allowlisted settings. `parity` compares
the loaded config, selected disk-only settings, static key and account tags,
account errors, the catalog for every client key, Cursor overlap, and plugin
registrations. The tool checks the server version and the pinned Cursor
library. It reads Will over SSH with a loopback worker and prints differences
without account identities or tags. Exit 0 means the selected checks passed.
Exit 1 indicates drift or an invalid offline contract. Exit 2 indicates the
probe could not complete. An expired exception fails live parity. The parity
report lists exception scopes with no observed difference in `unused_exceptions`.

The [release manifest](fleet-release.json) pins the shared config projection
and the initial full M5 fingerprint. The shared projection sets aside debug
logging, NVIDIA, and the Will quota plugin for scoped comparison. Discovery
and the log size cap are intentionally host-specific. Will's legacy YAML and
M5's v8 YAML feed the same loaded config view.

For `model_not_supported` cooldowns, parity resolves served model IDs through
the credential channel's global `oauth-model-alias` table and checks whether an
unrestricted upstream model remains. The management API omits per-credential
alias attributes, so parity cannot account for those overrides.

The read-only checks cover the management API contract. Use the existing
instance verifiers for Docker image and Compose checks. The bundle manifest
lists files for the later shipping step. Shipping, bundle activation receipts,
in-container egress checks, and scheduled health notifications follow in
separate units.

Operational differences recorded on November 18, 2026, for review by December 2, 2026:

- Will uses flat legacy YAML until the layout migration (fleet design November 17, unit 7).
- The Mac runs scripts from its primary checkout and Will has a hand-copied ops subset
  pending bundle receipts (units 2b and 2c).
- The installed Mac token helper differed from the tracked copy and reachable history
  during prechecks (unit 0).

Until bundle cutover, updating the watchdog, log archiver, Claude launcher,
their plists, or Compose configuration in the Mac's primary checkout is a
live change and requires Andrew's promote phrase. Edit those files in a
worktree. Activate the Mac first, then Will. If verification fails, roll back
the host just changed. A host that already passed requires Andrew's
instruction to roll back.

## Running fleet

Will is the always-on primary at `http://100.98.74.67:8317`. This address is private to the Tailscale network; Tailscale encrypts transport. Will's container remains bound to loopback. Tailscale Serve forwards TCP 8317 to it. The M5 Mac runs a local fallback at `http://127.0.0.1:8317`. The Mac's `claude_cliproxy` profile reaches Will by its tailnet name, `http://will.<tailnet>.ts.net:8317`. Public resolvers answer NXDOMAIN for that name, so with Tailscale stopped the lookup should fail at once and the launcher fails over, instead of sending traffic to whatever holds Will's tailnet address on another network.

`fleet-release.json` pins one application source commit, one Cursor plugin version, and the exact platform image and plugin hashes. Both images are built from the same commit. ARM64 runs on the Mac's Docker VM; AMD64 runs on Will. Compose uses immutable local image IDs with `pull_policy: never`, so a restart cannot silently select a new release. The two Compose files and owner contracts must be changed together for the next release.

The current release is `v8.0.66-fleet.4`, from source `4a1d49c3c839d97411a6a29b5daa18e16212c3bf`. It adds the cloaked Claude date pin keyed to the client's Claude Code session, so the injected date no longer changes mid-conversation and rewrites cached history. Andrew approved Mac first, a live inference gate, then Will. The Mac activated at 10:00:32 and Will at 10:01:57 Pacific on November 18, 2026. Both fleet verifiers and Will's owner verifier passed, both plugins stayed registered on Will, and no persistent unrelated container changed. On each host, four Claude Code-shaped requests (streaming and non-streaming pool turns, a named `t3-gmail` turn and a subagent turn in one session) returned the exact reply with one injected date, and the second turn read 37,574 cached tokens. Each host's fleet.4 first turn read the 37,545 tokens that the Mac's fleet.3 baseline run had written just before (Will's own baseline read the same prefix), so the cached prefix survived the switch. The [activation record](evidence/fleet4-activation-20261118/README.md) has the images, checks and rollback paths.

The previous release was `v8.0.66-fleet.3`, from source `4b93cbb545d0f37fe2fdf619f91dd58e0e2f9955`. Andrew approved deploying Mac first, verifying the upgraded account and shared pool, then deploying Will. Mac activated at 23:05:06 and Will at 23:05:44 Pacific on November 14, 2026. Both fleet verifiers and Will's owner verifier passed. The upgraded third account reported Max 20x, completed account-pinned verification, and returned complete Opus 5.5 `POOL_OK` replies through both its named route and the shared pool on each host. Both accounts ended active with no cooldowns. All 67 unrelated containers retained their IDs, images, running state, and start time.

The [account recheck command](CLAUDE_ACCOUNT_RECHECK.md) validates live identity and expected tier before invoking account-pinned server verification. Only a completed response with allowed quota signals can release an old credential-wide quota cooldown. The server compares the credential generation under its mutation lock and preserves independent sibling model restrictions and disabled model shadows. This is an explicit operator action after an upgrade; ordinary traffic does not bypass cooldowns.

The same recovery runs for direct account-pinned credential verification and optional verification after an OAuth login. Only the helper checks the account identity and expected tier. The current server guard conservatively retains the cooldown if any reported unified quota window is rejected, including a scoped model window.

GPT-6 Astra reviewed the server fix, deployment steps, and final streaming correction. The first Mac candidate, fleet.2, passed health checks but failed inference verification because its new native request omitted `stream: true`. Mac was rolled back before the corrected fleet.3 release. The failed attempt and rollback remain recorded. Exact source and metadata local-ci runs each passed both jobs. Focused quota race checks and complete auth and management package tests passed. The complete Go suite passed only after excluding three failures reproduced on untouched HEAD: WebRTC DataChannel creation, websocket temporary-directory cleanup, and heartbeat failover. This is a validation workaround; no existing test assertion was weakened. Release image pins and new regression coverage changed.

The [activation receipt](evidence/claude-quota-recheck-20261115/activation-summary.json) records platform image identities, successful live checks, and rollback paths. Private rollback backups are under `ops/runtime/claude-upgrade-recheck-20261114/fleet3/backup-20261115T070416Z` on both hosts. On Mac, the `rollback/` subdirectory preserves the actual prior fleet.1 metadata separately from newer source-tree pins. Rollback restores only deployment pins and the old image selection, preserving current configuration and OAuth files. Retained fleet.1 images are `sha256:7d6204c451faa133207817b05515bff1c96959cafee677b810c33ac4854d4bdd` on Mac and `sha256:8169e6e322b7db49c1576a608d7b2c18d957ce05ccde45bbf4f4fd58c4a4553e` on Will.

The previous release was `v8.0.66-fleet.1`, based on upstream v8.0.66, from source `815987ae113117253b37c7c0755e209fadd01372`. It was deployed to Will at 21:21 and M5 at 21:27 on November 10 Pacific time after Andrew instructed the session to update, commit, deploy, and test. Both live fleet verifiers, the Will owner verifier, authenticated catalogs, and exact pooled Luna and named Gmail Claude Fable replies passed. All three Claude credential profiles and usage endpoints returned HTTP 200 on each host. The [activation receipt](evidence/upgrade-v8.0.66-20261110/activation.json) records image identities, checks, backups, and qualification limits.

The required compile, focused quota/cooldown/scheduler race tests, 24,206 remaining Go tests, and both local-ci jobs passed. The full Go suite retains two failures reproduced on untouched upstream v8.0.66 in the same environment: WebRTC DataChannel creation and websocket temporary-directory cleanup. The remaining suite excludes exactly those two tests. This is a validation workaround. A new upstream Claude header test was adapted to the fleet's existing rule that omits Retry-After values over one minute while preserving provider reset headers. No workflows changed. Local CI compares the maintained fleet changes against v8.0.66.

Rollback from v8.0.66-fleet.1 uses private `ops/runtime/upgrade-v8.0.66-20261110/backup-20261111T050853Z` on Will and `backup-20261111T050852Z` under the same relative directory on M5. Retained v8.0.63-fleet.1 images are `sha256:e3b0986909ae18210d239fb7ad769ea655f92ad70136a230f9a0e577ba1d298c` on Will and `sha256:e859340e0e1b11d4f0560d83ee347a85af0bf7151b2affaf098bffc91574f7e8` on M5. Restore only each host's image selection and matching Compose, manifest, and owner metadata. Preserve current configuration and refreshed OAuth files. Recreate only `cli-proxy-api` with `docker compose up -d --no-deps --pull never --no-build cli-proxy-api`, then run the restored fleet verifier, Will owner verifier, and authenticated client checks.

The previous v8.0.63 activation and its older rollback records remain in [the November 9 receipt](evidence/upgrade-v8.0.63-20261109/README.md).

Both hosts use `routing.strategy: quota-aware` with session affinity, enabled on November 18, 2026, on the Mac first and on Will after a soak. Quota-aware selection places each new session and each subagent on an eligible account using bounded weighted rotation based on remaining shared quota and reset urgency. Affinity then keeps that stream on its account, so the account's prompt cache stays warm. Observations older than five minutes, missing data, and ambiguous window signals receive a neutral share. If no candidate has a usable fresh forecast, the selector uses ordinary rotation. Named account routes, authoritative cooldowns, and model eligibility remain authoritative. Configured priorities decide every new binding; an existing binding stays on its credential, including a lower-priority one, until that credential is unavailable or the binding idles out. Balancing therefore happens per session, not per request. The Mac switched at 00:15:41 Pacific on November 18, 2026; Will switches separately. See [the activation record](evidence/cache-affinity-20261118/README.md).

The previous November 2 release was `v8.0.52-fleet.3`, from source `53ff3c26a440e9b35eefdfae4868a9106b223ce5`. `v8.0.52-fleet.4` retained the Kimi OAuth model fallback, the one-minute client retry-header limit, and the one-hour quota cooldown cap. Fleet.3 extended the cooldown cap to handle the upstream independent model cooldown layer during restore and reload. An expired credential quota cannot erase a still-active independent failure or a newer quota observation.

Full retry deadlines remain in trusted auth metadata. The one-minute limit is applied after outgoing client headers are merged, including Home, plugin, passthrough, direct-response and Codex realtime paths. Four auth-layer test files match upstream again. The HTTP issue7017 assertion intentionally expects omission of a 256-second hint under the fleet policy.

Local HTTP interrupts cancel the upstream context before writing the terminal acknowledgement, so acknowledgement backpressure cannot defer that cancellation. The cancellation-observation test retains its channel synchronization. The Home close-order test uses a numeric loopback fixture to avoid go-redis constructor DNS detection. Test time bounds are unchanged.

For the historical rollback from fleet.3, the private backups are `/Users/rever/cliproxy/ops/runtime/workaround-fixes-20261102/backup-20261103T022733Z` on M5 and `/home/andrew/cliproxy/releases/v8.0.52-fleet.3/backup-20261103T022733Z` on Will. Both previous images run fleet.2. Their digests are `sha256:9ecc0f8d67e4c39732695152ef421e7e730a70709c0baaaa8739015a062450a7` on M5 and `sha256:17bccfe8912daf3fa2fe7add8a8fd0f50b0bf751db2bbf17ae1b05ac982b2cd5` on Will. Each receipt records file hashes, permissions, and the previous container. Earlier upgrade backups remain available.

For the historical rollback from `v8.0.52-fleet.4` (not the current `v8.0.66-fleet.4`, whose rollback is in [its activation record](evidence/fleet4-activation-20261118/README.md)), private deployment backups are `/Users/rever/cliproxy/ops/runtime/quota-activation-20261106/backup-20261107T055818Z` on M5 and `/home/andrew/cliproxy/ops/runtime/quota-activation-20261106/backup-20261107T055819Z` on Will. The retained `v8.0.52-fleet.3` image IDs are `sha256:3615ac678a7b9b3e428d6dfd03e56d019b71a944df34d498012dbf6f4365ecaf` on M5 and `sha256:318107e9333148eac12889eacf8b1db5e1fc184f39869500377d25e243684168` on Will. Restore only the image pins and routing strategy while preserving later config changes, credentials, plugins, and logs. A policy-only rollback can hot-reload `routing.strategy: round-robin`; update the matching contracts too. Config hot reload also reconfigures plugins and can close Cursor bridges, so account for active plugin work.

Before rollback, confirm no OAuth refresh or protected request is in flight and save the current deployment files in a new private directory. Restore only affected deployment files from that host's receipt, including `.env` and the M5 override. Preserve subsequent config edits and refreshed OAuth files. Recreate only the proxy with `docker compose up -d --no-deps --pull never --no-build cli-proxy-api`. Run the restored fleet verifier, the Will owner verifier when applicable, and an authenticated client check.

Root-fix receipts are under `ops/runtime/workaround-fixes-20261102/`. OpenAI Codex reviewers examined the interrupt ordering, retry boundary and CI resolver changes. The CI runner now has a durable explicit DNS policy. Gateway EDNS A replies were malformed, while the existing ISP resolver returned valid answers. The policy avoids that gateway path without hosts-file injection. It does not repair the gateway itself or change production container DNS.

Keep host configuration, logs, and OAuth credentials local to each instance. Never synchronize rotating OAuth tokens between active servers. Both hosts have three Claude credentials. The existing Gmail and iCloud routes use the `t3-gmail` and `t3-icloud` prefixes. A third Pro account was added through a separate OAuth login on each host on November 2. Changing a named account route must never select another account implicitly.

Each host's `auths/` directory is bind-mounted at `/root/.cli-proxy-api`; all three Claude credential files contain refresh tokens and have mode `0600`. CLIProxy loads these files on startup and refreshes credentials automatically. Both containers use the `always` restart policy, and Will's Docker and Tailscale services are enabled at boot. After a fresh login, check the new credential file's permissions and restrict it to `0600` if necessary. These persistence settings and mounted files were verified without restarting active clients.

The third account passed one refresh through `/v8/management/credentials/refresh` on each host. Both access and refresh tokens rotated and were saved to the host-mounted credential file. File permissions remained `0600`, runtime renewal verification succeeded, and the refreshed account returned HTTP 200 from Anthropic's read-only OAuth usage endpoint. Both servers retained all three accounts and their original container start times. The final permission check also corrected the two older Mac Claude files from `0644` to `0600`. The [token retention receipt](evidence/claude-pro-retention-20261102.json) records sanitized results, the automatic-refresh source paths, focused tests, and the server build. No model inference or restart test was run.

Plugins are enabled on both hosts. The Cursor provider is `auth-cursor` 2.1.1; its provider key is `cursor`. Plugin archives were verified against the publisher's registry before extracting the shared libraries. The libraries are persisted under each instance's `plugins/linux/<arch>/` directory. The plugin uses the official Cursor SDK Bridge and supports one API key file per Cursor account, round-robin selection, and optional weighted routing. No Cursor API keys were present at preparation time.

Add an account on the host that will own it using the supported interactive prompt:

```sh
docker exec -it cli-proxy-api /CLIProxyAPI/CLIProxyAPI --config /CLIProxyAPI/config.yaml --cursor-login
```

Create the key in the Cursor dashboard. Keep it out of shell arguments and chat. Account Privacy Mode applies to SDK requests. The plugin has no separate telemetry switch; no analytics client was found in its source. Both services set `DO_NOT_TRACK=1`, and CLIProxy usage aggregation is disabled.

Run `python3 ops/verify_fleet_instance.py` from each instance root to check its live image, source revision, plugin checksum, version, health, analytics settings, cooldown and retry settings, and routing policy against the shared manifest. The verifier prints no credentials.

The manifest pins `session-affinity: true` for both hosts under the quota-aware policy; [the activation record](evidence/cache-affinity-20261118/README.md) gives each host's switch time. Without it, quota-aware rotation moved consecutive turns of one conversation between accounts whenever two or more were eligible. Upstream prompt caches belong to the account, so each switch rewrote cache. Mac request logs showed Claude pool switches on 20% of consecutive turns on November 12 and 38% on November 13. Switched turns wrote 3.4 times more cache, adding about 11% and 24% to those days' input cost at API-price equivalents. Codex pool switches reached 66% of turns on November 14. The November 2 trial that kept affinity off used 120 synthetic Luna requests and did not measure Claude or native T3 traffic. See [the comparison and operating decision](cache-affinity-validation.md).

`fleet-release.json` pins five routing fields separately for `arm64` and `amd64`. Will's `config-policy.json` must agree with the `amd64` tuple. The active settings are `session-affinity-ttl: 1h`, `session-affinity-subagents: false`, and `session-affinity-rebind-on: any`. With subagents false, each Claude Code subagent gets its own binding from the quota-aware selector instead of inheriting its parent's account. The manifest also pins `transient-error-cooldown-seconds: -1` and `max-retry-interval: 75` for both hosts. The negative value disables cooldowns after 408, 5xx and transient-capacity 429 responses, ignoring their retry hints, so one upstream error does not rebind every session on an account. Quota 429s still cool down and rebind. The 75-second interval lets a request wait for a cooldown that ends within 75 seconds instead of returning an immediate 429. The limit applies to each retry round, so with `request-retry: 3` a request can wait about 225 seconds in total. With no transient cooldown, a failing request can also make up to four immediate rounds, up to 12 attempts across three accounts. The manifest pins `request-retry: 3` and `max-retry-credentials: 0` too. Copy the manifest and verifier helper together when updating deployed ops files.

The earlier canary verified account continuity, independent child distribution, fork inheritance, and hot-reload rollback. Those functional results did not establish cache savings. Affinity binds per provider and model, refreshes its idle TTL on every hit, and can concentrate requests without a session ID when their opening text matches. Any future trial needs evidence that its benefit justifies that concentration. Named Claude prefixes continue to select their intended accounts; affinity only chooses among credentials the route already allows.

For an affinity-only change, edit `routing.session-affinity` in place, preserving the configuration inode and all unrelated settings. Update the affected platform's manifest value and Will's owner policy when applicable. Verify the loaded configuration and mounted file with the fleet and owner verifiers, then check the `session-affinity:` lines in `logs/main.log`. Each change to the five routing fields replaces the selector and drops every binding, costing one cold cache write per active stream. Do not restore an old full configuration over later edits. Private backups and sanitized rollout receipts for the November 2 return to round robin are under `ops/runtime/affinity-validation-final-20261102/` on each host.

T3 on the Mac has explicit `Will primary` and `Mac fallback` entries for the shared Codex and Claude pools, plus the three named Claude accounts. The shared Claude profiles are `claude_cliproxy` and `claude_cliproxy_mac`. Will's T3 has `claude_cliproxy` connected to its own loopback proxy. The native Claude entry is labeled `Claude - Direct login`.

`ops/claude_account_launcher.mjs` supports explicit `shared-pool` mode through `CLIPROXY_CLAUDE_ROUTE`. This mode forwards unprefixed Claude model IDs to the existing credential selector. Named profiles keep their account prefixes. `ops/configure_claude_pool.mjs` provisions pool profiles through the running T3 settings API and stores keys in its secret store. The [routing receipt](evidence/claude-pool-routing-20261103.json) records tests, deployed artifact hashes, and runtime-check limits. This host-side integration does not change the fleet's server images or manifest.

`claude_cliproxy` on the Mac fails over automatically: Will is its primary and `CLIPROXY_FALLBACK_URL=http://127.0.0.1:8317` names the Mac (see [host failover](README.md#host-failover-for-the-shared-pool)). Every other profile keeps a fixed host and never fails over: `claude_cliproxy_mac`, the three named Claude profiles, the Codex-family profiles and Will's own `claude_cliproxy`. Where a profile for the other host exists, switching to it is manual. A rerun of `configure_claude_pool.mjs` for `claude_cliproxy` must pass `--fallback http://127.0.0.1:8317` again, or failover is removed. New sessions use the selected endpoint; an already running client keeps its launch environment. Native provider entries retain their direct subscription routes. Cursor is reserved for Cursor-only models and Gemini under the owner's model-routing policy.

For exact-model discovery and an authorized T3 task, follow [the Kimi CLIProxy routing recipe](KIMI_ROUTING.md).

Since November 11, M5's proxy also serves three NVIDIA-hosted models as an OpenAI-compatible upstream, selected in T3 through `codex_cliproxy_mac`. Will has no NVIDIA provider or key. See [the NVIDIA routing guide](NVIDIA_ROUTING.md).

The November 14 direct Z.ai Coding Plan activation added GLM 5.3 routes on both hosts. See [the GLM routing guide](GLM_ROUTING.md) for verified aliases, client-key layout compatibility, and the extra-reload workaround required on Will.

M1 is a client, not a third proxy host. On November 2, its T3 instance gained an enabled `codex_cliproxy` profile named `Codex - CLIProxy Will primary`, using `http://100.98.74.67:8317/v1`. The profile uses the Responses API and a local token helper. The client key has mode `0600`, and the helper has mode `0700`. Setup preserved all existing profiles and unrelated settings, and revoked its temporary T3 setup session. This profile is available for selection in new sessions. Existing threads keep their selected providers.

Authenticated model-catalog and synthetic generation checks passed from M1 through Will, and from M5 through both Will and its local fallback. The [client validation receipt](evidence/cache-affinity-20261102/client-validation.json) records these checks and the native T3 smoke results. Both proxy instances now run the verified quota-aware policy with affinity disabled. M1 could not be reached over SSH for a fresh client check during the November 6 rollout; the November 2 M1 receipt remains historical. M5's proxy remains loopback-only, so M1 cannot use it directly as a fallback. The M1 profile does not add a Claude account route or another proxy server.

The Will owner verifier additionally checks the exact private IPv4 and IPv6 listeners, the Tailscale Serve forwarding target, and all six loopback Docker bindings. PiSec records the private forwarding service separately from the Docker workload. The primary uses Will's own credentials and local storage, so the Mac is not in its request path.

For a future update, create one new release manifest, build its exact source on both platforms, verify both plugin artifacts, stage both configurations without production auth, run the app and contract checks, then recreate only the two proxy containers. Check the running image IDs, source revision labels, health endpoints, registered plugin version, authenticated model catalogs, and client routes against the manifest. Keep the previous images and private configuration backups for rollback.

On October 15, both hosts were updated to `v8.0.27-fleet.3` from source `07e5dff992f2ae93bd49f6a3de0ba158816a2dcc`, deployed sequentially with owner approval on October 15, 2026. It extends the one-minute client retry-header limit to auth-unavailable and Home errors and adds an optional quota cooldown maximum. Both live management APIs report `quota-cooldown-max-seconds: 3600` and `save-cooldown-status: true`. Their legacy configuration files use the root keys. Nested configurations use `routing.cooldown`. A binary upgrade alone leaves the cap disabled.

The cap applies to fresh quota errors, saved records and reloads, including different model deadlines on one account. It makes normal requests eligible to test recovery after at most the configured quota wait; another upstream 429 blocks the account again. It does not poll upstream or guarantee a strict request-per-hour limit under concurrency. Legacy state followed by a non-quota error remains conservative: that model and the model-less account summary may retain the old block, while an unaffected model can recover. The reported reset is retry eligibility, not a promise of an upstream quota reset.

Both live fleet verifiers passed exact image, source, health, Cursor plugin hash, analytics-off and cooldown policy checks. The Will owner verifier also passed. Real Codex requests and native T3 Gmail Claude requests returned the expected replies through both hosts. The iCloud Claude smoke on Will reported "Claude API rate limit" and its retry was stopped. Both hosts still report that account unavailable; the Mac iCloud generation was not attempted. This check does not prove a fresh upstream quota rejection or an upstream reset time.

The deployment retained both credential directories, mounts, loopback ports and restart settings. All ten unrelated containers retained their IDs and images. Receipts are under `/Users/rever/.t3/scratch/2026-10-15-why-is-this-thread-and-11d14e02/release-v8.0.27-fleet.3`. The full local-ci run passed both workflow jobs at metadata commit `79ccd55294e14cb2f914ec3d95d2bd7486270e12`. Runtime images retain source `07e5dff9`. GPT-6 Astra performed deployment verification; Claude Opus 5.5 reviewed runtime, release metadata and verifier changes in rounds R4 through R6.

The superseded prepared `v8.0.27-fleet.2` release built source `6376a061b448c62f099e0d48dcc480f9f5b8da59`. Cooldown responses omit `Retry-After` when the wait exceeds one minute, while retaining HTTP 429 and the full reset time in the JSON body. This prevents Codex 0.162.1 from sleeping for days on that response. It does not revalidate saved cooldowns. Both platform images passed isolated health, plugin, and analytics checks with no production credentials. Fleet.3 includes the later fixes and replaces this candidate.

The previous fleet.1 source is `d4fa77254e2922a834f6f97ddac0d7a88249b9ad`, based on upstream v8.0.27. Fleet.3 retains its local Kimi model fallback feature. Historical October 15 pre-deployment backups are `/Users/rever/cliproxy/ops/runtime/v8.0.27-fleet.3/backup-20261015T213909Z` and `/home/andrew/cliproxy/releases/v8.0.27-fleet.3/backup-20261015T213909Z`. Each private directory contains a receipt with the previous image, file hashes, permissions and presence. No OAuth credential directory is replaced during upgrade or rollback.

The following rollback record applies only to the October 15 fleet.3 deployment. First preserve the failed runtime configuration in a new private directory, stop only the proxy, and restore the backed-up Compose, `.env`, configuration and deployment metadata on that host. Restore the Mac's backed-up `docker-compose.override.yml` too. Both hosts had `.env` files before fleet.3; their image selections must be restored. Use the receipt to distinguish pre-existing files from metadata added during this deployment, and move added metadata into the failed-release backup. Recreate with `docker compose up -d --no-deps --pull never --no-build cli-proxy-api`, then run the restored verifier and a real client request. Preserve newly refreshed OAuth files. The retained Mac image is `sha256:1914227451c32a2e19cc3c1919c8e53fe48507dc2a09a2f0f1d76563bdae361e`. The retained Will image is `sha256:cff34aadce0684523ca94d7829be961954ce1bebfba7c8c3fb847c50b7281e0a`.

Tailscale Serve can remain in place during an application rollback. To remove only the new private route, use `sudo tailscale serve --tcp=8317 off` on Will. Do not reset Serve: HTTPS 443 belongs to T3. Revert T3 routes through `server.updateSettings` or the app settings, preserving secrets in the app's secret store.

## Operations on November 18, 2026

These changes are live but are not part of the release manifest.

- **Claude date timezone.** Both hosts set `claude-header-defaults.timezone: America/Los_Angeles` (Mac `upstream.claude.header-defaults.timezone`). The containers run in Asia/Shanghai, so the cloaked date was a day ahead from 08:00 to 24:00 Pacific. Config only; `fleetctl parity` compares it on both hosts.
- **Client key rotation.** Two new CLIProxy client keys were added at the top of both hosts' key lists, and every client moved to them: the five Mac T3 Claude profiles, Will's T3 profile and `client.key`, the Codex token helper, and M1's Codex and OpenCode key files. The two old keys stay accepted until a scheduled task removes them on 2026-11-19 at 10:30 UTC. Review agents had printed them in their own transcripts.
- **Provider keys.** A new Z.ai Coding Plan key ("CLIProxy fleet 2026-11-18") replaced the old one on both hosts and in CodexBar, and the old key was deleted in the Z.ai dashboard. The NVIDIA key (Mac only) was rotated the same day: "CLIProxy M5 2026-11-18" replaced "nvidia work" in the Mac config and `~/.config/nvidia/api.env`, a test request returned 200, and the old key was deleted. The new key expires on 2027-11-18 (NVIDIA's 12-month default).
- **Log archive.** The Mac archive moved from TheCloud, which was full, to Innie; see [the archive section](README.md).
- **Fleet design prechecks 0(b) and 0(c)** for the [design proposal](FLEET_DESIGN.md):
  - A refused connection or an unresolvable name fails in milliseconds. A silently dropped SYN on the Claude dial path waits about 133 to 135 seconds per dial (Mac 132.7 s, Will 135.1 s), because that dial has no connect bound. Both kernels accept the container sysctl `net.ipv4.tcp_syn_retries`: at 2 a dial fails in about 7 seconds, at 3 in about 20. With three credentials and `request-retry: 3` the worst case at 2 is still about 86 seconds, above the design's 30-second target. Both Compose files now set `sysctls: net.ipv4.tcp_syn_retries: 2`. A container picks it up only when it is recreated: at the next activation, or on the Mac as soon as its checkout has this change and the watchdog's repair runs `docker compose up`. Until then Will's verifier (`ops/instances/will/verify_live.py`) reports `compose_sysctls_drift` while Will's Compose copy lacks the setting and `container_sysctls_drift` while the running container does. The Mac verifier has no sysctl check; read `/proc/sys/net/ipv4/tcp_syn_retries` inside the container after the Mac activation.
  - Traffic never stayed quiet for two consecutive windows: across 80 windows of 15 seconds per host, a 1 KiB per window floor on the two-window gate would have blocked all 79 deploy checks on each host. The design's pre-deploy activity gate needs a different approach before it is used.
- **Claude pool failover** ([design](FLEET_DESIGN.md) unit 3), approved by Andrew ("Ship it now"):
  - PR #13 (`2994de5f`, `net.ipv4.tcp_syn_retries: 2` in both Compose files) and PR #14 (`55f33d16`, launcher failover) were squash-merged after four review rounds by Claude Fable 5.2 and Claude Opus 5.5 and a passing local-ci gate.
  - About 12:53 Pacific the Mac checkout moved to `55f33d16`. The changed files that exist in Will's copied tree (launcher, `configure_claude_pool.mjs`, `FLEET.md`, Will's policy, verifier and both Compose copies) were replaced on Will, with backups in `~/cliproxy/ops/runtime/ship-20261118/prev/`. The launcher's sha256 starts with `dd3947a1` on both hosts, matching `55f33d16`. No container was restarted.
  - `claude_cliproxy` then got the tailnet-name primary and the Mac fallback through `server.updateSettings`. Its API key, its `config` block and every other profile were unchanged; private before and after settings are under `ops/runtime/fleet-design-20261117/ship-20261118/t3/` on the Mac.
  - Checks: the strict preconditions held (the first new client key from the rotation above accepted on both hosts, `fleetctl parity` reporting `equal` with no exception on Claude rows). A throwaway launcher with the Mac as fallback answered `count_tokens` from the fallback for a closed port, an unresolvable name, a rejected TEST-NET address (all in under 0.5 s) and two blackholed addresses (3.4 s, the health-probe bound), and from the primary for Will by name. The installed launcher answered as before, without an `X-CLIProxy-Host` header, for the Mac pool, the named `t3-gmail` route and Will's profile on Will. Two new T3 sessions on `claude_cliproxy` answered; the second's launcher carried the new variables and held its upstream connection to Will.
  - Expected until the next activation: Will's verifier reports `container_sysctls_drift`, because Will's container predates the sysctl.
  - Roll back on Andrew's instruction, since both hosts passed. Through `server.updateSettings`, as for the activation, remove `CLIPROXY_FALLBACK_URL` from `claude_cliproxy`; new sessions then use Will only, and running sessions keep failover until they end. To undo the name change too, set `CLIPROXY_BASE_URL` back to the value in the private before settings. To restore the previous launcher, run `git -C ~/cliproxy checkout 0dca75ba -- ops/claude_account_launcher.mjs` on the Mac (its sha256 then starts with `49bfed87`) and copy the launcher backup from `~/cliproxy/ops/runtime/ship-20261118/prev/` over `~/cliproxy/ops/claude_account_launcher.mjs` on Will. To drop the sysctl before a container recreate, restore Will's Compose copies, policy and verifier from `prev/` together, and run `git -C ~/cliproxy checkout 0dca75ba -- docker-compose.yml` on the Mac.
- **Fleet design prechecks 0(a) and 0(f)**, all read-only:
  - The Z.ai Coding Plan model list serves `glm-5.2` (with 4.5 to 5.3 and the 5.3 flash variants), so design question Q12 resolves to a direct alias on both hosts.
  - Will's extra `kimi-ai` credential belongs to the same Kimi user as the `kimi` credential on both hosts (equal hashed user ID) but signs in through `kimi.ai` instead of `kimi.com`. Whether the two endpoints share quota is not known.
  - T3 marks a provider ineligible only for static reasons: the instance is disabled, no adapter is registered, the driver failed to start, or the CLI failed to run. Will's health does not enter, so with Will down `claude_cliproxy` stays selectable and only the launcher can fail over.
  - With MagicDNS on, the Mac resolves `will.<tailnet>.ts.net` to Will's tailnet address and public resolvers answer NXDOMAIN for it. With Tailscale stopped, a launcher pointed at that name fails the lookup at once and fails over. Tailscale was not stopped for this check.
  - All six CLIProxy Claude profiles (five on the Mac, one on Will) run the launcher through `config.binaryPath`. `configure_claude_pool.mjs` edits only pool profiles and rewrites their whole environment; the three named profiles are edited with a `server.updateSettings` patch.
