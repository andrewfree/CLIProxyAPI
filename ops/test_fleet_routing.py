import json
import unittest
from pathlib import Path

from ops.fleet_routing import ROUTING_POLICY_FIELDS, routing_policy_matches, routing_policy_values

OPS = Path(__file__).resolve().parent


class FleetRoutingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads((OPS / "fleet-release.json").read_text())["routing_policy"]["amd64"]
        self.config = {"routing": dict(self.policy)}

    def test_matching_management_config_passes(self):
        self.assertTrue(routing_policy_matches(self.config, self.policy))

    def test_observed_routing_excludes_unrelated_configuration(self):
        self.config["api-keys"] = ["synthetic-secret"]
        self.config["routing"]["unrelated"] = "synthetic-private-value"
        self.assertEqual(routing_policy_values(self.config), self.policy)

    def test_omitted_disabled_defaults_match_but_omitted_enabled_values_drift(self):
        disabled = {
            "strategy": "round-robin", "session-affinity": False,
            "session-affinity-ttl": "", "session-affinity-subagents": None,
            "session-affinity-rebind-on": "",
        }
        self.assertTrue(routing_policy_matches({"routing": {"strategy": "round-robin"}}, disabled))
        typed_drift = {"routing": dict(disabled, **{"session-affinity": 0})}
        self.assertFalse(routing_policy_matches(typed_drift, disabled))
        enabled = {
            "strategy": "round-robin", "session-affinity": True,
            "session-affinity-ttl": "1h", "session-affinity-subagents": False,
            "session-affinity-rebind-on": "any",
        }
        self.assertFalse(routing_policy_matches({"routing": {"strategy": "round-robin"}}, enabled))
        for field in ROUTING_POLICY_FIELDS:
            with self.subTest(field=field):
                drifted = {"routing": dict(enabled)}
                drifted["routing"][field] = "wrong"
                self.assertFalse(routing_policy_matches(drifted, enabled))

    def test_missing_expected_policy_fails_closed(self):
        self.assertFalse(routing_policy_matches(self.config, None))
        incomplete = dict(self.policy)
        del incomplete["session-affinity-subagents"]
        self.assertFalse(routing_policy_matches(self.config, incomplete))


if __name__ == "__main__":
    unittest.main()
