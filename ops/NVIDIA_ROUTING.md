# Run NVIDIA-hosted models through CLIProxy

The M5 Mac proxy at `http://127.0.0.1:8317` serves selected models from NVIDIA's hosted API (`build.nvidia.com`) as an OpenAI-compatible upstream. Will does not have this provider; the NVIDIA key exists only on M5.

## Use it from T3

Select the **CLIProxy pool · M** provider (`codex_cliproxy_mac`) and one of these model IDs:

| T3 / proxy model ID | NVIDIA model | Status on 2026-11-11 |
|---|---|---|
| `nvidia-nemotron-3.5-lightning` | `nvidia/nemotron-3.5-lightning-30b-a3b` | Chat, Responses, and a T3 child run passed. About 3 to 4 seconds. |
| `nvidia-minimax-m3.1-flash` | `minimaxai/minimax-m3.1-flash` | Chat and Responses passed. About 8 to 12 seconds. |
| `nvidia-nemotron-3-ultra` | `nvidia/nemotron-3-ultra-550b-a55b` | Passed through the proxy, but NVIDIA intermittently returns `finish_reason: stop` with empty content, also on direct calls. |

The `codex` driver is only the client. CLIProxy translates Responses API requests into Chat Completions for this upstream. `codex_kimi_mac` also lists these models because it targets the same proxy.

## Configuration

The provider lives in M5's ignored `config.yaml`. CLIProxy has no environment-variable reference for upstream keys, so the key is inline in that file (mode `0600`). The source copy is `~/.config/nvidia/api.env` (`NVIDIA_API_KEY`). Never commit the key or print it.

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

This config uses the legacy layout: root `api-keys:` is the list of client keys. In that layout the top-level `openai-compatibility` key list is named `api-key-entries`. The newer `keys:` name, shown in `config.example.yaml`, belongs to the nested `api-keys:` provider map. With `keys:` here, CLIProxy still lists the aliases in `/v1/models` but sends no `Authorization` header, and NVIDIA answers `Header of type authorization was missing`. A correct reload logs `1 OpenAI-compat`.

Edit the file in place to keep its inode, because Docker bind-mounts the single file. CLIProxy hot-reloads it without a restart.

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

Then send one small chat request per alias. In T3, run `orchestrator_capabilities({"search":"nvidia"})` to confirm the catalog. T3 caches it until the next provider health refresh.

## Roll back

Remove the `openai-compatibility` block from `config.yaml` in place. The pre-change copy is in the private, ignored `ops/runtime/nvidia-provider-20261111/backup-20261111T150145Z/`. Do not restore that whole file over later edits.

The [setup receipt](evidence/nvidia-provider-20261111.json) records the checks.
