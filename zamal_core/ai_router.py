"""Smart AI Router — selects provider by task type, availability, privacy level.

Policy (in order):
  1. Run privacy filter — BLOCKED content is never dispatched.
  2. Consult provider preference order for the task type.
  3. Skip providers that are UNAVAILABLE, SESSION_REQUIRED, RATE_LIMITED or NOT_CONFIGURED.
  4. SESSION_REQUIRED triggers a Telegram ACTION_REQUIRED notification (once per provider/task).
  5. On provider failure: fallback to next in preference order.
  6. Record full provenance: selected provider, fallback history, privacy level.
  7. Task checkpoint stored in Store context so it survives a session-expiry pause.
"""
import json
import threading
import time
import urllib.request
from dataclasses import dataclass, field

from .privacy_filter import PRIVACY_FILTER
from .ai_browser_transport import get_browser_transport, PROVIDER_CONFIGS

# ── Task-type preference orders ────────────────────────────────────────────

TASK_TYPE_PREFERENCES: dict = {
    'analysis':  ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi'),
    'research':  ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi'),
    'reasoning': ('claude', 'gemini', 'chatgpt', 'deepseek', 'kimi', 'qwen'),
    'coding':    ('deepseek', 'chatgpt', 'claude', 'gemini', 'qwen', 'kimi'),
    'summarize': ('gemini', 'claude', 'chatgpt', 'deepseek', 'qwen', 'kimi'),
    'creative':  ('claude', 'chatgpt', 'gemini', 'kimi', 'qwen', 'deepseek'),
    'default':   ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi'),
}

# Statuses that allow dispatch
_DISPATCHABLE = frozenset({'LIVE_VERIFIED', 'UNAVAILABLE'})


@dataclass
class RouterResult:
    ok: bool
    provider: str = ''
    response: str = ''
    task_type: str = ''
    privacy_level: str = ''
    fallback_history: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    error: str = ''
    status: str = ''


class AIRouter:
    """Smart router: privacy → selection → fallback → provenance → checkpoint."""

    def __init__(self):
        self._transport = get_browser_transport()
        self._notified: set = set()   # providers already notified this session
        self._lock = threading.Lock()

    def route(self, prompt: str, task_type: str = 'default',
              task_id: str = None, force_exclude: tuple = ()) -> RouterResult:
        """Route a task through the provider preference chain."""

        # 1. Privacy filter
        flt = PRIVACY_FILTER.check(prompt)
        if flt.level == 'BLOCKED':
            return RouterResult(
                ok=False,
                privacy_level='BLOCKED',
                error='privacy_filter_blocked: ' + ', '.join(flt.reasons),
                status='BLOCKED',
                task_type=task_type,
            )
        sanitized = flt.text
        privacy_level = flt.level

        # 2. Preference order
        preferences = TASK_TYPE_PREFERENCES.get(task_type, TASK_TYPE_PREFERENCES['default'])
        fallback_history: list = []

        # 3. Try each provider
        for provider in preferences:
            if provider in force_exclude:
                fallback_history.append({'provider': provider, 'reason': 'force_excluded'})
                continue

            health = self._transport.health_check(provider)
            status = health.status

            if status == 'NOT_CONFIGURED':
                fallback_history.append({'provider': provider, 'reason': 'not_configured'})
                continue
            if status == 'RATE_LIMITED':
                fallback_history.append({'provider': provider, 'reason': 'rate_limited'})
                continue
            if status == 'SESSION_REQUIRED':
                self._notify_session_required(provider, task_id)
                self._checkpoint(task_id, provider, sanitized, task_type, privacy_level)
                fallback_history.append({'provider': provider, 'reason': 'session_required'})
                continue
            if status == 'UNAVAILABLE':
                fallback_history.append({'provider': provider, 'reason': 'unavailable'})
                continue

            # Attempt dispatch
            result = self._transport.submit_task(provider, sanitized, timeout=90)

            if result['ok']:
                prov = {
                    'provider': provider,
                    'task_type': task_type,
                    'privacy_level': privacy_level,
                    'sanitized_reasons': flt.reasons,
                    'fallback_chain': list(fallback_history),
                    'timestamp': time.time(),
                    'response_length': result.get('response_length', 0),
                }
                return RouterResult(
                    ok=True,
                    provider=provider,
                    response=result['response'],
                    task_type=task_type,
                    privacy_level=privacy_level,
                    fallback_history=fallback_history,
                    provenance=prov,
                    status='LIVE_VERIFIED',
                )
            else:
                fallback_history.append({
                    'provider': provider,
                    'reason': result.get('error', 'unknown'),
                    'status': result.get('status', 'unknown'),
                })

        return RouterResult(
            ok=False,
            task_type=task_type,
            privacy_level=privacy_level,
            fallback_history=fallback_history,
            error='all_providers_exhausted',
            status='UNAVAILABLE',
        )

    def _notify_session_required(self, provider: str, task_id: str = None):
        key = provider + (task_id or '')
        with self._lock:
            if key in self._notified:
                return
            self._notified.add(key)

        try:
            from .control_center import _load_auth_env
            _load_auth_env()
            from .telegram_bot import TelegramConfig
            tc = TelegramConfig()
            if not tc.configured:
                return
            config = PROVIDER_CONFIGS.get(provider, {})
            msg = (
                f"\u26a0\ufe0f ACTION_REQUIRED \u2014 Browser AI Session Expired\n"
                f"Provider: {config.get('name', provider)}\n"
                f"URL: {config.get('url', 'unknown')}\n"
                f"Task: {task_id[:8] if task_id else 'N/A'}\n\n"
                f"Please log in to {config.get('name', provider)} in Chrome.\n"
                f"Task checkpoint preserved. Resume will be automatic."
            )
            token = tc._token
            for chat_id in tc._allowed:
                url = f'https://api.telegram.org/bot{token}/sendMessage'
                data = json.dumps({'chat_id': chat_id, 'text': msg}).encode()
                req = urllib.request.Request(
                    url, data=data, headers={'Content-Type': 'application/json'}
                )
                with urllib.request.urlopen(req, timeout=5):
                    pass
        except Exception:
            pass  # notification is best-effort

    def _checkpoint(self, task_id: str, provider: str, prompt: str,
                    task_type: str, privacy_level: str):
        """Persist task checkpoint to Store so it can resume after session login."""
        if not task_id:
            return
        try:
            from .store import Store
            store = Store()
            task = store.task(task_id)
            store.patch_context(task_id, {
                'checkpoint': {
                    'awaiting_session': provider,
                    'prompt_length': len(prompt),
                    'task_type': task_type,
                    'privacy_level': privacy_level,
                    'checkpoint_at': time.time(),
                }
            }, task['revision'])
            store.close()
        except Exception:
            pass

    def provider_statuses(self) -> dict:
        return self._transport.all_statuses()

    def force_health_check(self, provider: str) -> dict:
        self._transport.expire_health_cache(provider)
        health = self._transport.health_check(provider, force=True)
        return {
            'provider': provider,
            'status': health.status,
            'last_response_preview': health.last_response_preview,
            'consecutive_failures': health.consecutive_failures,
        }

    def clear_session_notification(self, provider: str, task_id: str = None):
        """Remove session-required notification gate so it fires again after login."""
        key = provider + (task_id or '')
        with self._lock:
            self._notified.discard(key)


# ── Singleton ──────────────────────────────────────────────────────────────

_ROUTER: 'AIRouter | None' = None
_ROUTER_LOCK = threading.Lock()


def get_router() -> AIRouter:
    global _ROUTER
    with _ROUTER_LOCK:
        if _ROUTER is None:
            _ROUTER = AIRouter()
    return _ROUTER
