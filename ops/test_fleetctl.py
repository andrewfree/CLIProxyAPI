from contextlib import redirect_stderr, redirect_stdout
import copy
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from ops import fleet_mac, fleetctl as fleet

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
        # The synthetic fleet below models the exceptions recorded on 2026-11-18, so the tests use that
        # frozen list; the live ops/fleet/exceptions.json is checked by `fleetctl check` and parity.
        self.exceptions = fleet.read_json(fleet.OPS / "test_fleetctl_exceptions.json")

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

    def test_go_nanosecond_times_parse_on_every_supported_python(self):
        # The proxy emits RFC3339Nano with trailing zeros trimmed; the monitor runs on Python 3.9.
        parsed = fleet.parse_time("2026-11-14T03:09:02.013444964Z")
        self.assertEqual(parsed, datetime(2026, 11, 14, 3, 9, 2, 13444, tzinfo=timezone.utc))
        self.assertEqual(fleet.parse_time("2026-11-14T03:09:02.5Z").microsecond, 500000)
        self.assertEqual(fleet.parse_time("2026-11-14T03:09:02Z").tzinfo, timezone.utc)
        self.assertEqual(fleet.parse_time("2026-11-14T03:09:02.123456789-08:00").utcoffset().total_seconds(), -28800)
        item = dict(HEALTHY, unavailable=True, status="error", status_message="quota exhausted",
                    next_retry_after="2099-01-01T00:00:00.123456789Z",
                    cooldowns=[{"reason": "credential_quota", "retry_at": "2099-01-01T00:00:00.123456789Z"}])
        self.assertEqual(fleet.credential_health(item, datetime(2026, 11, 18, tzinfo=timezone.utc)), "cooling")

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

    def test_transient_refresh_error_preserves_active_and_quota_cooling_health(self):
        mac, will = self.fixture()
        retry = "2099-01-01T00:00:00Z"
        item = will.files[0]
        item.update(access_token_expires_at=retry, requires_sign_in=False,
                    refresh_error={"code": "refresh_failed"})
        for state, counts in (
            ({}, {"ok": 2}),
            ({"status": "error", "unavailable": True, "next_retry_after": retry,
              "cooldowns": [{"scope": "credential", "reason": "quota", "retry_at": retry}]},
             {"ok": 1, "cooling": 1}),
        ):
            with self.subTest(counts=counts):
                item.update(state)
                report, code = self.report(mac, will)
                self.assertEqual((code, report["drift"]), (0, []))
                self.assertEqual(report["hosts"]["will"]["credential_health"], counts)

    def test_sign_in_hint_only_withholds_the_model_restriction_exemption(self):
        mac, will = self.fixture()
        will.files[0].update(next_action="sign_in")
        report, code = self.report(mac, will)
        self.assertEqual((code, report["drift"]), (0, []))
        # The hint alone still blocks the exemption when otherwise eligible.
        will.files[0].update(status="error", unavailable=True, cooldowns=[
            {"scope": "model", "model_key": "retired-model", "reason": "model_not_supported",
             "retry_at": "2099-01-01T00:00:00Z"},
        ])
        self.assert_drift(mac, will, "P7/credential-error")

    def test_model_not_supported_compares_forked_and_unforked_alias_targets(self):
        mac, will = self.fixture()
        aliases = {"codex": [
            {"alias": "ALIAS-A", "name": "UP-A", "fork": True},
            {"alias": " alias-b(high) ", "name": " camoff-astra/UP-B(high) "},
            {"alias": "alias-b(low)", "name": "up-a"},
        ], "claude": [{"alias": "alias-b(high)", "name": "up-a"}]}
        unprefixed = ("gpt-fixture", "up-a", "up-b", "alias-a", "alias-a(high)", "alias-b(HIGH)")
        ids = (*unprefixed, *("camoff-astra/" + model for model in unprefixed))
        for api in (mac, will):
            api.config["oauth-model-alias"] = aliases
            api.models[api.files[0]["id"]] = model_rows("openai", *ids)
        fingerprints, _, public = fleet.config_snapshot(mac.config, self.paths, NONCE)
        self.release["config_fingerprint"] = {
            "shared": fingerprints["shared"], "observed": {"m5": public["config_sha256"]},
        }

        unsupported = {"scope": "model", "reason": "model_not_supported",
                       "retry_at": "2099-01-01T00:00:00Z"}
        item = will.files[0]
        item.update(status="error", unavailable=True)
        for model in ("up-b", "camoff-astra/up-b"):
            with self.subTest(restricted=model):
                item["cooldowns"] = [dict(unsupported, model_key=key)
                                     for key in ("gpt-fixture", " up-a ", model)]
                report, code = self.report(mac, will)
                self.assertEqual((code, report["drift"]), (0, []))
                self.assertEqual(report["hosts"]["will"]["credential_health"], {"ok": 2})

        item["cooldowns"].append(dict(unsupported, model_key="up-b"))
        self.assert_drift(mac, will, "P7/credential-error")

    def test_unresolvable_channel_alias_table_withholds_the_exemption(self):
        _, will = self.fixture()
        item = will.files[0]
        item.update(provider=" CoDeX ", status="error", unavailable=True, cooldowns=[
            {"scope": "model", "model_key": "up-a", "reason": "model_not_supported",
             "retry_at": "2099-01-01T00:00:00Z"},
        ])
        valid = {"alias": "gpt-fixture", "name": "up-b"}
        duplicate = {"alias": "GPT-FIXTURE", "name": "up-a"}
        tables = (
            None, {"codex": None}, {"codex": {"gpt-fixture": "up-b"}}, {"codex": [42]},
            {"codex": [valid, {"alias": "unknown-target"}]},
            {"codex": [valid, {"name": "up-b"}]},
            {"codex": [valid, {"alias": "other", "name": " "}]},
            {"codex": [valid, {"alias": " ", "name": "up-b"}]},
            {"codex": [valid, duplicate]}, {"codex": [duplicate, valid]},
        )
        for table in tables:
            with self.subTest(table=table):
                will.config["oauth-model-alias"] = table
                self.assertEqual(self.probe("will", will)["public"]["credential_health"],
                                 {"error": 1, "ok": 1})

        will.config["oauth-model-alias"] = {"claude": None}
        self.assertEqual(self.probe("will", will)["public"]["credential_health"], {"ok": 2})

    def test_global_oauth_aliases_do_not_apply_to_api_keys_or_gemini(self):
        _, will = self.fixture()
        item = will.files[0]
        item.update(status="error", unavailable=True, cooldowns=[
            {"scope": "model", "model_key": "up-a", "reason": "model_not_supported",
             "retry_at": "2099-01-01T00:00:00Z"},
        ])
        alias = {"alias": "gpt-fixture", "name": "up-a"}
        for provider, account_type in (("codex", "api_key"), ("gemini", "oauth")):
            with self.subTest(provider=provider):
                item.update(provider=provider, account_type=account_type)
                will.config["oauth-model-alias"] = {provider: [alias]}
                self.assertEqual(self.probe("will", will)["public"]["credential_health"], {"ok": 2})

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


class MacCommandsTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.hosts = fleet.read_json(fleet.OPS / "fleet/hosts.json")
        self.hosts["m5"]["instance_root"] = str(self.root)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.hosts["m5"]["bundle_root"] = str(self.bundle)
        (self.bundle / "fleet_mac.py").write_text("def read_leases(root): pass", encoding="utf-8")
        for name in ("cliproxy_watchdog.py", "fleet_monitor.py"):
            (self.bundle / name).write_text("fleet_mac.read_marker(root)", encoding="utf-8")
        with (self.bundle / "cliproxy_watchdog.py").open("a", encoding="utf-8") as watchdog:
            watchdog.write("\nfleet_mac.read_leases(root)\n")
        self.settings = self.root / "settings.json"
        self.settings.write_text(json.dumps({"providerInstances": {"claude_cliproxy": {"environment": [
            {"name": "CLIPROXY_FALLBACK_URL", "value": "http://localhost:8317"},
            {"name": "ANTHROPIC_API_KEY", "value": CANARY, "sensitive": True},
            {"name": "CLIPROXY_BASE_URL", "value": "https://will.invalid:8317/", "sensitive": False},
        ]}}}), encoding="utf-8")
        for mock in (patch.object(fleet, "read_json", return_value=self.hosts),
                     patch.object(fleet_mac.sys, "platform", "darwin")):
            mock.start()
            self.addCleanup(mock.stop)
        probe = patch.object(fleet, "probe", return_value={"status": "ok"})
        self.probe = probe.start()
        self.addCleanup(probe.stop)
        run = patch.object(fleet_mac.subprocess, "run", side_effect=AssertionError("Unexpected process"))
        self.run = run.start()
        self.addCleanup(run.stop)
        for protocol in ("HTTP", "HTTPS"):
            connect = patch.object(fleet_mac.http.client, protocol + "Connection")
            connection = connect.start()
            self.addCleanup(connect.stop)
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"status":"ok","extra":true}'

    def invoke(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = fleet.main(["mac", *args], settings_path=self.settings)
        return code, out.getvalue()

    @staticmethod
    def result(stdout="", code=0, stderr="private subprocess output"):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def acquire_lease(self, **overrides):
        fields = dict(thread="ae4ac55e", project="omxlapi", purpose="agent benchmark", why_mac="local sandbox")
        return fleet_mac.acquire_lease(self.root, self.bundle, **{**fields, **overrides})

    def docker_pause_runner(self, argv, **kwargs):
        if argv[0] == "ps":
            return self.result()
        if argv[:3] == fleet_mac.DESKTOP:
            if argv[3] == "info":
                return self.result(code=int(getattr(self, "desktop_stopped", False)))
            if argv[3] == "ps":
                return self.result("cli-proxy-api\nother-service\n")
            if argv[3] == "inspect":
                return self.result(getattr(self, "restart_policy_json", '{"Name":"on-failure","MaximumRetryCount":5}'))
            if argv[3:] == ["update", "--restart=no", "cli-proxy-api"]:
                self.assertEqual(fleet_mac.read_marker(self.root)["restart_policy"], "on-failure:5")
                self.restart_policy_json = '{"Name":"no","MaximumRetryCount":0}'
                return self.result()
        if argv == ["docker", "desktop", "stop"]:
            self.desktop_stopped = True
            return self.result()
        self.fail(f"Unexpected command: {argv}")

    def write_monitor_health(self, checked_at, status):
        runtime = fleet_mac.runtime_dir(self.root)
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "health.json").write_text(json.dumps({
            "checked_at": checked_at.isoformat(), "hosts": {"will": {"status": status}},
        }), encoding="utf-8")

    def test_pause_refuses_when_will_is_down(self):
        self.probe.return_value = {"status": "unreachable"}
        self.run.side_effect = [self.result()]
        code, output = self.invoke("pause", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("Will proxy", output)
        self.assertIn("--force", output)
        self.probe.assert_called_once_with("will", self.hosts, None, None, None, "health")
        self.assertEqual(self.run.call_args.args[0][0], "ps")
        self.assertIn("Mac-only sessions: none.", output)
        self.assertIsNone(fleet_mac.read_marker(self.root))

    def test_pause_probes_the_will_client_origin_from_settings(self):
        self.run.side_effect = [self.result(), self.result(code=1), self.result(code=1), self.result()]
        code, output = self.invoke("pause", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual(output.count("Will proxy at client origin: ok"), 2)
        self.assertNotIn(CANARY, output)
        connect = fleet_mac.http.client.HTTPSConnection
        self.assertEqual(connect.call_count, 2)
        connect.assert_called_with("will.invalid", 8317, timeout=3)
        connect.return_value.request.assert_called_with("GET", "/healthz", headers={"Accept": "application/json"})
        self.assertEqual(connect.return_value.close.call_count, 2)
        fleet_mac.http.client.HTTPConnection.assert_not_called()

    def test_pause_explicit_will_origin_overrides_settings(self):
        self.settings.unlink()
        self.run.side_effect = [self.result(), self.result(code=1), self.result(code=1), self.result()]
        code, output = self.invoke("pause", "--yes", "--will-origin", "http://other-will.invalid")
        self.assertEqual(code, 0, output)
        fleet_mac.http.client.HTTPConnection.assert_called_with("other-will.invalid", None, timeout=3)
        fleet_mac.http.client.HTTPSConnection.assert_not_called()

    def test_pause_requires_a_non_loopback_http_origin(self):
        for origin in ("http://127.1:8317", "http://localhost.:8317", "http://[::1]:8317",
                       "http://[::ffff:127.0.0.1]:8317", "ftp://will.invalid",
                       "https://will.invalid:bad", "https://will.invalid/v1",
                       "https://will.invalid?query", f"https://{CANARY}@will.invalid"):
            with self.subTest(origin=origin):
                self.run.side_effect = [self.result()]
                code, output = self.invoke("pause", "--yes", "--will-origin", origin)
                self.assertNotEqual(code, 0)
                self.assertIn("Will client origin unavailable or invalid", output)
                self.assertNotIn(CANARY, output)
        fleet_mac.http.client.HTTPConnection.assert_not_called()
        fleet_mac.http.client.HTTPSConnection.assert_not_called()
        self.assertIsNone(fleet_mac.read_marker(self.root))

    def test_pause_does_not_read_a_sensitive_origin_value(self):
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        data["providerInstances"]["claude_cliproxy"]["environment"][-1]["sensitive"] = True
        self.settings.write_text(json.dumps(data), encoding="utf-8")
        self.run.side_effect = [self.result()]
        code, output = self.invoke("pause", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("Will client origin unavailable or invalid", output)
        fleet_mac.http.client.HTTPSConnection.assert_not_called()

    def test_pause_refuses_when_client_health_response_fails_launcher_rule(self):
        connection = fleet_mac.http.client.HTTPSConnection.return_value
        response = connection.getresponse.return_value
        for status, body in ((302, b'{"status":"ok"}'), (503, b'{"status":"ok"}'),
                             (200, b'{"status":"down"}'), (200, b'{}'), (200, b'not json'),
                             (200, b'null'), (200, b'{"status":"ok"}' + b" " * 4096),
                             (200, TimeoutError())):
            with self.subTest(status=status, body=type(body)):
                self.run.side_effect = [self.result()]
                response.status = status
                response.read.side_effect = [body]
                code, output = self.invoke("pause", "--yes")
                self.assertNotEqual(code, 0)
                self.assertIn("Will proxy at client origin: unhealthy", output)
                self.assertIn("--force", output)
                self.assertIsNone(fleet_mac.read_marker(self.root))
        response.read.assert_called_with(4097)

    def test_pause_refuses_fresh_unhealthy_monitor_report_and_accepts_stale_report(self):
        now = datetime(2026, 11, 21, tzinfo=timezone.utc)
        with patch.object(fleet_mac, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            self.write_monitor_health(now - timedelta(minutes=20), "egress-down")
            self.run.side_effect = [self.result()]
            code, output = self.invoke("pause", "--yes")
            self.assertNotEqual(code, 0)
            self.assertIn("Fleet monitor Will status: unhealthy", output)
            self.assertIsNone(fleet_mac.read_marker(self.root))

            self.write_monitor_health(now - timedelta(minutes=20, seconds=1), "egress-down")
            self.run.side_effect = [self.result(), self.result(code=1), self.result(code=1), self.result()]
            code, output = self.invoke("pause", "--yes")
            self.assertEqual(code, 0, output)
            self.assertIn("Fleet monitor health report is stale", output)
            self.assertIsNotNone(fleet_mac.read_marker(self.root))

    def test_pause_records_already_down_engine_without_listing_or_stopping(self):
        self.run.side_effect = [self.result(), self.result(code=1), self.result(code=1), self.result()]
        code, output = self.invoke("pause", "--yes")
        self.assertEqual(code, 0, output)
        self.assertIn("Docker engine was already down", output)
        self.assertIn("Fleet monitor health report is missing", output)
        self.assertIsNone(fleet_mac.read_marker(self.root)["containers"])
        self.assertIs(fleet_mac.read_marker(self.root)["restart_policy_disabled"], False)
        self.assertEqual([call.args[0][:4] for call in self.run.call_args_list], [
            ["ps", "-axww", "-o", "pid="],
            ["docker", "--context", "desktop-linux", "info"],
            ["docker", "--context", "desktop-linux", "info"],
            ["ps", "-axww", "-o", "pid="],
        ])

    def test_repeated_pause_while_docker_is_down_keeps_legacy_marker_and_advises_proxy_off(self):
        legacy = {"paused_at": "2026-11-21T21:05:00+00:00", "reason": "maintenance", "containers": []}
        path = fleet_mac.marker_path(self.root)
        path.parent.mkdir(parents=True)
        fleet_mac.write_json(path, legacy)
        self.run.side_effect = [self.result(), self.result(code=1), self.result(code=1), self.result(code=1)]
        code, output = self.invoke("pause", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual(fleet_mac.read_marker(self.root), {**legacy, "restart_policy_disabled": False})
        code, output = self.invoke("status")
        self.assertEqual(code, 0, output)
        self.assertIn("Mac proxy restart policy: unverified", output)
        self.assertIn("run `mac proxy-off` right after starting Docker", output)

    def test_proxy_off_requires_pause_marker_and_running_engine(self):
        code, output = self.invoke("proxy-off")
        self.assertEqual(code, 1, output)
        self.assertIn("requires a pause marker", output)
        self.run.assert_not_called()
        fleet_mac.write_marker(self.root, None, [])
        self.run.side_effect = [self.result(code=1)]
        code, output = self.invoke("proxy-off")
        self.assertEqual(code, 1, output)
        self.assertIn("requires the Docker engine to be up", output)
        self.run.assert_called_once()

    def test_proxy_off_saves_original_policy_and_stops_only_the_proxy_under_lock(self):
        marker = fleet_mac.write_marker(self.root, "maintenance", ["other-service"])
        actions = []

        def run(argv, **kwargs):
            with fleet_mac.transition_lock(self.root, blocking=False) as acquired:
                self.assertFalse(acquired)
            self.assertEqual(argv[:3], fleet_mac.DESKTOP)
            action = argv[3]
            actions.append(action)
            if action == "inspect":
                return self.result('{"Name":"always","MaximumRetryCount":0}')
            if action == "update":
                self.assertEqual(argv[4:], ["--restart=no", "cli-proxy-api"])
                self.assertEqual(fleet_mac.read_marker(self.root)["restart_policy"], "always")
            if action == "stop":
                self.assertEqual(argv[4:], ["cli-proxy-api"])
            return self.result()

        self.run.side_effect = run
        for expected in (["info", "inspect", "update", "stop"], ["info", "update", "stop"]):
            with self.subTest(actions=expected):
                actions.clear()
                code, output = self.invoke("proxy-off")
                self.assertEqual(code, 0, output)
                self.assertEqual(actions, expected)
                self.assertEqual(fleet_mac.read_marker(self.root), {
                    **marker, "restart_policy": "always", "restart_policy_disabled": True,
                })

    def test_engine_probe_timeout_and_os_error_are_unknown_not_down(self):
        self.run.side_effect = [subprocess.TimeoutExpired("docker info", 5), FileNotFoundError(),
                                self.result(code=1), self.result()]
        self.assertEqual(fleet_mac.engine_state(), "unknown")
        self.assertEqual(fleet_mac.engine_state(), "unknown")
        self.assertEqual(fleet_mac.engine_state(), "down")
        self.assertEqual(fleet_mac.engine_state(), "up")
        self.run.side_effect = [subprocess.TimeoutExpired("docker info", 5)]
        self.assertFalse(fleet_mac.engine_up())

    def test_pause_refuses_when_the_engine_is_too_slow_to_answer_inside_the_lock(self):
        # A running engine that times out on docker info must not be recorded as already down.
        self.run.side_effect = [self.result(), self.result(), self.result("cli-proxy-api\n"),
                                subprocess.TimeoutExpired("docker info", 5)]
        code, output = self.invoke("pause", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("Could not tell whether the Docker engine is running", output)
        self.assertNotIn("already down", output)
        self.assertIsNone(fleet_mac.read_marker(self.root))
        self.assertNotIn(["docker", "desktop", "stop"], [call.args[0] for call in self.run.call_args_list])

    def test_pause_refuses_when_the_engine_stops_after_its_containers_were_listed(self):
        self.run.side_effect = [self.result(), self.result(), self.result("cli-proxy-api\n"),
                                self.result(code=1)]
        code, output = self.invoke("pause", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("Docker engine stopped during preflight", output)
        self.assertIsNone(fleet_mac.read_marker(self.root))

    def test_pause_leaves_a_newly_started_engine_up_for_container_review(self):
        self.run.side_effect = [self.result(), self.result(code=1), self.result()]
        with patch("builtins.input", return_value="yes") as confirm:
            code, output = self.invoke("pause")
        self.assertNotEqual(code, 0)
        confirm.assert_called_once_with("Pause Mac fallback while Docker is down? [y/N] ")
        self.assertIn("Docker engine started during preflight", output)
        self.assertEqual(self.run.call_count, 3)
        self.assertIsNone(fleet_mac.read_marker(self.root))

    def test_pause_refuses_mac_only_sessions_and_only_displays_safe_fields(self):
        launcher = "/opt/homebrew/bin/node /test/claude_account_launcher.mjs"
        self.run.side_effect = [
            self.result(
                f"11 01:15 {launcher}\n12 00:22 {launcher}\n"
                '13 03:00 /opt/homebrew/bin/codex -c '
                'model_providers.cliproxy.base_url="http://127.0.0.1:8317/v1" app-server\n'
                '14 03:01 /opt/homebrew/bin/codex -c '
                'model_providers.cliproxy.base_url="https://will.invalid:8317/v1" app-server\n'
                '15 03:02 /opt/homebrew/bin/codex -c base_url="http://127.0.0.1:8317/v1" exec\n'
            ),
            self.result(launcher + " CLIPROXY_BASE_URL=http://[::1]:8317 PRIVATE=" + CANARY),
            self.result(launcher + " CLIPROXY_BASE_URL=http://localhost:8317 "
                        "CLIPROXY_FALLBACK_URL=https://will.invalid:8317 PRIVATE=" + CANARY),
        ]
        code, output = self.invoke("pause", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("11 01:15 Claude", output)
        self.assertIn("13 03:00 Codex", output)
        for item in ("12 00:22", "14 03:01", "15 03:02", CANARY, "CLIPROXY_BASE_URL"):
            self.assertNotIn(item, output)
        self.assertEqual(self.run.call_count, 3)
        self.assertEqual(self.run.call_args_list[1].args[0],
                         ["ps", "eww", "-p", "11", "-o", "command="])
        self.assertIsNone(fleet_mac.read_marker(self.root))

    def test_session_scan_ignores_the_t3_shell_wrapper_that_names_the_launcher(self):
        # T3's per-session /bin/sh wrapper names the launcher in its arguments, but ps eww shows it
        # no environment. Only node running the launcher is inspected.
        launcher = "node /test/claude_account_launcher.mjs --resume=abc"
        wrapper = ("/bin/sh -c read -r _ <&4 || exit 1\\012exec 4<&-\\012 sh "
                   "/test/claude_account_launcher.mjs --resume=abc")
        self.run.side_effect = [
            self.result(f"21 00:10 {wrapper}\n22 00:10 {launcher}\n"),
            self.result(launcher + " CLIPROXY_BASE_URL=http://127.0.0.1:8317 PRIVATE=" + CANARY),
        ]
        self.assertEqual(fleet_mac.mac_only_sessions(8317), [("22", "00:10", "Claude via Mac proxy")])
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.run.call_args_list[1].args[0], ["ps", "eww", "-p", "22", "-o", "command="])

    def test_codex_scan_matches_loopback_variants_on_mac_port(self):
        urls = (
            '"http://localhost:8317/v1/"',
            "'http://[::1]:8317/v1'",
            "http://127.0.0.2:8317/v1/",
            '"http://127.1:8317/v1"',
            '"http://[::ffff:127.0.0.1]:8317/v1"',
            '"http://localhost:8318/v1"',
            '"http://will.invalid:8317/v1/"',
            '"http://127.0.0.1:8317/v10"',
            '"http://localhost:invalid/v1"',
        )
        self.run.side_effect = [self.result("".join(
            f"{pid} 00:10 /opt/homebrew/bin/codex -c model_providers.cliproxy.base_url={url} app-server\n"
            for pid, url in enumerate(urls, 11)
        ))]
        self.assertEqual(fleet_mac.mac_only_sessions(8317),
                         [(str(pid), "00:10", "Codex via Mac proxy") for pid in range(11, 16)])

    def test_pause_lists_containers_and_can_be_declined(self):
        self.run.side_effect = [self.result(), self.result(), self.result("cli-proxy-api\nother-service\n")]
        with (patch("builtins.input", return_value="n") as confirm,
              patch.object(fleet_mac, "transition_lock") as transition):
            code, output = self.invoke("pause")
        self.assertNotEqual(code, 0)
        self.assertIn("cli-proxy-api", output)
        self.assertIn("other-service", output)
        confirm.assert_called_once()
        transition.assert_not_called()
        self.assertIsNone(fleet_mac.read_marker(self.root))
        self.assertEqual(self.run.call_count, 3)

    def test_pause_rechecks_will_and_sessions_under_lock_after_confirmation(self):
        for failure in ("Will", "new Mac-only session"):
            with self.subTest(failure=failure):
                events = []
                old_lock = fleet_mac.transition_lock

                def probe(name, *args):
                    events.append("will")
                    with old_lock(self.root, blocking=False) as acquired:
                        self.assertEqual(acquired, "confirm" not in events)
                    return {"status": "unreachable" if failure == "Will" and not acquired else "ok"}

                def run(argv, **kwargs):
                    if argv[0] == "ps":
                        events.append("sessions")
                        if "confirm" in events:
                            with old_lock(self.root, blocking=False) as acquired:
                                self.assertFalse(acquired)
                            if failure == "new Mac-only session":
                                return self.result("13 00:01 codex -c base_url=http://localhost:8317/v1/ app-server")
                        return self.result()
                    if argv[3:4] == ["info"]:
                        return self.result()
                    self.assertEqual(argv[3:4], ["ps"])
                    events.append("containers")
                    return self.result("cli-proxy-api")

                def confirm(prompt):
                    transition.assert_not_called()
                    events.append("confirm")
                    return "yes"

                self.probe.side_effect = probe
                self.run.side_effect = run
                with (patch.object(fleet_mac, "transition_lock", wraps=old_lock) as transition,
                      patch("builtins.input", side_effect=confirm)):
                    code, output = self.invoke("pause")
                self.assertNotEqual(code, 0)
                self.assertIn("Rechecking Will and session routes", output)
                self.assertEqual(events, ["will", "sessions", "containers", "confirm", "will", "sessions"])
                transition.assert_called_once_with(self.root)
                self.assertIsNone(fleet_mac.read_marker(self.root))

    def test_pause_requires_marker_support_in_all_bundle_files(self):
        for name in ("fleet_mac.py", "cliproxy_watchdog.py", "fleet_monitor.py"):
            with self.subTest(name=name):
                bundle_file = self.bundle / name
                original = bundle_file.read_text(encoding="utf-8")
                if name == "fleet_mac.py":
                    bundle_file.unlink()
                else:
                    bundle_file.write_text("# Old bundle script", encoding="utf-8")
                self.run.side_effect = [self.result(), self.result(code=1), self.result(code=1)]
                code, output = self.invoke("pause", "--yes")
                self.assertNotEqual(code, 0)
                self.assertIn("Ship the bundle first", output)
                self.assertIsNone(fleet_mac.read_marker(self.root))
                bundle_file.write_text(original, encoding="utf-8")

    def test_forced_pause_writes_private_marker_before_graceful_stop(self):
        self.probe.return_value = {"status": "unreachable"}
        fleet_mac.http.client.HTTPSConnection.return_value.getresponse.return_value.status = 503
        self.write_monitor_health(datetime.now(timezone.utc), "egress-down")
        (self.bundle / "fleet_mac.py").unlink()
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "ps":
                return self.result('13 03:00 codex app-server -c base_url="http://127.0.0.1:8317/v1"\n')
            if argv[3:4] == ["ps"]:
                return self.result("cli-proxy-api\nother-service\n")
            if argv[3:4] == ["inspect"]:
                return self.result('{"Name":"always","MaximumRetryCount":0}')
            if argv == [*fleet_mac.DESKTOP, "update", "--restart=no", "cli-proxy-api"]:
                self.assertEqual(fleet_mac.read_marker(self.root)["restart_policy"], "always")
                self.assertIs(fleet_mac.read_marker(self.root)["restart_policy_disabled"], False)
                return self.result()
            if argv == ["docker", "desktop", "stop"]:
                marker = fleet_mac.read_marker(self.root)
                self.assertEqual(marker["containers"], ["cli-proxy-api", "other-service"])
                self.assertEqual(marker["reason"], "maintenance")
                self.assertEqual(marker["restart_policy"], "always")
                self.assertIs(marker["restart_policy_disabled"], True)
                self.assertEqual(datetime.fromisoformat(marker["paused_at"]).utcoffset(),
                                 timezone.utc.utcoffset(None))
                self.assertEqual(stat.S_IMODE(fleet_mac.marker_path(self.root).stat().st_mode), 0o600)
                self.assertGreater(kwargs["timeout"], 0)
                return self.result()
            self.assertEqual(argv[:4], ["docker", "--context", "desktop-linux", "info"])
            return self.result(code=int(["docker", "desktop", "stop"] in calls))

        self.run.side_effect = run
        code, output = self.invoke("pause", "--force", "--yes", "--reason", "maintenance")
        self.assertEqual(code, 0, output)
        self.assertIn("Docker engine is down", output)
        self.assertIn("13 03:00 Codex", output)
        self.assertIn("Ship the bundle first", output)
        self.assertEqual(output.count("Will proxy at client origin: unhealthy"), 2)
        self.assertEqual(output.count("Fleet monitor Will status: unhealthy"), 2)
        self.assertEqual(len(calls), 9)

    def test_stop_failure_keeps_marker_for_retry(self):
        self.run.side_effect = [self.result(), self.result(), self.result("cli-proxy-api\n"),
                                self.result(), self.result(), self.result('{"Name":"always","MaximumRetryCount":0}'),
                                self.result(),
                                subprocess.TimeoutExpired(["docker", "desktop", "stop"], 45)]
        code, output = self.invoke("pause", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("marker remains", output)
        self.assertEqual(fleet_mac.read_marker(self.root)["containers"], ["cli-proxy-api"])
        self.assertEqual(fleet_mac.read_marker(self.root)["restart_policy"], "always")

    def test_resume_waits_for_engine_then_clears_marker_before_health(self):
        fleet_mac.write_marker(self.root, None, ["cli-proxy-api"])
        calls = []

        def run(argv, **kwargs):
            self.assertIsNotNone(fleet_mac.read_marker(self.root))
            if argv == ["docker", "desktop", "start", "--detach"]:
                calls.append("start")
                return self.result()
            self.assertEqual(argv[:4], ["docker", "--context", "desktop-linux", "info"])
            calls.append("engine")
            return self.result(code=0 if len(calls) == 4 else 1)

        def health(name, *args):
            calls.append("health")
            self.assertIsNone(fleet_mac.read_marker(self.root))
            self.assertEqual(name, "m5")
            with fleet_mac.transition_lock(self.root, blocking=False) as acquired:
                self.assertTrue(acquired)
            return {"status": "ok"}

        self.run.side_effect = run
        self.probe.side_effect = health
        with patch.object(fleet_mac.time, "sleep") as sleep:
            code, output = self.invoke("resume")
        self.assertEqual(code, 0, output)
        self.assertEqual(calls, ["engine", "start", "engine", "engine", "health"])
        sleep.assert_called_once()
        self.assertIn("Mac proxy is healthy", output)

    def test_resume_engine_wait_expiry_leaves_marker(self):
        fleet_mac.write_marker(self.root, None, [])
        self.run.side_effect = [self.result(code=1), self.result(), self.result(code=1)]
        with patch.object(fleet_mac.time, "monotonic", side_effect=[0, 1, 1_000]):
            code, output = self.invoke("resume")
        self.assertNotEqual(code, 0)
        self.assertIn("marker remains", output)
        self.assertIsNotNone(fleet_mac.read_marker(self.root))
        self.probe.assert_not_called()

    def test_resume_waits_for_a_slow_engine_without_starting_it_again(self):
        fleet_mac.write_marker(self.root, None, [])
        self.run.side_effect = [subprocess.TimeoutExpired("docker info", 5), self.result()]
        code, output = self.invoke("resume")
        self.assertEqual(code, 0, output)
        self.assertNotIn(["docker", "desktop", "start", "--detach"],
                         [call.args[0] for call in self.run.call_args_list])
        self.assertIsNone(fleet_mac.read_marker(self.root))

    def test_resume_skips_start_when_engine_is_already_up(self):
        fleet_mac.write_marker(self.root, None, [])
        self.run.side_effect = [self.result()]
        code, output = self.invoke("resume")
        self.assertEqual(code, 0, output)
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0][:4], ["docker", "--context", "desktop-linux", "info"])
        self.assertIsNone(fleet_mac.read_marker(self.root))
        self.probe.assert_called_once_with("m5", self.hosts, None, None, None, "health")

    def test_status_reports_pause_timestamp_engine_and_both_hosts(self):
        fleet_mac.write_marker(self.root, None, [])
        marker = fleet_mac.read_marker(self.root)
        self.run.side_effect = [self.result(code=1)]
        self.probe.side_effect = [{"status": "unreachable"}, {"status": "ok"}]
        code, output = self.invoke("status")
        self.assertEqual(code, 0, output)
        self.assertIn("Mac paused since " + marker["paused_at"], output)
        self.assertIn("Docker engine: down", output)
        self.assertNotIn("Docker is running while paused", output)
        self.assertIn("Mac proxy: unreachable", output)
        self.assertIn("Will proxy: ok", output)

    def test_status_warns_when_the_engine_is_up_with_a_marker(self):
        fleet_mac.write_marker(self.root, None, [])
        self.run.side_effect = [self.result()]
        code, output = self.invoke("status")
        self.assertEqual(code, 0, output)
        self.assertIn("Docker is running while paused with no live lease", output)
        self.assertIn("THREADS.md and the running containers before stopping it. mac pause stops every container.", output)

    def test_lease_cli_acquire_renew_list_and_release(self):
        self.run.return_value = self.result()
        self.run.side_effect = None
        code, output = self.invoke("lease", "acquire", "--thread", "ae4ac55e", "--project", "omxlapi",
                                   "--purpose", "benchmark", "--why-mac", "local sandbox", "--approved", "private quote")
        self.assertEqual(code, 0, output)
        lease, = fleet_mac.read_leases(self.root)
        lease_id = lease["id"]
        self.assertIn(lease_id, output)
        self.assertNotIn("private quote", output)
        self.assertIn("Docker work defaults to Will; this lease is a Mac failover.", output)
        self.run.assert_not_called()
        code, output = self.invoke("lease", "renew", lease_id, "--hours", "12")
        self.assertEqual(code, 0, output)
        code, output = self.invoke("lease", "list")
        self.assertEqual(code, 0, output)
        self.assertIn(f'Docker lease {lease_id} (omxlapi, thread ae4ac55e) "benchmark"', output)
        fleet_mac.write_marker(self.root, None, [])
        code, output = self.invoke("lease", "release", lease_id, "--thread", "ae4ac55e")
        self.assertEqual(code, 0, output)
        self.assertIn("No live leases. Docker is still running while the Mac is paused", output)
        self.assertEqual(fleet_mac.read_leases(self.root), [])
        self.assertEqual([call.args[0] for call in self.run.call_args_list], [
            [*fleet_mac.DESKTOP, "info", "--format", "{{.ServerVersion}}"],
        ])

    def test_lease_release_requires_the_matching_thread(self):
        lease_id = self.acquire_lease()["id"]
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as usage:
            fleet.main(["mac", "lease", "release", lease_id], settings_path=self.settings)
        self.assertEqual(usage.exception.code, 2)
        self.assertIn("--thread", output.getvalue())
        code, output = self.invoke("lease", "release", lease_id, "--thread", "other-thread")
        self.assertEqual(code, 1, output)
        self.assertIn("Only the owning thread can release", output)
        self.assertEqual(fleet_mac.live_leases(self.root)[0]["id"], lease_id)
        self.run.assert_not_called()

    def test_pause_refuses_a_live_lease_even_with_force(self):
        lease = self.acquire_lease()
        for flags in ((), ("--force",), ("--force", "--break-lease", lease["id"])):
            with self.subTest(flags=flags):
                code, output = self.invoke("pause", "--yes", *flags)
                self.assertEqual(code, 1, output)
                self.assertIn("--break-reason" if len(flags) > 1 else lease["id"], output)
                if len(flags) <= 1:
                    self.assertIn("Only the owning thread releases it; to override, use --break-lease ID --break-reason", output)
        self.run.assert_not_called()
        self.assertTrue(fleet_mac.read_leases(self.root)[0]["live"])
        self.assertIsNone(fleet_mac.read_marker(self.root))

    def test_dangling_lease_directory_blocks_forced_pause(self):
        directory = fleet_mac.lease_dir(self.root)
        directory.parent.mkdir(parents=True)
        directory.symlink_to(self.root / "missing", target_is_directory=True)
        code, output = self.invoke("pause", "--yes", "--force")
        self.assertEqual(code, 1, output)
        self.assertIn("Docker lease directory is unreadable (unknown owner)", output)
        self.run.assert_not_called()
        self.assertIsNone(fleet_mac.read_marker(self.root))

    def test_pause_prints_each_expired_lease_and_prompts_even_with_yes(self):
        now = datetime(2026, 11, 22, 5, 4, tzinfo=timezone.utc)
        with patch.object(fleet_mac, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now - timedelta(hours=4)
            first, second = self.acquire_lease(), self.acquire_lease(project="CodeOne")
            clock.now.return_value = now
            self.run.side_effect = self.docker_pause_runner
            with patch("builtins.input", side_effect=["no", "yes"]) as confirm:
                code, output = self.invoke("pause", "--yes")
                self.assertEqual(code, 1, output)
                self.assertIsNone(fleet_mac.read_marker(self.root))
                self.assertFalse(getattr(self, "desktop_stopped", False))
                for lease in (first, second):
                    self.assertIn(f"Lease {lease['id']} expired at", output)
                code, output = self.invoke("pause", "--yes")
            self.assertEqual(confirm.call_count, 2)
        self.assertEqual(code, 0, output)
        self.assertTrue(self.desktop_stopped)

    def test_pause_refuses_to_stop_if_an_unacknowledged_expired_lease_appears(self):
        now = datetime(2026, 11, 22, 5, 4, tzinfo=timezone.utc)
        lease = None

        def confirm(prompt):
            nonlocal lease
            lease = self.acquire_lease(hours=1)
            clock.now.return_value = now + timedelta(hours=1)
            return "yes"

        self.run.side_effect = self.docker_pause_runner
        with (patch.object(fleet_mac, "datetime", wraps=datetime) as clock,
              patch("builtins.input", side_effect=confirm)):
            clock.now.return_value = now
            code, output = self.invoke("pause")
        self.assertEqual(code, 1, output)
        self.assertIn(f"Lease {lease['id']} expired at", output)
        self.assertIn("Run mac pause again to confirm", output)
        self.assertIsNone(fleet_mac.read_marker(self.root))
        self.assertFalse(getattr(self, "desktop_stopped", False))

    def test_pause_rechecks_a_lease_acquired_after_preflight(self):
        lease_id = None

        def confirm(prompt):
            nonlocal lease_id
            lease_id = self.acquire_lease()["id"]
            return "yes"

        self.run.side_effect = self.docker_pause_runner
        with patch("builtins.input", side_effect=confirm):
            code, output = self.invoke("pause", "--force")
        self.assertEqual(code, 1, output)
        self.assertIn(lease_id, output)
        self.assertIsNone(fleet_mac.read_marker(self.root))
        self.assertFalse(getattr(self, "desktop_stopped", False))

    def test_pause_breaks_only_named_leases_and_records_them(self):
        first, second = self.acquire_lease(), self.acquire_lease(project="CodeOne")
        flags = ("--break-lease", first["id"], "--break-reason", "planned maintenance")
        code, output = self.invoke("pause", "--force", "--yes", *flags)
        self.assertEqual(code, 1, output)
        self.assertIn(second["id"], output)
        self.run.assert_not_called()
        self.run.side_effect = self.docker_pause_runner
        code, output = self.invoke("pause", "--yes", *flags, "--break-lease", second["id"])
        self.assertEqual(code, 0, output)
        self.assertTrue(self.desktop_stopped)
        self.assertEqual(fleet_mac.read_leases(self.root), [])
        marker = fleet_mac.read_marker(self.root)
        self.assertEqual({record["id"] for record in marker["broken_leases"]}, {first["id"], second["id"]})
        for record in marker["broken_leases"]:
            archived = fleet_mac.lease_dir(self.root) / "broken" / (record["id"] + ".json")
            self.assertEqual(json.loads(archived.read_text()), record)
            self.assertEqual(record["broken_by_reason"], "planned maintenance")
            self.assertEqual(record["containers"], ["cli-proxy-api", "other-service"])
            self.assertIn("broken_at", record)

    def test_failure_breaking_second_lease_keeps_first_receipts_and_allows_retry(self):
        first, second = "a" * 16, "b" * 16
        with patch.object(fleet_mac.secrets, "token_hex", side_effect=[first, second]):
            self.acquire_lease()
            self.acquire_lease(project="CodeOne")
        directory = fleet_mac.lease_dir(self.root)
        archive = directory / "broken"
        write_json = fleet_mac.write_json

        def fail_second_marker_write(path, value, **kwargs):
            if path == fleet_mac.marker_path(self.root) and any(
                    record["id"] == second for record in value.get("broken_leases", [])):
                raise PermissionError("second lease marker update failed")
            write_json(path, value, **kwargs)

        self.run.side_effect = self.docker_pause_runner
        with patch.object(fleet_mac, "write_json", side_effect=fail_second_marker_write):
            code, output = self.invoke("pause", "--yes", "--break-reason", "maintenance",
                                       "--break-lease", first, "--break-lease", second)
        self.assertEqual(code, 1, output)
        self.assertFalse(getattr(self, "desktop_stopped", False))
        self.assertEqual([lease["id"] for lease in fleet_mac.live_leases(self.root)], [second])
        first_archive = json.loads((archive / f"{first}.json").read_text())
        self.assertEqual(fleet_mac.read_marker(self.root)["broken_leases"], [first_archive])
        self.assertTrue((archive / f"{second}.json").is_file())

        code, output = self.invoke("pause", "--yes", "--break-lease", second, "--break-reason", "maintenance")
        self.assertEqual(code, 0, output)
        self.assertTrue(self.desktop_stopped)
        self.assertEqual(fleet_mac.read_leases(self.root), [])
        second_archive = json.loads((archive / f"{second}.json").read_text())
        self.assertEqual(fleet_mac.read_marker(self.root)["broken_leases"], [first_archive, second_archive])

    def test_pause_requires_a_named_break_for_a_malformed_lease(self):
        lease = self.acquire_lease()
        filename = f"{lease['id']}.json"
        directory = fleet_mac.lease_dir(self.root)
        (directory / filename).write_text("{")
        code, output = self.invoke("pause", "--force", "--yes")
        self.assertEqual(code, 1, output)
        self.assertIn("unreadable (unknown owner)", output)
        self.run.assert_not_called()

        self.run.side_effect = self.docker_pause_runner
        code, output = self.invoke("pause", "--yes", "--break-lease", lease["id"], "--break-reason", "invalid record")
        self.assertEqual(code, 0, output)
        self.assertFalse((directory / filename).exists())
        self.assertEqual(json.loads((directory / "broken" / filename).read_text())["owner"], "unknown")

    def test_pause_again_keeps_the_original_restart_policy(self):
        self.run.side_effect = self.docker_pause_runner
        code, output = self.invoke("pause", "--yes")
        self.assertEqual(code, 0, output)
        self.desktop_stopped = False
        code, output = self.invoke("pause", "--yes")
        self.assertEqual(code, 0, output)
        self.assertEqual(fleet_mac.read_marker(self.root)["restart_policy"], "on-failure:5")

    def test_resume_requires_lease_acknowledgment_and_restores_policy_before_recovery(self):
        lease = self.acquire_lease()
        fleet_mac.write_marker(self.root, None, [], restart_policy="on-failure:5")
        code, output = self.invoke("resume")
        self.assertEqual(code, 1, output)
        self.assertIn(lease["id"], output)
        self.assertIn("--yes-leases", output)
        self.run.assert_not_called()

        events = []

        def run(argv, **kwargs):
            self.assertIsNotNone(fleet_mac.read_marker(self.root))
            if argv == ["docker", "desktop", "start", "--detach"]:
                events.append("desktop start")
                return self.result()
            if argv[:4] == [*fleet_mac.DESKTOP, "inspect"]:
                return self.result('{"Name":"no","MaximumRetryCount":0}')
            if argv == [*fleet_mac.DESKTOP, "update", "--restart=on-failure:5", "cli-proxy-api"]:
                self.assertIn("desktop start", events)
                events.append("restore policy")
                return self.result()
            if argv == [*fleet_mac.DESKTOP, "start", "cli-proxy-api"]:
                events.append("start proxy")
                return self.result()
            self.assertEqual(argv[:4], [*fleet_mac.DESKTOP, "info"])
            return self.result(code=int("desktop start" not in events))

        def health(name, *args):
            self.assertIsNone(fleet_mac.read_marker(self.root))
            events.append("health")
            return {"status": "ok"}

        self.run.side_effect = run
        self.probe.side_effect = health
        code, output = self.invoke("resume", "--yes-leases")
        self.assertEqual(code, 0, output)
        self.assertEqual(events, ["desktop start", "restore policy", "start proxy", "health"])
        self.assertEqual(fleet_mac.read_leases(self.root)[0]["id"], lease["id"])

    def test_resume_policy_update_failure_preserves_pause(self):
        fleet_mac.write_marker(self.root, None, [], restart_policy="always")
        self.run.side_effect = [self.result(), self.result('{"Name":"no","MaximumRetryCount":0}'), self.result(code=1)]
        code, output = self.invoke("resume")
        self.assertEqual(code, 1, output)
        self.assertIn("pause marker remains", output)
        self.assertEqual(fleet_mac.read_marker(self.root)["restart_policy"], "always")
        self.probe.assert_not_called()

    def test_resume_leaves_missing_proxy_to_watchdog_compose_recovery(self):
        fleet_mac.write_marker(self.root, None, [], restart_policy="always", restart_policy_disabled=True)
        self.run.side_effect = [self.result(), self.result(code=1,
                                stderr="Error response from daemon: No such container: cli-proxy-api")]
        code, output = self.invoke("resume")
        self.assertEqual(code, 0, output)
        self.assertIn("The watchdog's compose recovery will recreate it", output)
        self.assertIsNone(fleet_mac.read_marker(self.root))
        self.assertEqual([call.args[0][3] for call in self.run.call_args_list], ["info", "inspect"])

    def test_resume_reports_failed_marker_removal_and_rolls_back_under_lock(self):
        path = fleet_mac.marker_path(self.root)
        unlink = Path.unlink

        def fail_marker_unlink(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError("pause marker cannot be removed")
            return unlink(candidate, *args, **kwargs)

        for rollback_ok in (True, False):
            with self.subTest(rollback_ok=rollback_ok):
                fleet_mac.write_marker(self.root, None, [], restart_policy="always", restart_policy_disabled=True)
                actions = []

                def run(argv, **kwargs):
                    with fleet_mac.transition_lock(self.root, blocking=False) as acquired:
                        self.assertFalse(acquired)
                    actions.append(argv[3:])
                    if argv[3] == "inspect":
                        return self.result('{"Name":"no","MaximumRetryCount":0}')
                    if argv[3:] == ["update", "--restart=no", "cli-proxy-api"]:
                        return self.result(code=int(not rollback_ok))
                    return self.result()

                self.run.side_effect = run
                with patch.object(Path, "unlink", new=fail_marker_unlink):
                    code, output = self.invoke("resume")
                self.assertEqual(code, 1, output)
                self.assertIn("Could not remove the pause marker", output)
                expected = "restart policy back to no" if rollback_ok else "Could not re-disable the restart policy"
                self.assertIn(expected, output)
                self.assertEqual(actions[2:5], [["update", "--restart=always", "cli-proxy-api"],
                                               ["start", "cli-proxy-api"],
                                               ["update", "--restart=no", "cli-proxy-api"]])
                self.assertEqual(actions[5:], [["stop", "cli-proxy-api"]] if rollback_ok else [])
                self.assertIs(fleet_mac.read_marker(self.root)["restart_policy_disabled"], rollback_ok)
                self.probe.assert_not_called()

    def test_status_names_a_live_lease_and_displays_expired_lease(self):
        now = datetime(2026, 11, 22, 5, 4, tzinfo=timezone.utc)
        with patch.object(fleet_mac, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now - timedelta(hours=4)
            expired = self.acquire_lease(project="CodeOne")
            clock.now.return_value = now
            live = self.acquire_lease()
            fleet_mac.write_marker(self.root, None, [], restart_policy="always", restart_policy_disabled=True)
            self.run.side_effect = [self.result()]
            code, output = self.invoke("status")
        self.assertEqual(code, 0, output)
        self.assertIn(f'Docker is running for lease {live["id"]} (omxlapi, thread ae4ac55e, "agent benchmark") '
                      'until 12:04 AM PST.', output)
        self.assertIn("Mac proxy restart policy: recorded as disabled.", output)
        self.assertIn(f'Lease {expired["id"]} expired at 2026-11-21 8:04 PM PST; '
                      'ask thread ae4ac55e to release it or acquire a new lease.', output)
