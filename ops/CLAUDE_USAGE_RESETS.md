# Check and redeem Claude usage resets

Anthropic gives Claude subscription accounts occasional usage resets. Examples
are a monthly full-limit reset and the Fable launch resets. Claude Code calls
these "cedar_ember" grants. `claude_usage_resets.py` reads each account's
limits and grants through CLIProxy, watches a limit, and redeems a grant. The
OAuth token stays inside CLIProxy. The tool sends each upstream request through
`POST /v8/management/requests/api-call` with `auth_index` and `$TOKEN$`.

Redeeming a grant consumes it. Andrew must approve each redemption: the
account, the grant ID, and the timing. Reading status needs no approval.

## Which host answers

Will is the primary proxy, so `status` and `watch` ask Will first. The tool
sends its own source to Will over SSH stdin (`python3 -I -B -`), using the
SSH destination, instance root, and port in `fleet/hosts.json`. On Will it reads
the `.env` under that instance root and calls Will's proxy at `127.0.0.1`, so no
management key leaves its host. The management client ignores `HTTP_PROXY`
settings and refuses redirects, so the key goes only to that URL.

The tool falls back to the local proxy only when SSH fails or Will's proxy never
answered, and only before Will has printed anything, so a `watch` never
switches hosts partway through. Any other error from Will is the answer: an
HTTP 429 from the usage endpoint, a management API error, or a connection lost
after Will's proxy answered. Stderr names the host and URL that answered, for
example `answered by Will (will.local) at http://127.0.0.1:8317`.

- `--host auto` is the default for `status` and `watch`.
- `--host will` or `--host local` asks only that host and never falls back.
  `--root` and `--base-url` name the local proxy, so the tool refuses them with
  `--host will`.
- From a worktree, which has no `.env`, `status` and `watch` read the local key
  from the Mac instance root in `fleet/hosts.json`. Global options go before the
  subcommand: `python3 ops/claude_usage_resets.py --root ~/cliproxy status --host local`.
- `claim` accepts only `--host will` or `--host local` (default `local`) and
  never falls back, because its request ID and receipt stay on the host that
  sent it. A rerun on another host would carry a new request ID and could
  spend a second grant. Run local claims from the checkout, as before, so their
  request IDs stay in one place.
- Run on Will itself, `auto` and `will` use Will's own proxy. A hand-copied
  `ops/` without `fleet/hosts.json` also answers from the local proxy, as before.
- Exit status 3 means no proxy answered at the URL tried.

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
python3 ops/claude_usage_resets.py watch --host will --account <email> --threshold 99 --notify \
  && python3 ops/claude_usage_resets.py claim --host will --account <email> --grant <grant-id> \
       --min-fable-percent 99 --yes
```

`--min-fable-percent 99` makes the claim check the threshold again on a fresh
read, so a stale or wrong trigger cannot spend the grant. Pin the watch and
the claim to the same host. `--notify` shows the notification on the machine
that runs the command, even when Will answered the watch.

## Redeem a grant

```sh
python3 ops/claude_usage_resets.py claim --host will --account <email> --grant <grant-id> --dry-run
python3 ops/claude_usage_resets.py claim --host will --account <email> --grant <grant-id> --yes
```

Run the dry run and the real claim on the same host. With `--host will`, the
request ID and receipt are written under `ops/runtime/claude-resets/` in Will's
instance root, and a `--receipt` path is a path on Will (relative to the SSH
user's home directory). Each receipt records the host that sent it.

The receipt is written with `result: sending` before the claim goes out. If the
connection drops before the response arrives, the receipt says
`indeterminate`, the tool exits 1, and the claim may have been sent. If SSH
drops during `--host will --yes`, the outcome is unknown in the same way.
Either way, rerun the same command on the same host. It repeats the stored
request ID, so a rerun on another host could spend a second grant.

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
