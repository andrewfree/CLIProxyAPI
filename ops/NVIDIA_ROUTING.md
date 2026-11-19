# Run NVIDIA-hosted models through CLIProxy

Both proxies, Will (primary) and the M5 Mac (`http://127.0.0.1:8317`), serve selected models from NVIDIA's hosted API (`build.nvidia.com`) as an OpenAI-compatible upstream. Will gained the provider, the same static key and the same three aliases on November 18, 2026 ([FLEET.md](FLEET.md#operations-on-november-18-2026)).

## Use it from T3

Select **CLIProxy pool · W** (`codex_cliproxy`, Will) or **CLIProxy pool · M** (`codex_cliproxy_mac`, the Mac) and one of these model IDs. Will has served the aliases since November 18. Confirm that T3 lists them for the profile you choose ([Verify](#verify)).

| T3 / proxy model ID | NVIDIA model | Status on 2026-11-11 |
|---|---|---|
| `nvidia-nemotron-3.5-lightning` | `nvidia/nemotron-3.5-lightning-30b-a3b` | Chat, Responses, and a T3 child run passed. About 3 to 4 seconds. |
| `nvidia-minimax-m3.1-flash` | `minimaxai/minimax-m3.1-flash` | Chat and Responses passed. About 8 to 12 seconds. |
| `nvidia-nemotron-3-ultra` | `nvidia/nemotron-3-ultra-550b-a55b` | Passed through the proxy, but NVIDIA intermittently returns `finish_reason: stop` with empty content, also on direct calls. |

The `codex` driver is only the client. CLIProxy translates Responses API requests into Chat Completions for this upstream. `codex_kimi_mac` also lists these models because it targets the same Mac proxy as `codex_cliproxy_mac`.

## Configuration

The provider lives in each host's ignored `config.yaml`, with the same key on both hosts. CLIProxy has no environment-variable reference for upstream keys, so the key is inline in that file (mode `0600`). The source copy is the Mac's `~/.config/nvidia/api.env` (`NVIDIA_API_KEY`). Never commit the key or print it.

```yaml
openai-compatibility:
  - name: "nvidia"
    base-url: "https://integrate.api.nvidia.com/v1"
    api-key-entries:
      - api-key: "<NVIDIA_API_KEY>"
    models:
      - name: "nvidia/nemotron-3-ultra-550b-a55b"
        alias: "nvidia-nemotron-3-ultra"
      - name: "nvidia/nemotron-3.5-lightning-30b-a3b"
        alias: "nvidia-nemotron-3.5-lightning"
      - name: "minimaxai/minimax-m3.1-flash"
        alias: "nvidia-minimax-m3.1-flash"
```

The block above is the legacy layout, which Will uses: root `api-keys:` is the list of client keys, and the top-level `openai-compatibility` key list is named `api-key-entries`. The Mac has used the v8 layout since November 14 ([GLM_ROUTING.md](GLM_ROUTING.md#client-authentication-and-configuration-layout)). There the provider sits in the nested `api-keys:` provider map, and its key list is named `keys:`, as in `config.example.yaml`. With `keys:` in a legacy-layout file, CLIProxy still lists the aliases in `/v1/models` but sends no `Authorization` header, and NVIDIA answers `Header of type authorization was missing`. A correct reload logs `1 OpenAI-compat`.

Edit the file in place to keep its inode, because Docker bind-mounts the single file. CLIProxy hot-reloads it without a restart. On the Mac, host-side edits do not load at the moment; follow [FLEET_ARCHITECTURE section 4](FLEET_ARCHITECTURE.md#4-what-is-still-manual-or-accepted).

## Choosing models

NVIDIA's `/v1/models` catalog is public and lists models the key cannot call. Before adding an alias, send a real chat request with the key. On 2026-11-11 these failed and were left out:

- `moonshotai/kimi-k2.6` and `nvidia/nemotron-3-super-120b-a12b`: HTTP 404 `Function ... Not found for account`.
- `z-ai/glm-5.3-flash` and `openai/gpt-oss-20b`: no response within 90 seconds.
- `deepseek-ai/deepseek-v4.1-flash`: 87 seconds once, then no response within 200 seconds. Removed after it was added.

Reasoning models can spend a small `max_tokens` budget on reasoning and return empty content. Use at least 2000 for smoke tests.

## Verify

```sh
cd ~/cliproxy
python3 ops/verify_fleet_instance.py          # expect "failures": []
tok=$(~/.local/bin/cliproxy-codex-token)
curl -sS -H "Authorization: Bearer $tok" http://127.0.0.1:8317/v1/models   # expect the nvidia-* IDs
```

From the Mac, check Will's catalog with the same token: `curl -sS -H "Authorization: Bearer $tok" http://will.<tailnet>.ts.net:8317/v1/models`. On Will, run the same `python3 ops/verify_fleet_instance.py` from its instance root.

Then send one small chat request per alias. In T3, run `orchestrator_capabilities({"search":"nvidia"})` to confirm the catalog. T3 caches it until the next provider health refresh.

## Roll back

On the Mac, remove only the `nvidia` entry from `openai-compatibility` in `config.yaml`, in place (host-side edits do not load there at the moment; follow [FLEET_ARCHITECTURE section 4](FLEET_ARCHITECTURE.md#4-what-is-still-manual-or-accepted)). The list also holds other providers, such as `zai-coding-plan` ([GLM_ROUTING.md](GLM_ROUTING.md)). The pre-change copy is in the private, ignored `ops/runtime/nvidia-provider-20261111/backup-20261111T150145Z/`. Do not restore that whole file over later edits.

On Will, remove only the `nvidia` entry from `openai-compatibility` in place; it was added on November 18 ([FLEET.md](FLEET.md#operations-on-november-18-2026)).

The [setup receipt](evidence/nvidia-provider-20261111.json) records the checks.
