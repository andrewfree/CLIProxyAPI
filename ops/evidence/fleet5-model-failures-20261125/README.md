# Fleet.5 build, November 25, 2026

`v8.0.66-fleet.5` is built from `e0f4810785231d68e0eb62fb6d17d1adcc4b3647`, the head of [PR #41](https://github.com/andrewfree/CLIProxyAPI/pull/41) after the Kimi review fixes. Its only runtime change from fleet.4 is per-model failure reporting: each credential keeps its latest upstream failure per model (HTTP status, code, upstream `error.type`, time), and `GET /v8/management/credentials` reports it as `model_failures`.

| Arch | Image | Binary sha256 | Parent (fleet.4) |
| --- | --- | --- | --- |
| amd64 (Will) | `sha256:ed4be9e845b66fee9d55cdf8823a19ae76efbc926e0cb5820c9892575035d767` | `ffc888abe63ca46942ecfebefb34662f01cc44f1fc4fdb067eb91b50ee8eb227` | `sha256:9e9da16e283e20da4a534d1397d4b6433a5708f4f04b6faf2d03b1367d357af6` |
| arm64 (Mac) | `sha256:53866f9a054f503710af8f263822b2c2bb15ceadf7e7b7a6e68666dc2090e446` | `10ca92ffdbc90cc5a53560c29f72a9c149095a14bfd7136a7f4b56860502cae0` | `sha256:f57649a96d5b1e8404309a5d5a25da050ea000c68f00f937f29b153a838e774f` |

## Build

Both binaries were compiled on Will in `cliproxy-cross-builder:20261102` (Go 1.26.10, `aarch64-linux-gnu-gcc` 12.2) with `--network none` and `GOPROXY=off`, from a `git archive` of the source (sha256 `f14930be179f2b1a988775e39a8634d1c73495879a1cd112d71b29fcbfa43012`, build date `2026-11-25T06:34:08Z`). `go.mod` is unchanged since fleet.4, so the existing module cache covered the build. In the same builder, `go test ./sdk/cliproxy/auth/ ./internal/api/handlers/management/ ./internal/runtime/executor/` passed. Will assembled the amd64 image. The arm64 binary was copied to the Mac, its hash checked, and the Mac assembled the arm64 image from it, as for v8.0.66-fleet.1 on November 10.

The first build run stopped at its own guard before compiling, because a November 9 binary already used the planned cache file name. The script now uses `CLIProxyAPI-v8066-fleet5-<arch>`, and the first run's logs are kept under `artifacts/attempt1/` on Will.

## Staging

`qualify_staging.py` is the fleet.4 qualifier with only its container name and synthetic probe token renamed. Each image passed it on its own host: `arm64 staging: passed` on the Mac, `amd64 staging: passed` on Will. It checks loopback health, version header, binary commit and OCI revision, image ID and binary hash, `auth-cursor` 2.1.1 registration and library hash, analytics off, no credentials, no published ports or writable binds, and container removal. Staging makes no model requests; the activation's inference gate covers that.

## Files

- `build_fleet5.py` is byte-identical to the copy run on Will under `/srv/share/drives/disk12/cliproxy-v8.0.66-fleet.5-20261125/artifacts/`.
- `qualify_staging.py` is byte-identical to the copies run on each host.
