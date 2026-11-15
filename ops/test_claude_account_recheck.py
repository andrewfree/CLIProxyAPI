import copy
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from ops import claude_account_recheck as check


TIER = 'default_claude_max_20x'
INDEX = 'selected-account'
EMAIL = 'private@example.com'


class FakeProxy:
    def __init__(self):
        self.account = {
            'auth_index': INDEX, 'id': 'credential-id', 'provider': 'claude', 'email': EMAIL,
            'disabled': False, 'requires_sign_in': False,
            'cooldowns': [{'scope': 'credential', 'reason': 'credential_quota',
                           'retry_at': '2026-11-15T07:07:29Z', 'remaining_seconds': 1800}],
        }
        self.other = {'auth_index': 'other', 'provider': 'claude', 'unavailable': True}
        self.profile = {'account': {'email': EMAIL}, 'organization': {'rate_limit_tier': TIER, 'uuid': 'secret-org'}}
        self.verify = {'status': 'ok', 'verified': True}
        self.calls = []
        self.on_verify = lambda: self.account.update(cooldowns=[])

    def mgmt(self, route, payload=None):
        self.calls.append((route, copy.deepcopy(payload)))
        if route == '/v8/management/credentials':
            return copy.deepcopy({'files': [self.account, self.other]})
        if route == '/v8/management/requests/api-call':
            if payload['url'] != check.PROFILE_URL or payload['method'] != 'GET':
                raise AssertionError('only the profile read may use api-call')
            return {'status_code': 200, 'body': json.dumps(self.profile)}
        if route == '/v8/management/credentials/verify':
            self.on_verify()
            return self.verify
        raise AssertionError(route)

    @property
    def probes(self):
        return [p for r, p in self.calls if r == '/v8/management/credentials/verify']


class RecheckTests(unittest.TestCase):
    def test_default_only_reads_profile(self):
        proxy = FakeProxy()
        result = check.recheck(proxy, INDEX, TIER)
        self.assertEqual(result['result'], 'dry_run')
        self.assertEqual(proxy.probes, [])
        self.assertTrue(proxy.account['cooldowns'])

    def test_success_uses_server_verification_without_blanket_reset(self):
        proxy = FakeProxy()
        other = copy.deepcopy(proxy.other)
        result = check.recheck(proxy, INDEX, TIER, apply=True)
        self.assertEqual(result['result'], 'recovered')
        self.assertEqual(proxy.probes, [{'auth_index': INDEX, 'model': check.MODEL}])
        self.assertEqual(proxy.other, other)
        self.assertNotIn(EMAIL, json.dumps(result))
        self.assertNotIn('secret-org', json.dumps(result))
        self.assertFalse(any('cooldown/reset' in route for route, _ in proxy.calls))

    def test_wrong_profile_identity_or_tier_prevents_probe(self):
        for change in [lambda p: p.profile['account'].update(email='other@example.com'),
                       lambda p: p.profile['organization'].update(rate_limit_tier='default_claude_pro')]:
            proxy = FakeProxy()
            change(proxy)
            with self.assertRaises(check.RecheckError):
                check.recheck(proxy, INDEX, TIER, apply=True)
            self.assertEqual(proxy.probes, [])

    def test_disabled_signin_or_ambiguous_account_prevents_probe(self):
        for key in ['disabled', 'requires_sign_in']:
            proxy = FakeProxy()
            proxy.account[key] = True
            with self.assertRaises(check.RecheckError):
                check.recheck(proxy, INDEX, TIER, apply=True)
            self.assertEqual(proxy.probes, [])
        proxy = FakeProxy()
        proxy.other['auth_index'] = INDEX
        with self.assertRaises(check.RecheckError):
            check.recheck(proxy, INDEX, TIER, apply=True)
        self.assertEqual(proxy.probes, [])

    def test_unverified_response_cannot_claim_recovery(self):
        for response in [{'status': 'ok', 'verified': False}, {'status': 'error', 'verified': False}]:
            proxy = FakeProxy()
            proxy.verify = response
            proxy.on_verify = lambda: None
            with self.assertRaises(check.RecheckError):
                check.recheck(proxy, INDEX, TIER, apply=True)
            self.assertEqual(len(proxy.probes), 1)
            self.assertTrue(proxy.account['cooldowns'])

    def test_remaining_sibling_restriction_is_allowed_and_reported(self):
        proxy = FakeProxy()
        sibling = {'scope': 'model', 'model_key': 'claude-fable-5-2', 'reason': 'quota', 'retry_at': 'later'}
        proxy.on_verify = lambda: proxy.account.update(cooldowns=[sibling])
        result = check.recheck(proxy, INDEX, TIER, apply=True)
        self.assertEqual(result['result'], 'recovered')
        self.assertEqual(result['remaining_model_cooldowns'], [sibling])

    def test_old_server_that_retains_credential_quota_cannot_claim_recovery(self):
        proxy = FakeProxy()
        proxy.on_verify = lambda: None
        with self.assertRaisesRegex(check.RecheckError, 'still blocked'):
            check.recheck(proxy, INDEX, TIER, apply=True)
        self.assertEqual(len(proxy.probes), 1)
        self.assertTrue(proxy.account['cooldowns'])

    def test_identity_change_after_verification_prevents_recovery_claim(self):
        proxy = FakeProxy()
        proxy.on_verify = lambda: proxy.account.update(id='replacement', cooldowns=[])
        with self.assertRaisesRegex(check.RecheckError, 'identity changed'):
            check.recheck(proxy, INDEX, TIER, apply=True)

    def test_cli_writes_safe_error_receipt_without_raw_http_body(self):
        proxy = FakeProxy()

        def rejected():
            raise urllib.error.HTTPError('http://localhost', 429, 'rejected', {}, io.BytesIO(b'raw-private-error'))

        proxy.on_verify = rejected
        with tempfile.TemporaryDirectory() as directory:
            receipt = pathlib.Path(directory) / 'receipt.json'
            with patch.object(check, 'Proxy', return_value=proxy), patch.object(check, 'load_management_key'), \
                    patch('sys.stdout'):
                code = check.main(['--auth-index', INDEX, '--expected-tier', TIER, '--yes', '--receipt', str(receipt)])
            self.assertEqual(code, 1)
            report = json.loads(receipt.read_text())
            self.assertEqual(report['verification_http_status'], 429)
            self.assertFalse(report['cooldown_cleared'])
            self.assertNotIn('raw-private-error', receipt.read_text())


if __name__ == '__main__':
    unittest.main()
