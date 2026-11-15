# Recheck a Claude account after an upgrade

A subscription upgrade can take effect at Anthropic while CLIProxy still holds
the account's previous quota cooldown. Use `claude_account_recheck.py` to verify
that exact account and release the stale restriction safely.

Run this on the proxy host that owns the credential. Mac and Will maintain
separate credentials and routing state. The command uses v8 management routes
and keeps OAuth tokens inside CLIProxy. It redeems no reset grants.

Select the account's `auth_index` from `GET /v8/management/credentials` and
supply the expected upstream `rate_limit_tier`. Max 20x uses
`default_claude_max_20x`.

```sh
python3 ops/claude_account_recheck.py \
  --auth-index <auth-index> --expected-tier default_claude_max_20x
```

This default invocation reads the live profile and cached cooldown only.
It sends no inference request and changes no routing state. It refuses an
unknown, disabled, ambiguous, or signed-out account and a mismatched identity
or tier.

To authorize one small account-pinned Opus 5.5 request:

```sh
python3 ops/claude_account_recheck.py \
  --auth-index <auth-index> --expected-tier default_claude_max_20x \
  --yes --receipt ops/runtime/claude-recheck/receipt.json
```

The command invokes `POST /v8/management/credentials/verify`. The server sends
a minimal prompt with a 32-output-token Claude budget using its normal executor,
translation, and payload configuration. There is no fallback account. The
command never calls the blanket cooldown reset endpoint.

The server requires a completed response and allowed quota signals before
releasing a Claude credential-wide restriction. It compares the credential
generation captured before the probe under the same mutation lock that applies
the result. A credential replacement, quota rejection, or other state change
during the probe prevents early recovery. Independent restrictions for sibling
models survive, including those hidden beneath the credential-wide restriction.
Ordinary successful requests retain the existing cooldown behavior.

This requires the server-side quota recheck fix. An older server may complete
the probe while retaining the credential cooldown. The command reports that
case as a failure and never falls back to a blanket reset.

A `recovered` result means server verification completed and a subsequent read
found no credential-wide or selected-model cooldown. Independent sibling model
cooldowns can remain and are included in the receipt. Follow with a normal
request through the intended shared pool to verify pool routing. The command
does not resume a T3 thread or establish completion of its task.

Receipts omit account emails, organization UUIDs, tokens, raw upstream bodies,
and keys. Preserve a failed receipt before retrying. Do not automate repeated
`--yes` probes during a real quota limit.

Run the tests from the repository root:

```sh
python3 -m unittest -v ops.test_claude_account_recheck ops.test_claude_usage_resets
go test ./sdk/cliproxy/auth -run TestClaudeQuotaVerify -count=1
```
