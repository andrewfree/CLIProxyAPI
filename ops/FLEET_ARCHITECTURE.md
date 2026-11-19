# How the CLIProxy fleet is set up, and why

This document explains the reasons behind the fleet's shape, as of November 18, 2026. Read it
before you change anything, so that you know which problem each part solves.

It is an explanation, not a procedure:

- [FLEET.md](FLEET.md) is the runbook: commands, release records, rollback paths.
- [FLEET_DESIGN.md](FLEET_DESIGN.md) is the reviewed proposal. Parts of it are built and parts are
  not; its status line says which.
- [README.md](README.md) documents the launcher, the watchdog and the log archiver.

Where this document and FLEET.md disagree, FLEET.md and the live verifiers win. Sentences marked
"(session record)" come from the November 17 and 18 working sessions and are not stated elsewhere
in the repository.

## 1. The goal and the shape

Andrew's goal, in his words: "will and mac should always be the same services and config and
should be more like network fall back for on the go or if will is down." (session record)

The fleet contract in [FLEET.md](FLEET.md#fleet-contract) says the same thing as a rule: Will is
the primary, the M5 Mac is the fallback, and both run the same release, effective config, model
catalog and accounts under the same prefixes. Each host keeps its own OAuth files, cooldown state,
secrets and logs.

```
 CLIENTS                                LAUNCHER (per session)            PROXY HOSTS                 PROVIDERS

 M5 Mac, T3
   claude_cliproxy  (shared pool) ----> claude_account_launcher.mjs --+-> Will  (primary)
                                        pool mode, failover ON        |   Linux amd64, always on
                                                                      |   container on loopback,
                                                                      |   Tailscale Serve TCP 8317   Anthropic
                                                                      |   by name, from the Mac:     (3 Claude accounts
                                                                      |   will.<tailnet>.ts.net       per host)
                                                                      |   by address: M1 only        OpenAI Codex
                                                                      +-> Mac   (fallback)           Kimi
   claude_cliproxy_mac (shared pool) -> launcher, pool mode ------------> Mac                        Z.ai Coding Plan
   3 named Claude profiles -----------> launcher, account prefix -------> Mac                        Cursor plugin
                                                                          Docker Desktop, arm64      NVIDIA
   codex_cliproxy --------------------> codex_fallback_launcher.mjs ---> Will  (tailnet name);
                                        picks a host at session start      Mac if Will fails at start
   codex_cliproxy_mac, codex_kimi_mac - (no launcher, one base_url) ----> Mac   127.0.0.1:8317

 Will, T3
   claude_cliproxy -------------------> launcher, pool mode ------------> Will  (own loopback)

 M1, T3
   codex_cliproxy --------------------- (no launcher) ------------------> Will  (tailnet address)

 claude_cliproxy fails over per request and codex_cliproxy per session; every other profile has
 one fixed host. On the Mac both launchers run from the ops bundle in ~/cliproxy-live/ops.
 The two proxies share no runtime state and neither is in the other's request path.
 CLIProxyBar (the Mac menu-bar quota app, not shown) still reaches Will by address (section 4).
```

Sources: profile list and hosts in [FLEET.md](FLEET.md#running-fleet); per-client routes in
[FLEET_DESIGN.md](FLEET_DESIGN.md) section 6; addresses and platforms in
[fleet-release.json](fleet-release.json).

Three ideas explain most of the picture:

1. **Two equal hosts, no shared state.** A request that may go to either host should get the same
   provider, account and model decision on both ([FLEET_DESIGN.md](FLEET_DESIGN.md) section 1):
   the same account for a named route, the same set of eligible accounts for the pool. Each host's
   selector still places a pooled session by itself, and token freshness and cooldown observations
   differ.
2. **Decisions that affect the prompt cache are pinned to the conversation.** The account, the
   injected date and the host all stay fixed while a conversation runs, unless something fails.
   Bindings and date pins are held in each host's memory, so a failover hands the conversation to
   a host that has neither: with two or more eligible accounts, its first turn there can land on
   another account and pay a cold cache write. That cost is why the launcher does not move a
   conversation back and forth on a flapping link (section 2.6, Recovery).
3. **Failover lives in the client's launcher, for one profile.** There is no new service and no
   daemon in the request path.

## 2. The choices and their reasons

### 2.1 Two hosts, one release, checked for parity

**What it is.** Will (Linux, AMD64, always on) is the primary. The M5 Mac (Docker Desktop, ARM64)
runs the same release as a local fallback at `127.0.0.1:8317`. Both run `v8.0.66-fleet.4`, built
from source `4a1d49c3`. [fleet-release.json](fleet-release.json) pins the image ID for each
architecture, and both Compose files use those IDs with `pull_policy: never`
([docker-compose.yml](../docker-compose.yml),
[instances/will/docker-compose.yml](instances/will/docker-compose.yml)).

**Why two hosts.** Will can be down, and the Mac travels. With Will unreachable, the Mac must
serve by itself, including offline from Will's network. That is why the hosts share nothing at
runtime: [FLEET_DESIGN.md](FLEET_DESIGN.md) section 5 rejects shared cooldown state because it
needs a shared dependency (Home over Redis) or a file sync inside `auths/`, and either one defeats
an offline fallback.

**Why Will is the primary.** It is always on, and M1 and Will's own T3 can only reach Will. The
Mac's proxy stays bound to loopback, so no other machine can use it
([FLEET.md](FLEET.md#running-fleet), design question 16). The Mac must not advertise a service on
foreign networks, which is also why `server.discovery` is Will only
([FLEET_DESIGN.md](FLEET_DESIGN.md) section 2).

**Why Will's container is on loopback behind Tailscale Serve.** The container publishes its six
ports on `127.0.0.1` only. Tailscale Serve forwards TCP 8317 on Will's tailnet address to it, so
the proxy is reachable only inside the tailnet and transport is encrypted by Tailscale
([FLEET.md](FLEET.md#running-fleet)). Will's owner verifier checks the exact listeners and the
Serve target ([instances/will/README.md](instances/will/README.md)).

**Why image IDs and `pull_policy: never`.** A tag can move; an image ID cannot. With immutable
local IDs a restart, a watchdog repair or a reboot cannot silently select a new release
([FLEET.md](FLEET.md#running-fleet)). The running image is chosen by `CLI_PROXY_IMAGE` in each
host's `.env`, which only an activation writes ([FLEET_DESIGN.md](FLEET_DESIGN.md) section 2).

**Why the Mac goes first on every release.** The Mac is the fallback, so a bad release costs only
fallback capacity. `v8.0.66-fleet.2` (November 14) showed this: it passed health checks, failed
inference on the Mac, and was rolled back before Will was touched
([FLEET.md](FLEET.md#running-fleet), [FLEET_DESIGN.md](FLEET_DESIGN.md) section 9). The same
incident is why activation needs a live inference gate and not only `/healthz`
([evidence/fleet4-activation-20261118/README.md](evidence/fleet4-activation-20261118/README.md)).
Since failover went live there is a second reason: while Will is recreated, `claude_cliproxy`
fails over to a Mac that has already passed ([FLEET_DESIGN.md](FLEET_DESIGN.md) section 9).
A host that already passed is never rolled back automatically, because while the other host is
failing it is the only healthy one.

**Why parity is a tool and not a habit.** Before November 18 the hosts had drifted in ways nobody
had chosen: the Mac had `max-retry-interval: 0` and transient cooldown `0` where Will had 75 and
-1, the Mac lacked one GLM alias, and each host had items the other did not
([FLEET_DESIGN.md](FLEET_DESIGN.md) section 2). `ops/fleetctl.py` (PRs #7 and #10) makes drift
visible with four read-only commands ([FLEET.md](FLEET.md#fleet-contract)):

- `check` compares the tracked pins with Will's owner policy, offline.
- `health` probes each host's `/healthz`.
- `fingerprint` checks one host against the pins.
- `parity` compares both hosts: the loaded config, selected disk-only settings, static key and
  account tags, account errors, the catalog for every client key, Cursor overlap and plugins.

Three details of the parity design matter when you change it:

- **It compares what the server loaded, not the file on disk.** A failed reload keeps the old
  config and only logs, so a disk comparison alone proves nothing
  ([FLEET_DESIGN.md](FLEET_DESIGN.md) sections 3 and 14). The loaded view also makes Will's legacy
  YAML and the Mac's v8 YAML comparable.
- **It never prints a secret.** Secret paths are removed, values are printed only for the
  allowlist in [fleet/paths.json](fleet/paths.json), and keys and accounts are compared by keyed
  tags made with a nonce that exists for one run ([FLEET_DESIGN.md](FLEET_DESIGN.md) sections 4
  and 8).
- **Every difference needs a dated exception.** [fleet/exceptions.json](fleet/exceptions.json)
  holds one entry, debug logging, dated November 18 with `review_by` December 2. Two more for
  the `camoff-astra` prefix on the Mac were removed that evening, and the prefix was then removed
  from both hosts because no request used it. Six more were seeded that day, for NVIDIA on the Mac only,
  the quota plugin on Will only and the extra `kimi-ai` credential on Will. They were removed the
  same day once those differences were gone
  ([FLEET.md](FLEET.md#operations-on-november-18-2026), parity cleanup). None is on a Claude row.
  An expired exception fails parity, so a temporary difference cannot become permanent by being
  forgotten.

**What went wrong with parity itself.** After the fleet.4 activation, parity reported a credential
error on Will. It was a Codex account with a `model_not_supported` cooldown for one model, which
made the account summary read unavailable although the selector still served the account for every
other model
([evidence/fleet4-activation-20261118/README.md](evidence/fleet4-activation-20261118/README.md)).
PR #10 treats such a credential as healthy when its only cooldowns are `model_not_supported` and
it still serves at least one unrestricted model, because a check that raises false warnings stops
being read.
One limit remains: the management API omits per-credential alias attributes, so parity cannot
account for those overrides ([FLEET.md](FLEET.md#fleet-contract)).

### 2.2 The launcher layer and account pinning

**What it is.** Every T3 Claude profile that uses CLIProxy runs
`ops/claude_account_launcher.mjs` in place of the Claude binary, through the profile's
`config.binaryPath` (five profiles on the Mac, one on Will; [FLEET.md](FLEET.md), prechecks 0(f)).
The launcher starts a loopback listener for that one Claude process, forwards its requests to
CLIProxy, and exits with the process ([README.md](README.md#choosing-a-claude-account-in-t3-code)).
It has two modes:

- **Named account** (`CLIPROXY_CLAUDE_ROUTE=account`, the default). The launcher adds the
  account's prefix (`t3-gmail`, `t3-icloud`, and so on) to the model ID, so CLIProxy can use only
  that account.
- **Shared pool** (`shared-pool`). The launcher forwards unprefixed model IDs, and CLIProxy picks
  any eligible account.

**Why a per-session launcher and not a config switch.** CLIProxy pins an account by model prefix
(`t3-gmail/claude-...`), but Claude Code and T3 work with ordinary model IDs. The launcher adds
the prefix to each request and strips it from the model list, so the session sees ordinary names
while CLIProxy sees a pinned route ([README.md](README.md#choosing-a-claude-account-in-t3-code)).
It needs no long-running service or fixed port, and each session carries its own choice in its
launch environment. A running session keeps that environment, which is why every profile change
applies to new sessions only.

**Why the launcher never picks another account.** A named route exists because a specific
subscription must pay for the work. The rule is stated in [FLEET.md](FLEET.md#running-fleet):
changing a named account route must never select another account implicitly. So a named profile
returns its account's cooldown to the chat, and choosing a different account stays a manual
action. In pool mode the launcher rejects account-qualified models, and it rejects a prefix
combined with pool mode, so the two modes cannot blur.

**Why the launcher accepts only three routes.** It accepts `POST /v1/messages`,
`POST /v1/messages/count_tokens` and `GET /v1/models`, the requests on which it can enforce the
account route, and rejects everything else before it reaches the pool (commit `b76e5003`, October
1; [README.md](README.md#choosing-a-claude-account-in-t3-code)). The same small surface later kept
the failover replay rules in section 2.6 complete: every request the launcher can see is either a
message or one of two read-only calls.

**Why CLIProxy, not the launcher, retries across accounts.** The proxy knows quota, cooldowns and
eligibility; the launcher knows none of that. The launcher therefore does not retry requests. It
switches hosts only, and only for the one profile that sets a fallback.

### 2.3 Session affinity and the prompt cache

**What went wrong.** The routing strategy is `quota-aware`, which spreads load by remaining quota.
Before November 18 it chose an account for every request, so consecutive turns of one conversation
moved between accounts whenever two or more were eligible. Anthropic's prompt cache belongs to the
account, so each move paid for a cold cache write of the whole conversation prefix. Mac request
logs showed Claude pool switches on 20% of consecutive turns on November 12 and 38% on November
13; switched turns wrote 3.4 times more cache, adding about 11% and 24% to those days' input cost
at API-price equivalents. Codex pool switches reached 66% of turns on November 14
([FLEET.md](FLEET.md#running-fleet), [cache-affinity-validation.md](cache-affinity-validation.md)).

**The fix.** `routing.session-affinity: true` on both hosts (PR #4). Affinity wraps the
quota-aware selector: a new conversation is still placed by quota, and then it stays on that
account. The binding key is `X-Claude-Code-Session-Id` plus the agent ID for Claude Code, or the
Codex `Session-Id` ([cache-affinity-validation.md](cache-affinity-validation.md)). Balancing
therefore happens per session, not per request. The Mac switched at 00:15:41 and Will at 01:59:46
Pacific on November 18
([evidence/cache-affinity-20261118/README.md](evidence/cache-affinity-20261118/README.md)).

**Why the other four routing fields have their values** ([FLEET.md](FLEET.md#running-fleet)):

- `session-affinity-subagents: false`. Each Claude Code subagent gets its own binding from the
  quota-aware selector instead of inheriting its parent's account, so subagent load is still
  balanced by quota.
- `session-affinity-ttl: 1h`. A binding idles out after an hour; every hit refreshes it.
- `session-affinity-rebind-on: any`. When a bound account becomes unavailable, `any` moves the
  binding to the next account chosen; `quota` moves it only for a quota or auth block and
  otherwise serves that request elsewhere without rebinding
  (`shouldPreserveBindingWhenUnavailable` in `sdk/cliproxy/auth/selector.go`). The reviewers split
  between `any` and `quota`. `any` was kept because with the transient cooldown disabled both
  modes cost one cold write after a transient error, and `any` stays stable if that cooldown is
  ever made non-negative.
- `strategy: quota-aware`. Unchanged. Named routes, authoritative cooldowns and model eligibility
  still override affinity; affinity only chooses among accounts the route already allows.

**Why an earlier trial said no.** On November 2 a trial with 120 synthetic Luna requests found no
reproducible cache benefit, and affinity was turned off. That trial measured the OpenAI pool, not
Claude traffic or native T3 traffic, where most pooled requests go. The November 18 decision came
from 47,887 real `/v1/messages` logs and 8,765 `/v1/responses` logs
([cache-affinity-validation.md](cache-affinity-validation.md)). The lesson for future changes: a
synthetic result on one provider does not transfer to another provider's cache.

**How to measure it.** `logs/main.log` truncates session IDs to eight characters, so per-stream
switch counts cannot come from those lines. Use the decision mix: hits and new bindings against
reselections and failovers without rebinding, per credential. Use request logs for stream-level
continuity. The first hour on the Mac had 101 decisions: 5 new bindings, 96 hits, no reselection.
Only one Claude account was eligible then, so this shows continuity, not balancing
([evidence/cache-affinity-20261118/README.md](evidence/cache-affinity-20261118/README.md)).

**What it costs.** Every change to any of the five routing fields replaces the selector and drops
every binding, which costs one cold cache write per active conversation
([FLEET.md](FLEET.md#running-fleet)). Affinity can also concentrate requests that have no session
ID when their opening text matches. A post-change measurement on a day with two or more eligible
accounts has not been run.

### 2.4 The date pin and the timezone

**What went wrong.** CLIProxy cloaks some Claude requests so that they look like Claude Code, and
the cloak injects a "Today's date" line. T3's `t3-code` entrypoint is cloaked this way (session
record). Two faults followed:

1. The injected date could change in the middle of a conversation. It flipped when a
   conversation's first assistant reply appeared
   ([evidence/fleet4-activation-20261118/README.md](evidence/fleet4-activation-20261118/README.md)).
   A changed date rewrites text near the start of the prompt, so everything cached after it is
   invalid: the same cost as an account switch, with affinity unable to help.
2. The containers run in Asia/Shanghai, so the injected date was a day ahead from 08:00 to 24:00
   Pacific ([FLEET.md](FLEET.md#operations-on-november-18-2026)).

**The fixes.**

- **Release `v8.0.66-fleet.4`** (PR #5, source `4a1d49c3`). The date pin is keyed to a hash of the
  client's `X-Claude-Code-Session-Id` and shared by that session's subagents. A subagent or fork
  therefore sends the parent's date, so its prefix bytes match the parent's. It reads the parent's
  cache only on the parent's account. This is the release's only runtime change from
  `v8.0.66-fleet.3`.
- **`claude-header-defaults.timezone: America/Los_Angeles`** on both hosts. This is config only,
  not part of the release manifest; `fleetctl parity` compares it, and
  [fleet/paths.json](fleet/paths.json) lists it as a printable value.

**Evidence.** The fleet.4 inference gate sent four Claude Code-shaped requests in one session
through each host: two pool turns, a named `t3-gmail` turn and a subagent turn. Every request
carried the same injected date (2026-11-18, the Los Angeles date), and the second turn read 37,574
cached tokens. Each host's first fleet.4 turn read exactly the 37,545 tokens that the Mac's
`v8.0.66-fleet.3` baseline had written, so the cached prefix survived the release switch
([evidence/fleet4-activation-20261118/README.md](evidence/fleet4-activation-20261118/README.md)).

**Why the two pins differ on subagents.** The date pin is shared so that a subagent's prefix can
match the parent's byte for byte. The account is chosen per subagent (section 2.3) so that
subagent load is balanced by quota. The cost: on a day with two or more eligible accounts, a
subagent placed on another account writes its own cache. That cost has not been measured.

### 2.5 Retry policy, cooldown policy and the no-retry header

Three layers can retry a Claude request: CLIProxy (across accounts and rounds), Claude Code (its
own HTTP retries) and the user. The settings are chosen so the layers cover different waits and do
not fight.

**CLIProxy's values** (all pinned in [fleet-release.json](fleet-release.json) under
`runtime_policy`; PR #4 gave the Mac the first two and pinned the retry counts, and the quota cap
dates from `v8.0.27-fleet.3` on October 15):

| Setting | Value | Why |
| --- | --- | --- |
| `max-retry-interval` | 75 | A request waits for a cooldown that ends within 75 seconds, in place of an immediate 429. The limit applies to each round, so with `request-retry: 3` a request can wait about 225 seconds in total ([FLEET.md](FLEET.md#running-fleet)). |
| `transient-error-cooldown-seconds` | -1 | No cooldown after 408, 5xx and transient-capacity 429 responses. Otherwise one upstream error benches an account and rebinds every session on it, which costs each of them a cold cache write. Quota 429s still cool down and rebind. |
| `quota-cooldown-max-seconds` | 3600 | A capped account becomes eligible to test recovery after at most an hour; another upstream 429 blocks it again ([FLEET.md](FLEET.md#running-fleet)). |
| `request-retry` / `max-retry-credentials` | 3 / 0 | Unchanged; pinned so both hosts agree. |

**What went wrong before.** The Mac had `max-retry-interval: 0`, so it returned cooldown 429s at
once, and Claude Code looped on them: the audit counted 614 final 429s on one named route
([FLEET_DESIGN.md](FLEET_DESIGN.md) section 5). Will already had 75 and -1, so the fix was to give
the Mac Will's values: no change on the primary.

**The cost of -1.** During an upstream 5xx incident no account is benched, so each request retries
every account, and a failing request can make up to four immediate rounds: up to 12 attempts
across three accounts ([FLEET.md](FLEET.md#running-fleet)). The trade was accepted because, with
three accounts and often only one usable, benching the only working account for a minute costs
more ([FLEET_DESIGN.md](FLEET_DESIGN.md) section 5).

**Why long `Retry-After` values are hidden.** The fleet omits `Retry-After` when the wait exceeds
one minute, while keeping HTTP 429 and the full reset time in the JSON body. This stops Codex CLI
from sleeping for days on that header ([FLEET.md](FLEET.md#running-fleet), the October 15
`v8.0.27-fleet.2` and `v8.0.27-fleet.3` records).

**Why the launcher adds `x-should-retry: false`** (PR #6). With no `Retry-After`, Claude Code kept
retrying a local cooldown 429 for minutes although the account would not recover for much longer.
The launcher now adds `x-should-retry: false` when a 429 has no `Retry-After` and its JSON body
(at most 64 KiB) reports a cooldown reset more than 180 seconds away
([README.md](README.md#choosing-a-claude-account-in-t3-code)).

**Why 180 seconds.** Claude Code's own retries span about 160 seconds, and CLIProxy waits up to 75
seconds per round for a cooldown that ends within that time, so a cooldown of up to three minutes
can still be absorbed without the user noticing. Only a longer one should end the turn (comment at
the top of `ops/claude_account_launcher.mjs`). The boundary started at 60 seconds and was raised
to 180 so that cooldowns Claude Code's own retries can absorb still get retried (commit
`684a7f69`).

**What failover does not do.** A 429 means the account's quota or the provider's capacity is
exhausted. Both hosts use the same accounts and the same providers, so the other host would answer
the same way, and failover never relieves a 429 ([FLEET_DESIGN.md](FLEET_DESIGN.md) section 5).

### 2.6 Client failover

Failover has been live since shortly after 12:53 Pacific on November 18, when `claude_cliproxy`
got its fallback (fleet design unit 3, PR #14;
[FLEET.md](FLEET.md#operations-on-november-18-2026)). PR #13, the SYN-retry bound, has been live
on both hosts since their containers were recreated at about 15:19 Pacific the same day
(section 2.7). The exact behaviour is
listed in [README.md](README.md#host-failover-for-the-shared-pool); this section gives the reasons.

#### Why the launcher, and why one profile (option E)

[FLEET_DESIGN.md](FLEET_DESIGN.md) section 6 compared five options:

| Option | Why not |
| --- | --- |
| A. The earlier attempt `3d88c013` as it was | No connect timeout, no reset handling, no half-open handling. |
| B. One Mac edge daemon for all clients | A single point of failure in every request path, the opposite of the goal. |
| C. Tailscale-level failover (a shared address) | Fails when Tailscale is stopped, exposes a laptop that sleeps, and has no layer-7 health. |
| D. The Mac server proxies to Will | Needs a Go change and adds a second hop to every request. |
| **E. Harden the launcher, for one profile** | **Chosen.** No new service, no Go change, and the launcher already sits in the request path of each session. |

**For Claude, only `claude_cliproxy` on the Mac fails over.** The launcher refuses `CLIPROXY_FALLBACK_URL` on
a named account route, because a pinned account must never move to another host. Named routes
never cross hosts, so failover cannot reach an account mismatch even if the hosts drift
([FLEET_DESIGN.md](FLEET_DESIGN.md) sections 6 and 14). The `_mac` profiles are already local and
need no fallback.

**The scope guard.** The same launcher file serves all five Mac profiles and Will's T3. Without
`CLIPROXY_FALLBACK_URL` the request path is the old code: the default connection agent, no timers,
no probes ([README.md](README.md#host-failover-for-the-shared-pool)). A change to failover must
keep this true, or it changes behaviour for every profile that never asked for it.

**Why T3 cannot do this.** T3 marks a profile ineligible only for static reasons: the instance is
disabled, no adapter is registered, the driver failed to start, or the CLI failed to run. Will's
health is not an input. With Will down, `claude_cliproxy` stays selectable, so only the launcher
can fail over ([FLEET.md](FLEET.md#operations-on-november-18-2026), prechecks 0(f)).

**The precondition.** Failover is only correct if both hosts would answer the same way. Before
activation the client key was accepted on both hosts and `fleetctl parity` reported `equal` with
no exception on Claude rows ([FLEET.md](FLEET.md#operations-on-november-18-2026);
[FLEET_DESIGN.md](FLEET_DESIGN.md) section 6 item 9, where no exception is accepted).

#### Why the primary is Will's tailnet name

The profile reaches Will at `http://will.<tailnet>.ts.net:8317`, not at the raw tailnet address.
Public resolvers answer NXDOMAIN for that name. With Tailscale stopped the lookup should therefore
fail at once, and the launcher would fail over. This was not tested: Tailscale was not stopped,
and the check was a resolver comparison. With the raw address, a foreign network that reuses the
100.x range could receive the traffic in clear
([FLEET.md](FLEET.md#running-fleet), design question 3). Since the November 18 parity cleanup the
Mac's `codex_cliproxy` uses the name too, for the same reason: its
`model_providers.cliproxy.base_url` is `http://will.<tailnet>.ts.net:8317/v1`. With the Codex
launcher enabled since November 18, a failed startup lookup sends a new Codex session to the Mac
(section 4). M1 and the Will entry in CLIProxyBar (the Mac menu-bar quota app) still use the raw
address.

The name does not help on a network whose resolver answers every name, as some captive portals do:
a session already running would send requests, with the client key, in clear to that address, and
the launcher would fail over only after a failed connect or three failed liveness probes, because
it delivers any HTTP answer from the primary as is. This case was not tested.

#### Why health means `/healthz` returning 200 with `status: ok`

A TCP accept proves nothing. Tailscale Serve may accept and then close when its backend is down,
which the design lists as an untested assumption ([FLEET_DESIGN.md](FLEET_DESIGN.md) section 13),
and a captive portal accepts everything ([FLEET_DESIGN.md](FLEET_DESIGN.md) section 6 item 3).
So the probe requires `GET /healthz` to return 200 with `{"status":"ok"}` within 3 seconds. It is
always a probe, never a client request, so it spends no quota.

#### Why each failure case is handled the way it is

The governing question is always: could the primary already be generating this message? Replaying
a message that is already running spends capped weekly quota twice.

| Situation | Action | Reason |
| --- | --- | --- |
| Refused, name does not resolve, or no connection within 3 s | Replay any request on the fallback, and take the primary out of service for new requests until it recovers | Nothing reached the primary, so a replay is always safe. The 3 s guard covers only the lookup and connect. After a connection exists, no timer ends a request; only the liveness watch can, and only when the host also fails its health probes. The root `AGENTS.md` timeout rule governs the Go server, not this Node launcher ([ops/AGENTS.md](AGENTS.md) says the root file covers the Go server); the launcher keeps the rule's intent for client requests: after a connection exists, only failed health probes can end one. The 3 s health-probe bound covers the whole probe exchange, after connect too, but a probe is never a client request. |
| 5xx whose body carries a dial error | Replay on the fallback, then hold new requests on the fallback | CLIProxy's last attempt could not connect to its upstream. Will's `/healthz` stays green when only its egress is down, so a probe cannot detect or clear this state. |
| Connection closed with zero response bytes | Probe once; a failed probe takes the primary out of service. `count_tokens` and the model list replay. A message gets a retryable 502. | The two read-only calls are safe to repeat. A message may already be generating, so the launcher does not replay it. Claude Code's retry takes the fallback if the probe failed. |
| Stream breaks after it started | Destroy the client response, no replay | Part of the answer was delivered and paid for; a replay would generate it again. |
| 401, 403, 429, 529, other 5xx | Pass through unchanged | A non-dial 5xx or a 529 means CLIProxy reached its upstream or failed after connecting, so the message may already have run. The fallback reaches the same providers with the same keys and accounts, so it would usually answer the same way. |

A failed health probe at launcher startup also takes the primary out of service, so a session
started while Will is down begins on the fallback.

**The egress hold.** After a dial-error replay, new requests stay on the fallback for 120 seconds.
A repeat after the hold ends doubles the next one, up to 600 seconds. A 2xx or 3xx from the
primary returns the next hold to 120 seconds without ending one that is running. The hold is
time-based because health cannot see egress: without it every new request would go to Will, wait
for Will's dial error, and only then move.

**The liveness watch.** While requests are open, the launcher probes their hosts every 20 seconds.
Three failed probes of the primary in a row take it out of service for new requests. An open
request is ended only after three failed probes of its host during which it received no bytes. A
stream that is still delivering is never cut. The reason is specific: during a Tailscale flap new
connections through Serve can fail while an established one still works, and cutting a working
stream wastes the same quota the no-replay rule protects
([FLEET_DESIGN.md](FLEET_DESIGN.md) sections 6 and 14). A slow model on a healthy host is not cut
either, because its host's probes pass.

**Recovery.** The primary returns after two passing probes 15 seconds apart, and no sooner than 30
seconds after it left. Two probes and a floor keep a flapping link from bouncing sessions between
hosts.

**One failover per request.** A request moves at most once. A request that starts on the fallback
while the primary is held or out of service tries the primary once if the fallback cannot be
reached. A request that has already moved gets a 502, so a double outage cannot make a request
ping-pong.

**Observability.** Every response from a CLIProxy host through a launcher that has a fallback
carries `X-CLIProxy-Host: primary` or `fallback`. The launcher's own 502s after a failed attempt
carry it too, naming the host last tried. The launcher's own 400, 404 and 415 rejections do not,
and other profiles' launchers send no such header. Each host transition is one JSON line in
`ops/runtime/fleet/failover.jsonl`, with no bodies, URLs or keys.

#### What the reviews changed

PR #14 went through four review rounds by Claude Fable 5.2 and Claude Opus 5.5
([FLEET.md](FLEET.md#operations-on-november-18-2026)). The findings below changed the code
(session record for the findings; the fixes are visible in `ops/claude_account_launcher.mjs` and
`ops/claude_account_failover.test.mjs`). Each one is a property to keep:

- **Concurrent egress errors compounded the hold.** Several requests failing in the same outage
  each doubled it, so one outage produced 120, 240 and 480 seconds at once. Now requests that hit
  the same outage share one hold.
- **"network is unreachable" also matched read errors.** That text appears in read and write
  errors on connections that were open, which means an earlier connection to the provider existed
  and the message may have run. The matcher now accepts only dial errors.
- **Stale probe-failure counts survived recovery.** A failure run from before recovery could count
  toward cutting a request afterwards. Recovery now breaks every failure run on the host.
- **First-tick timer jitter.** A timer that fired slightly early could delay recovery or a cut by
  a whole interval. Elapsed time is now counted in ticks.
- **`Date.now` replaced by a monotonic clock.** A wall-clock step can no longer shift a hold, a
  recovery or a liveness window.

**What the earlier attempt would have missed.** Commit `3d88c013` matched `dial tcp` only when a
colon followed it directly. That catches `dial tcp: lookup ...` but not the refused, `i/o timeout`
and dropped-SYN (`connect: connection timed out`) bodies the pinned image returned in the unit
0(b) staging container, such as `dial tcp <address>: connect: connection refused`. Its
`dial upstream:` alternative did catch the first-party dial path. Those captured bodies are the
fixtures at the top of `ops/claude_account_failover.test.mjs`. The lesson: classify upstream
errors from captured responses, not from a guess at their format.

#### The accepted residual

A dial-error 5xx proves only that CLIProxy's last retry attempt did not connect. If Will's egress
fails in the middle of a request, an earlier attempt in the same retry round can have reached the
provider, and the replay then runs the message a second time. This was accepted because Claude
Code would usually resend the message after that 5xx anyway; the replay sends that resend to the
host that can serve it. It adds a run only when Claude Code had no retries left
([README.md](README.md#host-failover-for-the-shared-pool)).

#### The drill

A throwaway launcher with the Mac as fallback answered `count_tokens` from the fallback for a
closed port, an unresolvable name and a rejected TEST-NET address, each in under 0.5 seconds, and
for two blackholed addresses in 3.4 seconds, which is the probe bound. It answered from the
primary for Will by name. Tailscale and Will were not touched
([FLEET.md](FLEET.md#operations-on-november-18-2026)).

`node ops/claude_failover_drill.mjs --primary <origin> [--primary <origin>]...` repeats it. It
needs at least one `--primary` and exits 2 without one. Exit 0 means only that every run returned
200; the `host` field of each line says which side answered
([README.md](README.md#host-failover-for-the-shared-pool)).

`node ops/set_pool_fallback.mjs` turns failover on or off for the profile. The key piped to it
must be the client key the profile already uses. Before it opens a T3 session, it sends
`GET /v1/models` with that key to the primary and, when one is given, the fallback, and it stops
unless each returns HTTP 200; `--no-key-check` skips that check.

The drill covered only pre-connect failures on a replayable call. The dial-error 5xx and egress
hold, the zero-byte reset and the liveness watch were not exercised live; they rest on the node
tests in `ops/claude_account_failover.test.mjs`.

### 2.7 The SYN-retry bound

**What went wrong.** Failover on "Will up, egress down" starts only when Will returns its
dial-error 5xx. Nothing bounded how long that took. The Claude dial path has no connect timeout.
The root `AGENTS.md` governs the Go server and allows timeouts there only during credential
acquisition, so a Go dial timeout would need Andrew's ruling and was not proposed
([FLEET_DESIGN.md](FLEET_DESIGN.md) section 6 item 10). The fix below, like the launcher's
connect guard, bounds only the time before a connection exists. A refused connection or
an unresolvable name fails in milliseconds, but a silently dropped SYN made one dial wait about
133 to 135 seconds (Mac 132.7 s, Will 135.1 s; [FLEET.md](FLEET.md#operations-on-november-18-2026),
prechecks 0(b)).

**The fix.** Both Compose files set `net.ipv4.tcp_syn_retries: 2` (PR #13), which makes a dial
fail in about 7 seconds. It is a kernel setting scoped to the container's network namespace, it
acts only before a connection exists, and it needs no Go change. It applies to every TCP connect
the container makes, including OAuth refreshes and every provider, so on a lossy network a connect
that needed a third SYN retransmission now fails. For a request, CLIProxy retries a failed dial on
the next credential or round, without a cooldown ([FLEET_DESIGN.md](FLEET_DESIGN.md) section 5).
The repository does not record how other connects, such as OAuth refreshes, retry. At 3 a dial
takes about 20 seconds ([FLEET.md](FLEET.md#operations-on-november-18-2026), prechecks 0(b)),
which at up to 12 dials per request (next paragraph) would be about four minutes. The repository
records measurements at 2 and 3 only, and no reason for not going lower.

**What it does not fix.** Dial errors are retried without cooldown, so one request can dial once
per account per round. With three accounts and `request-retry: 3` the worst case at 2 is still
about 86 seconds, above the design's 30-second target. The next remedies in the design (a launcher
health hint, then a dial bound in the fork that would need Andrew's ruling) are not built.

**When it went live.** A sysctl is applied when a container is created, so the merged setting
waited for a recreate. Both containers were recreated on unchanged images on November 18, the Mac
at 23:18:46Z and Will at 23:19:06Z, and each reads `net.ipv4.tcp_syn_retries` back as 2. Will's
verifier reported `container_sysctls_drift` until then and now reports ok
([FLEET.md](FLEET.md#operations-on-november-18-2026), parity cleanup).

**Why there is no pre-deploy idle gate.** A recreate drops every stream on the host, so the design
proposed deploying only when traffic was quiet for two 15-second windows. Measured across 80
windows per host, a floor of 1 KiB per window would have blocked all 79 deploy checks on each
host. A gate that always blocks gets overridden by habit, so it is not used; the design needs a
different approach ([FLEET.md](FLEET.md#operations-on-november-18-2026), prechecks 0(c)).

### 2.8 Where files live

| Location | What it is | Consequence |
| --- | --- | --- |
| `/Users/rever/cliproxy` on the Mac | Instance root for Compose, local config, logs, and private state. Since the November 18 cutover the installed jobs and new T3 sessions use the bundle, not its checkout paths. | A pull, checkout, or uncommitted edit no longer changes a job or new session. Compose inputs here are still live. |
| `~/cliproxy-live/ops` on the Mac | Unit 2b's separate bundle root, live since November 18. It supplies the watchdog and archiver LaunchAgents, the five T3 Claude launcher paths, the Codex fallback launcher for `codex_cliproxy` and the monitor LaunchAgent. | `fleetctl ship` publishes the allowlist from a recorded commit. The jobs use `CLIPROXY_ROOT` to find the instance. |
| `~/cliproxy` on Will | The instance and a hand-copied ops subset. Unit 2c will ship into its existing `ops/` directory. | The ship leaves private runtime state and unmanaged files where they are. |
| `ops/runtime/` on each host | Private state, ignored by git: backups, receipts, bounded logs, the failover log. | It can contain secrets. Never commit it. Check a receipt before copying it into `ops/evidence/`. |
| `ops/evidence/` | Sanitized, tracked records and as-run scripts of activations and measurements. | Maintained live tools and their tests live in `ops/`. |
| `config.yaml`, `.env`, `auths/` | Per-host secrets. | Keep them on the instance that owns them. |
| `/Volumes/Innie/backups/llm-logs/cliproxy/m5/live/` | The Mac's log archive since November 18. | See below. |

The checkout used to provide both the ops code and its working data. A checkout
edit could reach launchd within 120 or 300 seconds, or T3 on the next session.
Unit 2b moves the code into a shipped bundle. A ship reads exact Git blobs and
the allowlist from the chosen commit, records hashes and modes, and switches
the managed paths and manifest together by renaming a generation pointer.
Separate generations let the Will bundle share an `ops/` directory with private
state. The previous managed files have a dated backup beside the bundle root.

The Mac's Compose files still live in the instance root. Before a bundled
watchdog repair can run `compose up`, it specifies that root, the Compose
files, and `.env`, then checks the hashes and resolved image against the
activation receipt. A mismatch falls back to starting the existing container
by name. An expiring pause lets an approved activation temporarily suppress
all repairs. See the [cutover and rollback](FLEET.md#mac-ops-bundle-cutover).
Andrew approved the cutover and it was verified on November 18. The live-change
rule in [FLEET.md](FLEET.md#fleet-contract) now gates ships and reinstalls.
Sessions that predate the repoint retain their original launcher until they end.

**Why config is edited in place.** `config.yaml` is bind-mounted as a single file. Replacing or
renaming it breaks the mount's link to the inode, so every config change is one in-place write
that preserves the inode and mode 600, followed by a check of the loaded values
([GLM_ROUTING.md](GLM_ROUTING.md), [evidence/cache-affinity-20261118/README.md](evidence/cache-affinity-20261118/README.md)).
Never restore an old full config over later edits; remove only the lines you added.

**Why OAuth files are never synchronized.** Claude refresh tokens rotate. Two servers sharing one
token file would invalidate each other, so each host logs in separately to the same accounts
([FLEET.md](FLEET.md#running-fleet)). For the same reason, do not restart CLIProxy during a token
refresh ([README.md](README.md#oauth-health-and-reauthentication)).

**Why logs archive to Innie.** The Mac sets `logs-max-total-size-mb: 0`, because CLIProxy's
cleaner deletes without verifying an archive first. A separate job copies closed logs to an
archive drive, verifies them by SHA-256 and only then prunes. Until November 18 the archive was on
TheCloud. That NAS share filled up, so nothing was archived or pruned and the Mac data volume fell
to about 6 GiB free. Andrew chose Innie, a local drive with about 600 GiB free; the T9 drive was
skipped because it had just thrown a disk error ([README.md](README.md#log-retention)). Will uses
a 1,024 MB cap with no archive, which is why the log cap is a host-specific path and not drift.

### 2.9 Keys and secrets

**What went wrong.** During the November reviews, review agents printed CLIProxy client keys
([FLEET.md](FLEET.md#operations-on-november-18-2026)) and provider keys (session record) into
their own transcripts.

**The response, all on November 18:**

- Two new client keys were added at the top of both hosts' key lists and every client moved to
  them. The two old keys stay accepted until a scheduled task removes them on 2026-11-19 at 10:30
  UTC. The repository records no reason for this overlap. Its effect is that sessions already
  running keep working, because a running session keeps the key it launched with.
- The Z.ai Coding Plan key was replaced on both hosts and the old one deleted.
- The NVIDIA key (then Mac only) was rotated and the old one deleted. The new key expires on
  2027-11-18. Later that day the NVIDIA provider was added to Will as well.

FLEET.md records no rotation of OAuth credentials, the management key or other provider keys.

**The lesson.** Give a reviewer a pre-masked config dump, never the raw `config.yaml`, `.env` or
`auths/` (session record). The tooling follows the same rule: `fleetctl` removes secret paths,
prints only allowlisted values and compares keys by per-run tags (section 2.1), the watchdog's
auth report prints account emails and token metadata such as expiry and refresh results but no
token values ([README.md](README.md#oauth-health-and-reauthentication)), and the failover log
holds no bodies, URLs or keys.

**Where secrets may live.** In each host's `config.yaml`, `.env` and `auths/`; in T3's secret
store for profile keys; in client key files (Will's `client.key`, the Codex token helper's key,
M1's Codex and OpenCode key files); in `~/.config/nvidia/api.env` and CodexBar on the Mac; and in
mode-0600 files under `ops/runtime/` ([FLEET.md](FLEET.md#operations-on-november-18-2026)).
Nothing tracked by git holds one.

### 2.10 Health and quota monitoring

Once Andrew activates the LaunchAgent, the Mac runs it from the shipped
bundle. Each five-minute tick reads both instances' loopback `/healthz` and checks provider egress
with bounded TCP connects from inside their containers. The sockets send no
TLS, HTTP or credentials. A healthy proxy with a failed upstream dial is
`egress-down`. A failed proxy health check is `unreachable`. An incomplete
egress check is `unknown`. Will's checks use noninteractive SSH and run
against its local proxy and container. Hourly parity retains its own result,
even when the accompanying health report shows an outage. This schedule
makes no inference requests and does not change routing or configuration.

The monitor also collects read-only quota data through management API
credential substitution. It reads Codex, Kimi and Z.ai every tick and
Claude by elapsed time, about every 15 minutes, so the first tick after the
Mac wakes reads it. It uses the same Claude
usage endpoint as the reset tool and records scoped caps and reset-grant
counts. For Z.ai compatibility keys,
only a known HTTPS provider origin authorizes a quota request. The Mac is
preferred for shared accounts. Will supplies accounts the Mac did not poll,
as well as Will's independent cooldowns. Shared accounts are matched with
stable private account tags. Where OAuth account metadata is missing, the
credential index is the identity fallback. The monitor keeps its polling
deadlines in `monitor-state.json`, independently of the reset tool. A 429
backs off only the affected account for at least 20 minutes. An uncertain
Will probe extends only Will's guard. Each account gets at most one usage
request per tick.

`health.json`, `latest.json` and the monthly `quota-history-YYYY-MM.jsonl`
files live in the Mac's private runtime directory. The history uses route
labels when available and pseudonyms otherwise. It records percentages,
timestamps and cap states without raw account identities or upstream bodies.
Health and parity alerts open or close after two consecutive matching
samples. An unknown result breaks the streak. Quota alerts fire on
observed transitions and remember active caps across deferred ticks. The
daily summary includes outages observed between summaries. The Text Me
service sends to its configured recipient. The
[runbook](FLEET.md#health-and-quota-monitor) covers activation, pause, and
private charts generated from the JSONL history.

## 3. Problem, fix, location, evidence

| Problem | Fix | Where it lives | Evidence |
| --- | --- | --- | --- |
| A conversation moved between accounts on each request and paid a cold cache write each time | `routing.session-affinity: true` on both hosts | Host `config.yaml`; pinned in `fleet-release.json` `routing_policy` | PR #4; [evidence/cache-affinity-20261118/](evidence/cache-affinity-20261118/README.md); [cache-affinity-validation.md](cache-affinity-validation.md) |
| The injected date changed mid-conversation and rewrote cached history | Date pin keyed to a hash of `X-Claude-Code-Session-Id`, shared by subagents | Release `v8.0.66-fleet.4`, source `4a1d49c3` | PR #5; [evidence/fleet4-activation-20261118/](evidence/fleet4-activation-20261118/README.md) |
| The injected date was a day ahead from 08:00 to 24:00 Pacific | `claude-header-defaults.timezone: America/Los_Angeles` | Host `config.yaml` on both hosts | [FLEET.md](FLEET.md#operations-on-november-18-2026) |
| Claude Code retried local cooldown 429s for minutes | `x-should-retry: false` when the reset is more than 180 s away and there is no `Retry-After` | `ops/claude_account_launcher.mjs` | PR #6; [README.md](README.md#choosing-a-claude-account-in-t3-code) |
| The Mac returned cooldown 429s at once; with affinity on, a transient cooldown would rebind every session on the benched account | `max-retry-interval: 75`, `transient-error-cooldown-seconds: -1` on both hosts | Host `config.yaml`; `fleet-release.json` `runtime_policy` | PR #4; [FLEET_DESIGN.md](FLEET_DESIGN.md) section 5 |
| The hosts drifted without anyone deciding it | `fleetctl check`, `parity`, `fingerprint`, `health`; dated exceptions | `ops/fleetctl.py`, `ops/fleet/` | PRs #7 and #10; [FLEET.md](FLEET.md#fleet-contract) |
| Parity raised a false credential warning | A credential whose only cooldowns are `model_not_supported` counts as healthy while it still serves an unrestricted model | `ops/fleetctl.py` | PR #10; [evidence/fleet4-activation-20261118/](evidence/fleet4-activation-20261118/README.md) |
| With Will unreachable, the pooled Claude profile failed | Launcher failover from Will to the Mac for `claude_cliproxy` | `ops/claude_account_launcher.mjs`; T3 profile environment | PR #14; [README.md](README.md#host-failover-for-the-shared-pool) |
| With Tailscale stopped, traffic could go to whatever holds 100.x | The Mac's `claude_cliproxy` and `codex_cliproxy` reach Will by its tailnet name; M1 and CLIProxyBar still use the address (section 4) | T3 profile `CLIPROXY_BASE_URL`; Codex `model_providers.cliproxy.base_url` | [FLEET.md](FLEET.md#running-fleet) |
| A dropped SYN made one Claude dial wait about 133 to 135 s | `net.ipv4.tcp_syn_retries: 2`, live since the November 18 recreate | Both Compose files; Will's policy and verifier | PR #13; [FLEET.md](FLEET.md#operations-on-november-18-2026) |
| Z.ai answered `glm-5.2` with GLM-5.3, and Will's Cursor `glm-5.2` entry returned 503 `auth_not_found` | Client ID `glm-5.2` maps to upstream `glm-5.3` on `zai-coding-plan` | Host `config.yaml` on both hosts | [GLM_ROUTING.md](GLM_ROUTING.md#glm-52-maps-to-glm-53) |
| Only the Mac served the NVIDIA models | NVIDIA provider and its three aliases added to Will | Will `config.yaml` | [FLEET.md](FLEET.md#operations-on-november-18-2026) |
| Will ran an unpinned quota plugin for account types neither host has | `cliproxybar-quota` disabled, its config removed, its library moved to a private backup; the monitor collects quota history after activation | Will `config.yaml`; Will `ops/runtime/` | [FLEET.md](FLEET.md#operations-on-november-18-2026) |
| Will's extra `kimi-ai` login looked like extra Kimi capacity | Removed: its usage counters matched the `kimi` login, so both share one quota | Will `auths/`; private backup in Will `ops/runtime/` | [FLEET.md](FLEET.md#operations-on-november-18-2026); [KIMI_ROUTING.md](KIMI_ROUTING.md) |
| A restart could select a different release | Image IDs with `pull_policy: never` | Compose files; `fleet-release.json` | [FLEET.md](FLEET.md#running-fleet) |
| A release passed health checks and failed inference (`v8.0.66-fleet.2`) | Mac first, then a live inference gate, then Will | Release procedure | [FLEET.md](FLEET.md#running-fleet) |
| Codex CLI slept for days on a long `Retry-After` | The header is omitted above one minute | Fleet release (server) | [FLEET.md](FLEET.md#running-fleet) |
| The log archive filled and the Mac nearly ran out of disk | Archive moved to Innie | `ops/com.rever.cliproxy-log-archive.plist` | [README.md](README.md#log-retention) |
| Review agents printed keys into transcripts | Client keys, the Z.ai key and the NVIDIA key rotated; reviewers get masked dumps only | Host config; working practice | [FLEET.md](FLEET.md#operations-on-november-18-2026) |

## 4. What is still manual or accepted

**Codex session-start failover.** With the [Codex launcher enabled](README.md#codex-failover),
the Mac's `codex_cliproxy` checks Will at startup and selects the Mac automatically if Will
fails its health check and the Mac passes. If both fail, it starts with the Will URL. The
launcher reuses the three-second `/healthz` probe from section 2.6. A healthy `/healthz`
cannot reveal an upstream egress outage. Codex CLI keeps one `base_url` per process, so an
existing session stays on its selected host. Andrew enabled the launcher on November 18, from
the bundle. The `codex_cliproxy_mac` and `codex_kimi_mac` profiles remain fixed to the Mac.

**Not automatic:**

- **CLIProxyBar's Will entry uses Will's raw address.** Keep the app for live quota glances.
  The monitor supplies fleet quota history, alerts and summaries. The Mac's `codex_cliproxy`
  moved to the tailnet name on November 18 ([FLEET.md](FLEET.md#operations-on-november-18-2026)), but
  CLIProxyBar's Will entry still reaches Will at its raw tailnet IP address, so the section 2.6
  reason for the name applies to it too: with Tailscale stopped on another network, it would send
  its requests in clear to whatever holds that address. Moving it to the name is a separate change
  for Andrew.
- **Named Claude profiles.** They are fixed to one host by design (section 2.6). A cooldown on a
  named account is returned to the chat.
- **M1.** It reaches Will only. The Mac is loopback-only, so it cannot be M1's fallback; M1's
  fallback is its direct subscriptions. Its profile still uses Will's raw tailnet address
  ([FLEET.md](FLEET.md#running-fleet)), so the reason for the tailnet name in section 2.6 applies
  to M1 whenever it runs with Tailscale stopped on another network.
- **Will's own T3.** Loopback to Will's proxy only. The container's `restart: always` is the
  remedy.
- **Monitor paused.** The Mac LaunchAgent is installed from the bundle but booted out,
  because SSH from launchd jobs to Will times out on the Mac (FLEET.md, live activation).
  Until it resumes, use `fleetctl health` and `parity` by hand. With only egress down,
  `/healthz` can stay green. Look for `egress_hold` lines in
  `ops/runtime/fleet/failover.jsonl` on the Mac or dial-error 5xx answers from Will.
- **Will's first ship** (unit 2c) awaits its own approval. The Mac cutover ran on
  November 18 with a passing P0 check of the installed paths and checksums (section 2.8).

**Accepted:**

- **A message can run twice** if Will's egress fails in the middle of a request (section 2.6).
- **Egress-down delay.** Since both containers were recreated on November 18, a dropped-SYN dial
  fails in about 7 seconds, but one request can make up to 12 such dials with three credentials
  and `request-retry: 3`, about 86 seconds per request in the worst case (section 2.7).
- **A failover can cost a cold cache write**, because bindings and date pins are per host
  (section 1).
- **A resolver that answers every name** defeats the tailnet-name protection; not tested
  (section 2.6).
- **Failover never relieves a 429.** Quota belongs to the account and capacity to the provider,
  and both hosts use the same accounts and providers.
- **Each host learns a cap separately.** Cooldown state is not shared, so a capped account costs
  about one wasted call per host per window ([FLEET_DESIGN.md](FLEET_DESIGN.md) section 5).
- **One dated exception** (debug logging), due for review by December 2, 2026 (section 2.1).
  The two `camoff-astra` exceptions are gone; the prefix itself was removed from both hosts.
- **Will uses the flat legacy YAML layout** until the layout migration (design unit 7). The loaded
  view makes the two layouts comparable in the meantime.
- **Old client keys overlap** until 2026-11-19 10:30 UTC, so sessions launched with an old key
  keep working until then (section 2.9).
- **No measurement of affinity with two or more eligible accounts** after the change.
- **Deploys drop open streams**, because the idle gate does not work as designed (section 2.7).

## 5. When to change what

Before a change, find it in this list and keep the invariant next to it. Procedures are in
[FLEET.md](FLEET.md).

**Any fleet change.** Follow the [fleet contract](FLEET.md#fleet-contract). The invariant behind
it: the two hosts differ only where an unexpired exception says so.

**A new release.**

- One source commit, two images, both IDs in `fleet-release.json`, and both Compose files and
  Will's owner contract changed together.
- Keep `pull_policy: never` and the previous images for rollback.
- A health check is not enough. Run the inference gate, and check that the injected date is stable
  within a session and that the second turn reads cache.

**Routing fields.**

- Any change to the five routing fields drops every binding. Batch such changes.
- Keep new sessions balanced by quota and existing sessions on their account.
- Measure with the decision mix, not with truncated session IDs.

**Anything that touches the prompt prefix** (cloaking, injected dates, header defaults, system
text).

- The bytes at the start of a conversation must stay identical for the whole session and for its
  subagents. Test with a multi-turn session and read the cached token counts.

**Retry and cooldown values.**

- CLIProxy's per-round wait (75 s), Claude Code's retry span (about 160 s) and the launcher's
  no-retry boundary (180 s) depend on each other. If you change one, check the other two.
- Keep quota 429s cooling down. Keep long `Retry-After` values off the wire.

**The launcher.**

- It serves every Claude CLIProxy profile on both machines. On the Mac it runs from the bundle,
  so a change goes live only through an approved ship. Sessions started before the November 18
  cutover still run the checkout copy until they end.
- Without `CLIPROXY_FALLBACK_URL`, behaviour must not change: no timers, no probes.
- Never retry or move a request to another account. Never let a named route cross hosts.
- Never replay a message that may already be generating. Only pre-connect failures and dial-error
  5xx responses replay a message.
- Never cut a stream that is still delivering bytes.
- Classify upstream errors from captured responses, and match dial errors only.
- Use the monotonic clock, share a hold between concurrent requests, and clear failure counts on
  recovery.
- No timer may end a request after a connection exists; only the liveness watch may, when the
  host fails its probes and the request receives nothing.
- Keep failover configured on `claude_cliproxy` across profile reruns
  ([FLEET.md](FLEET.md#running-fleet)). To change only its two URLs, use `set_pool_fallback.mjs`;
  unless `--no-key-check` is given, it checks the key you pipe to it against the hosts before it
  opens a T3 session (section 2.6, The drill). It saves the piped key in place of the stored one, which T3 redacts and the tool cannot read.

**Enabling failover for another profile.**

- First prove strict parity for everything that profile can reach: keys, accounts and catalog,
  with no exception.
- Do not add it to a named route.

**Config.**

- One in-place write that keeps the inode and mode 600; then verify the loaded values, not the
  file.
- Remove only your own lines on rollback.
- A reload reconfigures plugins and can close Cursor bridges.

**Accounts.**

- Log in separately on each host. Never copy a token file.
- Keep one enabled credential per `t3-*` prefix and the same prefixes on both hosts.

**Secrets.**

- Never read or print `config.yaml`, `.env` or `auths/` in a transcript. Give reviewers masked
  dumps.
- New tooling prints only allowlisted values and writes private output to `ops/runtime/` with
  mode 600.

**Compose and container settings.**

- They take effect at a recreate, and a recreate drops every stream on that host.
- On the Mac the watchdog can run `docker compose up` by itself, so a Compose change pulled into
  the checkout can go live without an activation.
