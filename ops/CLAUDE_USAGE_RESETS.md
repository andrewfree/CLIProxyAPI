# Check and redeem Claude usage resets

Anthropic gives Claude subscription accounts occasional usage resets. Examples
are a monthly full-limit reset and the Fable launch resets. Claude Code calls
these "cedar_ember" grants. `claude_usage_resets.py` reads each account's
limits and grants through CLIProxy, watches a limit, and redeems a grant. The
OAuth token stays inside CLIProxy. The tool sends each upstream request through
`POST /v8/management/requests/api-call` with `auth_index` and `$TOKEN$`.

Redeeming a grant consumes it. Andrew must approve each redemption: the
account, the grant ID, and the timing. Reading status needs no approval.

## Read limits and grants

```sh
python3 ops/claude_usage_resets.py status                       # all Claude accounts
python3 ops/claude_usage_resets.py status --account <email> --json
```

The tool reads `GET https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1`.
This request is read-only. The claude-cli `User-Agent`, `x-app: cli`, and
`anthropic-beta: oauth-2025-04-20` headers are required to get the `cedar_ember` block.

- `limits` holds `weekly_all`, the weekly total. It also holds one
  `weekly_scoped` entry per capped model. In November 2026 Fable was the only
  capped model.
- `weekly_scoped_shares` puts a scoped limit on the weekly scale. On
  Max 20x, Fable's `allowance_percent_of_weekly` is 50. "Fable 89%" means 89%
  of that 50%, which is about 45% of the week.
- `cedar_ember.grants[]` lists each grant's `id`, `resets_left`,
  `usable_now`, and `clears`. `seven_day` is the weekly total.
  `seven_day_overage_included` is the scoped Fable limit. Each grant also
  lists `ends_at` and `gives_back_weekly`.
- `eligible` and `cooldown_until` gate every claim. `next_grant_id` is only a
  suggestion. On 2026-11-11 the server accepted a Fable grant while it named
  the full reset as next.

Run the tool from the checkout. The ops bundle ships a copy only for the fleet
monitor's imports, and that copy exits with an error if run as a command. The
monitor also reads each Claude account about every 15 minutes
and records the readings in `ops/runtime/fleet/quota-history-YYYY-MM.jsonl`.
Use that file for history only, not as a current reading, and pause the
monitor before a `watch`.

## Watch a limit

```sh
python3 ops/claude_usage_resets.py watch --account <email> --limit Fable --threshold 99 --notify
```

`watch` exits 0 when the limit reaches the threshold. It exits 2 after
`--max-failures` consecutive failed polls. `--limit all` watches the weekly
total. Keep `--interval` at 300 seconds or more. At 180 seconds the usage
endpoint returned HTTP 429 to some polls. Run it in the background. To redeem
when the limit triggers, chain a claim that Andrew approved:

```sh
python3 ops/claude_usage_resets.py watch --account <email> --threshold 99 --notify \
  && python3 ops/claude_usage_resets.py claim --account <email> --grant <grant-id> \
       --min-fable-percent 99 --yes
```

`--min-fable-percent 99` makes the claim check the threshold again on a fresh
read, so a stale or wrong trigger cannot spend the grant.

## Redeem a grant

```sh
python3 ops/claude_usage_resets.py claim --account <email> --grant <grant-id> --dry-run
python3 ops/claude_usage_resets.py claim --account <email> --grant <grant-id> --yes
```

For a conditional redemption, add `--min-fable-percent 99` to the claim.
The claim checks the account profile and reads usage again before spending
the grant. It refuses if the Fable limit is missing, is not a finite number (an
integer too large for a float counts as not finite), or is below the
threshold, even if an earlier watch already triggered. This check
also applies to dry runs.

`claim` refuses to run without `--yes` or `--dry-run`. Before sending, it does the following:

1. It reads `/api/oauth/profile` and checks that the account email matches. The organization UUID comes from this profile.
2. It reads usage with backoff on 429 and 5xx. With `--min-fable-percent N`,
   it refuses unless this fresh read reports a Fable limit of at least N
   percent. It also refuses if the account is ineligible, in cooldown, or the
   grant is missing, used, or unusable.
3. It sends the request Claude Code 2.1.322 sends:
   `POST /api/organizations/{org}/reset_rate_limits` with
   `{"program":"cedar_ember","grant_id":"<id>","request_id":"<id>"}`.
   `grant_id` matches `^[a-z0-9_-]{1,40}$`.
   `request_id` matches `^[A-Za-z0-9_-]{1,64}$`. The request ID persists in
   ignored `ops/runtime/claude-resets/request-ids.json` for each account and
   grant, so a rerun repeats the same request. This client copies Claude
   Code's retry identity. Server deduplication is untested.
4. It writes the receipt before rereading usage. A failed after-check cannot
   lose the claim outcome.

The response contains `result` (`reset`, `not_limited`, `cooldown`,
`already_used`, `ineligible`, and others), `resets_left`, `cleared`, and
`cooldown_until`. A cooldown of about three minutes follows a successful claim.
Receipts go to `ops/runtime/claude-resets/` unless `--receipt` is set. Each
receipt holds the account email. It holds no tokens or organization UUIDs: the
claim URL is recorded with `<org>` in place of the UUID.

## Record: 2026-11-11 Fable reset on the Gmail Max account

Andrew asked for one Fable reset when the Gmail Max account reached 99% of its
Fable cap. Claude Opus 5.5 ran this procedure with prototype scripts in a T3
thread. The scripts became this tool afterward. Evidence is in
[`evidence/fable-reset-20261111/`](evidence/fable-reset-20261111/).

- At 09:19 PT the status read showed 70% weekly and 89% Fable. Three grants
  were usable: `full-reset-2611`, `launch-2610-fable`, and `launch-2610-fable-6mo`.
- The claim endpoint and `program` value came from the Claude Code 2.1.322
  binary. The claim function holds the
  `/api/organizations/${org}/reset_rate_limits` template, and the constant
  `ae="cedar_ember"` sits beside the ID patterns above. A dry run passed
  without sending a claim.
- The watcher polled every 180 seconds. It logged 92% at 09:26 and 94% at
  09:36. Two polls got HTTP 429. It logged 99% at 09:45:49.
- The first claim attempt stopped before sending. Its pre-claim usage read
  got HTTP 429. The rerun, with backoff added, sent the claim at about 09:49.
  The response was HTTP 200 with `result: reset` and `resets_left: 0`.
  `cleared` contained `seven_day_overage_included`.
- Usage afterward: Fable fell from 100% to 2%, and the weekly total fell from
  77% to 28%. Because `gives_back_weekly: true` was set, the reset also
  returned the weekly usage that Fable had consumed.
- The full reset and the 6-month Fable reset stayed unused. A later dry run
  showed the used grant as `0 left, usable_now=False`.
