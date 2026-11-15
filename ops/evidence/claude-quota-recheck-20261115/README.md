# Claude account upgrade recovery activation

Andrew approved deploying the reviewed fix to Mac first, verifying the account
and shared pool, then deploying to Will. Both hosts now run
`v8.0.66-fleet.3` from source `4b93cbb545d0f37fe2fdf619f91dd58e0e2f9955`.
The source and release commits are local; no upstream contribution was published.

GPT-6.1 Sol implemented and deployed the change. GPT-6 Astra independently
reviewed the server recovery, deployment steps, and final streaming correction.

The first Mac candidate, fleet.2, returned HTTP 502 from inference verification
because its native Claude request omitted `stream: true`. The server preserved
the existing cooldown. Mac was rolled back, the missing wire field received a
failing-before regression, and Astra reviewed the correction before fleet.3.
Will was not changed until corrected Mac verification passed.

On each host, the upgraded third account reported Max 20x. Account-pinned
verification completed, released the stale credential quota, and ended active
with no cooldowns. Normal named-account and shared-pool Opus 5.5 requests each
returned HTTP 200, completed, and produced the exact text `POOL_OK`.
Fleet verifiers passed on both hosts, and Will's owner verifier passed.
All three unrelated Mac containers and 64 unrelated Will containers were
preserved, including their start times.

The [sanitized receipt](activation-summary.json) records checks, image identities,
source revision, activation times, and rollback paths. It contains no OAuth
credentials, account emails, organization identifiers, or keys.
Private working artifacts and the failed attempt are retained under
`ops/runtime/claude-upgrade-recheck-20261114/` on Mac. Will build artifacts are
under `/srv/share/drives/disk12/cliproxy-claude-recheck-fleet3-20261115/artifacts/`.

Source and release metadata local-ci runs each passed two jobs. Focused quota
race checks, complete auth and management package tests, and 24 Python tests
passed. The complete Go suite passed with three untouched-HEAD reproduced
failures excluded: WebRTC DataChannel creation, websocket temporary-directory
cleanup, and heartbeat failover. This is a validation workaround. New regression
tests and expected release-image pins changed; existing assertions were not
weakened. The initial read-only source mount also prevented one watcher test
from creating its fixture directory; a disposable writable source copy fixed
that validation environment.

No reset grants were redeemed, no credentials were copied between hosts, and
the original T3 thread was not resumed. Live checks establish proxy capacity,
not completion of the original thread's separate work.

What changes for the operator: the upgraded account is usable through both
shared pools, and future upgrades have a guarded account recheck command.
