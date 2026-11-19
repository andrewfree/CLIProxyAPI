# ops/AGENTS.md

Fleet-operations guidance for this fork. The root `AGENTS.md` follows upstream
and covers the Go server. Keep fork-only operational rules here.

## Claude usage and resets

- To check Claude account usage, scoped model caps such as Fable, or reset
  grants, run `python3 ops/claude_usage_resets.py status`. Do not write
  one-off scripts. See `ops/CLAUDE_USAGE_RESETS.md`.
- Get current usage and reset numbers with `status`. On the Mac it shares
  a private cache and lock with the fleet monitor for the same instance root.
  It serves a snapshot less than 300 seconds old, or waits until a new read
  is due when the last attempt failed or produced no local snapshot. Never
  reuse a percentage from an earlier turn or note as current.
- `status`, `watch`, and `claim --dry-run` are read-only and need no approval.
- `claim --yes` consumes a grant. Run it only with Andrew's explicit approval
  for that account and grant ID. A conditional approval counts, for example
  "when it hits 99%, reset Fable". Never substitute a different grant when the
  approved one is refused. Report the refusal instead.
- The monitor reads Claude every third 300-second tick. Leave at least 300
  seconds between usage reads for the same account. Pause the monitor before
  using `watch`. Claim checks and retries use their own read path.
- Report results from the claim receipt (`result`, `resets_left`, before and
  after limits), not from the request having been sent.
- Never print or commit tokens, organization UUIDs, or management keys.
  Receipts under `ops/runtime/` are ignored by git. Copy a receipt into
  `ops/evidence/` only after checking that it holds none of these.
- After changing the tool, run `python3 -m unittest -v ops.test_claude_usage_resets`.
