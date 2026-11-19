# Live activation, November 18, 2026 (evening)

Andrew approved three live changes with `promote fleet mac-bundle`, `promote fleet monitor` and
`promote fleet codex-failover`, and asked for the `camoff-astra` mirror on Will. Claude Opus 5.5
ran them in T3 from a clean worktree at merged commit `9273f727` (PRs #19, #20, #21). This
directory holds the as-run helper scripts. They print no keys, tokens or email addresses, and
mask the tailnet name. The private backups they wrote are under the instance root's
`ops/runtime/`, which git ignores.

## Mac bundle cutover (about 17:53 to 17:56 Pacific)

Followed [the FLEET.md runbook](../../FLEET.md#mac-ops-bundle-cutover) steps 1 to 4 as written.

- Step 1: `ship --dry-run` reported 24 files to add, and `~/cliproxy-live` did not exist yet.
  `repoint_claude_launchers.mjs --report` listed the five checkout launcher paths.
  [codex_binary_path.mjs](codex_binary_path.mjs) showed `codex_cliproxy` on
  `/opt/homebrew/bin/codex` with no environment.
- Step 2: plists and token helper saved to the private backup
  `/Users/rever/cliproxy/ops/runtime/mac-bundle-cutover.2Parb1BL`. `ship --apply` reported
  `shipped`, 24 added, previous files under `~/cliproxy-live/prev/20261119T015341160968Z-23rppfy1`.
  The Compose activation receipt was recorded; Compose files were unchanged since the last
  container recreate.
- Step 3: both LaunchAgents reinstalled from the bundle, the shipped token helper installed, and
  the five Claude profiles repointed to `/Users/rever/cliproxy-live/ops/claude_account_launcher.mjs`.
- Step 4: both jobs run the bundled scripts with `CLIPROXY_ROOT=/Users/rever/cliproxy` and the
  instance root as working directory. The token helper returned output. P0 returned `ok` with no
  issues. Watchdog cycles at 17:53:48 and 17:55:49 were all `ok`, and the archiver's first bundled
  run archived 6 files with 0 failures. New T3 sessions on `claude_cliproxy_mac` and
  `claude_gmail_mac` answered, and a new `claude_cliproxy` session ran the bundled launcher.
  Appending a line to a checkout copy of `cliproxy_watchdog.py` left the deployed hash unchanged.
  [claude_profiles.mjs](claude_profiles.mjs) lists the profiles' routes without secrets.

Rollback: the first-cutover rollback in FLEET.md, with `backup` set to the private directory above.

## Codex failover on `codex_cliproxy` (about 18:00 to 18:02)

- Drill: [codex_cli_drill.mjs](codex_cli_drill.mjs) reads the profile's launch arguments without
  printing them and passes them on stdin to [codex_cli_drill.py](codex_cli_drill.py). (As run, it
  passed them as an argument; the committed copy uses stdin and reports a timeout or launch error
  only by its type, so no message from the drill echoes them. They are still on the wrapper's and
  Codex's command lines while the drill runs; on a timeout the drill kills that whole process
  group.) The Python half swaps only the
  `base_url` value for a closed loopback port and runs one `codex exec` through the shipped
  wrapper with the real Codex binary. Result: exit 0, reply marker present, decision
  `exec`, `fallback`, `fallback_healthy`. This replaced the runbook's temporary T3 profile copy,
  because removing a provider instance needs an RPC the settings tools do not use yet.
- Apply: [codex_failover_profile.mjs](codex_failover_profile.mjs) `apply` set `binaryPath` to the
  bundled wrapper with `CLIPROXY_FALLBACK_URL=http://127.0.0.1:8317` and
  `CODEX_REAL_BINARY=/opt/homebrew/bin/codex`. Launch arguments and the 19 other profiles were
  unchanged. The as-run copy compared JSON strings and reported a false mismatch because T3
  returned the profile's keys in another order; a field-by-field diff of the redacted backups
  showed only the intended changes. The committed copy compares with sorted keys, adds or
  removes only its two variables, and refuses a profile that already holds one of them (apply)
  or does not hold both with the expected values (rollback).
- Check: a new T3 session answered, and `ops/runtime/fleet/codex-failover.jsonl` under the
  instance root logged `app-server`, `primary`, `primary_healthy` at 02:02:06Z. Nothing was
  written inside the release directory.

Rollback: `node codex_failover_profile.mjs ~/.t3 <new-private-dir> rollback`.

## `camoff-astra` on Will (about 18:05)

The Mac's `camoff-astra` prefix sits on one Codex OAuth credential. Will already had its own
active login for the same account (matching label hash, computed at run time), so no sign-in and no file copy was
needed. [will_set_camoff_prefix.py](will_set_camoff_prefix.py), run on Will over SSH, set the
prefix with `PATCH /v8/management/credentials/fields`: 20 models before, 40 after, all unprefixed
models kept. Will's `/v1/models` then listed the same 20 `camoff-astra/` models out of 187 as the
Mac, a `camoff-astra/gpt-5.6-luna` Responses request through Will returned 200, and
`fleetctl parity` reported `equal` with both `camoff-astra` exceptions unused. They were removed
from `ops/fleet/exceptions.json`.

Rollback: the same PATCH with `"prefix": ""`, then restore the two exceptions.

## `camoff-astra` prefix removed from both hosts (about 18:45)

Andrew asked for the cleanest fix to the default-model side effect below and approved removing
the prefix. The 5,985 of 8,765 unit 0 figure counted the `codex:camoff-astra` credential serving
unprefixed requests (the `att` field); 0 of the 8,765 requests named a `camoff-astra/` model, and
none of the 181 request logs kept locally since did either. [clear_camoff_prefix.py](clear_camoff_prefix.py)
(label hash passed at run time) ran on the Mac, then on Will over SSH: 40 models to 20 on each,
all unprefixed models kept. Both `/v1/models` lists then had 167 models with no `camoff-astra/`
entry, a `gpt-5.6-luna` pool request returned 200 on each host, `fleetctl parity` reported
`equal` with no unused exception, and a new `codex_cliproxy` session defaulted to `gpt-6.2-sol`.

Rollback: run [will_set_camoff_prefix.py](will_set_camoff_prefix.py) `<label-hash>` on each host.
The hash is the first 10 hex characters of the SHA-256 of the lowercased label of the Codex
credential whose ID is `camoff-astra`, computed at run time from the Mac's credential list.

## Monitor (installed 17:56, paused 17:57:57)

The install block in FLEET.md ran: `plutil` OK, Text Me health OK, bootstrap rc 0. The first run
exited 0 in about 12 seconds and wrote `health.json`, `latest.json` and 8 history rows; Text Me
reported the text as sent. Will came back `unreachable` with `ssh_unreachable: true`, while
`fleetctl health` from a shell reported Will `ok`. [ssh_probe_launchd.sh](ssh_probe_launchd.sh)
runs [ssh-probe.sh](ssh-probe.sh) from a one-shot LaunchAgent: SSH to `will.local` (resolving to
`will.home.arpa`) and to Will's Tailscale address both timed out with exit 255, and the same
commands from a shell or `env -i` connected. Little Snitch's network extension is active and the
macOS firewall is off. The job was booted out before a second matching sample could send a false
Will alert. Z.ai quota read `quota-unavailable` with no error; that needs its own follow-up.

Resume: allow SSH from launchd jobs to Will, rerun `ssh_probe_launchd.sh <file>` until both lines
show `rc=0`, then run the install block in FLEET.md again.

Follow-up at 19:03: after Andrew added a Little Snitch rule, the `/bin/sh` probe reached
`will.local` (`rc=0`; the bare Tailscale address then failed only on key auth because
`~/.ssh/config` has no entry for it). The resumed monitor still reported Will `unreachable`, and
because its 17:56 sample counted as the first of two, it opened the Will alert and sent a false
text. It was paused again at 19:03:30. A launchd job running `ssh` from Homebrew's
`/opt/homebrew/bin/python3` failed with `No route to host`, the Local Network privacy signature
from the November 7 Messenger Access case; a `/bin/sh` parent did not help, and Apple's
`/usr/bin/python3` connected (`rc=0`). The monitor plist now uses `/usr/bin/python3`.
`ssh_probe_launchd.sh <file>` now defaults to that chain ([ssh-probe.py](ssh-probe.py) under Apple's
`python3`); pass `sh` as the second argument for the original `/bin/sh` probe.

## Side effects found afterwards

- **Default model on `codex_cliproxy`.** A T3 session started at 18:02 with no model chosen ran
  `gpt-6.2-sol`. One started at 18:28, after Will gained the `camoff-astra/` models, ran
  `camoff-astra/gpt-6.2-sol`, which routes only to the one prefixed credential. Resolved by
  removing the prefix from both hosts (section above).
- **Wrapper processes killed by a test cleanup.** At about 18:28 a `pkill -f` meant for a stub in
  a temp directory also matched the real bundled Codex wrapper processes. T3 restarted the
  `codex_cliproxy` app-server within about 20 seconds, and a new session answered; a session
  that was mid-turn on that profile then had to resend its message.

## Primary checkout pull

At 17:52:45 Pacific, before the cutover, a `git pull --ff-only` that this session did not run
moved the primary checkout from `04fc8569` to `9273f727`. The checkout jobs ran the merged code
for about one cycle; the 17:53:05 watchdog cycle passed.
