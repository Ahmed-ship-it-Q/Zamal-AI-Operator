"""Tests for zamal_core/privacy_filter.py — content sanitizer for external AI dispatch."""
import unittest

from zamal_core.privacy_filter import PrivacyFilter, PRIVACY_FILTER
from zamal_core.store import Denied


class PrivacyFilterPublicTests(unittest.TestCase):
    def setUp(self):
        self.f = PrivacyFilter()

    def test_clean_text_is_public(self):
        r = self.f.check('What is the capital of France?')
        self.assertEqual(r.level, 'PUBLIC')
        self.assertEqual(r.text, 'What is the capital of France?')
        self.assertEqual(r.reasons, [])

    def test_long_clean_text_is_public(self):
        r = self.f.check('Explain how neural networks learn from data. ' * 20)
        self.assertEqual(r.level, 'PUBLIC')

    def test_empty_string_is_public(self):
        r = self.f.check('')
        self.assertEqual(r.level, 'PUBLIC')

    def test_non_string_coerced(self):
        r = self.f.check(42)
        self.assertEqual(r.level, 'PUBLIC')
        self.assertEqual(r.text, '42')


class PrivacyFilterSanitizedTests(unittest.TestCase):
    def setUp(self):
        self.f = PrivacyFilter()

    def test_openai_key_redacted(self):
        r = self.f.check('My key is sk-abcdefghijklmnopqrstuvwxyz12345 for the API.')
        self.assertEqual(r.level, 'SANITIZED')
        self.assertIn('[API_KEY]', r.text)
        self.assertNotIn('sk-abcdefghijklmn', r.text)
        self.assertTrue(any('redacted' in reason for reason in r.reasons))

    def test_github_token_redacted(self):
        r = self.f.check('Token: ghp_abcdefghijklmnopqrstuvwxyz12345')
        self.assertEqual(r.level, 'SANITIZED')
        self.assertNotIn('ghp_abcdefghijklmnopqrstuvwxyz12345', r.text)

    def test_slack_token_redacted(self):
        r = self.f.check('Slack bot token: xoxb-123456789012-abcdefghijklmno')
        self.assertEqual(r.level, 'SANITIZED')
        self.assertNotIn('xoxb-123456789012', r.text)

    def test_google_key_redacted(self):
        r = self.f.check('key=AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ1234567')
        self.assertEqual(r.level, 'SANITIZED')
        self.assertIn('[GOOGLE_KEY]', r.text)

    def test_bearer_token_redacted(self):
        r = self.f.check('Authorization: Bearer eyabcdefghijklmnopqrstuvwxyz12345')
        self.assertEqual(r.level, 'SANITIZED')
        self.assertIn('Bearer [TOKEN]', r.text)

    def test_bearer_short_not_redacted(self):
        # Bearer followed by <16 chars should not be redacted
        r = self.f.check('Bearer abcshort')
        self.assertEqual(r.level, 'PUBLIC')

    def test_jwt_redacted(self):
        text = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.abcdefghij123'
        r = self.f.check(text)
        self.assertEqual(r.level, 'SANITIZED')
        self.assertNotIn('eyJhbGciOiJIUzI1NiJ9', r.text)

    def test_key_equals_value_redacted(self):
        r = self.f.check('api_key = supersecretvalue123')
        self.assertEqual(r.level, 'SANITIZED')
        self.assertIn('[CREDENTIAL]', r.text)

    def test_password_equals_value_redacted(self):
        r = self.f.check("password: myS3cretPass!")
        self.assertEqual(r.level, 'SANITIZED')
        self.assertIn('[CREDENTIAL]', r.text)

    def test_url_with_credentials_redacted(self):
        r = self.f.check('Database: https://admin:password123@db.example.com/mydb')
        self.assertEqual(r.level, 'SANITIZED')
        self.assertIn('[CREDENTIALS]', r.text)
        self.assertNotIn('admin:password123', r.text)

    def test_multiple_secrets_all_redacted(self):
        r = self.f.check(
            'Key: sk-testkey12345678 and token: ghp_abcdefghijklmnopqrstuvwxyz'
        )
        self.assertEqual(r.level, 'SANITIZED')
        self.assertNotIn('sk-testkey12345678', r.text)
        self.assertNotIn('ghp_abcdefghijklmnopqrstuvwxyz', r.text)
        self.assertGreaterEqual(len(r.reasons), 2)


class PrivacyFilterBlockedTests(unittest.TestCase):
    def setUp(self):
        self.f = PrivacyFilter()

    def test_iban_blocked(self):
        r = self.f.check('My bank account: GB29NWBK60161331926819')
        self.assertEqual(r.level, 'BLOCKED')
        self.assertTrue(any('iban' in reason for reason in r.reasons))

    def test_visa_card_blocked(self):
        r = self.f.check('Card number: 4532015112830366')
        self.assertEqual(r.level, 'BLOCKED')
        self.assertTrue(any('payment_card' in reason for reason in r.reasons))

    def test_mastercard_blocked(self):
        r = self.f.check('Pay with: 5425233430109903')
        self.assertEqual(r.level, 'BLOCKED')

    def test_ssn_blocked(self):
        r = self.f.check('Social security: 123-45-6789')
        self.assertEqual(r.level, 'BLOCKED')
        self.assertTrue(any('ssn' in reason for reason in r.reasons))

    def test_uuid_not_blocked(self):
        r = self.f.check('Task ID: 550e8400-e29b-41d4-a716-446655440000')
        self.assertNotEqual(r.level, 'BLOCKED')

    def test_ordinary_number_not_blocked(self):
        r = self.f.check('The count is 12345678')
        self.assertNotEqual(r.level, 'BLOCKED')


class PrivacyFilterCheckAndRaiseTests(unittest.TestCase):
    def setUp(self):
        self.f = PrivacyFilter()

    def test_public_returns_text(self):
        result = self.f.check_and_raise('Safe public content here.')
        self.assertEqual(result, 'Safe public content here.')

    def test_sanitized_returns_redacted_text(self):
        result = self.f.check_and_raise('My key: sk-abcdefghijklmnopqrstuvwxyz')
        self.assertIn('[API_KEY]', result)

    def test_blocked_raises_denied(self):
        with self.assertRaises(Denied):
            self.f.check_and_raise('IBAN: GB29NWBK60161331926819')

    def test_singleton_accessible(self):
        r = PRIVACY_FILTER.check('Hello world')
        self.assertEqual(r.level, 'PUBLIC')


class PrivacyFilterSyntheticFixturesTests(unittest.TestCase):
    """Prove synthetic fake-secret fixtures are caught — no real secrets used."""

    def setUp(self):
        self.f = PrivacyFilter()

    def test_synthetic_api_key_blocked_before_dispatch(self):
        fake_prompt = (
            'Please analyze this: sk-proj-abcdefghijklmnopqrstuvwxyz1234 '
            'is the key we are testing'
        )
        r = self.f.check(fake_prompt)
        self.assertIn(r.level, ('SANITIZED', 'BLOCKED'))

    def test_synthetic_github_token_caught(self):
        fake = 'Using token ghp_abcdefghijklmnopqrstuvwxyz1234 to push'
        r = self.f.check(fake)
        self.assertNotEqual(r.level, 'PUBLIC')

    def test_clean_research_prompt_passes(self):
        prompt = (
            'What are the key differences between supervised and unsupervised '
            'machine learning? Provide examples for each approach.'
        )
        r = self.f.check(prompt)
        self.assertEqual(r.level, 'PUBLIC')
        result = self.f.check_and_raise(prompt)
        self.assertEqual(result, prompt)
