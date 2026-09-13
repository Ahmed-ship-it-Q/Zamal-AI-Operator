"""Privacy filter — sanitizes content before dispatch to external AI providers.

Classification levels:
  PUBLIC     — no secret material detected; safe to dispatch as-is
  SANITIZED  — secrets detected and redacted; safe to dispatch after redaction
  BLOCKED    — contains unredactable sensitive category; must not be dispatched

Never logs rejected content or match objects.
"""
import re

from .integration_security import SECRET_TEXT

# ── Additional banking/financial patterns (block, not redact) ──────────────

# IBAN: 2 letters + 2 digits + 4-30 alphanum — detect plausible IBANs
_IBAN_RE = re.compile(
    r'\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b'
)
# Credit/debit card numbers (Luhn-format length/prefix check); optional dash/space separators
_SEP = r'[\s-]?'
_CARD_RE = re.compile(
    r'\b(?:'
    r'4[0-9]{3}' + _SEP + r'[0-9]{4}' + _SEP + r'[0-9]{4}' + _SEP + r'[0-9]{1,4}'  # Visa
    r'|5[1-5][0-9]{2}' + _SEP + r'[0-9]{4}' + _SEP + r'[0-9]{4}' + _SEP + r'[0-9]{4}'  # MC
    r'|3[47][0-9]{2}' + _SEP + r'[0-9]{6}' + _SEP + r'[0-9]{5}'  # Amex
    r'|6(?:011|5[0-9]{2})' + _SEP + r'[0-9]{4}' + _SEP + r'[0-9]{4}' + _SEP + r'[0-9]{4}'  # Disc
    r')\b'
)
# US SSN
_SSN_RE = re.compile(r'\b\d{3}-\d{2}-\d{4}\b')

# ── Redactable patterns ────────────────────────────────────────────────────

_REDACT_PATTERNS = [
    # OpenAI / Anthropic secret keys
    (re.compile(r'\bsk-[A-Za-z0-9_-]{12,}\b'), '[API_KEY]'),
    # GitHub tokens
    (re.compile(r'\bghp_[A-Za-z0-9]{10,}\b'), '[GITHUB_TOKEN]'),
    (re.compile(r'\bgho_[A-Za-z0-9]{10,}\b'), '[GITHUB_TOKEN]'),
    (re.compile(r'\bghs_[A-Za-z0-9]{10,}\b'), '[GITHUB_TOKEN]'),
    # Slack tokens
    (re.compile(r'\bxox[bp]-[A-Za-z0-9-]{10,}\b'), '[SLACK_TOKEN]'),
    # Google API keys
    (re.compile(r'\bAIza[A-Za-z0-9_-]{30,}\b'), '[GOOGLE_KEY]'),
    # Publishable keys
    (re.compile(r'\bpk-[A-Za-z0-9_-]{12,}\b'), '[PUB_KEY]'),
    # OAuth Bearer tokens
    (re.compile(r'\bBearer\s+[A-Za-z0-9+/=_.-]{16,}'), 'Bearer [TOKEN]'),
    # PEM private key blocks
    (re.compile(r'-----BEGIN [^\n]*PRIVATE KEY-----.*?-----END [^\n]*PRIVATE KEY-----',
                re.DOTALL), '[PRIVATE_KEY]'),
    # key=value / key: value secret assignments
    (re.compile(
        r'(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*["\']?[^\s"\']{4,}',
        re.I,
    ), '[CREDENTIAL]'),
    # JWTs: three base64url segments
    (re.compile(
        r'\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b'
    ), '[JWT]'),
    # Embedded URL credentials (https://user:pass@...)
    (re.compile(r'https?://[^\s/@]+:[^\s/@]+@'), 'https://[CREDENTIALS]@'),
]

# High-sensitivity patterns: block entire message (cannot be safely redacted)
_BLOCK_PATTERNS = [
    (_IBAN_RE, 'banking_iban'),
    (_CARD_RE, 'payment_card'),
    (_SSN_RE, 'ssn_pii'),
]


class PrivacyFilterResult:
    __slots__ = ('level', 'text', 'reasons')

    def __init__(self, level: str, text: str, reasons: list):
        self.level = level    # 'PUBLIC' | 'SANITIZED' | 'BLOCKED'
        self.text = text      # sanitized text (or original if PUBLIC)
        self.reasons = reasons


class PrivacyFilter:
    """One-shot content sanitizer for external AI dispatch."""

    def check(self, text: str) -> PrivacyFilterResult:
        if not isinstance(text, str):
            text = str(text)

        # Check block patterns first — high-sensitivity categories
        for pattern, category in _BLOCK_PATTERNS:
            if pattern.search(text):
                return PrivacyFilterResult('BLOCKED', text, [f'blocked:{category}'])

        # Redact known secrets
        sanitized = text
        reasons = []
        for pattern, replacement in _REDACT_PATTERNS:
            new_text = pattern.sub(replacement, sanitized)
            if new_text != sanitized:
                reasons.append('redacted:' + replacement.strip('[]').lower().replace(' ', '_'))
                sanitized = new_text

        # Final sweep with existing SECRET_TEXT scanner on the sanitized output
        if SECRET_TEXT.search(sanitized):
            return PrivacyFilterResult('BLOCKED', sanitized, reasons + ['blocked:unredactable_secret'])

        if reasons:
            return PrivacyFilterResult('SANITIZED', sanitized, reasons)
        return PrivacyFilterResult('PUBLIC', text, [])

    def check_and_raise(self, text: str) -> str:
        """Sanitize; raise Denied if BLOCKED. Returns sanitized text."""
        from .store import Denied
        result = self.check(text)
        if result.level == 'BLOCKED':
            raise Denied('Privacy filter blocked dispatch: ' + ', '.join(result.reasons))
        return result.text


PRIVACY_FILTER = PrivacyFilter()
