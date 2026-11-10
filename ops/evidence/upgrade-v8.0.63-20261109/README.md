# CLIProxy v8.0.63 fleet activation

GPT-6.1 Sol prepared and deployed `v8.0.63-fleet.1` on November 9, 2026 Pacific time. Andrew authorized the work with "ok finish/deploy commit etc". The runtime source is `87e90c0c7629fd2d667ef5be13e2fc775005d5d8`. The source merges upstream v8.0.63 with the existing fleet policies and Codex quota fixes. Existing upstream provenance is preserved.

Will was deployed first at 02:51 UTC on November 10. The Mac followed at 02:53 UTC after Will's fleet verifier, owner verifier and exact Luna response passed. Both live fleet verifiers report no failures. Each host returned HTTP 200 and the expected text for one `gpt-6-luna` request, consuming 47 tokens per host. The Mac also read Will's authenticated catalog over its Tailscale address.

The staging reports show no production credentials, no published ports, no network access, unchanged plugin hashes, and successful plugin registration on both architectures. The preservation reports show unchanged configuration contents, inodes and modes, matching mounted configuration hashes, loopback ports, state mounts, and restart policy. One pre-existing one-shot MCP container on Will was absent at the final snapshot. Its cause is unknown. The rollout commands stopped only `cli-proxy-api`. Other observed baseline containers retained their IDs and images.

The required compile, focused quota race tests and local CI passed. Local CI ran both jobs against upstream v8.0.63 as the comparison base. That checks the fleet changes without attributing upstream's own translator edits to this fork. No workflows changed.

The full Go suite is not green. `TestPionMediaRelayBridgesAudioAndDataChannel` and `TestServerResponsesWebsocketReauthorizesEachTurn` also fail on untouched upstream v8.0.63 under the same Linux environment. All remaining tests passed with those two excluded. This is a validation workaround. New upstream cooldown test expectations were adapted to the existing one-minute retry-header policy, two local test calls use the new context argument, alias coverage checks cooldown migration and recovery, and owner-contract test hashes match the new images.

Mac fleet-routing tests passed. Will owner-verifier tests passed on Will, where PyYAML is installed. The Mac's system Python lacks that dependency, so that host-specific test invocation could not run there. PiSec inventory validation and 39 inventory tests passed. Inventory commit `b8d1dc4` is local on its existing branch. That branch contains unrelated unpublished history and edits, so it was not pushed by this rollout.

`activation.json` records the image IDs, private backup directories, checks and qualification limits. `build-receipt.json` records the binary and source archive hashes. `source-integrity.json` confirms that the Linux build source still matches the exact committed archive. No OAuth credentials, configuration values or request logs are included here.

The retained fleet.4 images support rollback. Restore only the host's image pin and matching Compose, manifest and owner-contract metadata from its private backup. Preserve current configuration and refreshed OAuth files. Run `docker compose up -d --no-deps --pull never --no-build cli-proxy-api`, then the fleet verifier, Will owner verifier and authenticated client checks.

No native T3 turn, exhaustive account rotation, OAuth refresh restart, or full provider qualification was performed. No independent agent review was run. The parent performed source review and direct checks without subagents, as Andrew's standing instructions require.
