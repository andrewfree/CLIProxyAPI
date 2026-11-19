# CLIProxy fleet design (proposal)

> **Status, November 18, 2026.** This is the reviewed design proposal, kept for reference. It is not a description of what runs; [FLEET_ARCHITECTURE.md](FLEET_ARCHITECTURE.md) explains what was built and why. Done: unit 0(b), 0(c), 0(d) and 0(e) (0(b) and 0(c) results in [FLEET.md](FLEET.md#operations-on-november-18-2026)) and all of 0(a) and 0(f) (results in the same FLEET.md section). Unit 1 (Mac drift batch) landed in PR #4. Unit 2a's read-only tooling (`fleetctl check`, `parity`, `fingerprint`, `health`) landed in PRs #7 and #10; `fleetctl ship` is deferred to the bundle units. Unit 3 (launcher failover, section 6 items 1 to 8) went live on November 18 for `claude_cliproxy` on the Mac (PR #14, activation in [FLEET.md](FLEET.md#operations-on-november-18-2026)); every other profile keeps a fixed host. The `tcp_syn_retries` sysctl (PR #13) has been live on both hosts since their containers were recreated on November 18. Unit 4: NVIDIA on Will, `glm-5.2` (now mapped to upstream `glm-5.3`), `kimi-ai` (removed from Will; it shares the `kimi` quota) and `cliproxybar-quota.so` (removed from Will) were done on November 18 (same FLEET.md section); `camoff-astra` on Will is pending Andrew's OAuth login. Not implemented: the Mac bundle (2b), Will bundle (2c), shared config (6), and the Will layout migration (7). The scheduled health, parity and quota job (5) is implemented and awaits live activation. Where this document and FLEET.md disagree, FLEET.md and the live verifiers win.


Synthesis by Claude Fable 5.2, 2026-11-17, from two independent designs (Fable, Opus 5.5) and their cross-critiques. Revised by Claude Fable 5.2 the same day after the Claude Opus 5.5 red team (58 claims checked, 5 wrong, 1 blocking, 5 major, 9 minor). Repo `/Users/rever/cliproxy`, main at `32fb5874`. Design only: nothing was changed.

Claim tags: **V** = read at the cited path:line by the Fable synthesis or this revision (section 13 lists what the revision re-read). **R** = verified by the Opus red team, not re-read by me. **O** = live fact from the Opus audit, not re-run. **A** = assumed.

## 0. Summary

Will stays the always-on primary. The Mac is a full peer that serves when Will is unreachable or its egress is down. The hosts share no runtime state and neither is in the other's request path.

- **One desired state.** A tracked, secret-free `ops/fleet/config.shared.yaml` is reconciled in place into each host's `config.yaml`. Secrets stay only in each host's own file.
- **One parity check.** `fleetctl parity` compares what each server has loaded, its catalog, its accounts and the ops files each host actually executes. Every difference needs a dated exception.
- **One failover point.** The existing per-session Claude launcher, hardened from `3d88c013`, for the single profile `claude_cliproxy`. No new long-running service.
- **One chain.** The per-release scripts become `ops/fleetctl.py`. Mac first, then Will, each behind Andrew's phrase.
- **No host runs from a git working tree.** Today the Mac does, so a merge or even an uncommitted edit is a deploy (section 9, "Where the Mac runs from"). Both hosts will run ops files from a shipped bundle at a recorded commit.

## 1. Invariants

"Interchangeable" means: a request a client may send to either host gets the same provider, account and model decision on both. Only token freshness and cooldown observations differ.

"Green" for any invariant means equal, or different with an unexpired entry in `ops/fleet/exceptions.json`. Where a section says "strictly equal", no exception is accepted.

| # | Property | Check |
|---|---|---|
| I0 | Same ops files, and they are the ones that run | sha256 of each file in `ops/fleet/bundle.txt` at each host's bundle root equals the shipped commit; every auto-executed path (LaunchAgent `ProgramArguments`, T3 profile launcher path, installed token helper) resolves into the bundle or matches its hash |
| I1 | Same release | Image ID per arch, revision label and `X-CPA-Version` match `ops/fleet-release.json` (exists, `ops/verify_fleet_instance.py:29-66` V); the image that `docker compose config --images` resolves equals the manifest |
| I2 | Same loaded config | Functional fingerprint of the loaded config equal on both hosts and equal to the pin; fields the loaded view hides are compared separately (section 8) |
| I3 | Disk matches apply | sha256 of `config.yaml` equals the value the last apply recorded on that host |
| I4 | Same static secrets | Per-run keyed tags of client keys and provider keys equal |
| I5 | Same accounts | Multiset of `(provider, prefix, account tag, disabled, priority)` equal; one enabled credential per `(provider, t3-* prefix)` |
| I6 | Credential health | No credential in an error state on one host only |
| I7 | Same catalog | `id -> owned_by` from `/v1/models` equal, per client key |
| I8 | Cursor deny | No ID served by a direct provider on either host appears in either host's Cursor catalog |
| I9 | Same plugins | Set of `(id, version, enabled)` equal; each library hash equals its per-arch pin |
| I10 | Same runtime shape | Mounts, six loopback ports, restart policy, `pull_policy: never`, `sysctls`, `DO_NOT_TRACK=1`, usage statistics off, Home off, effective container env key names; Compose input files equal the last activation receipt |
| I11 | Failover keeps identity | On failover the body bytes, model string, client key and query are unchanged; no failover for status < 500 or after the first response byte (node test) |
| I12 | No unexplained drift | Every difference in I0-I10 has an unexpired entry in `ops/fleet/exceptions.json` |

## 2. Surface classification

| Surface | Today | Class | Lives in |
|---|---|---|---|
| Image, source commit, version | Equal (O) | Shared, per-arch hash | `ops/fleet-release.json` |
| Live image selection | `.env` `CLI_PROXY_IMAGE` on both hosts overrides the Compose default (`docker-compose.yml:3`, `ops/instances/will/docker-compose.yml:5` V; key names R) | Activation pin | Host `.env`, written only by `activate` |
| Config layout | Will legacy, Mac v8 (O) | Drift | v8 on both (unit 7) |
| `routing.retry.max-retry-interval` | Will 75, Mac 0 (O; `ops/instances/will/config-policy.json:17` V) | Drift | Shared: 75 |
| `transient-error-cooldown-seconds` | Will -1, Mac 0 (O) | Drift | Shared: -1 (section 5) |
| Strategy, affinity, quota cap, save-cooldown, request-retry, quota-exceeded | Equal (O) | Shared | Shared file; pinned |
| Credential concurrency and in-flight | Explicit on Will only (O) | Latent drift | Shared, explicit |
| `server.discovery` | Will only (O) | Host-specific | Will only; the Mac must not advertise on foreign networks |
| Log size cap | Will 1024 (policy `config-policy.json:23` V), Mac 0 plus archiver | Host-specific | Host path allowlist |
| `debug` | Mac true, Will false (O) | Drift | Dated exception until Andrew decides (Q13) |
| Header defaults, plugin store source, telemetry off | Equal (O) | Shared | Shared file |
| `zai-coding-plan` aliases | Mac lacks `glm-5p3-flash` (O; `ops/GLM_ROUTING.md:15-18` V) | Drift | Shared file |
| `nvidia` provider | Mac only (`ops/FLEET.md:75` V) | Drift | Shared file once Will has the key |
| `glm-5.2` on Cursor | Both (O) | Policy question | Q12 |
| Client API keys | Equal by hash (O) | Shared secret | Each host's `config.yaml` |
| Management key, `.env` values | Per host | Host secret | Host `.env` |
| OAuth files, `.cds`, `kv.db` | Per host | Host-specific, never copied | `auths/` |
| Claude `t3-*` accounts | Equal (O) | Shared identity, local tokens | Separate login per host |
| Codex `camoff-astra` prefix | Mac only (O) | Drift | Q9 |
| `kimi-ai` credential | Will only (O) | Drift | Q10 |
| `auth-cursor` 2.1.1 | Equal, pinned (V manifest) | Shared | Manifest |
| `cliproxybar-quota.so` | Will only, unpinned (O) | Drift | Q11 |
| Compose | Will has `stop_grace_period` and log caps; the Mac has `build`, variable mounts, and an untracked override adding `env_file`, `restart: always` and `!override` ports (`docker-compose.yml:1-42`, `ops/instances/will/docker-compose.yml:1-42`, `docker-compose.override.yml`, `.gitignore:70` V) | Drift (shape) | Policy fields in I10; aligned at the next release |
| Port binding, Tailscale Serve | Will Serve on 8317, Mac loopback (O) | Host-specific | Will owner contract |
| Watchdog, log archiver | Mac launchd only, run from the git working tree (installed LaunchAgents point at `/Users/rever/cliproxy/ops/cliproxy_watchdog.py` every 120 s and `ops/archive_logs.py` every 300 s, V) | Host-specific (Docker Desktop); **deploy hole** | Bundle (unit 2b) |
| Claude launcher on the Mac | T3 `settings.json` holds the working-tree path `/Users/rever/cliproxy/ops/claude_account_launcher.mjs` five times, one per Claude CLIProxy profile (V, path strings only) | **Deploy hole** | Bundle (unit 2b) |
| Token helper on the Mac | T3 runs an installed copy, `~/.local/bin/cliproxy-codex-token`, whose sha256 differs from the tree's `ops/cliproxy-codex-token` (V, hash prefixes only) | Drift (installed copy) | I0; unit 0 finds which commit it matches |
| Will owner contract, PiSec | Will only (V) | Host-specific | `ops/instances/will/` |
| ops files on Will | Hand-copied subset, not a git checkout (V over SSH); hashes match main (R); Will's T3 launcher runs from `/home/andrew/cliproxy/ops` (R) | Drift surface | Bundle (I0) |
| T3 profiles | Per client machine | Client-specific | T3 settings, listed in FLEET.md |

## 3. Config source of truth

The loader has no include, overlay or env expansion (O, reader A). Two approaches were proposed: a full render with a new secrets store, and in-place path edits. I chose **reconcile in place**, which keeps one tracked desired state without a second copy of every secret.

| File | Tracked | Content |
|---|---|---|
| `ops/fleet/config.shared.yaml` | Yes | Functional config in v8 paths. No secret leaves; CI rejects secret-named keys |
| `ops/fleet/paths.json` | Yes | `secret_paths` (kept from the host, never printed), `host_paths` (closed allowlist: `server.discovery`, log size cap), `print_paths` (values the tools may show), `hidden_paths` (fields the loaded view cannot show, section 8) |
| `ops/fleet/exceptions.json` | Yes | `surface, hosts, reason, decision_ref, date, review_by` |
| `ops/fleet/hosts.json` | Yes | Per host: instance root, bundle root, SSH name, arch. No secrets |
| Host `config.yaml` | No (`.gitignore:6` V) | The only place static secrets live |

**`fleetctl config plan|apply --host H`**

0. Refuse a host whose on-disk file is not `config-version: 8` or still has legacy top-level keys (key names only, read on the host). Writing v8 paths into a legacy file makes the server delete the conflicting legacy fields on the next load (`internal/config/config_load.go:343-357` V). Will is therefore refused until unit 7 completes.
1. Read the host's file. Set every path from the shared file. List items (providers) match by `name`, and their secret leaves are kept from the host. Host paths are untouched. Any other path fails as "unmanaged" unless excepted. This matters because the plugin store and management handlers also write this file.
2. `plan` prints changed path names and non-secret values, and flags the reload effects. Every reload syncs the plugin runtime and re-registers executors (`sdk/cliproxy/service_config.go:233`, `:243` V), so every apply can close Cursor bridges. Retry, alias and OAuth changes also force an auth refresh (`internal/watcher/config_reload.go:159-160` V).
3. Qualify the candidate in the existing isolated staging shape (no auth, no network, no published port). It must load with no server rewrite (`config_load.go:349-354` V) and it yields the expected loaded fingerprint.
4. Keep one 0600 backup. Write in place with a single `write()` so the bind-mounted inode survives (`ops/GLM_ROUTING.md:79` V), then compare the length.
5. Succeed only when the **loaded** fingerprint equals the staged one. A failed reload keeps the old config and only logs (`config_reload.go:100-104` V), so a disk check alone proves nothing.
6. Rollback restores the backup only if the current sha equals the sha apply wrote. Otherwise stop and ask (`ops/FLEET.md:65` V: never restore an old full file over later edits). After a rollback run the same readback, I7 and I8 checks as after an apply.

Rule: config changes arrive only through this path. Management-API config writes show up as I3 drift.

**Will legacy-to-nested migration.** Use the server's own migrator, read-only. `GET /v8/management/config.yaml` returns the v8-normalized bytes of the on-disk file without saving them (`internal/api/handlers/management/config_v8.go:41`, `:76-78` V). Fetch it on Will into a 0600 file, qualify it in staging, and require the staged loaded fingerprint to equal live Will's. Both are loaded structs, so explicit-default keys cannot cause a false difference.

That equality is necessary but not sufficient. The loaded view omits every field tagged `json:"-"`: `host`, `port`, `github-token`, `remote-management`, `auth-dir` (`internal/config/config.go:24`, `:26`, `:29`, `:52`, `:58` V) and `response-compression` (`internal/config/sdk_config.go:29` V). A mis-mapped `remote-management` block would change management access with I2 green. Unit 7 therefore adds three checks:

- **Migrator proof in CI.** A fork-only Go test (the repo already has Go tests under `ops/`) feeds a synthetic legacy fixture, with a non-default value in every hidden field and no real secret, through `NormalizeConfigLayout(raw, true)`, loads both documents with the server's loader, and asserts the two structs are equal including the yaml-only fields. The fixture must contain every key path present in Will's file; unit 7 takes that inventory on Will as key names only.
- **Hidden fields on disk.** The v8 view decodes the YAML document itself, not the struct (`config_v8.go:46-47`, `:80-85` V), so it does show these fields under their v8 names (`management`, `oauth.auth-dir`, `server.response-compression`; `internal/config/config_v8.go:79`, `:89-90` V). Compare each `hidden_paths` entry between the staged migrated file and live Will: values for the non-secret ones, keyed tags for `management.secret-key` and `github-token`.
- **Behaviour after the write.** An authenticated management GET with Will's existing key returns 200. An unauthenticated one, and one from the Mac over the tailnet, return the same status codes as recorded before the write. The listener, the port and the credential count are unchanged (owner verifier).

Then write in place. The catalog must be unchanged, and one small request per OpenAI-compatible provider confirms the key is still sent, because a wrong key name silently drops the Authorization header (O, `ops/NVIDIA_ROUTING.md`). PiSec and `verify_live.py` must accept the v8 layout first; the verifier already maps both (`ops/instances/will/verify_live.py:48-70` V).

**Static keys.** `fleetctl secret copy --path P --from m5 --to will` pipes one value over SSH stdin. It never uses argv, prints nothing, and refuses any path under `auths/`.

**Ops files on both hosts.** `fleetctl ship --host H --sha S` pipes `git archive S` of the files in `bundle.txt` to the host's bundle root and records the commit (I0). Before writing it copies every file it will overwrite into `ops/runtime/fleet/ship/<S>/prev/` under the instance root. It never deletes a file, never touches anything else under `ops/runtime`, and leaves files that are not in the bundle alone. Rollback restores `prev/`, or ships the previous commit. Bundle roots: Will `/home/andrew/cliproxy/ops` (exists, V); Mac a new directory outside the working tree (proposed `~/cliproxy-live/ops`, does not exist today, V; name is Q18).

## 4. Accounts and credentials

- Each host logs in separately to the same accounts. Tokens are never copied (`ops/FLEET.md:41` V).
- Parity (I5) reads `GET /v8/management/credentials` on each host (`internal/api/server_management_v8.go:45` V) for `provider`, `label`, `disabled` and `priority` (`internal/api/handlers/management/auth_files.go:672-685`, `:758-769` V). The account tag is `HMAC-SHA256(nonce, provider + label)` cut to 12 hex. The nonce is random per parity run and sent to both hosts over stdin. Tags compare within one run and mean nothing afterwards, so no shared salt is stored and receipts hold only equal or unequal.
- **The prefix does not come from that list.** The list entry has no prefix field (`auth_files.go:654-800` V; the only `"prefix"` in the handlers is the PATCH path, `auth_files_fields.go:655-656` V). The earlier assumption was false. The prefix is derived from what the server registered for the credential: `GET /v8/management/credentials/models?name=<file>` (`server_management_v8.go:48`, `auth_files.go:336-381` V). A prefixed credential registers `prefix/model` for each of its models (`sdk/cliproxy/service_models.go:705-748` V), so the prefix is the `P` for which both `m` and `P/m` appear (or the common leading segment when `force-model-prefix` is on). This is loaded state, which is what routing uses. File names are passed to the endpoint and never printed. A disabled credential registers no models, so its prefix is reported as `unknown (disabled)` and it is compared on `(provider, tag, disabled)` only; the one-enabled-per-prefix rule concerns enabled credentials. A fixture test covers both prefix modes and the disabled case, and unit 2a confirms with a read-only call on each host that the endpoint returns the prefixed IDs (A until then). Fallback if it does not: a host-side reader that parses each auth JSON and emits only `(provider, prefix, disabled, priority, tag)`, with a canary test that no other key can leave it.
- Health (I6) uses the list's `status`, `unavailable`, `disabled`, `next_retry_after` and `last_refresh` fields (`auth_files.go:679-682`, `:730-733` V), reduced to `ok`, `cooling` or `error`. Cooling differences are expected. `error` on one host only fails parity; the remedy is a fresh login on that host.
- Adding, removing or re-logging an account is a fleet change: do both hosts, then run parity.

| One-host item | Disposition |
|---|---|
| `camoff-astra` prefix (Mac) | Count uses in Mac logs (count only). Zero: remove the prefix. Else mirror on Will. With `force-model-prefix` false a prefixed credential also serves unprefixed models (`internal/config/sdk_config.go:85-88` V), so pool sizes already match |
| `kimi-ai` (Will) | Same account as the existing Kimi credential: remove. Different: log in on the Mac |
| `cliproxybar-quota.so` (Will) | Remove; Will has no xAI or Grok credentials (O) |
| NVIDIA (Mac) | Copy the static key to Will; provider moves to the shared file |

## 5. Cooldown and quota state

**Not shared.**

- Quota belongs to the upstream account. Each host learns a cap from one 429. The cost is about one wasted call per capped account per host per window, bounded by the 3600 s cap.
- Sharing needs Home (Redis, which turns off local `.cds`; O) and defeats offline fallback, or file sync inside `auths/`, which is forbidden.
- Consequence: failover never relieves a 429.

**Retry values: Will's on both** (`max-retry-interval: 75`, transient `-1`).

- 75 only limits positive waits between rounds (`sdk/cliproxy/auth/conductor_selection.go:1764-1768`, `:1844` V). The Mac's 0 returns cooldown 429s at once; the audit logs show Claude Code looping on them (614 final 429s on one named route; `cache-evidence.md` O).
- `-1` affects only upstream HTTP 408/5xx (`sdk/cliproxy/auth/conductor_cooldown.go:122-137`, `:1369` V). Dial, DNS, reset and EOF errors skip credential cooldown at any setting (`:2253-2258`, `:2348-2380` V). The earlier claim that 0 would stall failover is withdrawn.
- Cost of `-1`: during an upstream 5xx incident no credential is benched, so each request retries every credential. With three accounts and often one usable, benching the only working account for 60 s costs more. Will's policy file does not require the transient value, so this is new policy (Q1).
- Because dial errors are retried without cooldown, one request can dial once per credential per round. That multiplies the egress-down delay (section 6 item 10).

## 6. Client failover architecture

| Option | Covers | New service | Verdict |
|---|---|---|---|
| A. `3d88c013` as is | Claude | None | Not enough: no timeout, reset or half-open handling |
| B. One Mac edge daemon for all clients | All | One launchd agent in every request path | Rejected: single point of failure, contradicts manual-fallback runbooks |
| C. Tailscale level (Serve on the Mac, shared address) | Tailnet hosts | None | Rejected: fails when Tailscale is stopped, exposes a laptop that sleeps, no layer-7 health |
| D. Mac server proxies to Will | All | None | Rejected: Go change, double hop |
| **E. Hardened launcher for one profile; Codex-family manual** | Claude pool automatic; others by profile choice | None | **Chosen** |

**Per client**

| Client | Route |
|---|---|
| `claude_cliproxy` (Mac) | Will first, Mac fallback, automatic |
| `claude_cliproxy_mac`, three named `_mac` profiles | Mac only, no fallback. They work offline already; a Mac proxy fault is the watchdog's job (120 s cycle). A named route never crosses hosts, so an account mismatch cannot be reached by failover |
| `codex_cliproxy`, `codex_cliproxy_mac`, `codex_kimi_mac` | Unchanged. Codex CLI 0.167.1 has one `base_url` (O). Parity makes the `_mac` profile an exact substitute; choosing it stays manual (`ops/FLEET.md:71`, `ops/KIMI_ROUTING.md:6` V). The health tick (section 8) tells Andrew when to switch |
| `claudeAgent` direct | Untouched |
| Will's T3 | Loopback only. `restart: always` is the remedy |
| M1 | Will only. Fallback is its direct subscriptions. The Mac stays loopback-only. M1 gets no automatic signal; Andrew gets the health notification |

**Launcher changes on top of `3d88c013`** (in `ops/claude_account_launcher.mjs`, selector factored so tests inject clock and probe). Account pinning stays where it is (`:70-79` V); host selection never reads the body.

**Scope guard.** The same launcher file serves all five Mac Claude CLIProxy profiles and Will's T3. Every item below is active only when `CLIPROXY_FALLBACK_URL` is set, which only `claude_cliproxy` on the Mac gets. Without it the request path is today's code: default agent, no timers, no probes. The existing tests stay unchanged and a new test asserts that an unset fallback creates no timer and no probe.

1. Connect guard, default 3 s, armed on `socket`, cleared on `connect`. Expiry counts as a pre-connect failure. Nothing times out after connect.
2. `agent: false`, so `connected` describes a fresh connection (flaw at the `socket` handler in `3d88c013` V).
3. Health probe: `GET /healthz` must return 200 with JSON `status: ok` (`internal/api/server_routes.go:46-55` V), 3 s total. It is a probe, never a client request; the watchdog sets the precedent (`ops/cliproxy_watchdog.py:223` V). It runs before first use and while the primary is marked down. A TCP-only probe would pass on Serve accepting and closing, or on a captive portal.
4. Reset or close with zero response bytes after connect: probe once. `count_tokens` and `GET /v1/models` replay on the fallback. `POST /v1/messages` is never replayed, because the primary may already be generating. Return a retryable 502, mark the primary down if the probe failed, and let the client's retry take the fallback.
5. Liveness watch. While a request waits or streams, probe its host every 20 s. Any response bytes on that request (headers or body) reset its failure count. Three consecutive failed probes mark the host down **for new requests**. An in-flight request is ended (502 before headers, destroy after) only when three consecutive probes failed **and** it received no bytes during that whole window. A stream that is still delivering bytes is never cut, whatever the probes say: during a Tailscale flap new connections through Serve can fail while an established one still works, and cutting it would waste capped weekly quota. A slow model on a healthy host is never cut either. Node tests: flowing stream with failing probes completes; silent request with failing probes is cut; silent request with passing probes is not.
6. Recovery by probe: while down, probe every 15 s; return after two successes and at least 30 s. For egress-classified failover `/healthz` stays green, so hold the fallback 120 s, doubling to 600 s, reset on a primary success.
7. One failover per request. If the sticky fallback fails before connect, try the primary once.
8. `X-CLIProxy-Host` header (exists) and one JSON line per transition in `ops/runtime/fleet/failover.jsonl` (ignored, `.gitignore:66` V). No bodies or keys.
9. Preconditions to enable, **strictly equal, no exception accepted**, limited to what this profile can reach: I4; I5 for the Claude provider rows; I7 and I8 for unprefixed `claude-*` IDs, which is all the shared-pool route accepts (`ops/claude_account_launcher.mjs:70-72` V). Mac-only catalog entries such as `camoff-astra/*` and `nvidia-*` are outside this set, so unit 3 does not wait for unit 4.
10. **Egress-down bound.** Failover on "Will up, egress down" starts only when Will returns its dial-error 5xx, and nothing bounds that today. The Claude path dials with `proxy.Direct`, a dialer with no timeout (`internal/runtime/executor/helps/utls_client.go:337-338`, dial at `:392` V). The optional `requests.tls-handshake-timeout-seconds` bound starts only after the dial returns (`utls_handshake_timeout.go:58-60` V), so it cannot bound a blackholed SYN. Dial errors are retried without cooldown (`conductor_cooldown.go:2348-2380` V), so the worst case is about (1 + `request-retry`) x credentials x one OS connect timeout (A; roughly two minutes each on Linux defaults). Three regimes behave differently: no route fails at once, a dead resolver fails after the resolver timeout, a blackhole waits for the SYN retries. Unit 0 measures all three in a disposable staging container (below) and unit 3 uses the captured response as its fixture. Target: Will's final dial-error response within 30 s. Remedies, in order, stopping at the first that meets the target:
    1. Compose only, no Go change: `sysctls: net.ipv4.tcp_syn_retries` (2 gives about 7 s per dial, 3 about 15 s) and, if the resolver regime needs it, `dns_opt` timeout and attempts. Both act only before a connection exists, are scoped to the container's network namespace (it has its own, `NetworkMode cliproxy_default` V), join I10, and land with the next activation because they need a recreate (Q17). A: that both kernels accept the namespaced sysctl; unit 0 tests it.
    2. Launcher hint: at session start `claude_cliproxy` reads the Mac's `ops/runtime/fleet/health.json` (section 8). If it is newer than 600 s and says Will's egress is down, start on the fallback under the item 6 hold. A missing, stale or unreadable file is ignored.
    3. Last resort, a separate proposal: a dial bound in the fork's Claude dialer. The repo rule allows timeouts only during credential acquisition, so this needs Andrew's explicit ruling and is not proposed now.

## 7. Failover decision table (`claude_cliproxy`)

| Situation | Action |
|---|---|
| Refused, DNS failure, no route | Replay on fallback (in `3d88c013` V) |
| Connect timeout, Tailscale stopped | Replay on fallback after the guard |
| Zero-byte reset after connect | Probe. Idempotent ops replay; `/v1/messages` gets 502, no replay |
| Accepted then silent, half-open | Liveness watch ends it after three failed probes with no bytes received; no replay of `/v1/messages` |
| Probes fail while the stream still delivers bytes | Stream continues. Host marked down for new requests only |
| Will up, upstream egress down (5xx JSON with dial error text) | Replay on fallback, backoff hold. Will's last attempt did not connect; an earlier attempt in the same retry round may have reached the provider if egress failed mid-request. Time to that answer is bounded by section 6 item 10 |
| Server's own 503 "no credential", other 5xx, 529 | Pass through (negative test) |
| 401, 403 | Pass through. A bad client key is equal on both hosts (I4). A dead token on one host is surfaced by I6 and fixed by login there |
| 429, pool or named prefix | Pass through. Report limit type and reset |
| Mid-stream drop | Destroy the client response, no replay |
| WebSocket | Not supported by the launcher (`:14-16` V) |
| `count_tokens`, `/v1/models` | Same rules; replay is always safe |
| Fallback also fails | Return its error |

## 8. Verification and drift detection

- **Loaded fingerprint (I2).** `GET /v0/management/config` returns the loaded struct with defaults, the same shape for either file layout (`internal/api/handlers/management/config_basic.go:26-31` V); the verifier already reads it (`ops/verify_fleet_instance.py:26` V). Fingerprint = sha256 of canonical JSON minus `secret_paths` and `host_paths`. The tool prints hashes per top-level key and values only for `print_paths`, so a new secret field can never be printed. A fixture test plants canary strings and fails if any appears.
- **Hidden fields (I2).** The loaded view cannot show `json:"-"` fields (section 3). Parity reads each `hidden_paths` entry from the v8 disk view on both hosts and compares it: values for non-secret ones (management flags, response compression), keyed tags for secret ones, and `host`, `port` and `auth-dir` against each host's expected value in `hosts.json`. This is a disk view (`config_v8.go:30` V re-reads the file), so it is paired with behaviour: the six published ports and the credential count (I10, I5) show that the loaded values match.
- **Catalog (I7) and Cursor (I8).** `owned_by` alone does not prove Cursor cannot serve an ID (`ops/GLM_ROUTING.md:49-50` V); the last registrant sets it (`internal/registry/model_registry.go:770` V). I8 reads `GET /v8/management/routing/model-definitions/cursor` (`server_management_v8.go:33` V), which reflects native claims made before plugin arbitration (`internal/pluginhost/adapters_executors.go:52-63` V). Run I8 after every reload and every recreate; cold-restart order is untested (`GLM_ROUTING.md:63-64` V).
- **`fleetctl parity`** runs the fingerprint locally and on Will over SSH, subtracts exceptions, and exits non-zero on anything unexplained or expired. "Will unreachable" is a separate result, not drift.
- **Pins.** Extend the existing `runtime_policy` in `fleet-release.json` with the retry values and add `config_fingerprint`, so one host can self-check alone. `fleetctl check` asserts Will's `config-policy.json` agrees with the shared file.
- **Schedule: one launchd job on the Mac.** Every 300 seconds it checks HTTP health and container egress on each host and reads Codex, Kimi and Z.ai quota. About every 900 seconds, by elapsed time, it reads Claude quota. It checks parity hourly and records the comparison in `ops/runtime/fleet/latest.json`. The job sends no inference requests. [FLEET.md](FLEET.md#health-and-quota-monitor) documents the reports, alerts and polling rules.
- **Non-inference health.** `/healthz`, authenticated `/v1/models`, credential list, plugin registration, and `claude_usage_resets.py status` no faster than every 300 s.
- **Inference canary, only** after a release activation (one pooled and one named reply per host on an account with headroom, from one usage call per deploy; capped accounts are recorded as skipped) and after a routing-changing config change (one small request per affected alias per host). Cursor and NVIDIA canaries need their own approval. Never in drift checks or probes.
- **CI.** New fork-only `.local-ci/workflows/fleet-ops.yml` on `ubuntu-latest` (discovered per `~/.config/local-ci/README.md:149-172` V): `go test ./ops/...`, `python3 -m unittest` over `ops`, `node --test ops`, `fleetctl check`. Today the PR workflow only builds (`.github/workflows/pr-test-build.yml:20-25` V). `docker-image.yml` runs only on tag pushes (`.github/workflows/docker-image.yml:3-6` V), so its `ubuntu-24.04-arm` job (`:52` V) never enters a PR gate. Arm images are built and qualified by the release scripts, not by CI.

## 9. Deploy chain

`ops/fleetctl.py`, stdlib Python, tests in `ops/test_fleetctl.py`. State per release in `ops/runtime/fleet/<id>/state.json`; sanitized receipt in `ops/evidence/<id>/`.

**Where the Mac runs from (blocking finding, accepted).** On the Mac the working tree is the deployed location. The installed LaunchAgents run `ops/cliproxy_watchdog.py` and `ops/archive_logs.py` from `/Users/rever/cliproxy` (V), T3 launches `ops/claude_account_launcher.mjs` from the same tree for all five Claude CLIProxy profiles (V), and Compose reads the tracked `docker-compose.yml` there (container label `working_dir`, V). So a merge, a pull, a branch checkout or an uncommitted edit in that tree is live within 120 s, before any phrase. The first draft treated the Mac as a shipping target; that was wrong.

Decision: **run the Mac from a shipped bundle, as Will does** (the red team's option b), with option a as the interim rule.

- *Interim rule, until unit 2b is verified.* Any PR that touches a Mac-executed file (`cliproxy_watchdog.py`, `archive_logs.py`, `claude_account_launcher.mjs`, the two plists, `docker-compose.yml`) is live tier, and "merge plus update the primary checkout" is the promote step behind the phrase. Work on those files happens only in worktrees (three exist under `.worktrees/`, V), never in the primary tree.
- *Unit 2b.* One PR makes the only in-tree change the watchdog and archiver ever need again: take the instance root from `CLIPROXY_ROOT` instead of the script's own location (`cliproxy_watchdog.py:117`, `archive_logs.py:251` V), add the Compose guard and the pause file below. Then `fleetctl ship --host m5`, reinstall the two LaunchAgents pointing at the bundle, and repoint the five T3 profile launcher paths (new sessions only). After that, merging to main changes nothing that runs, and `ship` is the gated step on both hosts.
- *Compose stays in the instance root*, because the bind mounts and `.env` live there. Two facts keep a merged Compose change from going live by itself. The image is selected by `.env` `CLI_PROXY_IMAGE`, which only `activate` writes. And the watchdog guard: before a `compose up` repair it requires the sha256 of `docker-compose.yml` and `docker-compose.override.yml`, and the image that `docker compose config --images` resolves (which includes `.env` and the override), to equal the last activation receipt. On a mismatch it does not run `up`; it starts the existing container without re-reading Compose and notifies. Parity also flags when `docker compose config --hash cli-proxy-api` differs from the running container's `config-hash` label, meaning the next `up` would recreate. Today they are equal on the Mac (`f6a62a48...`, V).
- *Parity row P0* compares the files that actually run: the paths in the installed LaunchAgents and T3 profiles must resolve into the bundle root, and their sha256 must equal the shipped commit. The token helper's installed copy is hashed too.
- *Residual.* Sessions started before the repoint keep their old launcher process in memory until they end. The interim rule stays in force until none remain (process list, read-only).

| Existing script (`ops/evidence/upgrade-v8.0.66-20261110/`, V list) | Becomes |
|---|---|
| `validate_source.py` | `fleetctl release validate` |
| `build_images.py`, `assemble_images.py` | `release build` (checks Will `df -h /` >= 150 GB; 283 GB today V) |
| `qualify_staging.py` | `release qualify`, `config` step 3 |
| `backup_deployment.py` | `backup --host` |
| `activate.py` | `activate --host` (writes `CLI_PROXY_IMAGE` in `.env` and the activation receipt) |
| `verify_fleet_instance.py`, `verify_client.py`, `verify_preservation.py` | `verify --host` (`verify_live.py` stays the Will owner check) |
| `verify_claude.py` | `canary --host` |

**Code release**

| Stage | Gate |
|---|---|
| 1. Source PR on the fork | `local-ci gate --pr N` = 0; Fable and Opus review |
| 2. Build both arches from the merged commit; qualify each image in staging | Disk floor; health, plugin hash, analytics off, no config rewrite, loaded fingerprint. A release that changes `.cds`, `kv.db` or auth file formats is flagged, since rollback then needs its own plan |
| 3. Pins PR: manifest, Compose default pins, Will contract | local-ci; two reviewers (precedent: pin commit `5c6788b7`, two local-ci runs, `ops/FLEET.md:13` V). After unit 2b a merge here changes nothing live on either host |
| 4. Prepare: diff, pins, activity per host, affected clients, canary plan, rollback commands | **Stop.** Phrase `promote fleet <id>`, which also authorizes automatic rollback of the host just changed |
| 5. Mac: `ship --host m5`, pause watchdog, activity check, backup, activate, verify, I8, canary, resume watchdog | Failure: roll the Mac back (image pin, Compose files, previous bundle), stop |
| 6. Will: `ship --host will`, same steps, owner verifier, and the PiSec inventory update **last**, only after Will verifies (`ops/instances/will/README.md:51-54` V) | Failure: roll back **Will only**, record a skew exception, ask Andrew |
| 7. Parity; one new session per profile class | |
| 8. Receipt and FLEET.md release paragraph | Docs tier, one reviewer |

- **Mac first** because it is the fallback: a bad release costs only fallback capacity, as fleet.2 showed (`ops/FLEET.md:13` V), and during Will's recreate the failover profile rides on a verified Mac.
- **A verified Mac is never rolled back automatically.** While Will is failing it is the only healthy host.
- **Skew after a failed Will activation.** Main's merged pins and the Mac are then ahead of Will. Will's rollback restores its `.env` image pin and Compose file from the backup, ships the previous bundle commit (so Will's owner verifier checks against the contract Will actually runs), and reverts the PiSec inventory if that step had already run. A: the PiSec inventory procedure lives outside this repo; unit 0 records it. The skew exception covers I0 and I1 for Will against main and against the Mac, with `review_by` at most 7 days. It closes in one of two ways, chosen by Andrew: `fleetctl release resume` activates Will after the cause is fixed, or a "revert pins" PR plus a Mac rollback under the phrase. Neither happens automatically. If Will is merely unreachable, the release stays open under the same exception.
- **Watchdog.** It can repair Compose and restart the container (`ops/cliproxy_watchdog.py:297-330` V) and would race activation. Unit 2b adds an expiring pause file (30 min at most) so a crashed deploy cannot leave it off, and the Compose guard above. A test asserts that a repair resolves the activation-pinned image from `.env` plus the override, not the Compose default, and that a mismatch blocks `up`.
- **Activity check.** A recreate drops every stream on that host. Counting established connections to 8317 does not work: keep-alive connections from Codex CLI and launchers, and tailscaled's connections through Serve on Will, rarely reach zero (R). The check measures activity instead: the container's network byte counters sampled over two 15 s windows must stay under a small floor, and on Will `ss -tin` must show no connection on 8317 with a receive or send in the last 30 s. Unit 0 fixes the exact method and floor per host (A). A request that is silent for longer than the window is missed, so the prepare report says "no observed activity", not "idle". Proceeding with activity present needs the phrase to say so.

**Config change:** PR on the shared file, CI, two reviewers, `plan` for both hosts, stop for `promote fleet-config <id>`, then Mac apply and verify, Will apply and verify, parity, receipt. Batch edits so each host reloads once. If I8 fails after an apply, run the documented nudge once: two writes (comment in, comment out; an unchanged file is skipped, `config_reload.go:76-78` V) with I3 suspended and recorded. If it still fails, roll back, and run the same I7 and I8 checks after the rollback.

**Rollback.** Code: restore the `.env` image pin and Compose files, keep config and OAuth, `docker compose up -d --no-deps --pull never --no-build cli-proxy-api`, ship the previous bundle commit, rerun verifiers (`ops/FLEET.md:21`, `:37` V). Backups never include `auths/`. Config: section 3 step 6.

**Idempotency.** Each stage records its input hash; a rerun verifies and does nothing. `activate` skips when image and fingerprint already match. Backups are write-once per release.

**Risk tiers.** None now. Every live promote needs the phrase. The only automatic action is rollback of the host just changed, inside an approved promote. Drift is reported, never re-converged automatically, because that could undo a deliberate emergency change and each reload closes Cursor bridges. Revisit after three clean gated runs (Q6).

## 10. Rules and runbook text

**`ops/FLEET.md`, new first section "Fleet contract":**

> Will and the M5 Mac are interchangeable CLIProxy hosts. Will is the default primary; the Mac serves when Will is unreachable or its egress is down. Both run the release in `fleet-release.json` and the functional config in `ops/fleet/config.shared.yaml`, with the same accounts under the same prefixes and the same model catalog. Each host keeps its own OAuth files, cooldown state, secrets and logs. None of these are copied, except static provider keys through `fleetctl secret copy`.
>
> Change the fleet only with `ops/fleetctl.py`. Do not edit `config.yaml` by hand or through the management API. A change that reaches one host must reach the other in the same change, or be recorded in `ops/fleet/exceptions.json` with a reason and a review date.
>
> Neither host runs ops files from a git working tree. Each runs the bundle that `fleetctl ship` installed at a recorded commit. Merging to main deploys nothing. Until the Mac bundle is in place, a change to the watchdog, the log archiver, the Claude launcher, their plists or `docker-compose.yml` in the Mac's primary checkout is a live change and needs Andrew's promote phrase; edit those files only in a worktree.
>
> Run `python3 ops/fleetctl.py parity` before and after any fleet change, login or logout. Unexplained differences block the change.
>
> `claude_cliproxy` fails over from Will to the Mac on network faults only. Failover never changes account, subscription or provider and never answers a 429. Named account profiles and Codex-family profiles do not fail over; select the `_mac` profile when Will is unreachable or the health notification reports Will's egress down.
>
> Activate the Mac first, then Will. A failed verification rolls back the host just changed. A host that already passed is rolled back only on Andrew's instruction.

`ops/KIMI_ROUTING.md:6` gains the standing authorization from Q5, if granted.

**Global `~/.codex/AGENTS.md`, through an `andrewfree/agent-rules` PR, after unit 3 is live:**

> - CLIProxy fleet: Will and the M5 Mac are interchangeable proxy hosts (runbook `~/cliproxy/ops/FLEET.md`). Never change config, credentials, plugins or the image on one host only: use `ops/fleetctl.py` for both, or record the exception in `ops/fleet/exceptions.json`. Run `fleetctl parity` before and after.
> - `claude_cliproxy` fails over to the Mac proxy by itself on network faults. A 429 is account quota: no host or profile switch relieves it.

The existing sentence naming `claude_cliproxy` then `claude_cliproxy_mac` stays as written. Global rules are in the installed-or-live tier: two reviewers, the agent-rules CI gate, and a live check (unit 9).

## 11. Migration plan

"Phrase" is Andrew's explicit promote approval. **Code tier** means two reviewers (Fable and Opus) and `local-ci gate` = 0. **Live tier** means code tier plus a live check after the change. Every live-tier step below also needs the phrase.

| # | Unit | Verify | Rollback | Gate |
|---|---|---|---|---|
| 0 | Read-only prechecks. (a) Does the Coding Plan model list serve `glm-5.2`; `camoff-astra` use count; `kimi-ai` account sameness; how T3 decides profile eligibility; MagicDNS behaviour with Tailscale stopped. (b) **Egress-down timing:** a disposable staging container from the pinned image on an isolated network, no real credential, one fake static Claude key whose base URL (`config.example.yaml:814` V) points at a sibling container that refuses, a name that does not resolve, and a sibling that drops SYNs. Record time to the final response and its text, with default settings and with `tcp_syn_retries` 2 and 3 on both kernels. (c) The activity-check method and floor per host. (d) Which commit the installed token helper matches. (e) The PiSec inventory procedure. (f) How the three named T3 profiles' launcher paths are edited | Findings recorded; the response captured in (b) becomes the unit 3 fixture | Remove the staging containers | None. Nothing live is touched and no real provider is contacted |
| 1 | **Mac drift batch, one reload:** add the `glm-5p3-flash` alias; retry 75; transient -1. No logging change. **Write method:** a 0600 backup, then one host-side `write()` of the new bytes to the existing inode (no rename, no temp file), then a length check. If no reload shows within 30 s (loaded values unchanged), write once more with a comment change; if still none, write the original bytes back and stop to ask. Do not switch to a management-API write silently | Loaded values read back from the server; Cursor catalog no longer lists the alias (I8 check by hand). If it still does, run the documented nudge once (comment in, comment out, `GLM_ROUTING.md:60-64` V) and recheck. `/v1/models` shows `zai-coding-plan`; one small request each to `glm-5p3-flash` and `glm-5.3-flash` (no recorded test, `GLM_ROUTING.md:16` V); before and after sha256 recorded | Remove only these lines with the same single in-place write, never an old full file. Then the same checks: loaded values read back, Cursor catalog, nudge if needed, client auth | Live tier on the exact diff; phrase; Q1 |
| 2a | Parity tooling PR, read-only at runtime: `fleetctl fingerprint|parity|check|ship|health`, `paths.json`, `hosts.json`, `bundle.txt`, `exceptions.json` seeded with today's drift, pins, `.local-ci/workflows/fleet-ops.yml`, FLEET.md contract. Touches no Mac-executed file | Parity passes with only seeded exceptions; a seeded fake difference is caught; the prefix fixture test passes and a read-only call on each host confirms prefixed IDs; local-ci gate 0 | Revert commit | Code tier |
| 2b | **Mac bundle.** PR: `CLIPROXY_ROOT` in watchdog and archiver, Compose guard, pause file, plists. Then update the primary checkout, `ship --host m5`, reinstall both LaunchAgents, repoint five T3 profile paths | Watchdog and archiver tests; P0 green on the Mac (running paths resolve into the bundle, hashes equal the shipped commit); a watchdog cycle logs normally; one new session on `claude_cliproxy_mac` and one on a named profile answer `count_tokens`; an edit in a scratch worktree changes nothing that runs | Repoint the profiles and LaunchAgents to the tree paths; revert the PR in the checkout | Live tier; phrase covers the checkout update, the ship, the LaunchAgent reinstall and the T3 change |
| 2c | Ship the bundle to Will | First ship reports every existing file unchanged (hashes match main, R) and lists the added ones; `prev/` backup exists; `ops/runtime` otherwise untouched; P0 green on Will; Will's T3 launcher hash unchanged | Restore `prev/`. Nothing is deleted | Live tier; phrase |
| 3 | Launcher failover: re-apply the `3d88c013` launcher and tests as a new commit under Andrew's identity (its author is `f <sgf>`, V), add section 6 hardening behind the scope guard, ship to both hosts, set `CLIPROXY_FALLBACK_URL` on `claude_cliproxy` | Node tests (31 on main, R; 40 in the `3d88c013` worktree, O) plus new ones: injected clock, the unit 0 egress fixture, the 503 negative test, the three liveness tests, and "no fallback set means no timers or probes". Live: a throwaway session whose primary is a closed local port, then a TEST-NET address, returns `count_tokens` with `X-CLIProxy-Host: fallback`; one new session each on `claude_cliproxy_mac`, a named profile and Will's T3 behaves as before. Tailscale and Will are not touched | Remove the env var (new sessions); ship the previous bundle commit to both hosts | PR code tier. Ship and the T3 change: live tier; phrase. Preconditions of section 6 item 9 strictly equal first |
| 4 | One-host items, each its own change on both hosts: NVIDIA key to Will, `camoff-astra`, `kimi-ai`, `cliproxybar-quota.so`, `glm-5.2` | I4, I5, I7, I8, I9 equal; exceptions removed one by one; parity after each item | Per item, in place, with the same checks after | **Live tier per item**; phrase per item; Q8-Q12 |
| 5 | Scheduled Mac job from the bundle: health and non-Claude quota every 300 s, Claude quota about every 900 s, parity hourly | `ops/runtime/fleet/health.json` and `ops/runtime/fleet/latest.json` refresh, monthly quota history grows, SSH works from launchd, and a sustained simulated state change produces one notification | Unload the plist | Live tier; phrase |
| 6 | Shared config and `config plan|apply`. Apply refuses non-v8 hosts, so only the Mac is applied here; Will follows unit 7 | First plan on the Mac shows zero changes; loaded fingerprint unchanged; a legacy fixture host is refused | Revert commit; section 3 step 6 for an apply | PR code tier. **First apply on each host: live tier**; phrase |
| 7 | Will layout migration (section 3) | Migrator Go test green; staged and live loaded fingerprints equal; hidden fields equal; management status codes unchanged from before; catalog unchanged; owner verifier and PiSec pass; one small request per OpenAI-compatible provider | Write the backup bytes back; rerun the same checks | Live tier; phrase |
| 8 | Release subcommands lifted from the per-release scripts; Compose policy fields aligned (`stop_grace_period`, log caps, `sysctls` if Q17 is accepted) in the next pins PR | Re-qualify current images; `activate` reports already at target with no restart | Revert commit; ship the previous bundle | PR code tier. Ship: live tier; phrase. Compose changes go live only with the next release activation |
| 9 | Rules: agent-rules PR for the global block; KIMI_ROUTING wording | Installed `~/.codex/AGENTS.md` equals the merged text and the repo's `snapshots/home-AGENTS.md`; a new session shows the block | Revert PR and reinstall the previous snapshot | **Live tier: Fable and Opus review, the agent-rules local-ci gate, and the installed-file check** |
| 10 | Optional: Codex-family automatic fallback through a per-session wrapper, if T3 allows one | Wrapper tests; closed-port drill | Point the profile back | Q5; live tier; phrase |

Units 1 to 3 (with 2a to 2c) deliver most of the request. Unit 3 does not depend on unit 4.

## 12. Open questions (recommended default first; "yes" accepts all)

1. Retry: Will's values on both (wait up to 75 s; transient cooldown off, which is new policy with the cost in section 5). Yes?
2. Failover scope: only `claude_cliproxy` fails over; named and `_mac` profiles stay Mac-only. Yes?
3. Point `claude_cliproxy` at `http://will.<tailnet>.ts.net:8317` instead of the raw address, so a stopped Tailscale fails fast by DNS and nothing goes in clear to a foreign network reusing that address range (after unit 0 confirms the behaviour). Yes?
4. Liveness watch: three failed health probes mark a host down for new requests. A request already in flight is ended only if it also received no bytes during that window; a stream that is still flowing is never cut. Yes?
5. Codex-family: no daemon; standing permission to select the `_mac` profile when Will is unreachable or the health notification reports its egress down; automatic wrapper only on a later request. Yes?
6. No auto-promote tier now; revisit after three clean runs. Yes?
7. If Will fails after the Mac passed: roll back Will only, record a skew exception of at most 7 days, and ask whether to resume Will or revert pins and the Mac. Yes?
8. NVIDIA: copy the static key to Will. Yes?
9. `camoff-astra`: remove if unused in the logs, else mirror on Will. Yes?
10. `kimi-ai`: remove from Will if it is the same account, else log in on the Mac. Yes?
11. `cliproxybar-quota.so`: remove from Will. Yes?
12. `glm-5.2`: add a direct alias on both hosts if the Coding Plan lists it; if not, keep it on Cursor as a dated exception. Yes?
13. Logging stays host-specific: Will capped at 1024 MB, the Mac uncapped with its archiver, Mac `debug: true` as a dated exception. The Mac cap is not touched while 148 GB sits un-archived and the archive job is failing (O). Yes?
14. Config by in-place reconcile, with secrets only in each host's file. Yes?
15. Ops files reach **both** hosts as a `git archive` bundle, so the Mac stops running the watchdog, archiver and launcher from the git working tree. Until that is done, changes to those files in the Mac's checkout need the phrase. Yes?
16. M1 and Will's T3 stay single-homed; the Mac stays loopback-only; `server.discovery` stays Will-only. Yes?
17. Egress-down bound: if unit 0 measures more than 30 s, add `net.ipv4.tcp_syn_retries` (and resolver options if needed) to both Compose files at the next release, with no Go change. Yes?
18. Mac bundle root `~/cliproxy-live/ops`. Yes, or name another directory outside the checkout?

## 13. Assumptions and unverified items

Re-read in this revision (V): the installed LaunchAgent paths and intervals; the launcher and token-helper paths in T3 `settings.json` (path strings only); the token helper hash difference; `auth_files.go:654-800` and the absence of a prefix field; `service_models.go:705-748`; `config.go:24-58` and the other `json:"-"` fields; the `config_v8.go` handler `:27-85` and the mapping at `internal/config/config_v8.go:78-90`; `config_load.go:343-357`; both Compose files and the Mac override; `docker-image.yml:3-6`, `:52`; `GLM_ROUTING.md:55-82`; the Claude dial path in `utls_client.go` and the handshake bound; `cliproxy_watchdog.py:96-160`, `:297-330`; Will's `ops/` listing and 283 GB free over SSH; Compose v5.7.0 `config --hash` equal to the running container's label on the Mac.

- Live facts tagged O come from the audit and were not re-run. Facts tagged R come from the red team.
- `/v0/management/config` stays available in future upstream releases (A). Release qualification asserts it; if removed, the fingerprint needs a new source.
- `GET /v8/management/credentials/models` returns the prefixed IDs registered for a credential (A; the registration call is V at `service_models.go:264`, the registry read-back is confirmed in unit 2a). The credential list's `label` is stable per account (A).
- A fake static Claude key in staging takes the same `proxy.Direct` dial path as an OAuth credential (A; unit 0).
- Worst-case egress-down delay is (1 + `request-retry`) x credentials x connect timeout (A; unit 0 measures it).
- Both kernels (Will, and Docker Desktop's VM on the Mac) accept `net.ipv4.tcp_syn_retries` as a namespaced container sysctl (A; unit 0).
- `.env` key names (Will: `MANAGEMENT_PASSWORD`, `CLI_PROXY_IMAGE`, `DO_NOT_TRACK`; Mac: the first two) are R. I did not open either `.env`.
- Whether Will sets `requests.tls-handshake-timeout-seconds` (R says it exists; `config-policy.json` does not list it, V). It would not bound the dial in any case.
- Tailscale Serve accepts then closes when its backend is down; Docker Desktop's forwarder may accept and stay silent (A).
- `*.ts.net` names do not resolve with Tailscale stopped (A; unit 0).
- Claude Code retries a 502 by itself (A).
- A 3 s connect guard suits a relayed Tailscale path (A; env-tunable).
- The activity check's byte-counter floor separates idle keep-alives from real requests (A; unit 0). Upstream streams send bytes often enough to register (A).
- Starting an existing container without `compose up` leaves its configuration unchanged (A; Docker behaviour, tested in unit 2b).
- The PiSec inventory procedure and what PiSec reads from Will's config (A; unit 0 and unit 7).
- How `orchestrator_capabilities` decides eligibility, and how the three named T3 profiles are edited (A; unit 0).
- The notification route for the health job. A `com.rever.text-me` LaunchAgent exists on the Mac (V, name only); its interface was not read (A; unit 5).
- Cursor bridge behaviour on reload beyond `FLEET.md:35`, restart-only settings, cold-restart registration order (unconfirmed).
- A host-side in-place write reaches the watcher through the Docker Desktop mount on the Mac (A; the last Mac change used the management API, `GLM_ROUTING.md:34` V). Unit 1 states what to do if it does not.
- The registration-order bug is an upstream issue. Reporting it to `router-for-me/CLIProxyAPI` is a third-party contribution needing separate approval; none is proposed.

## 14. Decision log

| Choice | Rejected | Why |
|---|---|---|
| Reconcile shared paths in place | Full render plus per-host secrets store; hand edits | No second copy of secrets, survives server and plugin-store writes, still one tracked desired state |
| Fingerprint the loaded config (`/v0` GET), plus a separate hidden-field comparison | v8 view of the disk file alone; loaded view alone | The v8 view re-reads disk (`config_v8.go:30` V), so a failed reload would pass. The loaded view hides `json:"-"` fields (red team, accepted), so it cannot prove a migration alone |
| Per-run nonce for account and key tags | Stored fleet salt; plain hashes | Nothing to store or distribute; plain hashes of emails are guessable |
| Prefix from the per-credential model list | The credential list (has no prefix, red team, accepted); a reader of auth files as the first choice | Loaded state, read-only endpoint, and no tool opens token files unless the endpoint proves insufficient |
| Migrate Will with the server's own normalized output, proven by a Go fixture test | Hand-written mapper; live management write | Read-only source, no mutation through the management API; the test covers the fields the fingerprint cannot see |
| Both hosts run a `git archive` bundle | The Mac keeps running from its working tree under a "merge is the promote" rule (red team option a) | Option a depends on nobody pulling, checking out or editing in that tree. A bundle makes `ship` the only path on both hosts and gives one I0 check. Option a is kept only as the interim rule |
| Compose stays in the instance root, guarded by file hashes and the resolved image | Move Compose into the bundle | The bind mounts and `.env` live there; moving them risks a recreate. `.env` is already the activation pin |
| Failover in the launcher for one profile, hardening active only when a fallback is set | Edge daemon; Tailscale; server-side proxying; hardening for all profiles | No new service, no Go change, and no behaviour change for the four Mac profiles and Will's T3 that share the file |
| No fallback on named routes | Fallback gated by a periodic parity check | A named route must never reach another account; a 6 h or hourly check cannot guarantee that |
| Never replay `POST /v1/messages` after connect | Replay after a failed probe | Possible double generation against capped weekly quota |
| Liveness watch that never cuts a stream still delivering bytes | Cut on three failed probes alone; fixed first-byte deadline; TCP keepalive only | Probe failures can mean only that new connections fail. Cutting a working stream wastes the quota the no-replay rule protects (red team, accepted) |
| Bound egress-down delay with a container sysctl, measured first | Go dial timeout now; the handshake bound; do nothing | Red team finding accepted with one correction: its cited line (`sdk/proxyutil/proxy.go:114`) is the SOCKS forward dialer, and direct standard transports clone `http.DefaultTransport` (`proxy.go:95-107` V). The unbounded dial is real on the Claude path (`utls_client.go:337-338` V). The handshake bound starts after the dial, so it does not help. A sysctl needs no Go change and stays before the connection |
| Health tick every 300 s in the one scheduled job | Hourly egress report; a watcher daemon | Hourly leaves Codex-family profiles on a dead route for up to an hour; still one periodic job |
| Activity-based pre-activation check | Count of established connections | Keep-alive and Serve connections never reach zero, so the gate would block always or be overridden by habit (red team, accepted) |
| Cursor deny check on provider catalogs | `owned_by` map; allowlist of Cursor's whole catalog | `owned_by` is last-registrant; a whole-catalog allowlist flaps |
| Will's retry values on both | Mac's values | No change on the primary; ends immediate cooldown-429 loops |
| No shared cooldown or Home | Home over Redis; `.cds` sync | Shared dependency defeats offline fallback; `auths/` is never synced |
| Mac first; roll back only the host just changed; skew closed by resume or by an approved revert | Roll back both on a Will failure; leave the skew open-ended | Never recreate the only healthy host; a dated exception with two named exits keeps main and Will from drifting apart silently |
| Global rules change at live tier | One reviewer for the agent-rules PR | Owner rule: global rules are installed-or-live (red team, accepted) |
| No auto tier | LOW tier for re-convergence | Could undo a deliberate change; reloads close Cursor bridges |
| Log retention host-specific | Cap the Mac at 1024 MB | Would delete un-archived logs |
| Codex-family stays manual | Edge daemon now | KISS; runbooks already say manual |

## 15. Parity contract

Normalization: JSON canonical form (sorted keys, UTF-8, no insignificant whitespace), sha256. Tags are `HMAC-SHA256(run nonce, value)` cut to 12 hex. The tool prints values only for `print_paths`.

| ID | Field | Source (read-only) | Rule | Allowed host-specific |
|---|---|---|---|---|
| P0 | ops bundle and running paths | sha256 per `bundle.txt` file at the bundle root; path strings in installed LaunchAgents and T3 profiles; sha256 of the installed token helper | Hashes equal the shipped commit on both hosts; every auto-executed path resolves into the bundle root | Watchdog and archiver exist on the Mac only |
| P1 | image ID, revision label, version | `docker inspect`, `X-CPA-Version`, `docker compose config --images` | Running and resolved image equal the manifest per arch | Image ID differs by arch |
| P2 | loaded config fingerprint | `GET /v0/management/config` | Equal across hosts and to `config_fingerprint` | `host_paths`: `server.discovery`, log size cap. `secret_paths` removed |
| P2h | hidden fields | `GET /v8/management/config/<path>` for each `hidden_paths` entry (disk view) | Non-secret values equal; secret ones equal by tag; `host`, `port`, `auth-dir` equal the host's expected value | Per-host expected values in `hosts.json` |
| P3 | pinned runtime values | same as P2 | `request-retry`, `max-retry-credentials`, `max-retry-interval`, transient cooldown, quota cap, save-cooldown, routing tuple equal to `runtime_policy` and `routing_policy` | None |
| P4 | disk hash | sha256 of `config.yaml` | Equal to the last apply receipt on that host | Per host by design |
| P5 | static secret tags | client keys (as a set), each provider key | Tags equal | Management key excluded |
| P6 | accounts | `GET /v8/management/credentials` for provider, label, disabled, priority; `GET /v8/management/credentials/models?name=` for the prefix | Multiset `(provider, prefix, tag, disabled, priority)` equal; exactly one enabled per `(provider, t3-* prefix)` | Exceptions only. Disabled credentials compare without prefix |
| P7 | credential health | credential list | No `error` on one host only | `cooling` differences |
| P8 | catalog | `GET /v1/models` per client key | `id -> owned_by` equal | None |
| P9 | Cursor deny | `GET /v8/management/routing/model-definitions/cursor` | Empty intersection with direct-provider IDs from both hosts | Dated exceptions (for example `glm-5.2`) |
| P10 | plugins | `GET /v8/management/plugins`, library sha256 | Set `(id, version, enabled)` equal; hash equals per-arch pin | Hash differs by arch |
| P11 | runtime shape | `docker inspect`, `docker compose config` | Four mounts, six loopback ports, `restart: always`, `pull_policy: never`, `sysctls`, usage statistics off, Home off; effective container env key names equal (names only; the one value read is `DO_NOT_TRACK=1`); Compose input file hashes equal the activation receipt; resolved Compose hash equals the running container's label | Mount source paths; Tailscale Serve (Will); watchdog and archiver (Mac) |
| P12 | exceptions | `ops/fleet/exceptions.json` | Every difference above matches an entry whose `review_by` is in the future | |
| P13 | host health (not drift) | `/healthz`; bounded TCP connect from inside the container to the upstream hosts | Reported as `ok`, `egress-down` or `unreachable`; a change notifies Andrew with the action to take | |

Results: `equal`, `drift` (non-zero exit), `unreachable` or `egress-down` (both reported separately from drift).

P11 compares effective env key names rather than `.env` key names because Will sets `DO_NOT_TRACK` in `.env` and the Mac sets it in Compose `environment` (R; both Compose files set it, V), so a `.env` comparison would report drift on day one.

## 16. Review trail

| Stage | Model | Role |
|---|---|---|
| Ground reading | Claude Opus 5.5, low effort | Read-only readers: server config, reload, retry, aliases, Home and verifiers (`ground-server.md`); client failover and deploy chain (`ground-client-deploy.md`); live audit of both hosts |
| Independent designs | Claude Fable 5.2 and Claude Opus 5.5 | Each wrote a full design without seeing the other's (`design-fable.md`, `design-opus.md`) |
| Cross-critique | Claude Opus 5.5 on the Fable design; Claude Fable 5.2 on the Opus design | `critique-of-fable.md`, `critique-of-opus.md` |
| Synthesis | Claude Fable 5.2 | First `DESIGN.md` |
| Red team | Claude Opus 5.5 | 58 claims checked, 53 held, 5 wrong; 1 blocking, 5 major, 9 minor findings; verdict "revise before approval" |
| Revision | Claude Fable 5.2 | This document. All 15 findings and all 5 wrong claims are fixed in the text. One was accepted with a corrected citation (the unbounded dial is at `utls_client.go:337-338`, not `sdk/proxyutil/proxy.go:114`), and the red team's list of tree-executed files was narrowed on evidence (the token helper runs from an installed copy, not the tree) |

No reviewer has seen this revision yet. It was written under the task's read-only rules: nothing was changed on either host, no inference request was sent, and no secret, `.env` or auth file was opened.
