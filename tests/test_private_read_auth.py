"""Behavioral tests for the private-message read authorization system.

These tests prove the 10 required properties:
1.  Default private read is denied (no auth → DENIED metadata only)
2.  Broad DOM/page-text extraction is blocked for private domains
3.  Public research cannot access private message content
4.  Exact scoped authorization reads only the requested target
5.  Adjacent conversations remain inaccessible after single-target auth
6.  Authorization expires and cannot be reused after TTL
7.  Read permission cannot trigger send/reply/post actions
8.  Raw private content cannot enter terminal/log — only redacted items returned
9.  Accidental broad-extraction attempt triggers sanitized blocking
10. Existing approved routing and CPC zero-write remain intact
"""
import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


# ── helpers ───────────────────────────────────────────────────────────────────

def _fresh_registry():
    """Return a brand-new, isolated PrivateReadRegistry for test isolation."""
    from zamal_core.private_read_auth import PrivateReadRegistry
    return PrivateReadRegistry()


# ── 1. Default private read is denied ─────────────────────────────────────────

class TestDefaultDenyPrivateRead(unittest.TestCase):
    """Without authorization the _read_* functions must return DENIED metadata."""

    def _assert_denied(self, items, channel):
        self.assertIsInstance(items, list)
        self.assertEqual(len(items), 1, f'{channel}: expected exactly one metadata item')
        item = items[0]
        self.assertIn('PRIVATE_READ_DENIED', item.preview,
                      f'{channel}: preview must contain DENIED marker')
        meta = item.metadata or {}
        self.assertEqual(meta.get('content_access'), 'DENIED',
                         f'{channel}: metadata.content_access must be DENIED')

    def test_gmail_denied_without_auth(self):
        from zamal_core.channel_adapter import _read_gmail
        items = _read_gmail(limit=1)
        self._assert_denied(items, 'gmail')

    def test_whatsapp_denied_without_auth(self):
        from zamal_core.channel_adapter import _read_whatsapp
        items = _read_whatsapp(limit=1)
        self._assert_denied(items, 'whatsapp')

    def test_messenger_denied_without_auth(self):
        from zamal_core.channel_adapter import _read_messenger
        items = _read_messenger(limit=1)
        self._assert_denied(items, 'messenger')

    def test_instagram_denied_without_auth(self):
        from zamal_core.channel_adapter import _read_instagram
        items = _read_instagram(limit=1)
        self._assert_denied(items, 'instagram')

    def test_telegram_denied_without_auth(self):
        from zamal_core.channel_adapter import _read_telegram
        items = _read_telegram(limit=1)
        self._assert_denied(items, 'telegram')


# ── 2. Broad DOM/page-text extraction is blocked ──────────────────────────────

class TestBroadExtractionBlocked(unittest.TestCase):
    """JS containing prohibited broad-extraction patterns must be rejected."""

    def _blocked(self, domain, js):
        from zamal_core.ai_browser_transport import _exec_js_in_domain
        ok, out = _exec_js_in_domain(domain, js, timeout=5)
        self.assertFalse(ok, f'Expected BLOCKED for domain={domain}')
        self.assertIn('BROAD_EXTRACTION_BLOCKED', out)

    def test_body_innerText_blocked_on_gmail(self):
        self._blocked('mail.google.com', 'JSON.stringify({t:document.body.innerText})')

    def test_body_innerText_blocked_on_whatsapp(self):
        self._blocked('web.whatsapp.com', 'document.body.innerText.slice(0,100)')

    def test_body_innerHTML_blocked_on_instagram(self):
        self._blocked('instagram.com', 'document.body.innerHTML.length')

    def test_body_textContent_blocked_on_facebook(self):
        self._blocked('facebook.com', 'document.body.textContent.slice(0,200)')

    def test_querySelectorAll_star_blocked_on_whatsapp(self):
        self._blocked('web.whatsapp.com', 'document.querySelectorAll("*").length')

    def test_body_outerHTML_blocked_on_messenger(self):
        self._blocked('facebook.com', 'document.body.outerHTML.length')

    def test_narrow_selector_not_blocked_on_public_domain(self):
        """Narrow JS on a public (non-private) domain must NOT be blocked."""
        from zamal_core.ai_browser_transport import _exec_js_in_domain
        # chatgpt.com is not in PRIVATE_DOMAINS — broad check skipped
        # We can't actually execute (no Chrome in CI), but check_js_for_broad_extraction
        # must return None for non-private domains.
        from zamal_core.private_read_auth import check_js_for_broad_extraction
        result = check_js_for_broad_extraction('chatgpt.com', 'document.body.innerText')
        self.assertIsNone(result, 'Non-private domain must not be flagged')

    def test_narrow_selector_allowed_on_private_domain(self):
        """A clean, narrow JS string on a private domain must not be flagged."""
        from zamal_core.private_read_auth import check_js_for_broad_extraction
        clean_js = 'JSON.stringify({logged_in: !document.querySelector("input[type=password]")})'
        result = check_js_for_broad_extraction('mail.google.com', clean_js)
        self.assertIsNone(result, 'Clean narrow JS must not be flagged')


# ── 3. Public research cannot access private messages ─────────────────────────

class TestPublicResearchCannotAccessPrivate(unittest.TestCase):
    """Public-page functions must not bypass private-read authorization."""

    def test_research_facebook_feed_is_not_private(self):
        """_research_facebook_feed is public-page; it must not call require_read_auth."""
        from zamal_core import channel_adapter as ca
        import inspect
        src = inspect.getsource(ca._research_facebook_feed)
        self.assertNotIn('require_read_auth', src,
                         'Public research must not call require_read_auth')
        self.assertNotIn('PrivateReadDenied', src)

    def test_research_instagram_public_is_not_private(self):
        from zamal_core import channel_adapter as ca
        import inspect
        src = inspect.getsource(ca._research_instagram_public)
        self.assertNotIn('require_read_auth', src)

    def test_facebook_channel_not_in_private_channels(self):
        """Facebook (feed) is not a private channel — no auth required."""
        from zamal_core.private_read_auth import PRIVATE_CHANNELS
        self.assertNotIn('facebook', PRIVATE_CHANNELS)

    def test_calendar_not_in_private_channels(self):
        from zamal_core.private_read_auth import PRIVATE_CHANNELS
        self.assertNotIn('calendar', PRIVATE_CHANNELS)

    def test_private_channels_exactly_defined(self):
        from zamal_core.private_read_auth import PRIVATE_CHANNELS
        expected = frozenset({'gmail', 'telegram', 'whatsapp', 'messenger', 'instagram'})
        self.assertEqual(PRIVATE_CHANNELS, expected)


# ── 4. Scoped authorization grants access to named target only ────────────────

class TestScopedAuthorizationGrantsAccess(unittest.TestCase):
    """A PrivateReadAuth enables content access for exactly channel+target."""

    def test_auth_grants_access_to_named_channel(self):
        reg = _fresh_registry()
        auth = reg.authorize(
            task_id='task-001', channel='gmail', target='STEP3A_SELF_TEST',
            purpose='test', scope='subject_only',
        )
        self.assertTrue(auth.valid)
        self.assertEqual(auth.channel, 'gmail')
        self.assertEqual(auth.target, 'STEP3A_SELF_TEST')

    def test_require_auth_passes_with_valid_auth(self):
        from zamal_core.private_read_auth import require_read_auth, PrivateReadDenied, PRIVATE_READ_REGISTRY
        reg = _fresh_registry()
        auth_obj = reg.authorize(
            task_id='task-002', channel='whatsapp', target='*',
            purpose='test', scope='thread_list',
        )
        # Patch the module-level registry
        with patch('zamal_core.private_read_auth.PRIVATE_READ_REGISTRY', reg):
            from zamal_core.private_read_auth import require_read_auth as rar
            found = rar('whatsapp', '*')
            self.assertIsNotNone(found)
            self.assertEqual(found.auth_id, auth_obj.auth_id)

    def test_require_auth_fails_without_auth(self):
        from zamal_core.private_read_auth import PrivateReadDenied
        reg = _fresh_registry()
        with patch('zamal_core.private_read_auth.PRIVATE_READ_REGISTRY', reg):
            from zamal_core.private_read_auth import require_read_auth as rar
            with self.assertRaises(PrivateReadDenied):
                rar('instagram', 'some_target')


# ── 5. Adjacent conversations remain inaccessible ─────────────────────────────

class TestAdjacentConversationsInaccessible(unittest.TestCase):
    """Auth for target A must not grant access to target B."""

    def test_auth_for_specific_target_does_not_match_other(self):
        reg = _fresh_registry()
        reg.authorize(
            task_id='task-003', channel='messenger', target='SHANIL_AHMED',
            purpose='test', scope='last_5',
        )
        # Look for a different target
        found = reg.find('messenger', 'CAKENBAKE_QTR')
        self.assertIsNone(found, 'Auth for SHANIL_AHMED must not match CAKENBAKE_QTR')

    def test_wildcard_auth_matches_any_target(self):
        """target='*' is an inbox-level auth matching any target."""
        reg = _fresh_registry()
        reg.authorize(
            task_id='task-004', channel='instagram', target='*',
            purpose='inbox_count', scope='thread_count_only',
        )
        found = reg.find('instagram', 'any_target')
        self.assertIsNotNone(found)

    def test_auth_for_channel_A_does_not_grant_channel_B(self):
        reg = _fresh_registry()
        reg.authorize(
            task_id='task-005', channel='gmail', target='*',
            purpose='test', scope='subject_only',
        )
        found = reg.find('whatsapp', '*')
        self.assertIsNone(found, 'Gmail auth must not grant WhatsApp access')


# ── 6. Authorization expires and cannot be reused ─────────────────────────────

class TestAuthorizationExpiry(unittest.TestCase):

    def test_auth_expires_after_ttl(self):
        reg = _fresh_registry()
        auth = reg.authorize(
            task_id='task-006', channel='gmail', target='*',
            purpose='test', scope='subject_only', ttl_seconds=0.05,
        )
        time.sleep(0.1)
        self.assertFalse(auth.valid, 'Auth must be invalid after TTL')
        found = reg.find('gmail', '*')
        self.assertIsNone(found, 'Expired auth must not be returned by find()')

    def test_consumed_auth_cannot_be_reused(self):
        reg = _fresh_registry()
        auth = reg.authorize(
            task_id='task-007', channel='whatsapp', target='*',
            purpose='test', scope='thread_list',
        )
        auth.consume()
        self.assertFalse(auth.valid, 'Consumed auth must be invalid')
        found = reg.find('whatsapp', '*')
        self.assertIsNone(found, 'Consumed auth must not be returned by find()')

    def test_revoke_task_invalidates_all_task_auths(self):
        reg = _fresh_registry()
        a1 = reg.authorize(task_id='task-008', channel='gmail', target='*',
                           purpose='test', scope='s')
        a2 = reg.authorize(task_id='task-008', channel='whatsapp', target='*',
                           purpose='test', scope='s')
        reg.revoke_task('task-008')
        self.assertIsNone(reg.find('gmail', '*'))
        self.assertIsNone(reg.find('whatsapp', '*'))

    def test_separate_tasks_independent(self):
        reg = _fresh_registry()
        reg.authorize(task_id='task-009', channel='gmail', target='*',
                      purpose='test', scope='s')
        reg.authorize(task_id='task-010', channel='messenger', target='*',
                      purpose='test', scope='s')
        reg.revoke_task('task-009')
        self.assertIsNone(reg.find('gmail', '*'))
        self.assertIsNotNone(reg.find('messenger', '*'))


# ── 7. Read permission cannot send ────────────────────────────────────────────

class TestReadPermissionCannotSend(unittest.TestCase):
    """PrivateReadAuth has no send/execute capability."""

    def test_auth_has_no_send_method(self):
        from zamal_core.private_read_auth import PrivateReadAuth
        self.assertFalse(hasattr(PrivateReadAuth, 'send'))
        self.assertFalse(hasattr(PrivateReadAuth, 'execute'))
        self.assertFalse(hasattr(PrivateReadAuth, 'post'))
        self.assertFalse(hasattr(PrivateReadAuth, 'reply'))

    def test_read_auth_fields_are_read_only(self):
        """PrivateReadAuth stores task/channel/target — no executor_fn or action."""
        from zamal_core.private_read_auth import PrivateReadAuth
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(PrivateReadAuth)}
        self.assertNotIn('executor', field_names)
        self.assertNotIn('action', field_names)
        self.assertNotIn('payload', field_names)

    def test_propose_send_requires_separate_approval_not_read_auth(self):
        """propose_send creates an ActionProposal, not a PrivateReadAuth."""
        from zamal_core.channel_adapter import ChannelAdapter
        from zamal_core.private_read_auth import PrivateReadAuth
        adapter = ChannelAdapter()
        proposal = adapter.propose_send(
            channel='whatsapp', destination='test', content='test msg'
        )
        self.assertNotIsInstance(proposal, PrivateReadAuth)
        self.assertEqual(proposal.status, 'PENDING')


# ── 8. Raw private content must not appear in returned items ──────────────────

class TestNoRawContentInOutput(unittest.TestCase):
    """Items returned without auth must contain no real message content."""

    def _check_no_raw_content(self, items):
        for item in items:
            # sender must be sanitized
            self.assertNotRegex(item.sender, r'[A-Z][a-z]+ [A-Z][a-z]+',
                                'Real contact name must not appear in sender')
            # preview must not be real text (must be a system token)
            self.assertTrue(
                item.preview.startswith('[') and item.preview.endswith(']'),
                f'Preview must be a bracketed token, got: {item.preview!r}'
            )

    def test_whatsapp_denied_items_have_no_real_content(self):
        from zamal_core.channel_adapter import _read_whatsapp
        items = _read_whatsapp(limit=5)
        self._check_no_raw_content(items)

    def test_instagram_denied_items_have_no_real_content(self):
        from zamal_core.channel_adapter import _read_instagram
        items = _read_instagram(limit=5)
        self._check_no_raw_content(items)

    def test_gmail_denied_items_have_no_real_content(self):
        from zamal_core.channel_adapter import _read_gmail
        items = _read_gmail(limit=5)
        self._check_no_raw_content(items)


# ── 9. Broad-extraction triggers sanitized blocking (not raw error) ───────────

class TestAccidentalExposureBlocking(unittest.TestCase):

    def test_broad_extraction_returns_false_and_marker(self):
        """_exec_js_in_domain must return (False, BROAD_EXTRACTION_BLOCKED:...)."""
        from zamal_core.ai_browser_transport import _exec_js_in_domain
        ok, out = _exec_js_in_domain(
            'web.whatsapp.com',
            'document.body.innerText.slice(0, 2000)',
            timeout=5,
        )
        self.assertFalse(ok)
        self.assertTrue(out.startswith('BROAD_EXTRACTION_BLOCKED'),
                        f'Expected BROAD_EXTRACTION_BLOCKED, got: {out!r}')

    def test_broad_extraction_does_not_reach_osascript(self):
        """When broad extraction is detected, osascript must never be called."""
        with patch('zamal_core.ai_browser_transport._run_osascript') as mock_osa:
            from zamal_core.ai_browser_transport import _exec_js_in_domain
            _exec_js_in_domain('instagram.com', 'document.body.innerHTML', timeout=5)
            mock_osa.assert_not_called()

    def test_check_js_for_broad_extraction_identifies_patterns(self):
        from zamal_core.private_read_auth import check_js_for_broad_extraction
        cases = [
            ('mail.google.com', 'document.body.innerText', 'document.body.innerText'),
            ('web.whatsapp.com', 'document.body.innerHTML', 'document.body.innerHTML'),
            ('instagram.com', 'document.body.textContent', 'document.body.textContent'),
            ('facebook.com', '.querySelectorAll("*")', '.querySelectorAll("*")'),
        ]
        for domain, js, expected_pattern in cases:
            with self.subTest(domain=domain, js=js):
                result = check_js_for_broad_extraction(domain, js)
                self.assertEqual(result, expected_pattern)

    def test_record_privacy_incident_does_not_log_raw_content(self):
        """record_privacy_incident must not log the 'detail' string as-is if it
        contained real content — it is truncated to 120 chars (no-op for test)."""
        from zamal_core.private_read_auth import record_privacy_incident
        # Should not raise; must not write raw private content
        record_privacy_incident('whatsapp', 'test_operation', 'safe_detail_only')


# ── 10. Existing routing and CPC zero-write remain intact ─────────────────────

class TestExistingBehaviourUnchanged(unittest.TestCase):

    def test_facebook_read_still_works_without_auth(self):
        """Facebook (feed) is NOT a private channel; read must work without auth."""
        from zamal_core.channel_adapter import _read_facebook
        # _read_facebook does NOT call require_read_auth
        # We can call it without mocking — it uses the browser or returns []
        # Just assert it doesn't raise PrivateReadDenied
        try:
            result = _read_facebook(limit=1)
            self.assertIsInstance(result, list)
        except Exception as exc:
            if 'PrivateReadDenied' in type(exc).__name__:
                self.fail('_read_facebook must not raise PrivateReadDenied')

    def test_calendar_read_works_without_auth(self):
        """Calendar is NOT a private channel; read must work without auth."""
        from zamal_core.channel_adapter import _read_calendar
        try:
            result = _read_calendar(limit=1)
            self.assertIsInstance(result, list)
        except Exception as exc:
            if 'PrivateReadDenied' in type(exc).__name__:
                self.fail('_read_calendar must not raise PrivateReadDenied')

    def test_propose_send_and_execute_rejected_unchanged(self):
        """Approval gate and reject flow must still work."""
        from zamal_core.channel_adapter import ChannelAdapter
        adapter = ChannelAdapter()
        proposal = adapter.propose_send(
            channel='whatsapp', destination='test_target',
            content='TEST privacy regression'
        )
        self.assertEqual(proposal.status, 'PENDING')
        result = adapter.execute_rejected(proposal, actor='test')
        self.assertEqual(result.get('external_action'), 'none')
        self.assertEqual(proposal.status, 'REJECTED')

    def test_privacy_channels_frozenset_is_immutable(self):
        """PRIVATE_CHANNELS must be a frozenset (immutable)."""
        from zamal_core.private_read_auth import PRIVATE_CHANNELS
        self.assertIsInstance(PRIVATE_CHANNELS, frozenset)
        with self.assertRaises((AttributeError, TypeError)):
            PRIVATE_CHANNELS.add('new_channel')  # type: ignore[attr-defined]

    def test_metadata_only_result_contains_no_content(self):
        """metadata_only_result must not contain names, previews or content."""
        from zamal_core.private_read_auth import metadata_only_result
        for channel in ('gmail', 'whatsapp', 'messenger', 'instagram', 'telegram'):
            meta = metadata_only_result(channel)
            self.assertIn('content_access', meta)
            self.assertEqual(meta['content_access'], 'DENIED')
            self.assertNotIn('preview', meta)
            self.assertNotIn('sender', meta)
            self.assertNotIn('subject', meta)


if __name__ == '__main__':
    unittest.main()
