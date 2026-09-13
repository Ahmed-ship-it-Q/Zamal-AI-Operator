"""Tests for zamal_core/integration_security.py — structured secret detection (L-7).

Covers:
  - JWT-format strings are detected and blocked
  - OAuth Bearer tokens are detected and blocked
  - Known API key prefixes (sk-, ghp_, xoxb-, AIza, etc.) are detected and blocked
  - Ordinary base64 data (short base64-encoded word) is NOT blocked
  - Existing allowed plain data remains allowed
  - PEM private key header is detected
  - Embedded URL credentials are detected
  - key=value patterns are detected
  - validate_data blocks credential keys
  - validate_data passes clean data
"""
import unittest

from zamal_core.integration_security import SECRET_TEXT, validate_data, SECRET_KEY
from zamal_core.store import Denied


class SecretTextDetectionTests(unittest.TestCase):
    """Tests for the SECRET_TEXT pattern (L-7 improvements)."""

    # ── JWT detection ─────────────────────────────────────────────────────────

    def test_jwt_format_is_detected(self):
        # Realistic JWT: eyJ<header>.<eyJ<payload>>.<signature>
        jwt = (
            'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'
            '.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ'
            '.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c'
        )
        self.assertIsNotNone(SECRET_TEXT.search(jwt),
                             'JWT-format token must be detected as a secret')

    def test_jwt_embedded_in_text_is_detected(self):
        text = 'access_token=eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.abc123_xyz456'
        self.assertIsNotNone(SECRET_TEXT.search(text))

    def test_jwt_with_minimal_segments_detected(self):
        # Minimum realistic JWT: 3 base64url segments starting with eyJ
        jwt = 'eyJhbGc.eyJzdWIiOiJ4In0.abcdef1234'
        self.assertIsNotNone(SECRET_TEXT.search(jwt))

    # ── OAuth Bearer token detection ──────────────────────────────────────────

    def test_bearer_token_is_detected(self):
        text = 'Authorization: Bearer ya29.a0AfH6SMBxyz1234567890abcdefghijklmnopqrstuvwxyz'
        self.assertIsNotNone(SECRET_TEXT.search(text),
                             'OAuth Bearer token must be detected')

    def test_bearer_token_short_value_is_not_flagged(self):
        # "Bearer " followed by <16 chars should NOT be flagged (too short to be a real token)
        text = 'Bearer abc123'
        self.assertIsNone(SECRET_TEXT.search(text),
                          'Short Bearer value (<16 chars) should not trigger detection')

    def test_bearer_token_long_value_is_flagged(self):
        token = 'Bearer ' + 'A' * 40
        self.assertIsNotNone(SECRET_TEXT.search(token))

    # ── API key prefix detection ──────────────────────────────────────────────

    def test_openai_sk_key_detected(self):
        text = 'API key: sk-proj-abcdefghijklmnopqrstuvwxyz1234'
        self.assertIsNotNone(SECRET_TEXT.search(text),
                             'OpenAI sk- key must be detected')

    def test_anthropic_sk_key_detected(self):
        text = 'sk-ant-api03-abcdefghijklmnopqrstuvwxyz'
        self.assertIsNotNone(SECRET_TEXT.search(text))

    def test_github_pat_ghp_detected(self):
        text = 'GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234'
        self.assertIsNotNone(SECRET_TEXT.search(text))

    def test_github_oauth_gho_detected(self):
        text = 'gho_abcdefghijklmnopqrstuvwxyz1234'
        self.assertIsNotNone(SECRET_TEXT.search(text))

    def test_github_server_ghs_detected(self):
        text = 'ghs_abcdefghijklmnopqrstuvwxyz1234'
        self.assertIsNotNone(SECRET_TEXT.search(text))

    def test_slack_bot_xoxb_detected(self):
        text = 'token=xoxb-123456789012-abcdefghij'
        self.assertIsNotNone(SECRET_TEXT.search(text))

    def test_slack_user_xoxp_detected(self):
        text = 'xoxp-123456789012-abcdefghij'
        self.assertIsNotNone(SECRET_TEXT.search(text))

    def test_google_api_key_aiza_detected(self):
        text = 'key=AIzaSyAbcdefghijklmnopqrstuvwxyz1234567'
        self.assertIsNotNone(SECRET_TEXT.search(text))

    # ── Existing patterns still work ──────────────────────────────────────────

    def test_pem_private_key_detected(self):
        text = '-----BEGIN RSA PRIVATE KEY-----\nMIIEow...'
        self.assertIsNotNone(SECRET_TEXT.search(text))

    def test_url_with_embedded_credentials_detected(self):
        # The existing pattern covers http/https embedded credentials
        text = 'https://admin:pass@db.example.com/mydb'
        self.assertIsNotNone(SECRET_TEXT.search(text))

    def test_password_equals_value_detected(self):
        text = 'password=SuperSecret123!'
        self.assertIsNotNone(SECRET_TEXT.search(text))

    def test_token_equals_value_detected(self):
        text = 'token: "gh_secretvalue_abc123"'
        self.assertIsNotNone(SECRET_TEXT.search(text))

    # ── Non-secret data must NOT be flagged ──────────────────────────────────

    def test_short_base64_word_not_flagged(self):
        # base64("hello") = "aGVsbG8=" — short, no suspicious prefix
        text = 'encoded: aGVsbG8='
        self.assertIsNone(SECRET_TEXT.search(text),
                          'Short base64-encoded word must NOT be flagged')

    def test_uuid_not_flagged(self):
        text = 'id=550e8400-e29b-41d4-a716-446655440000'
        self.assertIsNone(SECRET_TEXT.search(text),
                          'UUIDs must not be flagged')

    def test_plain_text_not_flagged(self):
        text = 'Please approve the Q3 budget request for accounting software'
        self.assertIsNone(SECRET_TEXT.search(text))

    def test_ordinary_base64_image_data_not_flagged(self):
        # A realistic short base64 chunk from an image, no secret prefix
        text = 'data: /9j/4AAQSkZJRgABAQAAAQABAAD=='
        self.assertIsNone(SECRET_TEXT.search(text),
                          'Base64 image data without secret prefix must not be flagged')

    def test_job_title_not_flagged(self):
        text = 'Senior Accountant, Doha Qatar — Oracle ERP, 5 years experience'
        self.assertIsNone(SECRET_TEXT.search(text))

    def test_empty_string_not_flagged(self):
        self.assertIsNone(SECRET_TEXT.search(''))

    def test_python_keyword_token_in_code_not_flagged(self):
        # Python keyword "token" in a code-like context with short value
        text = 'for token in tokens:'
        self.assertIsNone(SECRET_TEXT.search(text))


class ValidateDataTests(unittest.TestCase):
    """Tests for validate_data()."""

    def test_clean_dict_passes(self):
        validate_data({'title': 'Engineer', 'company': 'QCRI', 'location': 'Doha'})

    def test_clean_list_passes(self):
        validate_data(['accounting', 'payroll', 'Oracle'])

    def test_clean_nested_passes(self):
        validate_data({'job': {'title': 'Accountant', 'skills': ['Excel', 'WPS']}})

    def test_secret_key_raises(self):
        with self.assertRaises(Denied):
            validate_data({'password': 'should_not_be_here'})

    def test_token_key_raises(self):
        with self.assertRaises(Denied):
            validate_data({'token': 'abc123'})

    def test_api_key_raises(self):
        with self.assertRaises(Denied):
            validate_data({'api_key': 'abc123'})

    def test_secret_value_raises(self):
        with self.assertRaises(Denied):
            validate_data({'note': 'Bearer ya29.abcdefghijklmnopqrstuvwxyz123456789'})

    def test_jwt_value_raises(self):
        with self.assertRaises(Denied):
            validate_data({'note': 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig123456'})

    def test_nesting_limit_enforced(self):
        # Build a deeply-nested dict beyond depth 12
        nested = 'deep'
        for _ in range(15):
            nested = {'level': nested}
        with self.assertRaises(Denied):
            validate_data(nested)

    def test_oversized_payload_raises(self):
        with self.assertRaises(Denied):
            validate_data({'data': 'x' * 70000})

    def test_invalid_type_raises(self):
        with self.assertRaises(Denied):
            validate_data(object())

    def test_none_value_allowed(self):
        validate_data({'note': None})

    def test_bool_value_allowed(self):
        validate_data({'active': True})

    def test_int_value_allowed(self):
        validate_data({'score': 42})


class SecretKeyPatternTests(unittest.TestCase):
    """Tests for the SECRET_KEY pattern (field name detection)."""

    def test_password_key_detected(self):
        self.assertIsNotNone(SECRET_KEY.search('password'))

    def test_token_key_detected(self):
        self.assertIsNotNone(SECRET_KEY.search('token'))

    def test_api_key_detected(self):
        self.assertIsNotNone(SECRET_KEY.search('api_key'))

    def test_authorization_detected(self):
        self.assertIsNotNone(SECRET_KEY.search('authorization'))

    def test_company_not_detected(self):
        self.assertIsNone(SECRET_KEY.search('company'))

    def test_location_not_detected(self):
        self.assertIsNone(SECRET_KEY.search('location'))

    def test_title_not_detected(self):
        self.assertIsNone(SECRET_KEY.search('title'))


if __name__ == '__main__':
    unittest.main()
