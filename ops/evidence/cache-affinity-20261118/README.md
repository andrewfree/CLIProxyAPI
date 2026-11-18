# Session affinity activation, November 18, 2026

Records when each host switched to the pinned routing policy in PR 4. Values are from the live
management config, file metadata and the fleet verifiers. No keys, identities or prompt content are
recorded.

## Mac (arm64)

| Time (PST) | Change | Result |
| --- | --- | --- |
| 00:15:41 | `routing.session-affinity: true`, `routing.cooldown.transient-error-cooldown-seconds: -1`, `routing.retry.max-retry-interval: 75` in one in-place write | Loaded values match. Inode 88172703 and mode 600 kept. Host and container config SHA-256 `11735ca23b06...` match. No restart. |
| 00:16:13 | Z.ai alias `glm-5.3-flash` -> `glm-5p3-flash` in a separate write | `/v1/models` owner moved from `cursor` to `zai-coding-plan`. Cursor model definitions still listed the alias. |
| 00:16-00:17 | One reload with a temporary comment, then the exact bytes restored | Cursor definitions list only `glm-5.2`, matching Will. Config SHA-256 `145d6d458c16...` on host and container. |

Checks after the change:
- The PR's fleet verifier, run against the live Mac, reported no failures and the same account counts
  (claude 3, codex 3, cursor 1, kimi 1).
- `claude_usage_resets.py status` reported the same capped and eligible state for each route label
  before and after the reload.
- The first hour after cutover had 101 affinity decisions: 5 new bindings, 96 hits, no reselection.
  Only one Claude account was eligible, so this shows continuity, not balancing.

## Will (amd64)

The change is one key, `routing.session-affinity: true`, because Will already runs
`transient-error-cooldown-seconds: -1` and `max-retry-interval: 75`. Will went second, after a
five-model re-review (Claude Fable 5.2, Claude Opus 5.5, GPT-6.2 Astra, GPT-6.2 Sol, Kimi K3.1 Pro)
and a Mac gate: 286 hits, 10 new bindings and no reselection since cutover, plus a Codex canary at
01:43 that bound once and hit 11 times with 98 to 99% upstream cached input.

| Time (PST) | Change | Result |
| --- | --- | --- |
| 01:59:31 | Seven ops files (manifest, fleet helper and verifier, Will owner policy and verifier, two docs) backed up and replaced with this PR's versions | SHA-256 of all seven match the PR commit. The new verifiers flagged only the pending affinity value. |
| 01:59:46 | `routing.session-affinity: true` in one in-place write | Loaded value true. Inode 32397311 and mode 600 kept. Host and container config SHA-256 `32c6084b1b45...` match. No restart. |

Checks after the change: both Will verifiers reported no findings, account counts were unchanged,
Cursor's model definitions still listed only `glm-5.2`, and the first affinity binding appeared.
Rollback on Will alone: set the key back to `false` with the same in-place write and restore the
seven ops files from `ops/runtime/cache-affinity-apply-20261118/ops-backup-20261118T015931/`.

## As-run scripts

These are as-run records, not maintained tools. Their paths and hosts are those of 2026-11-18. They
are byte-identical copies of the private files in `ops/runtime/cache-affinity-apply-20261118/` on
the Mac. Their backups and receipts stay private. Check a copy with `shasum -a 256 <file>`.

- `apply_mac.py` (sha256 `2753d58bb573199676ff97cfd07ce61ec9e9d9348b556dd77325f9fce00de36f`): run on the Mac as `apply_mac.py routing|alias|timezone|revert-routing|revert-alias|revert-timezone [--dry-run]`. It edits `/Users/rever/cliproxy/config.yaml` in place, keeping the inode, through exact text anchors that must each occur once. It refuses to run with less than 1 GiB free. The `routing` and `alias` steps make the 00:15:41 and 00:16:13 edits in the Mac table. Every run prints the step, inode, mode, sha256 before and the changed lines. Without `--dry-run`, the script also writes a 0600 backup and a `receipt-<step>-<stamp>.json` next to itself, waits 8 seconds and prints that receipt. The receipt holds the host and container config sha256, the inode and mode checks, and loaded values from the management API. The script reads the management key from `.env` and never prints it. It exits with `POST-CHECK FAILED` if a check fails. The script has no step for the 00:16-00:17 reload with a temporary comment.
- `apply_will.py` (sha256 `c79bee5c3746b08e8949e8b32c408f8391d987ae6902988f8cb8bb0c220b3d34`): run on Will by sending it to `python3 -` over ssh with one mode: `dry-run`, `apply`, `revert`, `timezone`, `dry-run-timezone` or `revert-timezone`. `apply` is the 01:59:46 `routing.session-affinity: true` write in the Will table. It edits `/home/andrew/cliproxy/config.yaml` in place, keeping the inode, and the anchor must occur once. On Will it writes a 0600 backup and a receipt under `/home/andrew/cliproxy/ops/runtime/cache-affinity-apply-20261118/`, then waits 8 seconds. It prints one JSON line with the inode and mode before and after, the 16-character sha256 prefixes of the host and container config, the free GiB, the loaded affinity, cooldown, retry and timezone values before and after, and the checks. It exits with `POST-CHECK FAILED` if a check fails. It does not replace the seven ops files at 01:59:31.

Both files were last modified at 02:15:57 PST, after the affinity writes at 00:15:41 and 01:59:46.
This record does not show whether the affinity steps changed between those writes and that time.
Both files also have `timezone` steps that add `timezone: "America/Los_Angeles"` after the
`runtime-version: "v26.12.1"` line. `ops/FLEET.md` records that both hosts set that timezone on
November 18 ("Claude date timezone"), but this record does not show whether these steps made those
writes.
