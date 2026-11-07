# Quota-aware account routing

`quota-aware` is an opt-in policy for choosing among eligible pooled credentials.
It favors usable shared allowance that is nearer its reset, while spreading
selections through a fair weighted rotation. The default remains `round-robin`.

`quota-aware-shadow` calculates the proposed choice but returns the ordinary
round-robin choice. Differences appear at debug level without credential IDs,
emails, raw quota headers, or request content. This supports observation before
changing account selection. It does not prove a quota or latency improvement.

## Policy

The selector reads the existing passive `QuotaState.Signals` snapshot. It does
not poll providers or consume reset grants. Observations older than five minutes,
invalid windows, reset deadlines that have passed, and ambiguous additional
limits cannot establish a quota preference.

For each supported shared window, the policy multiplies its remaining fraction
by a reset urgency factor. The factor is the window duration divided by time
until reset, capped at two. The smallest window score limits the account score.
An imminent short reset therefore cannot conceal an exhausted weekly window.
Configured credential weights supply explicit relative capacity preferences.
Percentages alone do not establish equal capacity across subscription plans.
Zero configured weights are excluded before priority and affinity decisions when
other eligible credentials exist. Explicit pins and genuinely sole eligible
credentials retain their identity.

Claude observations support shared five-hour and seven-day windows. Codex
observations support the main primary and secondary windows using their reported
durations. Primary is not assumed to be the shorter window. Model-specific and
additional windows require a proven applicability mapping; ambiguous forecasts
fall back instead of inventing that mapping. Code-review and overage limits do
not disable ordinary shared allowance. See the parser tests for accepted signal
forms and conservative fallback cases.

Unknown observations retain a neutral share. If all candidates lack usable
forecasts, the selector uses ordinary round-robin. If forecasts predict zero
headroom for every otherwise eligible candidate, it also uses round-robin.
Passive forecasts do not manufacture an authoritative quota cooldown.

The custom selector receives the manager's existing eligible candidates. Named
account pins, requested model, priority, disabled credentials, authentication
failures, model eligibility, cooldowns, and retry exclusions retain their existing
meaning. A sole eligible credential or an explicit pin is not replaced because
of a quota forecast. Home dispatch and scheduler plugins retain their existing
override behavior.

Selections are serialized through bounded per-route fairness state. Dynamic
weights retain fairness credit instead of restarting at the first credential
after each observation. This spreads concurrent selections. It does not measure
the number or cost of in-flight requests. Retry, streaming, credential renewal,
paid-provider policy, and host failover are separate mechanisms.

OAuth token expiration, credit balances, and the proxy's capped retry eligibility
are not quota reset deadlines. An expired access token still follows the existing
credential refresh and eligibility rules.

## Configuration

After installing a qualified build, select shadow mode through the existing
configuration file:

```yaml
routing:
  strategy: quota-aware-shadow
  session-affinity: false
```

The supported v8 management API accepts a JSON string at
`PUT /v8/management/config/routing/strategy`. The string may be
`"quota-aware-shadow"`, `"quota-aware"`, or `"round-robin"` for rollback. The normal
configuration reload constructs a new selector when the strategy changes.
Preserve all unrelated fields when editing a mounted configuration.

The deprecated v0 strategy setter and clients that still use it cannot select
these new strategies. Use YAML or the v8 config API. This feature does not enable
session affinity, update the fleet manifest, or activate itself on an existing
server. Live fleet activation follows the owner's promotion gate.

## Implementation boundary

The active policy uses the existing custom-selector path rather than adding
another optimized scheduler strategy. That path already applies route-aware
candidate eligibility and retries. A quota-specific prevalidation marker prevents
a second check under an unresolved alias. Active quota selection stays outside
`isBuiltInSelector`, so the scheduler cannot silently bypass its `Pick` method.

Shadow mode delegates actual routing to its owned ordinary round-robin selector.
The manager retains both its legacy selection behavior and its native scheduler,
including alias cursor grouping and mixed-provider rotation. Forecast evaluation
reads eligible candidate views after the actual choice and advances only forecast
state. It does not advance the actual round-robin cursors.

The alternative was to add scoring to scheduler shards and each single-provider
and mixed-provider ready view. The custom selector keeps the new quota parsing
and fairness state in one place for these small account pools.
