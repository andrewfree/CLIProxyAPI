# Run OpenCode Go models through CLIProxy

Since November 19, 2026, both proxies, Will (primary) and the M5 Mac (`http://127.0.0.1:8317`), serve GLM-5.3 and GLM-5.3-Flash from Andrew's [OpenCode Go](https://opencode.ai/v2/docs/console/go) subscription as an OpenAI-compatible upstream. Go is a $10/month plan for the Console workspace `Default`. Its GLM-5.3 monthly allowance is $500 of list-price usage through November 22, 2026, and lower afterwards ([usage limits](https://opencode.ai/docs/console/go/#usage-limits)).

The provider is separate from `zai-coding-plan` ([GLM_ROUTING.md](GLM_ROUTING.md)). Both serve GLM-5.3, so Go uses its own aliases and never answers the existing `glm-5.3`, `glm-5p3` or `glm-5.2` client IDs.

## Use it

| Proxy model ID | Go model | Checked on 2026-11-19 |
|---|---|---|
| `opencode-go-glm-5.3` | `glm-5.3` | Chat and Responses on the Mac, chat on Will |
| `opencode-go-glm-5.3-flash` | `glm-5.3-flash` | Chat on the Mac, Responses on Will |

In T3, select **CLIProxy pool · W** (`codex_cliproxy`, Will) or **CLIProxy pool · M** (`codex_cliproxy_mac`, the Mac) and one of these model IDs. T3 caches the catalog until its next provider health refresh.

A Go response carries `usage.opencode.billing_source`. A value of `go` with `usage.cost` 0 means the request drew from the Go allowance and not from the Console balance. The Go page in OpenCode Console shows the 5-hour, weekly and monthly usage.

## Configuration

The provider lives in each host's ignored `config.yaml`, with the same key on both hosts. The key is a personal Console API key named `M5 OpenCode CLI + CLIProxy (Go) 2026-11-19`, scoped to model requests only, which expires 90 days after creation (around February 17, 2027). The source copy is the Mac's `~/.config/opencode-go-secret/key` (mode `0600`). The OpenCode CLI on the Mac uses the same key through its `opencode-go` integration. Never commit the key or print it. The Console shows a key once, at creation.

Mac (v8 layout, nested `api-keys:` provider map, key list `keys:`):

```yaml
api-keys:
  openai-compatibility:
    - name: "opencode-go"
      base-url: "https://opencode.ai/inference/go/v1"
      headers:
        x-opencode-session: "$CPA-SESSION-ID"
        User-Agent: "andrewfree-cliproxy/1.0"
      keys:
        - api-key: "<OPENCODE_GO_KEY>"
      models:
        - name: "glm-5.3"
          alias: "opencode-go-glm-5.3"
        - name: "glm-5.3-flash"
          alias: "opencode-go-glm-5.3-flash"
```

Will (legacy layout) uses the same entry under the top-level `openai-compatibility:` list, with the key list named `api-key-entries:`. The [NVIDIA guide](NVIDIA_ROUTING.md#configuration) describes the two layouts and the silent failure when the key list has the wrong name.

### Why the two headers

Go rejects a request without a session ID: HTTP 400 `MissingSessionID`, "Request is missing x-opencode-session and cannot be routed efficiently". Go's [client guidance](https://opencode.ai/v2/docs/console/go/#where-can-i-use-it) asks for a stable session ID per conversation and a client-specific user agent instead of a generic SDK name.

`$CPA-SESSION-ID` makes CLIProxy fill `x-opencode-session` with its own session ID for the request (`internal/util/header_helpers.go`, `extractCustomHeaders`). The resolver in `sdk/cliproxy/auth/session_resolver.go` takes the ID from the request context or from the client's own session headers, so a Codex or Claude Code conversation keeps one Go session. If no ID resolves, CLIProxy omits the header and Go answers 400. On November 19 a plain `curl` with no session header still succeeded on the Mac, so the resolver found an ID for it too.

## Deployment on November 19, 2026

- **Mac, 15:42:47 UTC.** Host-side edits do not load on the Mac at the moment ([FLEET.md](FLEET.md#operations-on-november-19-2026)). The host file, the container's copy and the `GET /v8/management/config` ETag had the same SHA-256 (`bdd3494e…`). The entry was added with `PUT /v8/management/config/api-keys/openai-compatibility`, which replaces the whole list, carrying that ETag as `If-Match`. The body was the fetched list with the new entry appended and the read-only `auth-index` fields removed. The proxy logged `config successfully reloaded` and `3 OpenAI-compat`. Afterwards the host and container copies were again identical (`a5d67e59…`).
- **Will, 15:44:07 UTC.** The legacy block was appended to `config.yaml` with `>>`, which keeps the bind-mounted inode (`32397311` before and after). The file parsed as YAML with the new entry, and the container's copy matched the host (`fc746fe2…`).
- **Pins.** `ops/fleet-release.json` pins the shared config projection and the Mac's full fingerprint, so this intended change made `fleetctl parity` report `P2/shared-pin` (both hosts) and `P2/host-pin` (Mac). The pins were updated to the new values: shared `e2c13e78…`, identical on both hosts, and Mac `20d6e0cf…`.

## Verify

```sh
cd ~/cliproxy
python3 ops/verify_fleet_instance.py          # expect "failures": []
tok=$(~/.local/bin/cliproxy-codex-token)
curl -sS -H "Authorization: Bearer $tok" http://127.0.0.1:8317/v1/models |
  jq -c '[.data[] | select(.id|test("opencode-go")) | {id, owned_by}]'
curl -sS http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
  -d '{"model":"opencode-go-glm-5.3","messages":[{"role":"user","content":"Reply with exactly: GO-OK"}],"max_tokens":2000}' |
  jq -c '{model, content:.choices[0].message.content, billing:.usage.opencode.billing_source, cost:.usage.cost}'
```

Expect both IDs owned by `opencode-go`, then `content` `GO-OK`, `billing` `go` and `cost` 0. GLM-5.3 reasons before it answers, so keep `max_tokens` at 2000 or more in smoke tests. On Will, run the same commands from its instance root. From a worktree, `python3 ops/fleetctl.py parity` should report `equal`.

Results on November 19, 2026:

| Host | Check | Result |
|---|---|---|
| Mac | `/v1/models` | both IDs, `owned_by` `opencode-go` |
| Mac | chat `opencode-go-glm-5.3`, with a `session_id` header | `PROXY-GO-OK`, `billing_source` `go`, cost 0 |
| Mac | chat `opencode-go-glm-5.3`, no session header | `NOSESSION-OK` |
| Mac | Responses `opencode-go-glm-5.3` | `completed`, `RESP-GO-OK` |
| Mac | chat `opencode-go-glm-5.3-flash` | `FLASH-GO-OK`, `billing_source` `go` |
| Will | `/v1/models` | both IDs, `owned_by` `opencode-go` |
| Will | chat `opencode-go-glm-5.3` | `WILL-GO-OK`, `billing_source` `go`, cost 0 |
| Will | Responses `opencode-go-glm-5.3-flash` | `completed`, `WILL-RESP-OK` |
| Both | `verify_fleet_instance.py` | `"failures": []` |
| Both | `fleetctl check`, `health`, `parity` after the pin update | `ok`, `ok`, `equal` |

## Roll back

On the Mac, fetch `GET /v8/management/config/api-keys/openai-compatibility` with its ETag, remove only the `opencode-go` entry, and `PUT` the list back with that ETag as `If-Match`, after confirming the host and container copies match. On Will, remove only the `opencode-go` entry from `openai-compatibility`, in place. The pre-change copies are in the private, ignored `ops/runtime/opencode-go-20261119/config.yaml.before` on each host. They predate any later edit, so do not restore a whole file over newer changes. Restore the previous `config_fingerprint` pins in `ops/fleet-release.json` in the same change. To stop the spending as well, cancel Go from **Manage subscription** on the Go page in OpenCode Console and revoke the key under **Account → Personal API keys**.
