# Run Kimi through an explicitly selected CLIProxy route

Kimi defaults to the direct Kimi CLI. When the owner or applicable standing instructions select an existing CLIProxy Kimi route, keep that selection for the authorized task without asking again per call. Preserve existing login and paid-provider restrictions. Route selection does not authorize new paid calls beyond the task.

1. Discover the exact model with T3 `orchestrator_capabilities({"search":"kimi-k3.1-pro"})` on the host that will run the task. Inspect the provider instance, `canRunChildTask`, cross-provider eligibility when needed, constraints, and the model's supported options. A broad result with `omittedModels` is incomplete. Search for the exact ID before concluding that a model is absent. Prefer this discovery path over reading credentials.
2. On M5, use the selected `codex_cliproxy` profile for Will primary at `http://100.98.74.67:8317/v1`. The `codex_cliproxy_mac` profile selects the local Mac fallback at `http://127.0.0.1:8317/v1` only when that manual fallback is authorized. M1 has the Will profile only. M5's loopback endpoint is not a direct fallback from M1. Rediscover the instance, model, and options on each host before dispatch.
3. Use the exact proxy model ID. Native CLI alias `kimi-code/k3.1-pro` and proxy ID `kimi-k3.1-pro` name different client interfaces. A `codex` driver identifies the client, not the model vendor. `kimi-default` remains K3 with K2.8 fallback. It does not satisfy an exact K3.1 request.
4. Pass the discovered selection to `delegate_task` for the already-authorized task. The November 2 M5 catalog supports this concrete `target` value:

```json
{
	"providerInstanceId": "codex_cliproxy",
	"model": "kimi-k3.1-pro",
	"options": {
		"reasoningEffort": "high",
		"serviceTier": "default"
	}
}
```

Follow the returned `next` instruction and completion notification. Do not poll a running task. When `next` says completion wakes the thread, end the turn and resume on that notification. Read the completed result and check for pending child work before reporting completion. The authorized task can supply execution evidence. No additional paid smoke test is required by this recipe.

Treat route, endpoint, account, and model fallback as separate choices. Selecting CLIProxy instead of the direct CLI does not permit an endpoint switch. Selecting the Mac endpoint does not permit a named-account switch. A failure does not permit substituting another model for exact K3.1. Preserve each selection unless its existing authorization permits the change.

Catalog visibility proves discovery only. A private review completed through Will's `codex_cliproxy` profile targeting `kimi-k3.1-pro` at `2026-11-02T23:25:33.200Z`. That establishes completed task execution through the selected route. It does not independently attest the upstream served model, subscription billing provenance, or available quota for a future call. This review did not exercise the Mac fallback. The earlier fleet `client-validation.json` receipt covers Luna, not Kimi.
