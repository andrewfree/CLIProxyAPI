from contextlib import redirect_stderr, redirect_stdout
import copy
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import io
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
ACCOUNT_LABELS = ("fixture-codex-label", "fixture-kimi-label")
HEALTHY = {"disabled": False, "status": "active", "unavailable": False}


class API:
    def __init__(self, config, files, models, public_models, plugins):
        self.config = config
        self.files = files
        self.models = models
        self.public_models = public_models
        self.plugins = plugins
        self.requests = []
        # A fixture host runs the pinned release unless a test changes it.
        self.version = fleet.read_json(fleet.OPS / "fleet-release.json")["version"]
        self.hidden = {
            fleet.V8 + "/config/server/port": 8317,
            fleet.V8 + "/config/oauth/auth-dir": "~/.cli-proxy-api",
        }
        self.cursor_models = model_rows("cursor", "cursor-only")
        self.plugins_enabled = True

    def get(self, url, *, client_key=None, optional_status=None):
        self.requests.append((url, client_key))
        path = urlsplit(url).path
        if path == "/healthz":
            return {"status": "ok"}, None
        if path == fleet.LOADED:
            return self.config, self.version
        if path.endswith("/credentials"):
            return {"files": self.files}, None
        if path.endswith("/credentials/models"):
            auth_id = parse_qs(urlsplit(url).query)["name"][0]
            return {"models": self.models[auth_id]}, None
        if path.startswith(fleet.V8 + "/config/"):
            return self.hidden.get(path), None
        if path.endswith("/plugins"):
            return {"plugins_enabled": self.plugins_enabled, "plugins": self.plugins}, None
        if path.endswith("/model-definitions/cursor"):
            return {"models": self.cursor_models}, None
        if path == "/v1/models" and client_key in self.public_models:
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
            "claude-api-key": [{"api-key": CANARY, "base-url": "https://provider.invalid",
                               "models": [{"name": "claude-static"}]}],
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
        mac_api = API(m5, [credential("codex", CANARY, ACCOUNT_LABELS[0], priority=2)],
                      {CANARY: m5_models},
                      {key: m5_models + nvidia_models for key in CLIENT_KEYS},
                      [cursor_plugin])
        will_api = API(will, [credential("codex", "will-codex", ACCOUNT_LABELS[0], priority=2),
                             credential("kimi-ai", "will-kimi", ACCOUNT_LABELS[1],
                                        organization_id=ORGANIZATION)],
                       {"will-codex": codex_models, "will-kimi": kimi_models},
                       {key: codex_models for key in CLIENT_KEYS},
            [copy.deepcopy(cursor_plugin), plugin("cliproxybar-quota", "0.1.0")])
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

    def assert_drift(self, mac, will, surface, *, today=TODAY):
        report, code = self.report(mac, will, today)
        self.assertEqual(code, 1, report)
        self.assertIn(surface, {row["surface"] for row in report["drift"]})

    def run_main(self, *args):
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            code = fleet.main(args)
        self.assertEqual(stderr.getvalue(), "")
        return json.loads(stdout.getvalue()), code

    def test_fingerprint_drops_secret_leaves_and_host_paths(self):
        raw = {
            "discovery": {"enabled": True},
            "logs-max-total-size-mb": 299,
            "api-keys": CLIENT_KEYS,
            "claude-api-key": [{"api-key": CANARY, "model": "fixture",
                               "headers": {"X-Custom": ORGANIZATION}}],
            "codex-api-key": CANARY,
            "gemini-api-key": [CANARY],
            "vertex-api-key": [{"models": []}, CANARY],
            "openai-compatibility": [{"name": "fixture",
                                      "api-key-entries": [{"api-key": CANARY}]}],
            "routing": {"strategy": "quota-aware"},
            "tls": {"key": CANARY},
            "plugins": {"store-auth": CANARY, "configs": {"cliproxybar-quota": {
                "enabled": True,
                "claude-api-key": [{"value": CANARY}],
                "openai-compatibility": [{"value": CANARY}],
            }}},
            "settings": [{"api_key": {"any": CANARY}, "token-list": [ORGANIZATION],
                          "refresh-tokens": [CANARY], "access-tokens": CANARY,
                          "credential": CANARY,
                          "clientSecret": CANARY, "Password": CANARY, "proxy": "https://u:p@fixture",
                          "base": "https://fixture.invalid/v1?key=" + CANARY,
                          "endpoint": "https://fixture.invalid/v1#" + CANARY,
                          "malformed-url": "https://[" + CANARY,
                          "headers": {"X-Custom": CANARY, "User-Agent": ORGANIZATION},
                          "use-max-completion-tokens": True, "max_tokens": 256},
                         {"headers": [CANARY]}, {"headers": CANARY}],
            "claude-header-defaults": {"timezone": "Etc/UTC"},
        }
        hidden = []
        masked = fleet.redact(raw, self.paths["secret_paths"], hidden)
        self.assertEqual(masked["settings"], [{"proxy": None, "base": None, "endpoint": None,
                                             "malformed-url": None,
                                             "headers": {}, "use-max-completion-tokens": True,
                                             "max_tokens": 256}, {}, {}])
        self.assertEqual(masked["claude-api-key"], [{"model": "fixture", "headers": {}}])
        self.assertEqual(masked["openai-compatibility"], [{"name": "fixture"}])
        self.assertNotIn("api-keys", masked)
        for value in (CANARY, ORGANIZATION, *CLIENT_KEYS, "u:p"):
            self.assertNotIn(value, json.dumps(masked))
        hidden_by_path = dict(hidden)
        for path in (("codex-api-key",), ("gemini-api-key",), ("vertex-api-key",), ("tls", "key"),
                     ("plugins", "store-auth"), ("settings", "0", "refresh-tokens"),
                     ("settings", "0", "access-tokens"), ("settings", "0", "credential"),
                     ("settings", "0", "base"), ("settings", "0", "endpoint"),
                     ("settings", "0", "malformed-url"),
                     ("settings", "1", "headers"), ("settings", "2", "headers")):
            with self.subTest(path=path):
                self.assertIn(path, hidden_by_path)
                self.assertIn(CANARY, json.dumps(hidden_by_path[path]))
        config, private, public = fleet.config_snapshot(raw, self.paths, NONCE)
        self.assertEqual(config["cliproxybar-quota"], fleet.fingerprint({"enabled": True}))
        client_changed = dict(raw, **{"api-keys": ["new-client-fixture"]})
        _, client_private, _ = fleet.config_snapshot(client_changed, self.paths, NONCE)
        self.assertEqual(private["shared"], client_private["shared"])
        changed = copy.deepcopy(raw)
        changed["logs-max-total-size-mb"] = 0
        changed["claude-api-key"][0]["api-key"] = "rotated"
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

    def test_custom_header_rotation_only_changes_private_tag(self):
        raw = {"claude-api-key": [{"headers": {"X-Custom": CANARY}}]}
        config, private, public = fleet.config_snapshot(raw, self.paths, NONCE)
        raw["claude-api-key"][0]["headers"]["X-Custom"] = "rotated-fixture"
        config2, private2, public2 = fleet.config_snapshot(raw, self.paths, NONCE)
        self.assertEqual((config, public), (config2, public2))
        self.assertNotEqual(private["shared"], private2["shared"])

    def test_malformed_openai_compatibility_is_hidden_in_all_fingerprints(self):
        for value in (CANARY, [CANARY], {"name": CANARY}):
            with self.subTest(shape=type(value).__name__):
                raw = {"openai-compatibility": value}
                digests, private, public = fleet.config_snapshot(raw, self.paths, NONCE)
                self.assertEqual(digests["nvidia"], fleet.fingerprint([]))
                self.assertEqual(public["config_sha256"], fleet.fingerprint({}))
                self.assertNotIn("openai-compatibility", public["section_sha256"])
                self.assertNotEqual(private["shared"], fleet.tag(NONCE, "provider-config", []))

    def test_static_provider_base_url_and_model_change_shared_fingerprint(self):
        for family in fleet.STATIC_KEY_FAMILIES:
            for field, changed in (("base-url", "https://other.invalid"),
                                   ("models", [{"name": "new-model"}])):
                with self.subTest(family=family, field=field):
                    config = {family: [{"api-key": CANARY, "base-url": "https://provider.invalid",
                                        "models": [{"name": "fixture"}]}]}
                    original, _, _ = fleet.config_snapshot(config, self.paths, NONCE)
                    config[family][0][field] = changed
                    result, _, _ = fleet.config_snapshot(config, self.paths, NONCE)
                    self.assertNotEqual(original["shared"], result["shared"])

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
        for value in (CANARY, ORGANIZATION, *CLIENT_KEYS, *ACCOUNT_LABELS,
                      fleet.tag(NONCE, "account", "codex", ACCOUNT_LABELS[0])):
            self.assertNotIn(value, emitted)
        for api in mac, will:
            self.assertEqual({key for url, key in api.requests if url == "/v1/models"},
                             set(CLIENT_KEYS))

    def test_worker_payload_omits_raw_secrets_and_labels(self):
        mac, will = self.fixture()
        for name, api in (("m5", mac), ("will", will)):
            with self.subTest(host=name):
                snapshot = self.probe(name, api)
                self.assertEqual(snapshot["status"], "ok")
                emitted = json.dumps(snapshot)
                for value in (CANARY, ORGANIZATION, *CLIENT_KEYS, *ACCOUNT_LABELS):
                    self.assertNotIn(value, emitted)

    def test_probe_error_shows_safe_operator_message(self):
        message = "Management key missing or ambiguous"
        with patch.object(fleet.LocalAPI, "get",
                          side_effect=[({"status": "ok"}, None), fleet.ProbeError(message)]):
            report, code = self.run_main("fingerprint", "--host", "m5")
        self.assertEqual((report["status"], code), ("error", 2))
        self.assertEqual(report["hosts"]["m5"]["error"], message)

    def test_unexpected_exceptions_do_not_expose_input(self):
        for error_type in (KeyError, ValueError, RuntimeError):
            with self.subTest(error_type=error_type.__name__):
                with patch.object(fleet.LocalAPI, "get",
                                  side_effect=[({"status": "ok"}, None), error_type(CANARY)]):
                    result = fleet.safe_collect(self.hosts["m5"], self.paths, self.release,
                                                NONCE, "fingerprint")
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"], "Could not read fleet probe inputs")
                self.assertNotIn(CANARY, json.dumps(result))
                with patch.object(fleet.LocalAPI, "get",
                                  side_effect=[({"status": "ok"}, None), error_type(CANARY)]):
                    report, code = self.run_main("fingerprint", "--host", "m5")
                self.assertEqual((report["status"], code), ("error", 2))
                self.assertNotIn(CANARY, json.dumps(report))
                with patch.object(fleet, "probe", side_effect=error_type(CANARY)):
                    report, code = self.run_main("fingerprint", "--host", "m5")
                self.assertEqual((report["status"], code), ("error", 2))
                self.assertNotIn(CANARY, json.dumps(report))

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

    def test_camoff_credential_prefix_requires_an_exception(self):
        mac, will = self.fixture()
        self.exceptions = [row for row in self.exceptions if row["surface"] != "P6/camoff-astra"]
        self.assert_drift(mac, will, "P6/camoff-astra")

    def test_prefix_catalog_exception_requires_registration_and_owner(self):
        mac, will = self.fixture()
        mac.public_models[CLIENT_KEYS[0]] = (mac.public_models[CLIENT_KEYS[0]]
                                                + model_rows("openai", "camoff-astra/not-registered"))
        report, code = self.report(mac, will)
        self.assertEqual(code, 1)
        self.assertIn("P8/shared", {row["surface"] for row in report["drift"]})

    def test_new_kimi_catalog_entry_causes_drift(self):
        mac, will = self.fixture()
        will.public_models[CLIENT_KEYS[0]] += will.models["will-kimi"]
        self.assert_drift(mac, will, "P8/kimi-ai")

    def test_model_not_supported_cooldowns_are_neutral_for_parity(self):
        mac, will = self.fixture()
        retry = "2099-01-01T00:00:00Z"
        unsupported = {"scope": "model", "model_key": "gpt-6-chat-thinking",
                       "reason": "model_not_supported", "retry_at": retry}
        item = will.files[0]
        item.update(status="error", unavailable=True, next_retry_after=retry, status_message="",
                    cooldowns=[unsupported])

        report, code = self.report(mac, will)
        self.assertEqual(code, 0, report["drift"])
        self.assertEqual(report["hosts"]["will"]["credential_health"], {"ok": 2})

        item.update(status_message="model not supported",
                    cooldowns=[unsupported, dict(unsupported, model_key="other-model")])
        report, code = self.report(mac, will)
        self.assertEqual(code, 0, report["drift"])
        self.assertEqual(report["hosts"]["will"]["credential_health"], {"ok": 2})

    def test_credential_wide_failures_override_a_model_not_supported_cooldown(self):
        mac, will = self.fixture()
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        retry = "2099-01-01T00:00:00Z"
        item = dict(will.files[0], status="error", unavailable=True, next_retry_after=retry,
                    status_message="provider-specific response", requires_sign_in=False,
                    access_token_expires_at=retry, cooldowns=[
                        {"scope": "model", "model_key": "retired-model",
                         "reason": "model_not_supported", "retry_at": retry},
                    ])
        failures = (
            {"access_token_expires_at": "2026-11-17T23:59:59Z"},
            {"access_token_expires_at": "2026-11-18T00:00:00Z"},
            {"refresh_error": {"code": "refresh_outcome_unknown", "message": "retry later"}},
            {"next_action": "sign_in"},
        )
        with patch.object(fleet, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            for failure in failures:
                with self.subTest(failure=failure):
                    will.files[0] = dict(item, **failure)
                    self.assert_drift(mac, will, "P7/credential-error")

    def test_model_not_supported_exemption_requires_model_scope_and_key(self):
        mac, will = self.fixture()
        retry = "2099-01-01T00:00:00Z"
        item = will.files[0]
        item.update(status="error", unavailable=True, next_retry_after=retry)
        for fields in ({"scope": "credential"},
                       {"scope": "credential", "model_key": "retired-model"},
                       {"model_key": "retired-model"},
                       {"scope": "model"},
                       {"scope": "model", "model_key": "  "}):
            with self.subTest(fields=fields):
                item["cooldowns"] = [dict(fields, reason="model_not_supported", retry_at=retry)]
                self.assert_drift(mac, will, "P7/credential-error")

    def test_model_not_supported_exemption_requires_a_served_model_without_a_restriction(self):
        mac, will = self.fixture()
        retry = "2099-01-01T00:00:00Z"
        item = will.files[0]
        item.update(status="error", unavailable=True, next_retry_after=retry,
                    access_token_expires_at=retry, refresh_error={}, next_action="retry")
        unsupported = {"scope": "model", "reason": "model_not_supported", "retry_at": retry}
        item["cooldowns"] = [dict(unsupported, model_key="gpt-fixture")]
        for ids in (("gpt-fixture", "gpt-second", "gpt-third"), ("provider/gpt-fixture",)):
            with self.subTest(ids=ids):
                will.models[item["id"]] = model_rows("openai", *ids)
                report, code = self.report(mac, will)
                self.assertEqual((code, report["drift"]), (0, []))
                self.assertEqual(report["hosts"]["will"]["credential_health"], {"ok": 2})

        # The optional prefix adds route IDs, but it adds no underlying models.
        models = model_rows("openai", "gpt-fixture", "gpt-second",
                            "camoff-astra/gpt-fixture", "camoff-astra/gpt-second")
        mac.models[mac.files[0]["id"]] = will.models[item["id"]] = models
        item["cooldowns"] = [dict(unsupported, model_key=" GPT-FIXTURE(high) "),
                             dict(unsupported, model_key="camoff-astra/gpt-second")]
        with self.subTest(catalog_size=len(models)):
            self.assert_drift(mac, will, "P7/credential-error")

        for models in ([], model_rows("openai", ""),
                       model_rows("openai", "gpt-fixture", "a/gpt-fixture", "b/gpt-fixture")):
            with self.subTest(catalog_size=len(models)):
                will.models[item["id"]] = models
                self.assert_drift(mac, will, "P7/credential-error")

    def test_partial_model_not_supported_cooldowns_do_not_hide_a_transient_error(self):
        mac, will = self.fixture()
        retry = "2099-01-01T00:00:00Z"
        item = will.files[0]
        will.models[item["id"]] = model_rows("openai", "gpt-fixture", "gpt-second", "gpt-third")
        item.update(status="error", unavailable=True, next_retry_after=retry, cooldowns=[
            {"scope": "model", "model_key": "gpt-fixture",
             "reason": "model_not_supported", "retry_at": retry},
            {"scope": "model", "model_key": "gpt-second",
             "reason": "transient_error", "retry_at": retry},
        ])
        self.assert_drift(mac, will, "P7/credential-error")

    def test_other_failures_still_cause_drift_with_a_model_not_supported_cooldown(self):
        mac, will = self.fixture()
        retry = "2099-01-01T00:00:00Z"
        unsupported = {"scope": "model", "model_key": "gpt-6-chat-thinking",
                       "reason": "model_not_supported", "retry_at": retry}
        item = will.files[0]
        item.update(status="error", unavailable=True, next_retry_after=retry, status_message="")
        for reason in ("unauthorized", "payment_required", "invalid_grant",
                       "transient_error", "not_found", "cloudflare_challenge"):
            with self.subTest(reason=reason):
                item["cooldowns"] = [unsupported, {"scope": "model", "model_key": "gpt-fixture",
                                                  "reason": reason, "retry_at": retry}]
                self.assert_drift(mac, will, "P7/credential-error")

        item.update(cooldowns=[unsupported], status_message="invalid_grant")
        self.assert_drift(mac, will, "P7/credential-error")
        item.update(status_message="", requires_sign_in=True)
        self.assert_drift(mac, will, "P7/credential-error")

    def test_quota_at_matching_deadline_still_cools_with_a_model_not_supported_cooldown(self):
        mac, will = self.fixture()
        retry = "2099-01-01T00:00:00Z"
        will.files[0].update(status="error", unavailable=True, next_retry_after=retry,
                             status_message="", cooldowns=[
                                 {"scope": "model", "model_key": "gpt-6-chat-thinking",
                                  "reason": "model_not_supported", "retry_at": retry},
                                 {"scope": "model", "model_key": "gpt-fixture",
                                  "reason": "quota", "retry_at": retry},
                             ])
        report, code = self.report(mac, will)
        self.assertEqual(code, 0, report["drift"])
        self.assertEqual(report["hosts"]["will"]["credential_health"]["cooling"], 1)

    def test_active_quota_and_rate_limit_cooldowns_do_not_cause_drift(self):
        mac, will = self.fixture()
        retry = "2099-01-01T00:00:00Z"
        will.files[0].update(status="error", unavailable=True, next_retry_after=retry,
                             status_message=" Quota Exhausted ")
        report, code = self.report(mac, will)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["hosts"]["will"]["credential_health"]["cooling"], 1)
        for reason in ("quota", "credential_quota", "transient_rate_limit"):
            with self.subTest(reason=reason):
                will.files[0].update(status_message="provider-specific response", cooldowns=[
                    {"reason": "transient_error", "retry_at": retry},
                    {"reason": reason, "retry_at": retry},
                ])
                report, code = self.report(mac, will)
                self.assertEqual(code, 0, report)
                self.assertEqual(report["hosts"]["will"]["credential_health"]["cooling"], 1)

    def test_auth_and_payment_errors_cause_drift_even_with_quota_cooldowns(self):
        mac, will = self.fixture()
        retry = "2099-01-01T00:00:00Z"
        will.files[0].update(status="error", unavailable=True, next_retry_after=retry,
                             cooldowns=[{"reason": "quota", "retry_at": retry}])
        for message in ("unauthorized", "payment_required", "invalid_grant", "token expired"):
            with self.subTest(message=message):
                will.files[0]["status_message"] = message
                self.assert_drift(mac, will, "P7/credential-error")
        will.files[0].update(status_message="quota exhausted", requires_sign_in=True)
        self.assert_drift(mac, will, "P7/credential-error")

    def test_other_retry_timers_are_errors_even_with_quota_observations(self):
        mac, will = self.fixture()
        will.files[0].update(status="error", unavailable=True, next_retry_after="2099-01-01T00:00:00Z",
                             status_message="transient upstream error",
                             quota={"signals": {"used-percent": "90"}})
        self.assert_drift(mac, will, "P7/credential-error")

    def test_non_quota_cooldown_at_matching_deadline_causes_drift(self):
        mac, will = self.fixture()
        retry = "2099-01-01T00:00:00Z"
        will.files[0].update(unavailable=True, next_retry_after=retry,
                             status_message="provider-specific response")
        for reason in ("unauthorized", "payment_required", "invalid_grant",
                       "transient_error", "not_found"):
            with self.subTest(reason=reason):
                will.files[0]["cooldowns"] = [{"reason": reason, "retry_at": retry}]
                self.assert_drift(mac, will, "P7/credential-error")

    def test_quota_cooldown_at_different_deadline_causes_drift(self):
        mac, will = self.fixture()
        will.files[0].update(unavailable=True, next_retry_after="2099-01-01T00:00:00Z",
                             status_message="provider-specific response",
                             cooldowns=[{"reason": "quota", "retry_at": "2099-01-02T00:00:00Z"}])
        self.assert_drift(mac, will, "P7/credential-error")

    def test_unavailable_or_error_status_alone_causes_drift(self):
        for changes in ({"unavailable": True}, {"status": "error"}):
            with self.subTest(changes=changes):
                mac, will = self.fixture()
                will.files[0].update(changes)
                self.assert_drift(mac, will, "P7/credential-error")

    def test_model_auth_failure_causes_drift_despite_matching_quota_deadline(self):
        mac, will = self.fixture()
        retry = "2099-01-01T00:00:00Z"
        will.files[0].update(unavailable=True, next_retry_after=retry,
                             status_message="provider-specific response")
        for reason in ("unauthorized", "payment_required", "invalid_grant"):
            with self.subTest(reason=reason):
                will.files[0]["cooldowns"] = [
                    {"reason": "quota", "retry_at": retry},
                    {"reason": reason, "retry_at": "2099-01-02T00:00:00Z"},
                ]
                self.assert_drift(mac, will, "P7/credential-error")

    def test_only_an_active_quota_deadline_marks_a_credential_cooling(self):
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        retry = "2026-11-18T00:00:00Z"
        item = dict(HEALTHY, unavailable=True, next_retry_after=retry,
                    status_message="quota exhausted")
        self.assertEqual(fleet.credential_health(item, now), "error")
        item.update(unavailable=False, next_retry_after="2099-01-01T00:00:00Z")
        self.assertEqual(fleet.credential_health(item, now), "ok")

    def test_model_auth_failure_is_an_error_even_if_credential_is_available(self):
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        item = dict(HEALTHY, cooldowns=[{"reason": "unauthorized",
                                       "retry_at": "2099-01-01T00:00:00Z"}])
        self.assertEqual(fleet.credential_health(item, now), "error")
        for disabled in ({"disabled": True}, {"status": "disabled"}):
            with self.subTest(disabled=disabled):
                self.assertEqual(fleet.credential_health(dict(item, **disabled), now), "disabled")

    def test_provider_key_rotation_causes_private_config_drift(self):
        mac, will = self.fixture()
        will.config["claude-api-key"][0]["api-key"] = "rotated-fixture"
        self.assert_drift(mac, will, "P5/shared")

    def test_different_client_key_set_causes_drift(self):
        mac, will = self.fixture()
        will.config["api-keys"].remove(CLIENT_KEYS[1])
        self.assert_drift(mac, will, "P5/client-keys")

    def test_hidden_management_setting_causes_drift(self):
        mac, will = self.fixture()
        will.hidden[fleet.V8 + "/config/management/allow-remote"] = True
        self.assert_drift(mac, will, "P2h/management.allow-remote")

    def test_hidden_host_setting_against_expectation_causes_drift(self):
        mac, will = self.fixture()
        will.hidden[fleet.V8 + "/config/server/port"] = 12345
        self.assert_drift(mac, will, "P2h/server.port")

    def test_wrong_server_version_causes_drift(self):
        mac, will = self.fixture()
        will.version = "fixture-wrong-version"
        self.assert_drift(mac, will, "P1/version")

    def test_changed_full_config_pin_causes_host_drift(self):
        mac, will = self.fixture()
        mac.config["debug"] = False
        self.assert_drift(mac, will, "P2/host-pin")

    def test_extra_plugin_causes_drift(self):
        mac, will = self.fixture()
        will.plugins.append(plugin("fixture-extra-plugin", "1.0.0"))
        self.assert_drift(mac, will, "P10/shared")

    def test_wrong_cursor_library_pin_causes_drift(self):
        mac, will = self.fixture()
        self.release["cursor_plugin"]["libraries"]["amd64"]["sha256"] = "0" * 64
        self.assert_drift(mac, will, "P10/auth-cursor-pin")

    def test_wrong_cursor_plugin_version_causes_pin_drift(self):
        mac, will = self.fixture()
        will.plugins[0]["metadata"]["version"] = "fixture-wrong-version"
        self.assert_drift(mac, will, "P10/auth-cursor-pin")

    def test_unregistered_or_disabled_cursor_plugin_causes_pin_drift(self):
        for field in ("registered", "effective_enabled"):
            with self.subTest(field=field):
                mac, will = self.fixture()
                will.plugins[0][field] = False
                self.assert_drift(mac, will, "P10/auth-cursor-pin")

    def test_global_plugin_disablement_causes_cursor_pin_drift(self):
        mac, will = self.fixture()
        will.plugins_enabled = False
        self.assert_drift(mac, will, "P10/auth-cursor-pin")

    def test_duplicate_named_prefix_causes_drift(self):
        mac, will = self.fixture()
        model = model_rows("kimi", "kimi-extra", "t3-fixture/kimi-extra")
        will.models["will-kimi"] = model
        will.files.append(credential("kimi-ai", "duplicate", "duplicate-fixture-label"))
        will.models["duplicate"] = model
        self.assert_drift(mac, will, "P6/duplicate-named-prefix")

    def test_missing_credential_label_causes_drift(self):
        mac, will = self.fixture()
        will.files[0]["label"] = ""
        self.assert_drift(mac, will, "P6/missing-label")

    def test_unknown_credential_prefix_causes_drift(self):
        mac, will = self.fixture()
        will.models["will-codex"] = []
        self.assert_drift(mac, will, "P6/unknown-prefix")

    def test_different_exception_count_does_not_excuse_drift(self):
        mac, will = self.fixture()
        next(row for row in self.exceptions if row["surface"] == "P2/debug")["count"] = 2
        self.assert_drift(mac, will, "P2/debug")

    def test_future_exception_does_not_excuse_drift(self):
        mac, will = self.fixture()
        next(row for row in self.exceptions if row["surface"] == "P2/debug")["date"] = "2026-11-19"
        self.assert_drift(mac, will, "P2/debug")

    def test_exception_without_a_difference_is_reported_as_unused(self):
        mac, will = self.fixture()
        self.exceptions.append(dict(self.exceptions[0], surface="P10/shared", hosts=["will"]))
        report, code = self.report(mac, will)
        self.assertEqual(code, 0, report)
        self.assertEqual(report["unused_exceptions"], [{"surface": "P10/shared", "hosts": ["will"]}])

    def test_expired_exception_does_not_excuse_matching_drift(self):
        mac, will = self.fixture()
        self.assert_drift(mac, will, "P2/debug", today=date(2026, 12, 2))

    def test_expired_exception_without_current_drift_still_fails(self):
        mac, will = self.fixture()
        self.exceptions.append(dict(self.exceptions[0], surface="P10/shared", hosts=["will"],
                                    date="2026-11-01", review_by="2026-11-17"))
        report, code = self.report(mac, will)
        self.assertEqual(code, 1, report)
        self.assertEqual(report["drift"], [])
        self.assertEqual(report["expired_exceptions"],
                         [{"surface": "P10/shared", "hosts": ["will"]}])

    def test_glm_52_cursor_overlap_causes_drift(self):
        mac, will = self.fixture()
        will.models["will-codex"] += model_rows("openai", "glm-5.2")
        will.cursor_models += model_rows("cursor", "glm-5.2")
        self.assert_drift(mac, will, "P9/glm-5.2")

    def test_static_direct_model_overlap_causes_drift(self):
        mac, will = self.fixture()
        will.cursor_models += model_rows("cursor", "claude-static")
        self.assert_drift(mac, will, "P9/Cursor-overlap")

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
        self.assertEqual(args[0][-7:], ["--", "will.local", "python3", "-I", "-B", "-", "--worker"])
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

    def test_http_error_uses_only_fixed_messages(self):
        with patch.object(fleet.http.client, "HTTPConnection") as factory:
            for status, expected in ((401, "Read-only API returned HTTP 401"),
                                     (599, "Read-only API returned an unexpected HTTP status"),
                                     (CANARY, "Read-only API returned an unexpected HTTP status")):
                with self.subTest(status_type=type(status).__name__):
                    factory.return_value.getresponse.return_value = Mock(status=status)
                    with self.assertRaises(fleet.ProbeError) as raised:
                        fleet.LocalAPI(8317, None).get("/healthz")
                    self.assertEqual(str(raised.exception), expected)

    def test_tracked_policy_check_catches_a_changed_will_pin(self):
        policy = fleet.read_json(fleet.OPS / "instances/will/config-policy.json")["required_config"]
        bundle = (fleet.OPS / "fleet/bundle.txt").read_text().splitlines()
        args = self.hosts, self.paths, self.release, self.exceptions, policy, bundle
        self.assertTrue(fleet.validate_contract(*args))
        policy["max-retry-interval"] = 0
        self.assertFalse(fleet.validate_contract(*args))

    def test_invalid_ssh_destination_is_rejected_before_any_probe(self):
        policy = fleet.read_json(fleet.OPS / "instances/will/config-policy.json")["required_config"]
        bundle = (fleet.OPS / "fleet/bundle.txt").read_text().splitlines()
        targets = {"option": "-oProxyCommand=" + CANARY, "verbose": "-v",
                   "batch": "-oBatchMode", "metacharacter": "will;" + CANARY,
                   "empty": "", "null": None, "boolean": True}
        cases = [("will", label, target) for label, target in targets.items()]
        cases.append(("m5", "remote alias", "will.local"))
        for name, label, target in cases:
            with self.subTest(host=name, target=label):
                hosts = copy.deepcopy(self.hosts)
                hosts[name]["ssh"] = target
                self.assertFalse(fleet.validate_contract(
                    hosts, self.paths, self.release, self.exceptions, policy, bundle
                ))
                for command in ("health", "fingerprint", "parity"):
                    with (patch.object(fleet.subprocess, "run") as run,
                          patch.object(fleet.LocalAPI, "get") as get):
                        result = fleet.probe(name, hosts, self.paths, self.release,
                                             NONCE, command)
                    run.assert_not_called()
                    get.assert_not_called()
                    self.assertEqual(result["status"], "error")
                    self.assertNotIn(CANARY, json.dumps(result))

    def test_check_rejects_invalid_exception_surfaces(self):
        read_json = fleet.read_json
        for surface in ("P2/shared-pin", "P2/host-pin", "P3/max-retry-interval",
                        "P6/duplicate-named-prefix", "P6/missing-label", "P6/unknown-prefix",
                        "P10/auth-cursor-pin", "P100/shared", "P2/", "P2"):
            with self.subTest(surface=surface):
                exceptions = self.exceptions + [dict(self.exceptions[0], surface=surface)]
                with patch.object(fleet, "read_json", side_effect=lambda path: (
                    exceptions if path.name == "exceptions.json" else read_json(path)
                )):
                    self.assertEqual(self.run_main("check"), ({"status": "invalid"}, 1))

    def test_runtime_pins_cannot_be_excepted(self):
        mac, will = self.fixture()
        will.config["max-retry-interval"] = 0
        self.exceptions.append(dict(self.exceptions[0], surface="P3/max-retry-interval",
                                    hosts=["will"]))
        report, code = self.report(mac, will)
        self.assertEqual(code, 1)
        self.assertIn("P3/max-retry-interval", {row["surface"] for row in report["drift"]})
