# Will Claude pool OAuth recovery

Will's shared Claude pool recovered after Andrew completed a fresh iCloud OAuth
login from M5 on November 14, 2026, at 02:47:36 UTC. Four existing unprefixed
Opus 5.5 requests then selected iCloud and completed successfully. No new
inference probe, proxy restart, deployment, or credential copy was needed.

The investigation and recovery were performed by gpt-6-astra through the Codex
harness. This directory contains sanitized evidence only. The full local
working records remain under `ops/runtime/will-opus-rotation-20261114/`.

## Why both requests failed

Both failed T3 runs used `claude_cliproxy`, the shared Will pool, with unprefixed
`claude-opus-5-5`. The original gateway processes had `shared-pool` mode and no
account prefix. Session affinity was disabled. Retained request bodies matched
11 attempts to each run, all rejected with local HTTP 503 before upstream
inference. The terminal requests were `8bbc7cd9` and `f067c74a` at 02:30:09 and
02:33:42 UTC.

Will's selector considered all three accounts. None was eligible:

| Account label | Evidence before recovery |
| --- | --- |
| iCloud | Access token expired November 12 at 06:46:59 UTC. Renewal returned HTTP 400 `invalid_grant` with `Refresh token expired`. |
| Gmail | Exact Opus upstream 429 at 02:09:02 UTC, with credential-wide `seven_day` rejection. Upstream reset was November 18 at 02:00 UTC. |
| Pro | Exact Opus upstream 429 at 02:16:38 UTC, with credential-wide `five_hour` rejection. Upstream reset was November 14 at 05:20 UTC. |

Labels were resolved from actual `t3-icloud`, `t3-gmail`, and `t3-pro` credential
prefixes, not account-address domains. The separately maintained Mac iCloud
credential was valid. Its available capacity could not repair Will's rejected
credential. Will's usage-endpoint HTTP 429 did not establish inference-quota
exhaustion for iCloud.

Both hosts ran `v8.0.66-fleet.1`, deployed source
`815987ae113117253b37c7c0755e209fadd01372`. Both fleet verifiers passed.
The local checkout was `main` at
`caca0a047155bc3b53d9eb0c199cde93d51c0db6`; the reviewed server files matched
the deployed source byte for byte.

## Why the error was misleading

The selector rejects expired access tokens before applying quota-aware
weights. When candidates have mixed failure reasons, the error selection
prefers a model-level failure over a credential-level failure. The resulting
`auth_unavailable` message displayed a rate-limit error and concealed iCloud's
renewal failure. There was no evidence of faulty rotation.

At the deployed source, `isTerminalRefreshGrantMessage` recognizes
`refresh_token_expired`, but misses the spaced description `Refresh token
expired`. Bare `invalid_grant` is terminal only for Codex in that classifier.
Consequently, this Claude credential reported `retry_scheduled` and
`requires_sign_in=false` despite its explicit expired-refresh response.

The relevant source is `sdk/cliproxy/auth/selector.go:1223`,
`quota_selector.go:66`, `conductor_selection.go:898`, `scheduler.go:1552`,
`conductor_cooldown.go:2562`, and `conductor_refresh.go:1582`. File hashes and
the deployed pin are recorded in [source-identities.json](source-identities.json).

## Recovery and verification

Andrew authorized the re-login with “help me do that from m5 here.” The existing
Will container ran its supported Claude OAuth login. Andrew received the login
URL untouched and supplied the callback through `request_secret`. Token exchange
and persistence occurred on Will. The callback was not retained in these records.

Will adopted iCloud with its original account identity, `t3-icloud` prefix,
0600 permissions, and a new access-token expiry of November 14 at 10:47:36 UTC.
The running proxy reported it active, available, without a refresh error or
Opus cooldown, and registered for both shared and named Opus routes.

Existing requests `2996063b`, `1983b4c2`, `cb9490c6`, and `e80bef5f` selected
iCloud for unprefixed `claude-opus-5-5`. Each recorded upstream and downstream
HTTP 200, a downstream `message_stop`, and no downstream error event. They
completed between 02:47:48 and 02:47:59 UTC. This session neither created nor
resumed those requests. Their success does not establish completion of the
separate rotary-design task.

Mac Claude credential files and Will's Gmail file were unchanged. Pro's
credential renewed concurrently at its normal four-hour lead time, with its
quota block preserved. The original whole-file comparison recorded that
difference; it was not rewritten into an unchanged-file claim. Will's container
identity, start time, configuration, and environment file were unchanged.
All three account identities and prefixes were preserved.

The login and SSH processes exited, and the callback pipe and consumed secret
file were removed. A read-only closeout check at 03:14:25 UTC still found iCloud
active, with no refresh error and a last verified inference at 03:08:11 UTC.

## Boundaries and closeout

The operational recovery is complete. The first automatic iCloud renewal after
the fresh login remains unobserved; no wait or forced refresh was requested.
The cause of the upstream grant expiry is unknown. The diagnostic classifier
issue remains unchanged and is a possible separate code task, not a pending
operation in this recovery. No deployment or publication was authorized.

The unrelated edits to `ops/CLAUDE_USAGE_RESETS.md`,
`ops/claude_usage_resets.py`, `ops/test_claude_usage_resets.py`, and the existing
untracked cache were preserved. This evidence commit changes no server code,
tests, credentials, or live settings.

[receipt.json](receipt.json) preserves the selected observations, failed-request
hashes, successful-request hashes, preservation checks, and closeout state.
[SHA256SUMS.json](SHA256SUMS.json) identifies the committed evidence files.

What changes for the operator: Will can use its iCloud credential again, and
existing shared-pool Opus traffic has already succeeded through it.
