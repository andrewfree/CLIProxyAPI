# Fleet.5 build, November 25, 2026

`v8.0.66-fleet.5` is built from `312f2ef991d0ab18d4daea3afaa27751d9473d2a` on [PR #41](https://github.com/andrewfree/CLIProxyAPI/pull/41), after the Kimi K3.1 Pro and Claude Fable 5.2 review fixes. Its only runtime change from fleet.4 is per-model failure reporting. Each credential keeps its latest upstream failure per model: HTTP status, code, upstream `error.type` and time. `GET /v8/management/credentials` reports it as `model_failures`.

| Arch | Image | Binary sha256 | Parent (fleet.4) |
| --- | --- | --- | --- |
| amd64 (Will) | `sha256:c403377e4da97875909ba37506bb2a69c3a55fd1731d204cce4f331c105fedec` | `954504e853b38b171ff2b37c837d71b6f9ef9285dcfaef8ffaa2438387c4ac53` | `sha256:9e9da16e283e20da4a534d1397d4b6433a5708f4f04b6faf2d03b1367d357af6` |
| arm64 (Mac) | `sha256:dd6522aa27e21d3aab83e98cae2899b23125b873040765bcac08fa35af052876` | `52420a1af3d0655f514bc053f0c1cd74d2009dab56f080abfe1caa53f84eab82` | `sha256:f57649a96d5b1e8404309a5d5a25da050ea000c68f00f937f29b153a838e774f` |

## Build

Both binaries were compiled on Will in `cliproxy-cross-builder:20261102` (Go 1.26.10, `aarch64-linux-gnu-gcc` 12.2) with `--network none` and `GOPROXY=off`. The input was a `git archive` of the source (sha256 `b61f6e7bdad930f68aba14e42bb7cc8e46ffc1c6394a40ef63c8dd9fe13fd6fc`), and the build date is `2026-11-25T06:53:55Z`. `go.mod` is unchanged since fleet.4, so the existing module cache covered the build. In the same builder, `go test ./sdk/cliproxy/auth/ ./internal/api/handlers/management/ ./internal/runtime/executor/` passed. Will assembled the amd64 image. The arm64 binary was copied to the Mac and its hash checked, and the Mac assembled the arm64 image from it, as for v8.0.66-fleet.1 on November 10.

An earlier fleet.5 build from `e0f48107` (amd64 `sha256:ed4be9e8…`, arm64 `sha256:53866f9a…`) passed staging, but the final review found that streaming Claude Fast failures lost their error type. Those images were never deployed and were deleted. Their build folder is `/srv/share/drives/disk12/cliproxy-v8.0.66-fleet.5-20261125` on Will. The first run in that folder stopped at its own guard before compiling, because a November 9 binary already used the planned cache file name. Its logs are under `artifacts/attempt1/`.

## Staging

`qualify_staging.py` is the fleet.4 qualifier with only its container name and synthetic probe token renamed. Each image passed it on its own host: `arm64 staging: passed` on the Mac and `amd64 staging: passed` on Will. It checks:

- loopback health and the version header
- the binary commit and OCI revision
- the image ID and binary hash
- `auth-cursor` 2.1.1 registration and its library hash
- analytics off
- no credentials, no published ports and no writable binds
- removal of the staging container

Staging makes no model requests; the activation's inference gate covers that.

## Files

- `build_fleet5.py` is byte-identical to the copy run on Will under `/srv/share/drives/disk12/cliproxy-v8.0.66-fleet.5-20261125-r2/artifacts/`.
- `qualify_staging.py` is byte-identical to the copies run on each host.

## Activation

Andrew approved with `promote fleet model-failures`: Mac first, a live inference gate, then Will, rolling back only a host that fails. PR #41 merged into `m5-ops` as `e0c5afc7` with a merge commit, so the source commit stays in `m5-ops` history.

| Host | Activated (PST, Nov 24) | Image | Previous image kept for rollback |
| --- | --- | --- | --- |
| Mac (arm64) | 23:30:54 | `sha256:dd6522aa…` | `sha256:f57649a96d5b1e8404309a5d5a25da050ea000c68f00f937f29b153a838e774f` |
| Will (amd64) | 23:33:01 | `sha256:c403377e…` | `sha256:9e9da16e283e20da4a534d1397d4b6433a5708f4f04b6faf2d03b1367d357af6` |

Steps:

1. The final review found that the hourly fleet monitor would send a parity-drift alert while the hosts and the shipped bundle disagreed on the version. So `com.rever.cliproxy-fleet-monitor` was booted out at 23:30 and bootstrapped again at 23:41:24.
2. On each host, the activation saved private 0600 backups of `.env`, the Compose file, the manifest, the workload contract and the container list. It then changed `CLI_PROXY_IMAGE` in `.env` in place and ran `docker compose up -d --no-deps --pull never --no-build cli-proxy-api`.
3. On the Mac, `~/cliproxy` moved from PR #26's branch to a local `m5-ops` tracking `fork/m5-ops`, which changed only the Compose default image line. The watchdog was paused with `ops/runtime/watchdog.pause` until the bundled watchdog had recorded the new Compose activation receipt.
4. Will first received the merged Compose file, manifest and workload contract. Each of Will's copies matched the fleet.4 versions before the change.
5. Each container was healthy within about 10 seconds.

| Check | Mac | Will |
| --- | --- | --- |
| `verify_fleet_instance.py` | no failures | no failures |
| Will owner verifier `verify_live.py` | not applicable | `config_policy_drift`, from before the activation: see below |
| Inference gate (fleet.4 `inference_gate.py`, unchanged) | Claude gate failed on account state, see below; Kimi exact reply passed | passed |
| New field | `model_failures` lists `claude-haiku-5-5` HTTP 429 `rate_limit_error`; `status --host local` prints it | `model_failures` present and empty after the restart |

On Will the gate's four Claude Code-shaped `claude-haiku-5-5` requests returned the exact reply with `end_turn`:

- a pool turn (streaming)
- a second pool turn (non-streaming)
- a named `t3-gmail` turn
- a subagent turn

The second turn read the 37,545 cached tokens the first wrote, and all four logged requests carried one injected date.

### Exceptions found during activation

- **Mac Claude accounts.** The Mac's iCloud credential and one Gmail credential fail every refresh with `invalid_grant`, starting at 00:38:44 UTC on November 25 (16:38 PST on November 24), seven hours before the activation. The Mac's other two Claude credentials returned HTTP 429 from Anthropic. No Claude request can succeed through the Mac with either release. The gate's own failure lines show Anthropic's 429 and the proxy's `auth_unavailable`. A non-streaming `kimi-k2.8-code` request through the Mac then returned the exact reply. The first Kimi try returned empty text because its 64-token budget was below the 69 tokens the reasoning model used; with 1,024 it passed. The Mac's Codex account was at its weekly usage limit. The Mac stayed on fleet.5. A rollback cannot fix account state, and the Mac proxy is the paused fallback. New Mac Gmail and iCloud logins are needed. Each host keeps its own OAuth files.
- **Mac catalog.** After the restart, the Mac serves 109 models against Will's 169. The 60 missing are the Claude models of the two `invalid_grant` credentials: 20 under `t3-gmail/`, 20 under `t3-icloud/`, and 20 unprefixed. `fleetctl parity` reports this as `P8/shared` and `P7/credential-error`. The named `t3-gmail` and `t3-icloud` T3 profiles route to Will, so they are unaffected.
- **Will owner verifier.** `logs-max-total-size-mb` is 61440 in Will's `config.yaml`, which was last changed at 18:25 PST on November 23, against 1024 in `ops/instances/will/config-policy.json`. [PR #40](https://github.com/andrewfree/CLIProxyAPI/pull/40) aligns the policy.
- **Bundle.** `fleetctl ship --host m5 --commit e0c5afc7 --apply` also carried PR #34's unshipped `archive_logs.py`, `fleet_bundle.py` and log-archive plist. The log-archive job was disabled and unloaded before and after the ship. P0 now reports `P0/com.rever.cliproxy-log-archive` until PR #34's plist is installed, which is that PR's own activation step. The previous bundle is `~/cliproxy-live/prev/20261125T073959577678Z-qzwskdvg`.
- **Parity.** After the ship there is no `P1/version` drift. `P2/host-pin` and `P2/shared-pin` were already in the monitor's report before the activation.

## Rollback

Roll back only the affected host:

1. Restore `.env` from the private backup: `ops/runtime/fleet5-activation-20261125/backup-20261125T073029Z/` on the Mac, or `ops/runtime/fleet5-activation-20261125/backup-20261125T073252Z/` on Will. On Will, also restore the Compose file, manifest and workload contract.
2. Run the same `docker compose up` command.
3. On the Mac, run it with `ops/runtime/watchdog.pause` in place, and record the Compose receipt again afterward.
4. Run both verifiers.

Config and OAuth files are not part of the rollback. To return the Mac bundle as well, disable and unload the log-archive job first, as `ops/FLEET.md` requires before any bundle rollback, then run `python3 ops/fleetctl.py ship --host m5 --commit 18e4f8e14db64b9a501ee338e784911ce0b7f6ef --apply` from a worktree at that commit, which is the bundle that ran before this ship.
