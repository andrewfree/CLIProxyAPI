# Fleet.4 activation, November 18, 2026

`v8.0.66-fleet.4` is built from `4a1d49c3c839d97411a6a29b5daa18e16212c3bf`, the m5-ops tip after PR #5. Its only runtime change from fleet.3 is the cloaked Claude date pin: it is keyed to a hash of the client's `X-Claude-Code-Session-Id`, shared by that session's subagents, so the injected date no longer flips when a conversation's first assistant reply appears. Pins landed in PR #8. Andrew approved activation on November 18: Mac first, a live inference gate, then Will.

| Host | Image | Activated (PST) | Previous image kept for rollback |
| --- | --- | --- | --- |
| Mac (arm64) | `sha256:f57649a96d5b1e8404309a5d5a25da050ea000c68f00f937f29b153a838e774f` | 10:00:32 | `sha256:43c85866015ac08365741c004a033a56e42ccc5b89d999bcc6d83425b2aad5af` |
| Will (amd64) | `sha256:9e9da16e283e20da4a534d1397d4b6433a5708f4f04b6faf2d03b1367d357af6` | 10:01:57 | `sha256:bd8ebc2b1ee329d88ac4ada235aaaa2744856740933e404966ec8a9eca18f473` |

## Qualification before activation

GPT-6.2 Sol built both images from a clean `git archive` of the source and ran each in an isolated loopback staging container with an empty auth directory: health 200, version header, binary commit and OCI revision, `auth-cursor` 2.1.1 with its pinned library hash, usage aggregation off with `DO_NOT_TRACK=1`, and the date-pin regression tests on the exact source. Claude Opus 5.5 also ran `go test ./internal/runtime/executor/... ./sdk/cliproxy/...` on the merged source. Claude Fable 5.2 and Claude Opus 5.5 reviewed the pins and required a live inference gate, because staging made no model requests (fleet.2 passed health checks and failed inference).

## Activation and checks

Each host: preflight (fleet.3 running, fleet.4 image present with the right labels, one `CLI_PROXY_IMAGE` line in `.env`), private 0600 backups of `.env` and Compose files, an in-place `.env` edit, then `docker compose up -d --no-deps --pull never --no-build cli-proxy-api`. The Mac watchdog was paused for the recreate and resumed after. Will received the merged Compose file, manifest and workload contract first. Each container was healthy 8 seconds after the recreate.

| Check | Mac | Will |
| --- | --- | --- |
| `verify_fleet_instance.py` | no failures, `v8.0.66-fleet.4`, source `4a1d49c3` | no failures |
| Will owner verifier `verify_live.py` | not applicable | no findings |
| Plugins | `auth-cursor` 2.1.1 registered | `auth-cursor` 2.1.1 and `cliproxybar-quota` 0.1.0 registered and enabled |
| `config.yaml` | unchanged | unchanged |
| Unrelated containers | 3 unchanged | 61 persistent unchanged; only auto-removed one-off MCP search containers changed |
| Inference gate | passed | passed |

The inference gate sends four Claude Code-shaped requests in one client session through the host: a pool turn (streaming), a second pool turn (non-streaming), a named `t3-gmail` turn (streaming) and a subagent turn (streaming, `X-Claude-Code-Agent-Id`). On both hosts each returned HTTP 200, `end_turn` and the exact expected text, every request in the session carried the same injected date (2026-11-18, the Los Angeles date), and the second turn read 37,574 cached tokens. The same gate also passed on fleet.3 just before activation. The Mac baseline's first turn wrote 37,545 cached tokens, Will's baseline read that same prefix because both hosts use the same account, and each host's first fleet.4 turn read exactly those 37,545, so the switch kept the cached prefix. Only `t3-gmail` had weekly quota at the time.

After activation, `fleetctl parity` reported no configuration drift and one `P7/credential-error` on Will. It is a Codex account with a `model_not_supported` cooldown for one model, set at 03:17 PST before activation. After a restart that cooldown is the account's only model state, so the account-level summary reads unavailable, but the selector still serves the account for every other model (`sdk/cliproxy/auth/selector.go:1259-1290`). The cooldown ends at 15:17 PST.

## Rollback

On the affected host only: restore `.env` (and on Will the Compose file, manifest and workload contract) from the private backup under `ops/runtime/fleet4-activation-20261118/`, then run the same `docker compose up` command and both verifiers. Config and OAuth files are not part of the rollback.
