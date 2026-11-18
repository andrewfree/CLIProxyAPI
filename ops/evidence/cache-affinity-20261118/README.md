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

Pending. The change is one key, `routing.session-affinity: true`, because Will already runs
`transient-error-cooldown-seconds: -1` and `max-retry-interval: 75`.
