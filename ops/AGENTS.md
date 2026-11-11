# ops/AGENTS.md

Fleet-operations guidance for this fork. The root `AGENTS.md` follows upstream
and covers the Go server. Keep fork-only operational rules here.

## Claude usage and resets

- To check Claude account usage, scoped model caps such as Fable, or reset
  grants, run `python3 ops/claude_usage_resets.py status`. Do not write
  one-off scripts. See `ops/CLAUDE_USAGE_RESETS.md`.
- Usage and reset numbers are live. Query them fresh. Never reuse a percentage
  from an earlier turn or note as current.
- `status`, `watch`, and `claim --dry-run` are read-only and need no approval.
- `claim --yes` consumes a grant. Run it only with Andrew's explicit approval
  for that account and grant ID. A conditional approval counts, for example
  "when it hits 99%, reset Fable". Never substitute a different grant when the
  approved one is refused. Report the refusal instead.
- Poll usage no faster than every 300 seconds. Shorter intervals hit HTTP 429.
- Report results from the claim receipt (`result`, `resets_left`, before and
  after limits), not from the request having been sent.
- Never print or commit tokens, organization UUIDs, or management keys.
  Receipts under `ops/runtime/` are ignored by git. Copy a receipt into
  `ops/evidence/` only after checking that it holds none of these.
- After changing the tool, run `python3 -m unittest -v ops.test_claude_usage_resets`.
