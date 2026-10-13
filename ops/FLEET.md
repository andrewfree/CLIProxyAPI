# CLIProxy fleet

Will is the always-on primary at `http://100.98.74.67:8317`. This address is private to the Tailscale network; Tailscale encrypts transport. Will's container remains bound to loopback. Tailscale Serve forwards TCP 8317 to it. The M5 Mac runs a local fallback at `http://127.0.0.1:8317`.

`fleet-release.json` pins one application source commit, one Cursor plugin version, and the exact platform image and plugin hashes. Both images are built from the same commit. ARM64 runs on the Mac's Docker VM; AMD64 runs on Will. Compose uses immutable local image IDs with `pull_policy: never`, so a restart cannot silently select a new release. The two Compose files and owner contracts must be changed together for the next release.

Keep host configuration, logs, and OAuth credentials local to each instance. Never synchronize rotating OAuth tokens between active servers. The Mac has an additional iCloud Claude account that Will does not have. Its named account route stays on the Mac. Changing a named account route must never select another account implicitly.

Plugins are enabled on both hosts. The Cursor provider is `auth-cursor` 2.1.1; its provider key is `cursor`. Plugin archives were verified against the publisher's registry before extracting the shared libraries. The libraries are persisted under each instance's `plugins/linux/<arch>/` directory. The plugin uses the official Cursor SDK Bridge and supports one API key file per Cursor account, round-robin selection, and optional weighted routing. No Cursor API keys were present at preparation time.

Add an account on the host that will own it using the supported interactive prompt:

```sh
docker exec -it cli-proxy-api /CLIProxyAPI/CLIProxyAPI --config /CLIProxyAPI/config.yaml --cursor-login
```

Create the key in the Cursor dashboard. Keep it out of shell arguments and chat. Account Privacy Mode applies to SDK requests. The plugin has no separate telemetry switch; no analytics client was found in its source. Both services set `DO_NOT_TRACK=1`, and CLIProxy usage aggregation is disabled.

For a future update, create one new release manifest, build its exact source on both platforms, verify both plugin artifacts, stage both configurations without production auth, run the app and contract checks, then recreate only the two proxy containers. Check the running image IDs, source revision labels, health endpoints, registered plugin version, authenticated model catalogs, and client routes against the manifest. Keep the previous images and private configuration backups for rollback.

The October 13 source build is `d4fa77254e2922a834f6f97ddac0d7a88249b9ad`, based on upstream v8.0.27. It preserves the local Kimi model fallback feature. The runtime backups are `/Users/rever/cliproxy/ops/runtime/v8.0.27-fleet.1/backup` and `/home/andrew/cliproxy/releases/v8.0.27-fleet.1/backup`. No OAuth credential directory is replaced during upgrade or rollback.
