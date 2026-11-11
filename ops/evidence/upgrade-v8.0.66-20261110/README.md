# CLIProxy v8.0.66 fleet release

Release v8.0.66-fleet.1 merges upstream v8.0.66 with the maintained fleet quota-aware selector, one-hour quota cooldown cap, one-minute outgoing retry-header limit, Kimi OAuth catalog fallback, and preserved upstream error selection. Runtime source is `815987ae113117253b37c7c0755e209fadd01372`.

Andrew authorized preparation, commits, live deployment, and verification with "do it and get everything commit/live/deployed/working tested etc." No upstream contribution or agent delegation is part of this release.

The required compile, focused quota/cooldown/scheduler race tests, 24,206 remaining Go tests, and both local-ci jobs passed for the runtime source. The full Go suite has two failures that also reproduce on untouched v8.0.66 in the same builder: `TestPionMediaRelayBridgesAudioAndDataChannel` and `TestServerResponsesWebsocketReauthorizesEachTurn`. Running the remaining suite with exactly those two exclusions is a validation workaround. The new upstream Claude cooldown-header test was adapted to the existing fleet rule that omits Retry-After values over one minute while preserving authoritative Anthropic reset headers.

Both Linux binaries were built on Will with the existing Go 1.26.10 cross-builder and shared cache. The AMD64 image was assembled on Will. The ARM64 image was assembled on the Mac from the same exact source binary after Will's Docker engine could not create a snapshot from the imported ARM64 parent. No compiler or new tool was installed. Both images passed isolated health, routing, plugin registration/hash, no-auth, no-network, no-published-port, and analytics-off checks.

BuildKit's local-ID interpretation failure and the imported ARM64 parent snapshot failure are retained in ignored runtime logs. Completed binaries were reused after checking their recorded hashes; no failed image was deployed.

Live activation receipts and final validation will be recorded here after both server gates pass.
