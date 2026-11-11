# CLIProxy v8.0.66 fleet release

Release v8.0.66-fleet.1 merges upstream v8.0.66 with the maintained fleet quota-aware selector, one-hour quota cooldown cap, one-minute outgoing retry-header limit, Kimi OAuth catalog fallback, and preserved upstream error selection. Runtime source is `815987ae113117253b37c7c0755e209fadd01372`.

Andrew authorized preparation, commits, live deployment, and verification with "do it and get everything commit/live/deployed/working tested etc." No upstream contribution or agent delegation is part of this release.

The required compile, focused quota/cooldown/scheduler race tests, 24,206 remaining Go tests, and both local-ci jobs passed for the runtime source. The full Go suite has two failures that also reproduce on untouched v8.0.66 in the same builder: `TestPionMediaRelayBridgesAudioAndDataChannel` and `TestServerResponsesWebsocketReauthorizesEachTurn`. Running the remaining suite with exactly those two exclusions is a validation workaround. The new upstream Claude cooldown-header test was adapted to the existing fleet rule that omits Retry-After values over one minute while preserving authoritative Anthropic reset headers.

Both Linux binaries were built on Will with the existing Go 1.26.10 cross-builder and shared cache. The AMD64 image was assembled on Will. The ARM64 image was assembled on the Mac from the same exact source binary after Will's Docker engine could not create a snapshot from the imported ARM64 parent. No compiler or new tool was installed. Both images passed isolated health, routing, plugin registration/hash, no-auth, no-network, no-published-port, and analytics-off checks.

BuildKit's local-ID interpretation failure and the imported ARM64 parent snapshot failure are retained in ignored runtime logs. Completed binaries were reused after checking their recorded hashes; no failed image was deployed.

Will was activated at 05:21 UTC and the Mac at 05:27 UTC on November 11, corresponding to November 10 Pacific time. Both exact source and image checks passed with health healthy, all three Claude credentials present, unchanged policies, and analytics off. Each host returned the exact expected reply from pooled gpt-6-luna and the pinned t3-gmail/claude-fable-5-2 route. Each Claude account returned HTTP 200 for profile and usage. The Mac read Will's authenticated catalog. The monitor verification reports three distinct accounts with matching current quotas.

The first Claude smoke request used a 32-token output budget without disabling thinking and did not return the expected text. The bounded retry explicitly disabled thinking and used 128 output tokens. Both hosts returned end_turn, the requested model, and the expected text. The first failed check is not counted as a pass.

The initial monitor verifier compared a historical curve point against current usage and failed after Gmail usage advanced. SessionWatcher's latest-observation file and visible menu preview both showed the new value. The verifier now compares that current observation; it does not rewrite application data or loosen quota equality.

Configuration contents, inode and permissions, loopback ports, state mounts, restart policy, and plugin hashes passed preservation checks. The Mac had no missing unrelated containers. Five transient baseline containers on Will were absent later, including an auto-removed build container and four MCP web-research containers. Their disappearance does not prove causation; rollout commands targeted only the proxy and owned staging/build containers. Present unrelated containers retained their IDs and images.

PiSec shared inventory commits a596403 and 62370cc are published to codeone/pisec master. The focused inventory change was also applied locally as fbaf16b to the existing Windows branch without publishing its unrelated history or changing its four dirty Windows files. Inventory tests passed on both the shared-base worktree and the active checkout.

No independent agent review, forced OAuth rotation, exhaustive account selection, or full provider qualification was run. The current T3 session continued through its existing Mac proxy route after deployment. Source review and direct verification were performed by GPT-6.1 Sol without delegates.

Private rollback backups and retained previous images are listed in activation.json and ops/FLEET.md. Restore image/owner metadata only and preserve newer config and OAuth state.
