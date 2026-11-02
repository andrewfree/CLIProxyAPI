ROUTING_POLICY_DEFAULTS = {
    "strategy": "",
    "session-affinity": False,
    "session-affinity-ttl": "",
    "session-affinity-subagents": None,
    "session-affinity-rebind-on": "",
}
ROUTING_POLICY_FIELDS = tuple(ROUTING_POLICY_DEFAULTS)


def routing_policy_values(config):
    routing = config.get("routing", {}) if isinstance(config, dict) else {}
    if not isinstance(routing, dict):
        routing = {}
    return {field: routing.get(field, default) for field, default in ROUTING_POLICY_DEFAULTS.items()}


def routing_policy_matches(config, expected):
    if not isinstance(expected, dict) or set(expected) != set(ROUTING_POLICY_FIELDS):
        return False
    observed = routing_policy_values(config)
    return all(
        type(observed[field]) is type(expected[field]) and observed[field] == expected[field]
        for field in ROUTING_POLICY_FIELDS
    )
