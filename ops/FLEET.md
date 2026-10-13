# CLIProxy fleet

Will is the always-on primary at `http://100.98.74.67:8317`. This address is private to the Tailscale network; Tailscale encrypts transport. Will's container remains bound to loopback. Tailscale Serve forwards TCP 8317 to it. The M5 Mac runs a local fallback at `http://127.0.0.1:8317`.

`fleet-release.json` pins one application source commit, one Cursor plugin version, and the exact platform image and plugin hashes. Both images are built from the same commit. ARM64 runs on the Mac's Docker VM; AMD64 runs on Will. Compose uses immutable local image IDs with `pull_policy: never`, so a restart cannot silently select a new release. The two Compose files and owner contracts must be changed together for the next release.

Keep host configuration, logs, and OAuth credentials local to each instance. Never synchronize rotating OAuth tokens between active servers. Both hosts have separate Gmail and iCloud Claude credentials, selected by the `t3-gmail` and `t3-icloud` prefixes. Changing a named account route must never select another account implicitly.

Each host's `auths/` directory is bind-mounted at `/root/.cli-proxy-api`; both Claude credential files contain refresh tokens and have mode `0600`. CLIProxy loads these files on startup and refreshes credentials automatically. Both containers use the `always` restart policy, and Will's Docker and Tailscale services are enabled at boot. After a fresh login, check the new credential file's permissions and restrict it to `0600` if necessary. These persistence settings and mounted files were verified without restarting active clients.

Plugins are enabled on both hosts. The Cursor provider is `auth-cursor` 2.1.1; its provider key is `cursor`. Plugin archives were verified against the publisher's registry before extracting the shared libraries. The libraries are persisted under each instance's `plugins/linux/<arch>/` directory. The plugin uses the official Cursor SDK Bridge and supports one API key file per Cursor account, round-robin selection, and optional weighted routing. No Cursor API keys were present at preparation time.

Add an account on the host that will own it using the supported interactive prompt:

```sh
docker exec -it cli-proxy-api /CLIProxyAPI/CLIProxyAPI --config /CLIProxyAPI/config.yaml --cursor-login
```

Create the key in the Cursor dashboard. Keep it out of shell arguments and chat. Account Privacy Mode applies to SDK requests. The plugin has no separate telemetry switch; no analytics client was found in its source. Both services set `DO_NOT_TRACK=1`, and CLIProxy usage aggregation is disabled.

Run `python3 ops/verify_fleet_instance.py` from each instance root to check its live image, source revision, plugin checksum, version, health, and analytics settings against the shared manifest. The verifier prints no credentials.

T3 on the Mac has explicit `Will primary` and `Mac fallback` entries for the shared Codex pool and both Claude accounts. Selecting a fallback is manual. New sessions use the selected endpoint; an already running client keeps its launch environment. Native provider entries retain their direct subscription routes. Cursor is reserved for Cursor-only models and Gemini under the owner's model-routing policy.

M1 is a client, not a third proxy host. On October 13, authenticated requests from M1 to Will verified the model catalog and both Claude account routes. The check supplied the existing client key in memory; it did not save credentials or create T3 profiles on M1. M5's proxy remains loopback-only, so M1 cannot use it directly as a fallback. M1's SSH path to M5 also lacks a trusted host-key entry. M1 client setup and M5 fallback access were left unchanged at the owner's request to limit that follow-up to access checks.

The Will owner verifier additionally checks the exact private IPv4 and IPv6 listeners, the Tailscale Serve forwarding target, and all six loopback Docker bindings. PiSec records the private forwarding service separately from the Docker workload. The primary uses Will's own credentials and local storage, so the Mac is not in its request path.

For a future update, create one new release manifest, build its exact source on both platforms, verify both plugin artifacts, stage both configurations without production auth, run the app and contract checks, then recreate only the two proxy containers. Check the running image IDs, source revision labels, health endpoints, registered plugin version, authenticated model catalogs, and client routes against the manifest. Keep the previous images and private configuration backups for rollback.

The October 13 source build is `d4fa77254e2922a834f6f97ddac0d7a88249b9ad`, based on upstream v8.0.27. It preserves the local Kimi model fallback feature. The runtime backups are `/Users/rever/cliproxy/ops/runtime/v8.0.27-fleet.1/backup` and `/home/andrew/cliproxy/releases/v8.0.27-fleet.1/backup`. No OAuth credential directory is replaced during upgrade or rollback.

For rollback, first preserve the failed runtime configuration in the release directory, stop only the proxy, and restore the backed-up Compose and configuration files. On the Mac, also restore the backed-up `.env` and `docker-compose.override.yml`. On Will, the previous release had no `.env`: move the new `.env` into the private release backup directory before starting the old Compose file, because its image override would otherwise select the new image again. Recreate with `docker compose up -d --no-deps --pull never --no-build cli-proxy-api`, then verify health and a real client request. Preserve any newly refreshed OAuth files. The old Mac image is `cliproxy-m5:60c24a02`; the old Will image is `eceasy/cli-proxy-api@sha256:f077e153476466e0ea8355400e39bf1508e637585b661ed3991b7b8129ce054d`.

Tailscale Serve can remain in place during an application rollback. To remove only the new private route, use `sudo tailscale serve --tcp=8317 off` on Will. Do not reset Serve: HTTPS 443 belongs to T3. Revert T3 routes through `server.updateSettings` or the app settings, preserving secrets in the app's secret store.
