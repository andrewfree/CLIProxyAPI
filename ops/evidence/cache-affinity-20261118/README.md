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
