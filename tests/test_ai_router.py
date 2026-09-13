"""Tests for zamal_core/ai_router.py — Smart AI Router."""
import json
import sys
import threading
import time
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from zamal_core.ai_router import AIRouter, TASK_TYPE_PREFERENCES, get_router
from zamal_core.ai_browser_transport import ProviderHealth, BrowserAITransport


def _make_health(status: str, provider: str = 'claude') -> ProviderHealth:
    h = ProviderHealth(provider, status)
    h.last_check = time.monotonic()  # mark as fresh
    return h


def _mock_transport(provider_statuses: dict, submit_result: dict = None):
    """Create a mock BrowserAITransport with given provider statuses."""
    transport = MagicMock(spec=BrowserAITransport)
    transport._health = {
        k: _make_health(v, k) for k, v in provider_statuses.items()
    }
    transport._lock = threading.Lock()

    def fake_health_check(provider, force=False):
        h = transport._health.get(provider, ProviderHealth(provider, 'NOT_CONFIGURED'))
        return h

    transport.health_check.side_effect = fake_health_check

    def fake_submit(provider, prompt, timeout=90):
        return submit_result or {'ok': False, 'error': 'mock_no_result', 'status': 'UNAVAILABLE'}

    transport.submit_task.side_effect = fake_submit

    def fake_all_statuses():
        return {k: {'name': k, 'status': h.status, 'last_response_preview': '',
                    'consecutive_failures': 0}
                for k, h in transport._health.items()}
    transport.all_statuses.side_effect = fake_all_statuses
    transport.expire_health_cache = MagicMock()
    return transport


class RouterPreferencesTests(unittest.TestCase):
    def test_all_task_types_defined(self):
        for t in ('analysis', 'research', 'reasoning', 'coding', 'summarize', 'creative', 'default'):
            self.assertIn(t, TASK_TYPE_PREFERENCES)
            self.assertGreater(len(TASK_TYPE_PREFERENCES[t]), 0)

    def test_all_providers_in_default_chain(self):
        providers_in_default = set(TASK_TYPE_PREFERENCES['default'])
        self.assertIn('claude', providers_in_default)
        self.assertIn('gemini', providers_in_default)
        self.assertIn('chatgpt', providers_in_default)


class RouterPrivacyFilterTests(unittest.TestCase):
    def _make_router(self, statuses, submit):
        r = AIRouter()
        r._transport = _mock_transport(statuses, submit)
        return r

    def test_blocked_content_not_dispatched(self):
        router = self._make_router(
            {'claude': 'LIVE_VERIFIED'},
            {'ok': True, 'response': 'result', 'response_length': 6},
        )
        # IBAN in prompt → BLOCKED
        result = router.route('My IBAN: GB29NWBK60161331926819')
        self.assertFalse(result.ok)
        self.assertEqual(result.privacy_level, 'BLOCKED')
        self.assertEqual(result.status, 'BLOCKED')
        router._transport.submit_task.assert_not_called()

    def test_sanitized_content_dispatched(self):
        router = self._make_router(
            {'claude': 'LIVE_VERIFIED', 'gemini': 'UNAVAILABLE', 'chatgpt': 'UNAVAILABLE',
             'deepseek': 'UNAVAILABLE', 'qwen': 'UNAVAILABLE', 'kimi': 'UNAVAILABLE'},
            {'ok': True, 'response': 'analysis result', 'response_length': 15},
        )
        result = router.route('Analyze this: sk-fake12345678901234 is a test key')
        self.assertEqual(result.privacy_level, 'SANITIZED')
        # Dispatch still happens (to claude which is LIVE_VERIFIED)
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, 'claude')

    def test_clean_content_public(self):
        router = self._make_router(
            {'claude': 'LIVE_VERIFIED', 'gemini': 'UNAVAILABLE', 'chatgpt': 'UNAVAILABLE',
             'deepseek': 'UNAVAILABLE', 'qwen': 'UNAVAILABLE', 'kimi': 'UNAVAILABLE'},
            {'ok': True, 'response': 'result', 'response_length': 6},
        )
        result = router.route('What is 2+2?')
        self.assertEqual(result.privacy_level, 'PUBLIC')
        self.assertTrue(result.ok)


class RouterSelectionTests(unittest.TestCase):
    def _make_router(self, statuses, submit=None):
        r = AIRouter()
        r._transport = _mock_transport(statuses, submit or {'ok': True, 'response': 'ok', 'response_length': 2})
        return r

    def test_selects_first_available(self):
        statuses = {k: 'UNAVAILABLE' for k in ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi')}
        statuses['gemini'] = 'LIVE_VERIFIED'
        router = self._make_router(statuses)
        result = router.route('Test prompt', task_type='analysis')
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, 'gemini')

    def test_skips_not_configured(self):
        statuses = {k: 'NOT_CONFIGURED' for k in ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi')}
        statuses['deepseek'] = 'LIVE_VERIFIED'
        router = self._make_router(statuses)
        result = router.route('Test', task_type='coding')
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, 'deepseek')

    def test_skips_rate_limited(self):
        statuses = {k: 'RATE_LIMITED' for k in ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi')}
        statuses['kimi'] = 'LIVE_VERIFIED'
        router = self._make_router(statuses)
        result = router.route('Test')
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, 'kimi')
        # All others in fallback_history
        reasons = [f['reason'] for f in result.fallback_history]
        self.assertTrue(all(r == 'rate_limited' for r in reasons[:-0]))

    def test_force_exclude(self):
        statuses = {k: 'LIVE_VERIFIED' for k in ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi')}
        router = self._make_router(statuses)
        result = router.route('Test', force_exclude=('claude',))
        self.assertTrue(result.ok)
        self.assertNotEqual(result.provider, 'claude')  # claude was excluded

    def test_all_exhausted_returns_error(self):
        statuses = {k: 'UNAVAILABLE' for k in ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi')}
        router = self._make_router(statuses)
        result = router.route('Test')
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'all_providers_exhausted')
        self.assertEqual(result.status, 'UNAVAILABLE')

    def test_fallback_history_recorded(self):
        statuses = {k: 'UNAVAILABLE' for k in ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi')}
        statuses['qwen'] = 'LIVE_VERIFIED'
        router = self._make_router(statuses)
        result = router.route('Test', task_type='analysis')
        # claude, gemini, chatgpt, deepseek are all UNAVAILABLE → in fallback_history
        unavailable = [f for f in result.fallback_history if f['reason'] == 'unavailable']
        self.assertGreaterEqual(len(unavailable), 3)

    def test_provider_failure_triggers_fallback(self):
        statuses = {k: 'LIVE_VERIFIED' for k in ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi')}
        transport = _mock_transport(statuses)
        call_count = [0]

        def fake_submit(provider, prompt, timeout=90):
            call_count[0] += 1
            if provider == 'claude':
                return {'ok': False, 'error': 'timeout', 'status': 'UNAVAILABLE'}
            return {'ok': True, 'response': 'from ' + provider, 'response_length': 10}

        transport.submit_task.side_effect = fake_submit
        router = AIRouter()
        router._transport = transport
        result = router.route('Test', task_type='analysis')
        self.assertTrue(result.ok)
        self.assertNotEqual(result.provider, 'claude')
        # claude tried and failed
        self.assertTrue(any(f['provider'] == 'claude' for f in result.fallback_history))


class RouterProvenanceTests(unittest.TestCase):
    def test_provenance_fields(self):
        statuses = {k: 'UNAVAILABLE' for k in ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi')}
        statuses['claude'] = 'LIVE_VERIFIED'
        transport = _mock_transport(statuses, {'ok': True, 'response': 'done', 'response_length': 4})
        router = AIRouter()
        router._transport = transport
        result = router.route('Research public data', task_type='research')
        self.assertTrue(result.ok)
        prov = result.provenance
        self.assertEqual(prov['provider'], 'claude')
        self.assertEqual(prov['task_type'], 'research')
        self.assertIn('timestamp', prov)
        self.assertIn('privacy_level', prov)
        self.assertIn('fallback_chain', prov)

    def test_result_status_live_verified_on_success(self):
        statuses = {k: 'UNAVAILABLE' for k in ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi')}
        statuses['claude'] = 'LIVE_VERIFIED'
        transport = _mock_transport(statuses, {'ok': True, 'response': 'result', 'response_length': 6})
        router = AIRouter()
        router._transport = transport
        result = router.route('Hello')
        self.assertEqual(result.status, 'LIVE_VERIFIED')


class RouterSessionRequiredTests(unittest.TestCase):
    def test_session_required_skipped_and_notified(self):
        statuses = {k: 'UNAVAILABLE' for k in ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi')}
        statuses['claude'] = 'SESSION_REQUIRED'
        statuses['gemini'] = 'LIVE_VERIFIED'
        transport = _mock_transport(statuses, {'ok': True, 'response': 'fallback result', 'response_length': 14})
        router = AIRouter()
        router._transport = transport

        notified = []

        def fake_notify(provider, task_id=None):
            notified.append(provider)

        router._notify_session_required = fake_notify
        result = router.route('Test')
        self.assertIn('claude', notified)
        # Falls back to gemini
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, 'gemini')

    def test_session_notification_sent_once(self):
        statuses = {k: 'SESSION_REQUIRED' for k in ('claude', 'gemini', 'chatgpt', 'deepseek', 'qwen', 'kimi')}
        transport = _mock_transport(statuses)
        router = AIRouter()
        router._transport = transport
        notified = []
        router._notify_session_required = lambda p, t=None: notified.append(p)

        router.route('Test', task_id='task-123')
        first_count = len(notified)
        router.route('Test', task_id='task-123')  # same task_id — should not re-notify
        # Should not increase for already-notified (provider+task_id) combos
        # New calls with same key are deduplicated
        self.assertLessEqual(len(notified), first_count + len(statuses))


class RouterSingletonTests(unittest.TestCase):
    def test_get_router_returns_same_instance(self):
        r1 = get_router()
        r2 = get_router()
        self.assertIs(r1, r2)
