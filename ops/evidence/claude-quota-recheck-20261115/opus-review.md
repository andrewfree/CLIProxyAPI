# Opus review and closeout

Claude Opus 5.5 independently reviewed deployed source `4b93cbb5` and project
documentation at `8087828a` through the subscribed Will CLIProxy pool. It found
no correctness bugs and judged the deployment safe to keep running on Mac and
Will. No runtime change or redeployment was required by this review.

Commit `19a77f79` addresses its documentation and coverage findings:

- The documented Go test selector now includes all `TestClaudeQuota` tests.
- The historical 24-test Python result is identified as a working-tree run with
  the user's pre-existing test edits. A clean committed export passes 21 tests
  with the same three-module command, or 20 with the documented two-module subset.
- Direct account-pinned verification and optional post-OAuth verification are
  documented as recovery entry points. Only the helper checks the expected tier.
- Added tests cover pure credential propagation, disabled sibling shadows,
  rejected quota windows despite overall allowed status, scheduler and registry
  recovery, and native `message_stop` completion.

One conservative behavior is retained and documented. A rejected scoped unified
quota window can prevent early recovery even when another model could run.
The review did not establish whether Opus responses actually report that case,
so the guard was not relaxed on an assumption.

The parent ran all ten focused quota tests with the race detector, the complete
auth package, the server build, and the clean committed Python check. Local-ci
passed both jobs for `19a77f79`. The updated runbook and recheck instructions
were synchronized to Will and verified by file hashes. Both running proxies
remain on `v8.0.66-fleet.3`, source `4b93cbb5`.

The review was read-only and published no findings to a repository host. Raw
review output and dispositions remain in the private local runtime directory.
All task code, regression tests, project documentation, and sanitized activation
evidence are committed locally under Andrew's Git identity. Unrelated existing
usage-reset edits remain untouched. No upstream contribution was published.

What changes for the operator: both pools retain the verified recovery fix,
and the project documents the review findings and the remaining conservative
quota behavior.
