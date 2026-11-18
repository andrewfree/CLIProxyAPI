# Direct GLM Coding Plan routing

The Mac fallback and Will primary have a `zai-coding-plan` OpenAI-compatible
provider at `https://api.z.ai/api/coding/paas/v4`. It uses Andrew's active Z.ai
Coding Plan credential from OpenCode v2, distinct from the older `zai` API key.
Keep credentials in private runtime configuration and out of Git, arguments,
logs, and reports.

## Verified aliases

The November 14, 2026 activation recorded these routes:

| Host | Client alias | Upstream model | Verification |
| --- | --- | --- | --- |
| Mac | `glm-5p3`, `glm-5.3` | `glm-5.3` | HTTP 200, `GLM_OK` |
| Mac | `glm-5p3-flash`, `glm-5.3-flash` | `glm-5.3-flash` | `glm-5p3-flash` added November 18; no recorded completion test |
| Will | `glm-5p3`, `glm-5.3` | `glm-5.3` | HTTP 200, `GLM_OK` |
| Will | `glm-5p3-flash`, `glm-5.3-flash` | `glm-5.3-flash` | HTTP 200, `GLM_OK` |

Until November 18 the Mac lacked the `glm-5p3-flash` alias, so the Mac catalog listed that name under the Cursor plugin. Will's `glm-5.3` check used `/v1/responses`. Its other three checks used
`/v1/chat/completions`. Each response named the expected upstream model.
Will then retained the older `glm-5.2` Cursor entry, outside this direct
Coding Plan configuration and not used for inference. Since November 18
`glm-5.2` is a direct alias for `glm-5.3` on both hosts
([below](#glm-52-maps-to-glm-53)).

The [sanitized Will receipt](evidence/glm-direct-20261114.json) records both
client-key catalog checks and the live fleet and owner verifiers. GPT-6.1 Sol
performed activation and verification, with source reviews by Claude Fable 5.2
and Claude Opus 5.5. The Go server build, 16 owner-verifier unit tests, and
focused native catalog priority tests passed. These checks establish the
recorded activation, not remaining subscription quota.

## glm-5.2 maps to glm-5.3

Since November 18, 2026, both hosts map client ID `glm-5.2` to upstream model
`glm-5.3` on `zai-coding-plan`: the provider's model list has an entry with
`name: glm-5.3` and `alias: glm-5.2`. Andrew asked for this fix.

Z.ai's model list for the GLM Coding Plan still names `glm-5.2`, but Z.ai
redirects `glm-5.2` and `glm-5.1` requests to GLM-5.3 on its side
([Z.ai: latest model](https://docs.z.ai/devpack/latest-model)). A direct
`glm-5.2` alias answered correctly, but every reply named `glm-5.3`. The Cursor
plugin's `glm-5.2` entry on Will returned 503 `auth_not_found`
(`providers=cursor`), so it was broken. With the mapping, the request CLIProxy
sends names the model that answers.

Verify on each host:

- `/v1/models` lists `glm-5.2` owned by `zai-coding-plan`. This held on both
  hosts on November 18.
- `fleetctl parity` reports no `P9/glm-5.2`. That check flags a Cursor
  `glm-5.2` entry while a direct one exists. Will's stale Cursor registration
  cleared after its container was recreated on November 18.
- In the request log of one small `glm-5.2` request, the `model` in the
  `=== API REQUEST ===` section is `glm-5.3`, and the response's `model` is
  `glm-5.3`. The response named `glm-5.3` before the mapping too, so only the
  `API REQUEST` model shows that the mapping applies. On November 18 the Mac's
  log showed `glm-5.2` in `=== REQUEST BODY ===` and `glm-5.3` in
  `=== API REQUEST ===`.

## Client authentication and configuration layout

The Mac configuration write used the v8 management API and migrated its client
keys to `access.api-keys`. The T3 token helper previously read the legacy root
`api-keys` field, which holds provider configuration in v8. The helper now
recognizes both layouts and rejects malformed client-key data. The helper fix
is committed separately in `ae4e0cf0`.

Will retained the legacy layout. Its provider was appended to
`openai-compatibility` without changing existing configuration semantics,
client keys, the mounted file's inode, or mode `0600`. Both client keys returned
matching catalogs after activation. The proxy container and start time stayed
unchanged, and the instance remained healthy. No OAuth tokens were copied
between hosts.

## Provider ownership needs a separate check

The public `/v1/models` response exposes one model entry per ID. Its `owned_by`
value alone does not prove that another provider cannot serve the same ID.
Cursor's static plugin executor entries also bypass the proposed global
`oauth-excluded-models` rule. That rule was not added on Will.

Native provider registrations take priority for the four direct aliases. Verify
Cursor's provider-specific catalog through the authenticated read-only endpoint
`GET /v8/management/routing/model-definitions/cursor`. After activation it listed
only `glm-5.2` among GLM IDs, while the public catalog listed all four direct
aliases under `zai-coding-plan`. Since November 18 `glm-5.2` is a direct alias
too, and parity's `P9/glm-5.2` check covers it
([above](#glm-52-maps-to-glm-53)).

The first hot-add left stale Cursor alias registrations. One extra watcher
reload with unchanged business configuration reconciled them. Its temporary
comment was removed afterward, and the final provider-specific catalog was
checked again. This is an operational workaround for registration ordering;
the server source was not fixed and cold restart behavior was not tested.

The operational verifier was corrected to distinguish disposable research
containers from persistent services. It still checked persistent container IDs
and images. Existing unit tests were unchanged.

## Private recovery evidence

Each host retains private activation files under
`ops/runtime/glm-direct-20261114/`. The current `STATUS.md` identifies the
pre-change backups, apply receipt, final verifier, and review resolutions.
Those files are ignored by Git and can contain secrets.

For rollback, first compare the current configuration with the activation
record so later edits are preserved. Remove only the affected provider change
in place. Do not replace or rename the single bind-mounted `config.yaml` file.
After the watcher reloads, verify client authentication, provider-specific
catalogs, and the fleet and owner checks. Never restore an old full
configuration over later edits.
