import json
import pathlib
import tempfile
import unittest
from typing import Any

from ops import claude_usage_resets as resets

EMAIL = "person@example.com"


def usage_body(fable: int = 99, weekly: int = 77, cooldown: str | None = None,
               fable_left: int = 1, eligible: bool = True) -> dict[str, Any]:
    """Shape of a 2026-11-11 /api/oauth/usage?cedar_ember=1 response, trimmed."""
    return {
        "limits": [
            {"kind": "weekly_all", "percent": weekly, "resets_at": "2026-11-18T02:00:00+00:00", "scope": None},
            {"kind": "weekly_scoped", "percent": fable, "resets_at": "2026-11-18T02:00:00+00:00",
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        ],
        "weekly_scoped_shares": [
            {"limit_index": 1, "used_percent_of_weekly": fable // 2, "allowance_percent_of_weekly": 50},
        ],
        "cedar_ember": {
            "eligible": eligible, "ineligible_reason": None if eligible else "tier",
            "cooldown_until": cooldown, "next_grant_id": "full-reset-2611",
            "grants": [
                {"id": "full-reset-2611", "label": "One full limit reset", "type": "weekly",
                 "clears": ["seven_day", "seven_day_overage_included"], "resets_left": 1, "usable_now": True,
                 "unavailable_reason": None, "ends_at": "2026-12-10T22:00:00+00:00", "gives_back_weekly": False},
                {"id": "launch-2610-fable", "label": "New model launch: one Fable reset", "type": "full",
                 "clears": ["seven_day_overage_included"], "resets_left": fable_left,
                 "usable_now": fable_left > 0, "unavailable_reason": None if fable_left else "unavailable",
                 "ends_at": "2027-03-09T07:59:00+00:00", "gives_back_weekly": True},
            ],
        },
    }


class FakeProxy(resets.Proxy):
    def __init__(self, usage: list[tuple[int, Any]], claim_response: Any = None, profile_email: str = EMAIL):
        super().__init__("http://proxy.invalid", "unused")
        self.usage = list(usage)
        self.claim_response = claim_response or {"result": "reset", "resets_left": 0}
        self.profile_email = profile_email
        self.calls: list[tuple[str, str, Any]] = []

    def upstream(self, email, method, url, body=None):
        self.calls.append((method, url, body))
        if url == resets.PROFILE_URL:
            return 200, {"account": {"email": self.profile_email}, "organization": {"uuid": "org-1"}}
        if url == resets.USAGE_URL:
            return self.usage.pop(0)
        return 200, self.claim_response

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class SummarizeUsageTest(unittest.TestCase):
    def test_scoped_limit_joins_its_weekly_share(self):
        summary = resets.summarize_usage(usage_body(fable=89, weekly=70))
        self.assertEqual(summary["weekly_percent"], 70)
        self.assertEqual(summary["scoped"]["Fable"]["percent"], 89)
        self.assertEqual(summary["scoped"]["Fable"]["allowance_percent_of_weekly"], 50)
        self.assertEqual(resets.limit_percent(summary, "fable"), 89)
        self.assertEqual(resets.limit_percent(summary, "all"), 70)
        self.assertIsNone(resets.limit_percent(summary, "Opus"))

    def test_grants_are_listed(self):
        grants = resets.summarize_usage(usage_body())["resets"]["grants"]
        self.assertEqual([g["id"] for g in grants], ["full-reset-2611", "launch-2610-fable"])


class CheckClaimableTest(unittest.TestCase):
    def check(self, body, grant="launch-2610-fable"):
        return resets.check_claimable(resets.summarize_usage(body), grant)

    def test_usable_grant_passes(self):
        self.assertEqual(self.check(usage_body())["id"], "launch-2610-fable")

    def test_refusals(self):
        cases = {
            "cooldown": usage_body(cooldown="2026-11-11T17:49:09+00:00"),
            "eligible": usage_body(eligible=False),
            "not usable": usage_body(fable_left=0),
        }
        for message, body in cases.items():
            with self.subTest(message), self.assertRaisesRegex(resets.ResetError, message):
                self.check(body)
        with self.assertRaisesRegex(resets.ResetError, "not offered"):
            self.check(usage_body(), "launch-2610-fable-6mo")
        with self.assertRaisesRegex(resets.ResetError, "malformed"):
            self.check(usage_body(), "../reset")


class ClaimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name)
        self.receipt = self.state / "receipt.json"

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self, proxy, dry_run=False):
        return resets.claim(proxy, EMAIL, "launch-2610-fable", self.state, self.receipt, dry_run, sleep=lambda _: None)

    def test_claim_posts_claude_code_shape_and_records_before_after(self):
        proxy = FakeProxy([(200, usage_body(fable=100)), (200, usage_body(fable=2, weekly=28, fable_left=0))])
        receipt = self.claim(proxy)
        [(_, url, body)] = proxy.posts()
        self.assertEqual(url, "https://api.anthropic.com/api/organizations/org-1/reset_rate_limits")
        self.assertEqual(body["program"], "cedar_ember")
        self.assertEqual(body["grant_id"], "launch-2610-fable")
        self.assertRegex(body["request_id"], resets.REQUEST_ID_RE)
        self.assertEqual(receipt["result"], "reset")
        self.assertEqual(receipt["after"]["scoped"]["Fable"]["percent"], 2)
        self.assertNotIn("org-1", self.receipt.read_text())

    def test_dry_run_sends_nothing(self):
        receipt = self.claim(FakeProxy([(200, usage_body())]), dry_run=True)
        self.assertEqual(receipt["result"], "dry_run")
        self.assertTrue(self.receipt.exists())

    def test_rate_limited_precheck_retries_then_claims(self):
        proxy = FakeProxy([(429, {}), (429, {}), (200, usage_body()), (200, usage_body(fable=2))])
        self.assertEqual(self.claim(proxy)["result"], "reset")
        self.assertEqual(len(proxy.posts()), 1)

    def test_failed_after_check_keeps_claim_outcome(self):
        proxy = FakeProxy([(200, usage_body())] + [(429, {})] * 6)
        receipt = self.claim(proxy)
        self.assertEqual(receipt["result"], "reset")
        self.assertIn("HTTP 429", receipt["after_error"])
        self.assertEqual(json.loads(self.receipt.read_text())["result"], "reset")

    def test_wrong_identity_or_ineligible_never_posts(self):
        for proxy in (FakeProxy([(200, usage_body())], profile_email="other@example.com"),
                      FakeProxy([(200, usage_body(cooldown="2026-11-11T17:49:09+00:00"))])):
            with self.assertRaises(resets.ResetError):
                self.claim(proxy)
            self.assertEqual(proxy.posts(), [])

    def test_rerun_reuses_request_id(self):
        first = resets.request_id_for(self.state, EMAIL, "launch-2610-fable")
        self.assertEqual(resets.request_id_for(self.state, EMAIL, "launch-2610-fable"), first)
        self.assertNotEqual(resets.request_id_for(self.state, EMAIL, "full-reset-2611"), first)

    def test_threshold_guard_refuses_low_or_missing_fable_without_posting(self):
        missing = usage_body()
        missing["limits"] = missing["limits"][:1]
        for body in (usage_body(fable=98), missing):
            proxy = FakeProxy([(200, body)])
            with self.subTest(body=body), self.assertRaisesRegex(resets.ResetError, "below required 99%"):
                resets.claim(proxy, EMAIL, "launch-2610-fable", self.state, self.receipt,
                             False, sleep=lambda _: None, min_fable_percent=99)
            self.assertEqual(proxy.posts(), [])
            self.assertFalse((self.state / "request-ids.json").exists())

    def test_threshold_guard_accepts_99_and_100_and_records_precondition(self):
        for percent in (99, 100):
            proxy = FakeProxy([(200, usage_body(fable=percent)), (200, usage_body(fable=2))])
            with self.subTest(percent=percent):
                receipt = resets.claim(proxy, EMAIL, "launch-2610-fable", self.state, self.receipt,
                                       False, sleep=lambda _: None, min_fable_percent=99)
                self.assertEqual(len(proxy.posts()), 1)
                self.assertEqual(receipt["min_fable_percent"], 99)
                self.assertEqual(receipt["before"]["scoped"]["Fable"]["percent"], percent)

    def test_threshold_guard_rejects_invalid_threshold_before_network(self):
        proxy = FakeProxy([])
        for percent in (-1, 101):
            with self.subTest(percent=percent), self.assertRaisesRegex(resets.ResetError, "between 0 and 100"):
                resets.claim(proxy, EMAIL, "launch-2610-fable", self.state, self.receipt,
                             False, min_fable_percent=percent)
        self.assertEqual(proxy.calls, [])


class CliGuardTest(unittest.TestCase):
    def test_claim_requires_yes_or_dry_run(self):
        args = type("Args", (), {"yes": False, "dry_run": False})()
        self.assertEqual(resets.cmd_claim(FakeProxy([]), args), 2)


if __name__ == "__main__":
    unittest.main()
