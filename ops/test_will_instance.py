import copy
import json
import subprocess
import unittest
from pathlib import Path

from ops.instances.will.verify_live import (
    REMOTE_PROBE,
    collect_observation,
    evaluate_observation,
)


INSTANCE_DIR = Path(__file__).resolve().parent / "instances" / "will"


def load_json(name):
    return json.loads((INSTANCE_DIR / name).read_text(encoding="utf-8"))


def matching_observation():
    contract = load_json("workload-contract.json")
    policy = load_json("config-policy.json")
    workload = contract["workload"]
    runtime = workload["runtime"]
    ports = {
        exposure["port"] for exposure in workload["expected_lan_exposure"]
    }
    mounts = [
        {
            "type": "bind",
            "source": mount["source"],
            "target": mount["target"],
        }
        for mount in policy["required_mounts"]
    ]
    return {
        "compose": {
            "image": runtime["image"],
            "pull_policy": "always",
            "restart": "always",
            "ports": [
                {
                    "host_ip": "127.0.0.1",
                    "target": port,
                    "published": str(port),
                    "protocol": "tcp",
                }
                for port in ports
            ],
            "volumes": mounts,
            "healthcheck": {"test": ["CMD-SHELL", "bounded health probe"]},
            "stop_grace_period": "20s",
            "logging": {
                "driver": "json-file",
                "options": {"max-size": "10m", "max-file": "3"},
            },
        },
        "container": {
            "image_ref": runtime["image"],
            "image_id": "sha256:" + runtime["image"].rsplit("sha256:", 1)[-1],
            "state": "running",
            "health": "healthy",
            "restart_policy": "always",
            "ports": {
                f"{port}/tcp": [
                    {"HostIp": "127.0.0.1", "HostPort": str(port)}
                ]
                for port in ports
            },
            "mounts": mounts,
        },
        "config": {
            **{
                key: str(value)
                for key, value in policy["required_config"].items()
            },
            "mode": "600",
            "owner": "andrew:andrew",
            "host_sha256": "a" * 64,
            "container_sha256": "a" * 64,
        },
        "listeners": [f"127.0.0.1:{port}" for port in ports],
        "health_status": 200,
    }


class WillInstanceVerifierTests(unittest.TestCase):
    def setUp(self):
        self.contract = load_json("workload-contract.json")
        self.policy = load_json("config-policy.json")

    def test_matching_observation_has_no_findings(self):
        self.assertEqual(
            evaluate_observation(
                self.contract,
                self.policy,
                matching_observation(),
            ),
            [],
        )

    def test_wildcard_or_extra_port_is_exposure_drift(self):
        observation = matching_observation()
        observation["container"]["ports"]["8317/tcp"][0]["HostIp"] = "0.0.0.0"
        observation["listeners"].append("0.0.0.0:9000")

        findings = evaluate_observation(self.contract, self.policy, observation)

        self.assertIn("container_port_exposure_drift", findings)
        self.assertIn("listener_exposure_drift", findings)

    def test_retry_policy_and_mount_identity_drift_are_detected(self):
        observation = matching_observation()
        observation["config"]["max-retry-interval"] = "0"
        observation["config"]["container_sha256"] = "b" * 64

        findings = evaluate_observation(self.contract, self.policy, observation)

        self.assertIn("config_policy_drift", findings)
        self.assertIn("config_mount_identity_drift", findings)

    def test_image_and_compose_port_drift_are_detected(self):
        observation = matching_observation()
        observation["compose"]["image"] = "example.invalid/changed@sha256:" + "0" * 64
        observation["compose"]["ports"].append(
            {
                "host_ip": "127.0.0.1",
                "target": 9000,
                "published": "9000",
                "protocol": "tcp",
            }
        )

        findings = evaluate_observation(self.contract, self.policy, observation)

        self.assertIn("compose_image_drift", findings)
        self.assertIn("compose_port_exposure_drift", findings)

    def test_remote_probe_is_secret_minimizing(self):
        self.assertNotIn("cat \"$config_file\"", REMOTE_PROBE)
        self.assertNotIn("auths/", REMOTE_PROBE)
        self.assertNotIn("api-key", REMOTE_PROBE.lower())
        self.assertNotIn("token", REMOTE_PROBE.lower())

    def test_collection_rejects_untrusted_target_before_runner(self):
        def runner(*args, **kwargs):
            self.fail("runner should not be called")

        with self.assertRaises(ValueError):
            collect_observation("will.local;touch /tmp/no", runner=runner)

    def test_collection_parses_only_remote_json(self):
        expected = matching_observation()

        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=json.dumps(expected),
                stderr="",
            )

        observed = collect_observation("will.local", runner=runner)

        self.assertEqual(observed, expected)

    def test_evaluator_does_not_mutate_observation(self):
        observation = matching_observation()
        original = copy.deepcopy(observation)

        evaluate_observation(self.contract, self.policy, observation)

        self.assertEqual(observation, original)


if __name__ == "__main__":
    unittest.main()
