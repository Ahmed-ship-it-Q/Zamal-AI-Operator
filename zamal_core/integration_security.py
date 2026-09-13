"""Bounded integration data validation; never log rejected input or exceptions."""
import re
from .store import Denied, encode

SECRET_KEY = re.compile(r'(password|passwd|secret|token|api.?key|authorization|cookie|private.?key|encryption.?key|credentials?)', re.I)

# PRIMARY pattern: known credential structures and key=value patterns.
_SECRET_TEXT_PRIMARY = re.compile(
    r'('
    r'-----BEGIN .*PRIVATE KEY'              # PEM private key block
    r'|\bBearer\s+[A-Za-z0-9+/=_.-]{16,}'  # OAuth Bearer tokens (>=16 chars)
    r'|\bsk-[A-Za-z0-9_-]{12,}'            # OpenAI / Anthropic secret keys
    r'|https?://[^\s/@]+:[^\s/@]+@'         # Embedded URL credentials
    r'|(?:password|secret|token|api[_-]?key)\s*[=:]\s*["\']?[^\s"\']{4,}'  # key=value secrets
    r')',
    re.I,
)

# STRUCTURED credential patterns: detect by format/prefix, not broad base64.
# JWT: three base64url segments (header.payload.signature); minimum realistic size.
_JWT_RE = re.compile(
    r'\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b'
)

# Known API-key prefixes used by major providers (structural detection, not content).
_API_KEY_PREFIX_RE = re.compile(
    r'\b('
    r'sk-[A-Za-z0-9_-]{12,}'          # OpenAI / Anthropic
    r'|pk-[A-Za-z0-9_-]{12,}'         # Publishable keys (various)
    r'|ghp_[A-Za-z0-9]{10,}'          # GitHub personal access token
    r'|gho_[A-Za-z0-9]{10,}'          # GitHub OAuth token
    r'|ghs_[A-Za-z0-9]{10,}'          # GitHub server token
    r'|xoxb-[A-Za-z0-9-]{10,}'        # Slack bot token
    r'|xoxp-[A-Za-z0-9-]{10,}'        # Slack user token
    r'|AIza[A-Za-z0-9_-]{30,}'        # Google API key
    r')\b'
)

# Combined SECRET_TEXT used throughout the codebase.
def _match_secret_text(text):
    return (
        _SECRET_TEXT_PRIMARY.search(text)
        or _JWT_RE.search(text)
        or _API_KEY_PREFIX_RE.search(text)
    )

class _SecretTextPattern:
    """Drop-in replacement for a compiled re object; .search() delegates to all sub-patterns."""
    def search(self, text):
        return _match_secret_text(text)

SECRET_TEXT = _SecretTextPattern()


def validate_data(value, depth=0):
    if depth > 12:
        raise Denied('Integration nesting limit')
    if isinstance(value, dict):
        for k,v in value.items():
            if not isinstance(k,str) or SECRET_KEY.search(k):
                raise Denied('Credential-bearing integration data denied')
            validate_data(v,depth+1)
    elif isinstance(value,list):
        for v in value: validate_data(v,depth+1)
    elif isinstance(value,str):
        if SECRET_TEXT.search(value): raise Denied('Credential-bearing integration data denied')
    elif value is not None and type(value) not in (bool,int,float):
        raise Denied('JSON integration data required')
    if depth==0 and len(encode(value).encode())>65536:
        raise Denied('Integration payload too large')


def failure(outcome, code, **ids):
    return {'outcome':outcome,'ok':False,'code':code,**ids}
