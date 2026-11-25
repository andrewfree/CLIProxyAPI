# ops/AGENTS.md

Fleet-operations guidance for this fork. The root `AGENTS.md` follows upstream
and covers the Go server. Keep fork-only operational rules here.

## Claude usage and resets

- To check Claude account usage, scoped model caps such as Fable, or reset
  grants, run `python3 ops/claude_usage_resets.py status`. Do not write
  one-off scripts. See `ops/CLAUDE_USAGE_RESETS.md`.
- Usage and reset numbers change all the time. For any number you act on or
  report as current (a claim, a fan-out check, a cap decision), run `status`
  in this turn. Never reuse a percentage from an earlier turn or note as
  current.
- Run the tool from the checkout. The ops bundle ships a copy only for the
  fleet monitor's imports, and that copy refuses to run as a command.
- `status` and `watch` ask Will first and fall back to the local proxy only
  when Will is unreachable. The last stderr line names the host that answered;
  report it with the numbers.
- `status`, `watch`, and `claim --dry-run` are read-only and need no approval.
- `claim` runs only on the host it names and never falls back. `claim --yes`
  requires `--host will` or `--host local`. Run a dry run and its real claim on
  the same host. After an `indeterminate` receipt, check `status` on that host
  before any rerun, and rerun only there.
- `claim --yes` consumes a grant. Run it only with Andrew's explicit approval
  for that account and grant ID. A conditional approval counts, for example
  "when it hits 99%, reset Fable". Never substitute a different grant when the
  approved one is refused. Report the refusal instead.
- A scheduled watch passes its thresholds and weekly-window pin to `claim`
  (`--min-fable-percent`, `--min-weekly-percent`, `--weekly-resets-at`). Do
  not compare `captured_at` or `resets_at` by hand in a task prompt; see
  "Timestamps in a scheduled watch" in `ops/CLAUDE_USAGE_RESETS.md`.
- Poll usage no faster than every 300 seconds. Shorter intervals hit HTTP 429.
  The fleet monitor also reads each Claude account about every 15 minutes,
  so pause it before a `watch` and resume it afterwards
  ([Install or resume on the Mac](FLEET.md#install-or-resume-on-the-mac)).
- The monitor's `ops/runtime/fleet/quota-history-YYYY-MM.jsonl` is for history
  and trend questions only. Quote a row's `captured_at` time with it. A row can
  be 15 or more minutes old and has no grant IDs.
- Report results from the claim receipt (`result`, `resets_left`, before and
  after limits), not from the request having been sent.
- Never print or commit tokens, organization UUIDs, or management keys.
  Receipts under `ops/runtime/` are ignored by git. Copy a receipt into
  `ops/evidence/` only after checking that it holds none of these.
- After changing the tool, run `python3 -m unittest -v ops.test_claude_usage_resets`.
