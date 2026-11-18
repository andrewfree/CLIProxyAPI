import copy
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit


SPEC = importlib.util.spec_from_file_location("fleetctl", Path(__file__).with_name("fleetctl.py"))
fleet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet)

NONCE = b"private fixture nonce"
TODAY = date(2026, 11, 18)
CANARY = "do-not-print-canary@example.invalid"
ORGANIZATION = "00000000-0000-4000-8000-111111111111"
CLIENT_KEYS = ["client-fixture-A", "client-fixture-B"]
HEALTHY = {"disabled": False, "status": "active", "unavailable": False}


class API:
    def __init__(self, config, files, models, public_models, plugins):
        self.config = config
        self.files = files
        self.models = models
        self.public_models = public_models
        self.plugins = plugins
        self.requests = []

    def get(self, url, *, client_key=None, optional_status=None):
        self.requests.append((url, client_key))
        path = urlsplit(url).path
        if path == "/healthz":
            return {"status": "ok"}, None
        if path == fleet.LOADED:
            return self.config, "v8.0.66-fleet.3"
        if path.endswith("/credentials"):
            return {"files": self.files}, None
        if path.endswith("/credentials/models"):
            auth_id = parse_qs(urlsplit(url).query)["name"][0]
            return {"models": self.models[auth_id]}, None
        if path.startswith(fleet.V8 + "/config/"):
            return {
                fleet.V8 + "/config/server/port": 8317,
                fleet.V8 + "/config/oauth/auth-dir": "~/.cli-proxy-api",
            }.get(path), None
        if path.endswith("/plugins"):
            return {"plugins_enabled": True, "plugins": self.plugins}, None
        if path.endswith("/model-definitions/cursor"):
            return {"models": model_rows("cursor", "cursor-only")}, None
        if path == "/v1/models" and client_key in CLIENT_KEYS:
            return {"data": self.public_models[client_key]}, None
        raise AssertionError("unexpected fixture API request")


def model_rows(owner, *model_ids):
    return [{"owned_by": owner, "id": model_id} for model_id in model_ids]


def credential(provider, identifier, label, **overrides):
    return dict(HEALTHY, provider=provider, id=identifier, label=label, **overrides)


def plugin(plugin_id, version):
    return {"id": plugin_id, "metadata": {"version": version}, "registered": True,
            "effective_enabled": True}


class FleetCTLTest(unittest.TestCase):
    def setUp(self):
        self.paths = fleet.read_json(fleet.OPS / "fleet/paths.json")
        self.release = fleet.read_json(fleet.OPS / "fleet-release.json")
        self.hosts = fleet.read_json(fleet.OPS / "fleet/hosts.json")
        self.exceptions = fleet.read_json(fleet.OPS / "fleet/exceptions.json")

    def fixture(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = {
            **self.release["runtime_policy"],
            "api-keys": CLIENT_KEYS,
            "force-model-prefix": False,
            "usage-statistics-enabled": False,
            "routing": self.release["routing_policy"]["arm64"],
            "claude-header-defaults": {"timezone": "America/Los_Angeles"},
            "plugins": {"configs": {"auth-cursor": {"enabled": True}}},
            "openai-compatibility": None,
            "unexpected": {"clientSecret": [CANARY, {"org": ORGANIZATION}]},
        }
        m5, will = copy.deepcopy(base), copy.deepcopy(base)
        m5.update(debug=True, discovery={"enabled": False}, **{"logs-max-total-size-mb": 0})
        will.update(debug=False, discovery={"enabled": True}, **{"logs-max-total-size-mb": 1024})
        m5["openai-compatibility"] = [
            {"name": "nvidia", "api-key-entries": [{"api-key": CANARY}],
             "models": [{"alias": alias, "name": "internal"} for alias in sorted(fleet.NVIDIA_MODELS)]}
        ]
        will["plugins"]["configs"]["cliproxybar-quota"] = {"enabled": True}
        fingerprints, _, public = fleet.config_snapshot(m5, self.paths, NONCE)
        self.release["config_fingerprint"] = {
            "shared": fingerprints["shared"], "observed": {"m5": public["config_sha256"]}
        }
        cursor_plugin = plugin("auth-cursor", self.release["cursor_plugin"]["version"])
        codex_models = model_rows("openai", "gpt-fixture")
        m5_models = codex_models + model_rows("openai", "camoff-astra/gpt-fixture")
        nvidia_models = model_rows("nvidia", *sorted(fleet.NVIDIA_MODELS))
        kimi_models = model_rows("kimi", "kimi-extra")
        mac_api = API(m5, [credential("codex", CANARY, CANARY, priority=2)],
                      {CANARY: m5_models},
                      {key: m5_models + nvidia_models for key in CLIENT_KEYS},
                      [cursor_plugin])
        will_api = API(will, [credential("codex", "will-codex", CANARY, priority=2),
                             credential("kimi-ai", "will-kimi", ORGANIZATION)],
                       {"will-codex": codex_models, "will-kimi": kimi_models},
                       {key: codex_models + kimi_models for key in CLIENT_KEYS},
                       [cursor_plugin, plugin("cliproxybar-quota", "0.1.0")])
        for host in self.hosts.values():
            host["instance_root"] = directory.name
            library = self.release["cursor_plugin"]["libraries"][host["arch"]]
            filename = Path(directory.name) / "plugins/linux" / host["arch"] / library["filename"]
            filename.parent.mkdir(parents=True)
            filename.write_bytes(b"fixture plugin data")
            library["sha256"] = hashlib.sha256(filename.read_bytes()).hexdigest()
        return mac_api, will_api

    def probe(self, name, api):
        return fleet.collect(self.hosts[name], self.paths, self.release, NONCE, "parity", api)

    def report(self, mac, will, today=TODAY):
        return fleet.report("parity", {"m5": self.probe("m5", mac), "will": self.probe("will", will)},
                            self.exceptions, today)

    def test_fingerprint_drops_secret_subtrees_and_host_paths(self):
        raw = {
            "discovery": {"enabled": True},
            "logs-max-total-size-mb": 299,
            "claude-api-key": [{"api-key": CANARY, "model": ORGANIZATION}],
            "routing": {"strategy": "quota-aware"},
            "settings": [{"api_key": {"any": CANARY}, "token-list": [ORGANIZATION],
                          "clientSecret": CANARY, "Password": CANARY, "proxy": "https://u:p@fixture",
                          "allowed": True}],
            "claude-header-defaults": {"timezone": CANARY},
        }
        hidden = []
        masked = fleet.redact(raw, self.paths["secret_paths"], hidden)
        self.assertEqual(masked["settings"], [{"proxy": None, "allowed": True}])
        self.assertNotIn("claude-api-key", masked)
        config, private, public = fleet.config_snapshot(raw, self.paths, NONCE)
        changed = copy.deepcopy(raw)
        changed["logs-max-total-size-mb"] = 0
        changed["claude-api-key"] = [{"secret": "rotated"}]
        changed["settings"][0]["Password"] = "rotated"
        config2, private2, public2 = fleet.config_snapshot(changed, self.paths, NONCE)
        self.assertEqual(config, config2)
        self.assertEqual(public, public2)
        self.assertNotEqual(private, private2)
        self.assertEqual((config, private, public), fleet.config_snapshot(
            dict(reversed(list(raw.items()))), self.paths, NONCE
        ))
        self.assertNotIn(CANARY, json.dumps(public))
        self.assertNotIn(ORGANIZATION, json.dumps(public))
        self.assertNotIn("claude-header-defaults.timezone", public["values"])

    def test_registered_prefix_modes_and_ambiguous_models(self):
        self.assertEqual(fleet.registered_prefix({"m", "t3-route/m"}, False), "t3-route")
        self.assertEqual(fleet.registered_prefix({"t3-route/m", "t3-route/n"}, True), "t3-route")
        self.assertEqual(fleet.registered_prefix({"organization/model"}, False), "")
        self.assertIsNone(fleet.registered_prefix({"m", "a/m", "b/m"}, False))
        self.assertIsNone(fleet.registered_prefix(set(), False))

    def test_disabled_credentials_compare_without_prefix_or_priority(self):
        item = dict(credential("claude", "not-fetched", CANARY), disabled=True)
        first = API({}, [dict(item, priority=1)], {}, {}, [])
        second = API({}, [dict(item, priority=2)], {}, {}, [])
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        self.assertEqual(fleet.credential_snapshot(first, {}, NONCE, now),
                         fleet.credential_snapshot(second, {}, NONCE, now))
        self.assertEqual(first.requests, [(fleet.V8 + "/credentials", None)])

    def test_full_pair_accepts_only_live_scoped_exceptions_and_prints_no_tags(self):
        mac, will = self.fixture()
        report, code = self.report(mac, will)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "equal")
        self.assertEqual(report["drift"], [])
        self.assertIn("P8/camoff-astra", {row["surface"] for row in report["exceptions"]})
        self.assertEqual(report["hosts"]["m5"]["camoff_astra_credentials"], 1)
        emitted = json.dumps(report)
        for value in (CANARY, ORGANIZATION, *CLIENT_KEYS,
                      fleet.tag(NONCE, "account", "codex", CANARY)):
            self.assertNotIn(value, emitted)
        for api in mac, will:
            self.assertEqual({key for url, key in api.requests if url == "/v1/models"},
                             set(CLIENT_KEYS))

    def test_new_loaded_setting_and_changed_catalog_owner_fail(self):
        mac, will = self.fixture()
        will.config["new-settings"] = {"mode": "fake-drift"}
        will.public_models[CLIENT_KEYS[1]] = model_rows("unexpected-owner", "gpt-fixture")
        report, code = self.report(mac, will)
        self.assertEqual(code, 1)
        self.assertTrue({"P2/shared", "P2/shared-pin", "P8/owner"} <= {
            row["surface"] for row in report["drift"]
        })

    def test_camoff_exception_cannot_hide_account_substitution(self):
        mac, will = self.fixture()
        will.files[0]["label"] = ORGANIZATION
        report, code = self.report(mac, will)
        self.assertEqual(code, 1)
        self.assertIn("P6/shared", {row["surface"] for row in report["drift"]})

    def test_prefix_catalog_exception_requires_registration_and_owner(self):
        mac, will = self.fixture()
        mac.public_models[CLIENT_KEYS[0]] = (mac.public_models[CLIENT_KEYS[0]]
                                                + model_rows("openai", "camoff-astra/not-registered"))
        report, code = self.report(mac, will)
        self.assertEqual(code, 1)
        self.assertIn("P8/shared", {row["surface"] for row in report["drift"]})

    def test_error_drift_but_not_cooling(self):
        mac, will = self.fixture()
        will.files[0].update(status="error", unavailable=True, next_retry_after="2099-01-01T00:00:00Z")
        self.assertEqual(self.report(mac, will)[1], 0)
        will.files[0]["requires_sign_in"] = True
        report, code = self.report(mac, will)
        self.assertEqual(code, 1)
        self.assertIn("P7/credential-error", {row["surface"] for row in report["drift"]})

    def test_extra_kimi_credential_does_not_mask_unrelated_cursor_overlap(self):
        mac, will = self.fixture()
        mac.models[CANARY] += model_rows("openai", "cursor-only")
        report, code = self.report(mac, will)
        self.assertEqual(code, 1)
        self.assertIn("P9/Cursor-overlap", {row["surface"] for row in report["drift"]})

    def test_expired_exception_and_unreachable_are_distinct(self):
        mac, will = self.fixture()
        report, code = self.report(mac, will, date(2026, 12, 2))
        self.assertEqual(code, 1)
        self.assertTrue(report["expired_exceptions"])
        report, code = fleet.report("parity", {
            "m5": self.probe("m5", mac), "will": {"status": "unreachable"},
        }, self.exceptions, TODAY)
        self.assertEqual((report["status"], code), ("unreachable", 2))
        self.assertNotIn("drift", report)

    def test_ssh_sends_worker_input_without_installing_files(self):
        with patch.object(fleet.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"status":"ok"}', "")
            self.assertEqual(fleet.probe("will", self.hosts, self.paths, self.release,
                                         NONCE, "health"), {"status": "ok"})
        args, kwargs = run.call_args
        self.assertEqual(args[0][-6:], ["will.local", "python3", "-I", "-B", "-", "--worker"])
        self.assertIn(repr(NONCE), kwargs["input"])
        self.assertNotIn(repr(NONCE), args[0])
        self.assertTrue(kwargs["capture_output"])

    def test_http_uses_read_only_loopback_and_no_secret_in_errors(self):
        with TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("MANAGEMENT_PASSWORD='" + CANARY + "' # comment\n", encoding="utf-8")
            with patch.object(fleet.http.client, "HTTPConnection") as factory:
                connection = factory.return_value
                connection.getresponse.return_value = Mock(status=302)
                with self.assertRaisesRegex(fleet.ProbeError, "^Read-only API returned HTTP 302$"):
                    fleet.LocalAPI(8317, env).get(fleet.LOADED)
        factory.assert_called_once_with("127.0.0.1", 8317, timeout=10)
        connection.request.assert_called_once()
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("GET", fleet.LOADED))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + CANARY)
        connection.close.assert_called_once()

    def test_tracked_policy_check_catches_a_changed_will_pin(self):
        policy = fleet.read_json(fleet.OPS / "instances/will/config-policy.json")["required_config"]
        bundle = (fleet.OPS / "fleet/bundle.txt").read_text().splitlines()
        args = self.hosts, self.paths, self.release, self.exceptions, policy, bundle
        self.assertTrue(fleet.validate_contract(*args))
        policy["max-retry-interval"] = 0
        self.assertFalse(fleet.validate_contract(*args))

    def test_runtime_pins_cannot_be_excepted(self):
        mac, will = self.fixture()
        will.config["max-retry-interval"] = 0
        self.exceptions.append(dict(self.exceptions[0], surface="P3/max-retry-interval",
                                    hosts=["will"]))
        report, code = self.report(mac, will)
        self.assertEqual(code, 1)
        self.assertIn("P3/max-retry-interval", {row["surface"] for row in report["drift"]})
