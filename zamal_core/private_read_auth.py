"""Private-read authorization — default-deny policy for all private message channels.

Zamal must never read private message bodies, previews, inbox rows, notifications,
email content or AI chat history unless the caller holds a valid PrivateReadAuth
for the exact channel and target.

A PrivateReadAuth is:
  - scoped to one task, one channel, one target/conversation;
  - created by an explicit operator command;
  - single-use and discarded when the task completes;
  - read-only (never authorizes send/reply/delete/react).

Without an active authorization the channel-read functions return only sanitized
metadata: authenticated, tab_found, unread_count, compose_available — never names,
previews, subjects or any content.

BROAD EXTRACTION is prohibited in all browser JS regardless of authorization:
  - document.body.innerText / textContent
  - full innerHTML / outerHTML
  - whole-page DOM / accessibility-tree dumps
  - conversation-list or sidebar dumps
  - unrestricted querySelectorAll collecting text from arbitrary elements

Violations: the affected operation is stopped, a PRIVACY_BLOCKED event is recorded,
a sanitized Telegram warning is sent, and a new scoped instruction is required.
"""
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


# ── Authorization record ──────────────────────────────────────────────────────

@dataclass
class PrivateReadAuth:
    """Scoped authorization to read one private channel/target combination."""
    auth_id: str
    task_id: str
    channel: str
    target: str          # conversation identifier, contact label, or '*' for inbox-list only
    purpose: str
    scope: str           # e.g. 'last_10_messages', 'inbox_count', 'thread_ids_only'
    created_at: float
    expires_at: float
    authorized_by: str   # originating operator command reference (not raw user text)
    used: bool = field(default=False, repr=False)

    @property
    def valid(self) -> bool:
        return not self.used and time.time() < self.expires_at

    def consume(self):
        """Mark authorization as consumed; subsequent calls are denied."""
        self.used = True


# ── Registry (process-singleton) ──────────────────────────────────────────────

class PrivateReadRegistry:
    """Thread-safe registry of active private-read authorizations."""

    def __init__(self):
        self._lock = threading.Lock()
        self._auths: dict[str, PrivateReadAuth] = {}

    def authorize(
        self,
        *,
        task_id: str,
        channel: str,
        target: str,
        purpose: str,
        scope: str,
        ttl_seconds: float = 120.0,
        authorized_by: str = 'operator_command',
    ) -> PrivateReadAuth:
        """Create and register a new private-read authorization. TTL default: 120 s."""
        now = time.time()
        auth = PrivateReadAuth(
            auth_id=str(uuid.uuid4()),
            task_id=task_id,
            channel=channel,
            target=target,
            purpose=purpose,
            scope=scope,
            created_at=now,
            expires_at=now + ttl_seconds,
            authorized_by=authorized_by,
        )
        with self._lock:
            # Purge expired entries first
            expired = [k for k, a in self._auths.items() if not a.valid]
            for k in expired:
                del self._auths[k]
            self._auths[auth.auth_id] = auth
        return auth

    def find(self, channel: str, target: str) -> Optional[PrivateReadAuth]:
        """Return a valid unused auth for this channel/target, or None."""
        with self._lock:
            for auth in self._auths.values():
                if (auth.valid
                        and auth.channel == channel
                        and (auth.target == target or auth.target == '*')):
                    return auth
        return None

    def revoke(self, auth_id: str):
        """Immediately revoke an authorization by ID."""
        with self._lock:
            auth = self._auths.get(auth_id)
            if auth:
                auth.consume()

    def revoke_task(self, task_id: str):
        """Revoke all authorizations for a completed task."""
        with self._lock:
            for auth in self._auths.values():
                if auth.task_id == task_id:
                    auth.consume()

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for a in self._auths.values() if a.valid)


PRIVATE_READ_REGISTRY = PrivateReadRegistry()


# ── Enforcement helper ─────────────────────────────────────────────────────────

# Channels where private-read authorization is required for content access.
# Public channels (facebook feed, public web) are not listed here.
PRIVATE_CHANNELS = frozenset({
    'gmail', 'telegram', 'whatsapp', 'messenger', 'instagram',
})


class PrivateReadDenied(Exception):
    """Raised when private-read is attempted without a valid authorization."""


def require_read_auth(channel: str, target: str = '*') -> PrivateReadAuth:
    """Raise PrivateReadDenied unless a valid auth exists for channel/target.

    Call this at the top of any _read_* function that handles private content.
    The returned auth is NOT consumed here — call auth.consume() after the read.
    """
    if channel not in PRIVATE_CHANNELS:
        # Not a private channel; no auth required.
        raise ValueError(f'{channel!r} is not a private channel; do not call require_read_auth')
    auth = PRIVATE_READ_REGISTRY.find(channel, target)
    if auth is None:
        raise PrivateReadDenied(
            f'Private-read denied: no active authorization for channel={channel!r} '
            f'target={target!r}. Create a PrivateReadAuth via '
            f'PRIVATE_READ_REGISTRY.authorize(...) first.'
        )
    return auth


# ── Broad-extraction detector ──────────────────────────────────────────────────

# Strings whose presence in injected JS indicates a prohibited broad-extraction
# technique.  Only checked for JS targeting private-channel domains.
_BROAD_PATTERNS = (
    'document.body.innerText',
    'document.body.textContent',
    'document.body.innerHTML',
    'document.body.outerHTML',
    'document.documentElement.innerText',
    'document.documentElement.innerHTML',
    'document.documentElement.outerHTML',
    '.querySelectorAll("*")',
    ".querySelectorAll('*')",
    'getAccessibilityTree',
    'getComputedAccessibilityTree',
)

# Domains considered private-message surfaces; broad extraction is blocked here.
PRIVATE_DOMAINS = frozenset({
    'mail.google.com',
    'web.whatsapp.com',
    'facebook.com',
    'www.facebook.com',
    'messenger.com',
    'instagram.com',
    'www.instagram.com',
})


def check_js_for_broad_extraction(domain: str, js: str) -> Optional[str]:
    """Return the first forbidden pattern found in js, or None if clean.

    Only applies to private-domain JS; public-page JS is not checked.
    """
    if domain not in PRIVATE_DOMAINS:
        return None
    for pat in _BROAD_PATTERNS:
        if pat in js:
            return pat
    return None


# ── Sanitized metadata response ───────────────────────────────────────────────

def metadata_only_result(channel: str) -> dict:
    """Return sanitized metadata when private-read is not authorized."""
    return {
        'authenticated': True,
        'tab_found': True,
        'unread_count': None,
        'compose_available': None,
        'content_access': 'DENIED',
        'channel': channel,
        'reason': 'no_private_read_authorization',
    }


# ── Incident recorder ─────────────────────────────────────────────────────────

def record_privacy_incident(channel: str, operation: str, detail: str = ''):
    """Record a PRIVACY_BLOCKED/INCIDENT event without logging raw content.

    Sends a sanitized Telegram warning and writes to the operator store audit log.
    'detail' must never contain raw message content, names, or identifiers.
    """
    import logging
    _log = logging.getLogger('zamal.privacy')
    _log.warning(
        'PRIVACY_BLOCKED channel=%s operation=%s detail=%s',
        channel, operation, detail[:120] if detail else '',
    )
    # Async Telegram notification (best-effort, non-blocking)
    try:
        import threading as _t

        def _notify():
            try:
                from .control_center import _load_auth_env
                _load_auth_env()
                from .telegram_bot import TelegramConfig, TelegramAPI
                tc = TelegramConfig()
                if not tc.configured:
                    return
                api = TelegramAPI(tc)
                msg = (
                    f'\u26a0\ufe0f PRIVACY_BLOCKED\n'
                    f'channel: {channel}\n'
                    f'operation: {operation}\n'
                    f'detail: {(detail or "")[:80]}\n'
                    f'action: operation stopped; new scoped instruction required'
                )
                for cid in tc._allowed:
                    try:
                        api.send_message(cid, msg)
                    except Exception:
                        pass
            except Exception:
                pass

        _t.Thread(target=_notify, daemon=True).start()
    except Exception:
        pass
