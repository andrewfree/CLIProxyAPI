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
