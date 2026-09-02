# Will CLIProxy instance

This directory is the secret-free owner contract for the primary CLIProxy
instance on `will.local`. The application repository owns the Compose shape,
image pin, configuration invariants, and live verifier. PiSec owns the host
catalog, expected exposure, and cross-workload coordination.

The mutable runtime remains under `/home/andrew/cliproxy` on Will. Never commit
or copy its `config.yaml`, `auths/`, `logs/`, or `plugins/` into this repository.
The tracked configuration policy contains only non-secret field names and
expected operational values.

## Verify source and live state

The Go owner-contract tests reject image, restart-policy, healthcheck, mount,
or published-port drift in the tracked files. The live verifier uses fixed,
read-only SSH probes and returns only stable drift codes; it never prints API
keys, OAuth tokens, request bodies, or model responses.

```sh
go test ./ops -count=1
python3 -m unittest -v ops.test_will_instance
python3 ops/instances/will/verify_live.py --json
```

The verifier checks all six Docker bindings and host listeners against
`127.0.0.1`, verifies the running image and restart policy, compares the host
and in-container configuration hashes, enforces the retry/routing policy, and
requires `/healthz` HTTP 200. A clean verifier result is still not proof that a
provider can generate. After an update, also run an authenticated models check,
a real `/v1/responses` request, and the affected Codex/T3 thread.

## Update boundary

1. Confirm no OAuth refresh or protected T3 request is in flight.
2. Update the image digest in this directory's Compose and workload contract in
   one focused commit. PiSec should report owner-contract drift until its
   inventory is updated to the same digest.
3. Back up Will's current Compose file, copy the reviewed tracked Compose file
   to `/home/andrew/cliproxy/docker-compose.yml`, and preserve mode `0600` and
   owner `andrew:andrew`.
4. From the Will instance directory, pull and recreate only
   `cli-proxy-api`. Never delete the bind-mounted state directories.
5. Run the live verifier, then the authenticated model, generation, and exact
   client/thread checks. Roll back the Compose file and previous digest if any
   layer fails.

Docker engine/package updates and host reboots do not replace the bind-mounted
runtime state. A CLIProxy image change is an explicit contract change and must
pass the same verification sequence.
