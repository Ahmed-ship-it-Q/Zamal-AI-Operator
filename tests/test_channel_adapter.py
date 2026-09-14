"""Tests for zamal_core/channel_adapter.py — unified channel adapter.

No real external network calls or Chrome access. All browser/bot I/O is mocked.
"""
import json
import threading
import time
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import zamal_core.channel_adapter as ca
from zamal_core.channel_adapter import (
    ActionProposal,
    ChannelAdapter,
    ChannelItem,
    ChannelResult,
    _get_executor,
    _get_reader,
    _read_gmail,
    _read_whatsapp,
    _read_calendar,
    _read_facebook,
    _read_instagram,
    _read_telegram,
    _exec_gmail_send,
    _exec_whatsapp_send,
    _exec_facebook_send,
    _exec_instagram_dm,
    _exec_calendar_create,
    _exec_telegram_send,
    _get_emergency_stop,
    get_channel_adapter,
)


# ── Helper: build a minimal ActionProposal ─────────────────────────────────

def _make_proposal(channel='gmail', action='send_message',
                   approval_id='appr-123', proposal_id=None) -> ActionProposal:
    import uuid
    return ActionProposal(
        proposal_id=proposal_id or str(uuid.uuid4()),
        channel=channel,
        action=action,
        destination='test@example.com',
        preview='Hello world',
        task_id='task-1',
        approval_id=approval_id,
        payload={'channel': channel, 'destination': 'test@example.com',
                 'content': 'Hello world', 'privacy_level': 'PUBLIC'},
        timestamp=time.time(),
    )


# ── ChannelAdapter.read() ──────────────────────────────────────────────────

class TestReadUnknownChannel(unittest.TestCase):
    def setUp(self):
        self.adapter = ChannelAdapter()

    def test_unknown_channel_returns_error_result(self):
        result = self.adapter.read('twitter')
        self.assertIsInstance(result, ChannelResult)
        self.assertEqual(result.channel, 'twitter')
        self.assertEqual(result.items, [])
        self.assertEqual(result.count, 0)
        self.assertIn('unknown_channel', result.error)
        self.assertIn('twitter', result.error)

    def test_unknown_channel_has_empty_provenance(self):
        result = self.adapter.read('slack')
        self.assertEqual(result.provenance, {})

    def test_unknown_channel_task_id_preserved(self):
        result = self.adapter.read('fax', task_id='mytask')
        self.assertEqual(result.task_id, 'mytask')


class TestReadNotDarwin(unittest.TestCase):
    def setUp(self):
        self.adapter = ChannelAdapter()

    @patch('zamal_core.channel_adapter._IS_DARWIN', False)
    def test_non_darwin_returns_error(self):
        result = self.adapter.read('gmail')
        self.assertIsInstance(result, ChannelResult)
        self.assertEqual(result.error, 'not_darwin')
        self.assertEqual(result.items, [])

    @patch('zamal_core.channel_adapter._IS_DARWIN', False)
    def test_non_darwin_all_channels(self):
        for ch in ('telegram', 'gmail', 'calendar', 'whatsapp', 'facebook', 'instagram'):
            result = self.adapter.read(ch)
            self.assertEqual(result.error, 'not_darwin',
                             f'Channel {ch} should return not_darwin error')

    @patch('zamal_core.channel_adapter._IS_DARWIN', False)
    def test_non_darwin_preserves_channel_name(self):
        result = self.adapter.read('whatsapp', task_id='t1')
        self.assertEqual(result.channel, 'whatsapp')
        self.assertEqual(result.task_id, 't1')


class TestReadPrivacyFilter(unittest.TestCase):
    """read() must apply privacy filter to all items — block SSN, sanitize API keys."""

    def setUp(self):
        self.adapter = ChannelAdapter()

    @patch('zamal_core.channel_adapter._IS_DARWIN', True)
    @patch('zamal_core.channel_adapter._get_reader')
    def test_ssn_in_preview_blocks_item(self, mock_get_reader):
        """Items containing SSN are blocked (filtered out of results)."""
        mock_reader = MagicMock(return_value=[
            ChannelItem(channel='gmail', item_id='1', sender='a@b.com',
                        subject='test', preview='SSN: 123-45-6789', timestamp=time.time()),
        ])
        mock_get_reader.return_value = mock_reader
        result = self.adapter.read('gmail')
        # SSN triggers BLOCKED level → item dropped
        self.assertEqual(result.count, 0)
        self.assertEqual(result.items, [])

    @patch('zamal_core.channel_adapter._IS_DARWIN', True)
    @patch('zamal_core.channel_adapter._get_reader')
    def test_api_key_in_preview_is_sanitized(self, mock_get_reader):
        """Items with API keys are sanitized and included with redacted text."""
        mock_reader = MagicMock(return_value=[
            ChannelItem(channel='gmail', item_id='2', sender='a@b.com',
                        subject='creds', preview='Key: sk-abcdefghijklmnopqrstuvwxyz123456',
                        timestamp=time.time()),
        ])
        mock_get_reader.return_value = mock_reader
        result = self.adapter.read('gmail')
        self.assertEqual(result.count, 1)
        self.assertEqual(result.privacy_level, 'SANITIZED')
        self.assertIn('[API_KEY]', result.items[0].preview)
        self.assertNotIn('sk-abcde', result.items[0].preview)

    @patch('zamal_core.channel_adapter._IS_DARWIN', True)
    @patch('zamal_core.channel_adapter._get_reader')
    def test_clean_content_is_public(self, mock_get_reader):
        mock_reader = MagicMock(return_value=[
            ChannelItem(channel='gmail', item_id='3', sender='a@b.com',
                        subject='Hello', preview='Just a normal message.',
                        timestamp=time.time()),
        ])
        mock_get_reader.return_value = mock_reader
        result = self.adapter.read('gmail')
        self.assertEqual(result.count, 1)
        self.assertEqual(result.privacy_level, 'PUBLIC')
        self.assertEqual(result.items[0].preview, 'Just a normal message.')

    @patch('zamal_core.channel_adapter._IS_DARWIN', True)
    @patch('zamal_core.channel_adapter._get_reader')
    def test_preview_truncated_to_500_chars(self, mock_get_reader):
        long_preview = 'A' * 1000
        mock_reader = MagicMock(return_value=[
            ChannelItem(channel='gmail', item_id='4', sender='x@y.com',
                        subject='long', preview=long_preview, timestamp=time.time()),
        ])
        mock_get_reader.return_value = mock_reader
        result = self.adapter.read('gmail')
        self.assertLessEqual(len(result.items[0].preview), 500)

    @patch('zamal_core.channel_adapter._IS_DARWIN', True)
    @patch('zamal_core.channel_adapter._get_reader')
    def test_multiple_items_mixed_privacy(self, mock_get_reader):
        """SSN item dropped, API key item sanitized, clean item passed through."""
        mock_reader = MagicMock(return_value=[
            ChannelItem(channel='gmail', item_id='a', sender='x', subject='',
                        preview='SSN: 111-22-3333', timestamp=time.time()),
            ChannelItem(channel='gmail', item_id='b', sender='y', subject='',
                        preview='key sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx ok',
                        timestamp=time.time()),
            ChannelItem(channel='gmail', item_id='c', sender='z', subject='',
                        preview='Regular message', timestamp=time.time()),
        ])
        mock_get_reader.return_value = mock_reader
        result = self.adapter.read('gmail')
        self.assertEqual(result.count, 2)  # SSN item dropped
        ids = [item.item_id for item in result.items]
        self.assertNotIn('a', ids)         # SSN blocked
        self.assertIn('b', ids)            # API key sanitized
        self.assertIn('c', ids)            # clean
        self.assertEqual(result.privacy_level, 'SANITIZED')

    @patch('zamal_core.channel_adapter._IS_DARWIN', True)
    @patch('zamal_core.channel_adapter._get_reader')
    def test_reader_exception_returns_error_result(self, mock_get_reader):
        mock_get_reader.return_value = MagicMock(side_effect=RuntimeError('boom'))
        result = self.adapter.read('gmail')
        self.assertIn('boom', result.error)
        self.assertEqual(result.count, 0)

    @patch('zamal_core.channel_adapter._IS_DARWIN', True)
    @patch('zamal_core.channel_adapter._get_reader')
    def test_provenance_populated_on_success(self, mock_get_reader):
        mock_get_reader.return_value = MagicMock(return_value=[])
        result = self.adapter.read('gmail', limit=5, query='foo', task_id='t42')
        self.assertEqual(result.provenance['channel'], 'gmail')
        self.assertEqual(result.provenance['limit'], 5)
        self.assertEqual(result.provenance['query'], 'foo')
        self.assertIn('timestamp', result.provenance)


# ── propose_send() ─────────────────────────────────────────────────────────

class TestProposeSend(unittest.TestCase):
    def setUp(self):
        self.adapter = ChannelAdapter()

    @patch.object(ChannelAdapter, '_create_approval', return_value='appr-abc')
    def test_returns_action_proposal(self, _mock):
        proposal = self.adapter.propose_send(
            'gmail', 'alice@example.com', 'Hello Alice', task_id='t1')
        self.assertIsInstance(proposal, ActionProposal)
        self.assertEqual(proposal.channel, 'gmail')
        self.assertEqual(proposal.action, 'send_message')
        self.assertEqual(proposal.approval_id, 'appr-abc')
        self.assertEqual(proposal.task_id, 't1')
        self.assertEqual(proposal.status, 'PENDING')

    @patch.object(ChannelAdapter, '_create_approval', return_value='appr-xyz')
    def test_destination_in_payload(self, _mock):
        proposal = self.adapter.propose_send(
            'whatsapp', '+15551234567', 'Hi there')
        self.assertEqual(proposal.destination, '+15551234567')
        self.assertIn('destination', proposal.payload)

    @patch.object(ChannelAdapter, '_create_approval', return_value='id-1')
    def test_proposal_id_is_uuid(self, _mock):
        import uuid
        proposal = self.adapter.propose_send('telegram', 'user123', 'msg')
        uuid.UUID(proposal.proposal_id)   # raises if invalid UUID

    def test_blocked_content_raises_value_error(self):
        """SSN in message content → ValueError (privacy_filter_blocked)."""
        with self.assertRaises(ValueError) as ctx:
            self.adapter.propose_send('gmail', 'bob@x.com', 'My SSN is 123-45-6789')
        self.assertIn('privacy_filter_blocked', str(ctx.exception))

    def test_blocked_credit_card_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.adapter.propose_send('gmail', 'bob@x.com',
                                       'Card: 4111 1111 1111 1111 please charge')

    @patch.object(ChannelAdapter, '_create_approval', return_value='id-2')
    def test_api_key_in_content_is_sanitized_not_blocked(self, _mock):
        """API keys are sanitized (SANITIZED level), not blocked."""
        proposal = self.adapter.propose_send(
            'gmail', 'bob@x.com', 'Key sk-abcdefghijklmnopqrstuvwxyz12345 sent')
        self.assertIn('[API_KEY]', proposal.preview)
        self.assertNotIn('sk-abcdef', proposal.preview)

    @patch.object(ChannelAdapter, '_create_approval', return_value='id-3')
    def test_long_destination_truncated(self, _mock):
        proposal = self.adapter.propose_send('gmail', 'a' * 300, 'content')
        self.assertLessEqual(len(proposal.destination), 200)

    @patch.object(ChannelAdapter, '_create_approval', return_value='id-4')
    def test_preview_truncated_to_500(self, _mock):
        proposal = self.adapter.propose_send('gmail', 'x@y.com', 'X' * 2000)
        self.assertLessEqual(len(proposal.preview), 500)

    @patch.object(ChannelAdapter, '_create_approval', return_value='id-5')
    def test_content_in_payload_truncated_to_2000(self, _mock):
        proposal = self.adapter.propose_send('gmail', 'x@y.com', 'Y' * 3000)
        self.assertLessEqual(len(proposal.payload['content']), 2000)


# ── propose_calendar_event() ───────────────────────────────────────────────

class TestProposeCalendarEvent(unittest.TestCase):
    def setUp(self):
        self.adapter = ChannelAdapter()

    @patch.object(ChannelAdapter, '_create_approval', return_value='cal-appr-1')
    def test_returns_action_proposal(self, _mock):
        proposal = self.adapter.propose_calendar_event(
            'Team meeting', '2026-09-15T10:00', '2026-09-15T11:00',
            description='Discuss roadmap', task_id='t2')
        self.assertIsInstance(proposal, ActionProposal)
        self.assertEqual(proposal.channel, 'calendar')
        self.assertEqual(proposal.action, 'create_event')
        self.assertEqual(proposal.approval_id, 'cal-appr-1')
        self.assertIn('Team meeting', proposal.preview)

    def test_blocked_title_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.adapter.propose_calendar_event(
                'Meeting with SSN 123-45-6789', '2026-01-01T10:00', '2026-01-01T11:00')


# ── execute_rejected() ────────────────────────────────────────────────────

class TestExecuteRejected(unittest.TestCase):
    def setUp(self):
        self.adapter = ChannelAdapter()

    def test_returns_zero_external_action(self):
        proposal = _make_proposal()
        result = self.adapter.execute_rejected(proposal, actor='human')
        self.assertTrue(result['ok'])
        self.assertEqual(result['action'], 'rejected')
        self.assertEqual(result['external_action'], 'none')

    def test_sets_status_rejected(self):
        proposal = _make_proposal()
        self.adapter.execute_rejected(proposal, actor='human')
        self.assertEqual(proposal.status, 'REJECTED')

    def test_proposal_id_in_result(self):
        proposal = _make_proposal()
        pid = proposal.proposal_id
        result = self.adapter.execute_rejected(proposal, actor='admin')
        self.assertEqual(result['proposal_id'], pid)

    def test_actor_in_result(self):
        proposal = _make_proposal()
        result = self.adapter.execute_rejected(proposal, actor='alice')
        self.assertEqual(result['actor'], 'alice')

    def test_no_executor_called_on_reject(self):
        """execute_rejected must never invoke any channel executor."""
        proposal = _make_proposal()
        with patch('zamal_core.channel_adapter._get_executor') as mock_exec:
            self.adapter.execute_rejected(proposal, actor='human')
            mock_exec.assert_not_called()


# ── execute_approved() — idempotency ─────────────────────────────────────

class TestExecuteApprovedIdempotency(unittest.TestCase):
    def setUp(self):
        self.adapter = ChannelAdapter()

    @patch('zamal_core.channel_adapter._get_emergency_stop', return_value={})
    @patch('zamal_core.channel_adapter._get_executor')
    def test_second_call_same_proposal_id_returns_already_executed(
            self, mock_exec, _mock_stop):
        mock_executor = MagicMock(return_value={'ok': True, 'stage': 'sent'})
        mock_exec.return_value = mock_executor

        proposal = _make_proposal()
        result1 = self.adapter.execute_approved(proposal, 'appr-123', 'human')
        result2 = self.adapter.execute_approved(proposal, 'appr-123', 'human')

        self.assertTrue(result1['ok'])
        self.assertFalse(result2['ok'])
        self.assertEqual(result2['error'], 'already_executed')
        # Executor was called exactly once
        self.assertEqual(mock_executor.call_count, 1)

    @patch('zamal_core.channel_adapter._get_emergency_stop', return_value={})
    @patch('zamal_core.channel_adapter._get_executor')
    def test_different_proposal_ids_both_execute(self, mock_exec, _mock_stop):
        mock_executor = MagicMock(return_value={'ok': True})
        mock_exec.return_value = mock_executor

        p1 = _make_proposal(proposal_id='pid-1')
        p2 = _make_proposal(proposal_id='pid-2')
        r1 = self.adapter.execute_approved(p1, 'appr-123', 'human')
        r2 = self.adapter.execute_approved(p2, 'appr-123', 'human')

        self.assertTrue(r1['ok'])
        self.assertTrue(r2['ok'])
        self.assertEqual(mock_executor.call_count, 2)

    @patch('zamal_core.channel_adapter._get_emergency_stop', return_value={})
    @patch('zamal_core.channel_adapter._get_executor')
    def test_concurrent_calls_only_one_executes(self, mock_exec, _mock_stop):
        """Thread safety: only one concurrent execution wins for same proposal_id."""
        call_count = {'n': 0}
        barrier = threading.Barrier(5)

        def slow_executor(payload):
            call_count['n'] += 1
            time.sleep(0.05)
            return {'ok': True}

        mock_exec.return_value = slow_executor
        proposal = _make_proposal()
        results = []

        def run():
            barrier.wait()
            results.append(
                self.adapter.execute_approved(proposal, 'appr-123', 'human'))

        threads = [threading.Thread(target=run) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successful = [r for r in results if r.get('ok')]
        already_exec = [r for r in results if r.get('error') == 'already_executed']
        self.assertEqual(len(successful), 1)
        self.assertEqual(len(already_exec), 4)


# ── execute_approved() — Emergency Stop ───────────────────────────────────

class TestExecuteApprovedEmergencyStop(unittest.TestCase):
    def setUp(self):
        self.adapter = ChannelAdapter()

    @patch('zamal_core.channel_adapter._get_emergency_stop',
           return_value={'stop_everything': True, 'pause_external_actions': False})
    def test_stop_everything_blocks_execution(self, _mock):
        proposal = _make_proposal()
        result = self.adapter.execute_approved(proposal, 'appr-123', 'human')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'emergency_stop_active')

    @patch('zamal_core.channel_adapter._get_emergency_stop',
           return_value={'stop_everything': False, 'pause_external_actions': True})
    def test_pause_external_actions_blocks_execution(self, _mock):
        proposal = _make_proposal()
        result = self.adapter.execute_approved(proposal, 'appr-123', 'human')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'emergency_stop_active')

    @patch('zamal_core.channel_adapter._get_emergency_stop', return_value={})
    @patch('zamal_core.channel_adapter._get_executor')
    def test_no_emergency_stop_proceeds(self, mock_exec, _mock_stop):
        mock_exec.return_value = MagicMock(return_value={'ok': True, 'stage': 'sent'})
        proposal = _make_proposal()
        result = self.adapter.execute_approved(proposal, 'appr-123', 'human')
        self.assertTrue(result['ok'])

    @patch('zamal_core.channel_adapter._get_emergency_stop',
           return_value={'stop_everything': False, 'pause_external_actions': False})
    @patch('zamal_core.channel_adapter._get_executor')
    def test_false_flags_proceed(self, mock_exec, _mock_stop):
        mock_exec.return_value = MagicMock(return_value={'ok': True})
        proposal = _make_proposal()
        result = self.adapter.execute_approved(proposal, 'appr-123', 'human')
        self.assertTrue(result['ok'])


# ── execute_approved() — approval_id mismatch ────────────────────────────

class TestExecuteApprovedApprovalIdCheck(unittest.TestCase):
    def setUp(self):
        self.adapter = ChannelAdapter()

    @patch('zamal_core.channel_adapter._get_emergency_stop', return_value={})
    def test_mismatched_approval_id_rejected(self, _mock_stop):
        proposal = _make_proposal(approval_id='correct-id')
        result = self.adapter.execute_approved(proposal, 'wrong-id', 'human')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'approval_id_mismatch')

    @patch('zamal_core.channel_adapter._get_emergency_stop', return_value={})
    def test_empty_proposal_approval_id_skips_check(self, _mock_stop):
        """If proposal.approval_id is empty (fallback), no mismatch check."""
        with patch('zamal_core.channel_adapter._get_executor') as mock_exec:
            mock_exec.return_value = MagicMock(return_value={'ok': True})
            proposal = _make_proposal(approval_id='')
            result = self.adapter.execute_approved(proposal, 'any-id', 'human')
            self.assertTrue(result['ok'])


# ── execute_approved() — result fields ───────────────────────────────────

class TestExecuteApprovedResult(unittest.TestCase):
    def setUp(self):
        self.adapter = ChannelAdapter()

    @patch('zamal_core.channel_adapter._get_emergency_stop', return_value={})
    @patch('zamal_core.channel_adapter._get_executor')
    def test_result_contains_required_fields(self, mock_exec, _mock_stop):
        mock_exec.return_value = MagicMock(return_value={'ok': True, 'stage': 'sent'})
        proposal = _make_proposal()
        result = self.adapter.execute_approved(proposal, 'appr-123', 'human')
        self.assertIn('ok', result)
        self.assertIn('result', result)
        self.assertIn('proposal_id', result)
        self.assertIn('approval_id', result)
        self.assertIn('actor', result)

    @patch('zamal_core.channel_adapter._get_emergency_stop', return_value={})
    @patch('zamal_core.channel_adapter._get_executor')
    def test_executor_exception_returns_error_not_raises(self, mock_exec, _mock_stop):
        mock_exec.return_value = MagicMock(side_effect=RuntimeError('network down'))
        proposal = _make_proposal()
        result = self.adapter.execute_approved(proposal, 'appr-123', 'human')
        self.assertFalse(result['ok'])
        self.assertIn('network down', result['error'])


# ── _read_gmail() ─────────────────────────────────────────────────────────
# _exec_js_in_domain is imported inside reader functions from ai_browser_transport,
# so the correct patch target is zamal_core.ai_browser_transport._exec_js_in_domain.

_JS_PATCH = 'zamal_core.ai_browser_transport._exec_js_in_domain'


class TestReadGmail(unittest.TestCase):
    def _make_js_output(self):
        data = [
            {'id': 'thread1', 'sender': 'alice@example.com',
             'subject': 'Hello', 'preview': 'Hey there', 'time': 'Mon 10:00 AM'},
            {'id': 'thread2', 'sender': 'bob@example.com',
             'subject': 'Invoice', 'preview': 'Please find attached', 'time': 'Tue 2:00 PM'},
        ]
        return json.dumps(data)

    @patch(_JS_PATCH)
    def test_parses_chrome_js_output_correctly(self, mock_js):
        mock_js.return_value = (True, self._make_js_output())
        items = _read_gmail(limit=10)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].channel, 'gmail')
        self.assertEqual(items[0].sender, 'alice@example.com')
        self.assertEqual(items[0].subject, 'Hello')
        self.assertEqual(items[0].preview, 'Hey there')
        self.assertEqual(items[0].item_id, 'thread1')
        self.assertEqual(items[0].metadata['time'], 'Mon 10:00 AM')

    @patch(_JS_PATCH)
    def test_returns_empty_on_js_failure(self, mock_js):
        mock_js.return_value = (False, '')
        items = _read_gmail()
        self.assertEqual(items, [])

    @patch(_JS_PATCH)
    def test_returns_empty_on_null_output(self, mock_js):
        mock_js.return_value = (True, 'null')
        items = _read_gmail()
        self.assertEqual(items, [])

    @patch(_JS_PATCH)
    def test_returns_empty_on_missing_value(self, mock_js):
        mock_js.return_value = (True, 'missing value')
        items = _read_gmail()
        self.assertEqual(items, [])

    @patch(_JS_PATCH)
    def test_returns_empty_on_invalid_json(self, mock_js):
        mock_js.return_value = (True, 'not-valid-json{{{')
        items = _read_gmail()
        self.assertEqual(items, [])

    @patch(_JS_PATCH)
    def test_all_items_have_gmail_channel(self, mock_js):
        mock_js.return_value = (True, self._make_js_output())
        items = _read_gmail()
        for item in items:
            self.assertEqual(item.channel, 'gmail')

    @patch(_JS_PATCH)
    def test_empty_array_returns_empty_list(self, mock_js):
        mock_js.return_value = (True, '[]')
        items = _read_gmail()
        self.assertEqual(items, [])


# ── _read_whatsapp() ──────────────────────────────────────────────────────

class TestReadWhatsapp(unittest.TestCase):
    def _make_js_output(self):
        data = [
            {'id': '0', 'name': 'Alice', 'preview': 'See you tomorrow'},
            {'id': '1', 'name': 'Bob', 'preview': 'Can we reschedule?'},
        ]
        return json.dumps(data)

    @patch(_JS_PATCH)
    def test_parses_chrome_js_output_correctly(self, mock_js):
        mock_js.return_value = (True, self._make_js_output())
        items = _read_whatsapp(limit=10)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].channel, 'whatsapp')
        self.assertEqual(items[0].sender, 'Alice')
        self.assertEqual(items[0].preview, 'See you tomorrow')

    @patch(_JS_PATCH)
    def test_returns_empty_on_js_failure(self, mock_js):
        mock_js.return_value = (False, '')
        items = _read_whatsapp()
        self.assertEqual(items, [])

    @patch(_JS_PATCH)
    def test_returns_empty_on_empty_array(self, mock_js):
        mock_js.return_value = (True, '[]')
        items = _read_whatsapp()
        self.assertEqual(items, [])

    @patch(_JS_PATCH)
    def test_all_items_have_whatsapp_channel(self, mock_js):
        mock_js.return_value = (True, self._make_js_output())
        items = _read_whatsapp()
        for item in items:
            self.assertEqual(item.channel, 'whatsapp')

    @patch(_JS_PATCH)
    def test_returns_empty_on_missing_value(self, mock_js):
        mock_js.return_value = (True, 'missing value')
        items = _read_whatsapp()
        self.assertEqual(items, [])

    @patch(_JS_PATCH)
    def test_sender_is_conversation_name(self, mock_js):
        mock_js.return_value = (True, json.dumps([{'id': '0', 'name': 'Charlie', 'preview': 'hi'}]))
        items = _read_whatsapp()
        self.assertEqual(items[0].sender, 'Charlie')


# ── _read_calendar() ─────────────────────────────────────────────────────

class TestReadCalendar(unittest.TestCase):
    @patch(_JS_PATCH)
    def test_parses_events(self, mock_js):
        data = [{'id': '0', 'title': 'Stand-up', 'time': ''},
                {'id': '1', 'title': 'Lunch', 'time': ''}]
        mock_js.return_value = (True, json.dumps(data))
        items = _read_calendar()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].channel, 'calendar')
        self.assertEqual(items[0].subject, 'Stand-up')
        self.assertEqual(items[0].sender, 'Google Calendar')

    @patch(_JS_PATCH)
    def test_returns_empty_on_failure(self, mock_js):
        mock_js.return_value = (False, '')
        self.assertEqual(_read_calendar(), [])

    @patch(_JS_PATCH)
    def test_returns_empty_on_empty_array(self, mock_js):
        mock_js.return_value = (True, '[]')
        self.assertEqual(_read_calendar(), [])


# ── _read_facebook() ─────────────────────────────────────────────────────

class TestReadFacebook(unittest.TestCase):
    @patch(_JS_PATCH)
    def test_parses_threads(self, mock_js):
        data = [{'id': '0', 'preview': 'Hey!'}, {'id': '1', 'preview': 'What time?'}]
        mock_js.return_value = (True, json.dumps(data))
        items = _read_facebook()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].channel, 'facebook')
        self.assertEqual(items[0].preview, 'Hey!')

    @patch(_JS_PATCH)
    def test_returns_empty_on_failure(self, mock_js):
        mock_js.return_value = (False, '')
        self.assertEqual(_read_facebook(), [])


# ── _read_instagram() ────────────────────────────────────────────────────

class TestReadInstagram(unittest.TestCase):
    @patch(_JS_PATCH)
    def test_parses_dms(self, mock_js):
        data = [{'id': '0', 'preview': 'Check this out'}]
        mock_js.return_value = (True, json.dumps(data))
        items = _read_instagram()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].channel, 'instagram')
        self.assertEqual(items[0].preview, 'Check this out')

    @patch(_JS_PATCH)
    def test_returns_empty_on_missing_value(self, mock_js):
        mock_js.return_value = (True, 'missing value')
        self.assertEqual(_read_instagram(), [])


# ── _read_telegram() ─────────────────────────────────────────────────────

class TestReadTelegram(unittest.TestCase):
    def test_returns_empty_when_telegram_not_configured(self):
        """When TelegramConfig reports not configured, reader returns []."""
        mock_tc = MagicMock()
        mock_tc.configured = False
        mock_tg_mod = MagicMock()
        mock_tg_mod.TelegramConfig.return_value = mock_tc
        mock_cc_mod = MagicMock()
        mock_cc_mod._load_auth_env = MagicMock()
        with patch.dict('sys.modules', {
            'zamal_core.control_center': mock_cc_mod,
            'zamal_core.telegram_bot': mock_tg_mod,
        }):
            items = _read_telegram(limit=5)
        self.assertEqual(items, [])

    def test_returns_empty_on_api_exception(self):
        """If the Telegram API raises, _read_telegram returns []."""
        mock_tc = MagicMock()
        mock_tc.configured = True
        mock_tc._token = 'fake-token'
        mock_api = MagicMock()
        mock_api.call.side_effect = RuntimeError('network error')
        mock_tg_mod = MagicMock()
        mock_tg_mod.TelegramConfig.return_value = mock_tc
        mock_tg_mod.TelegramAPI.return_value = mock_api
        mock_cc_mod = MagicMock()
        mock_cc_mod._load_auth_env = MagicMock()
        with patch.dict('sys.modules', {
            'zamal_core.control_center': mock_cc_mod,
            'zamal_core.telegram_bot': mock_tg_mod,
        }):
            items = _read_telegram(limit=5)
        self.assertEqual(items, [])

    def test_parses_updates_into_channel_items(self):
        """Valid Telegram updates are parsed into ChannelItem list."""
        updates = [
            {'update_id': 101, 'message': {
                'text': 'Hello bot', 'date': 1700000000,
                'from': {'username': 'alice', 'first_name': 'Alice'},
            }},
        ]
        mock_tc = MagicMock()
        mock_tc.configured = True
        mock_tc._token = 'fake-token'
        mock_api = MagicMock()
        mock_api.call.return_value = updates
        mock_tg_mod = MagicMock()
        mock_tg_mod.TelegramConfig.return_value = mock_tc
        mock_tg_mod.TelegramAPI.return_value = mock_api
        mock_cc_mod = MagicMock()
        mock_cc_mod._load_auth_env = MagicMock()
        with patch.dict('sys.modules', {
            'zamal_core.control_center': mock_cc_mod,
            'zamal_core.telegram_bot': mock_tg_mod,
        }):
            items = _read_telegram(limit=5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].channel, 'telegram')
        self.assertEqual(items[0].item_id, '101')
        self.assertEqual(items[0].sender, 'alice')
        self.assertEqual(items[0].preview, 'Hello bot')


# ── _get_reader() and _get_executor() ────────────────────────────────────

class TestGetReader(unittest.TestCase):
    def test_all_channels_have_reader(self):
        for ch in ('telegram', 'gmail', 'calendar', 'whatsapp', 'facebook', 'instagram'):
            reader = _get_reader(ch)
            self.assertTrue(callable(reader), f'{ch} reader not callable')

    def test_unknown_channel_raises_key_error(self):
        with self.assertRaises(KeyError):
            _get_reader('twitter')


class TestGetExecutor(unittest.TestCase):
    VALID_PAIRS = [
        ('gmail', 'send_message'),
        ('calendar', 'create_event'),
        ('whatsapp', 'send_message'),
        ('facebook', 'send_message'),
        ('instagram', 'send_message'),
        ('telegram', 'send_message'),
    ]

    def test_all_valid_pairs_return_callable(self):
        for channel, action in self.VALID_PAIRS:
            executor = _get_executor(channel, action)
            self.assertTrue(callable(executor),
                            f'{channel}:{action} executor not callable')

    def test_unknown_pair_raises_value_error(self):
        with self.assertRaises(ValueError):
            _get_executor('gmail', 'delete_all')

    def test_unknown_channel_raises_value_error(self):
        with self.assertRaises(ValueError):
            _get_executor('twitter', 'send_message')


# ── Executor unit tests ───────────────────────────────────────────────────

class TestExecGmailSend(unittest.TestCase):
    @patch(_JS_PATCH)
    def test_returns_dict_on_success(self, mock_js):
        # compose opened, form filled, send clicked
        mock_js.side_effect = [
            (True, json.dumps({'ok': True, 'stage': 'compose_opened'})),
            (True, json.dumps({'ok': True, 'stage': 'filled'})),
            (True, json.dumps({'ok': True, 'stage': 'sent'})),
        ]
        result = _exec_gmail_send({'destination': 'x@y.com', 'content': 'Hi', 'subject': 'Test'})
        self.assertIsInstance(result, dict)

    @patch(_JS_PATCH)
    def test_returns_error_when_compose_fails(self, mock_js):
        mock_js.return_value = (False, '')
        result = _exec_gmail_send({'destination': 'x@y.com', 'content': 'Hi'})
        self.assertFalse(result['ok'])


class TestExecWhatsappSend(unittest.TestCase):
    @patch(_JS_PATCH)
    def test_returns_dict(self, mock_js):
        mock_js.return_value = (True, json.dumps({'ok': True, 'stage': 'sent'}))
        result = _exec_whatsapp_send({'content': 'Hello'})
        self.assertIsInstance(result, dict)

    @patch(_JS_PATCH)
    def test_invalid_json_returns_fallback(self, mock_js):
        mock_js.return_value = (True, 'not json')
        result = _exec_whatsapp_send({'content': 'Hi'})
        # Should not raise; returns dict with 'raw' or 'ok'
        self.assertIsInstance(result, dict)


class TestExecFacebookSend(unittest.TestCase):
    @patch(_JS_PATCH)
    def test_returns_dict(self, mock_js):
        mock_js.return_value = (True, json.dumps({'ok': True, 'stage': 'sent'}))
        result = _exec_facebook_send({'content': 'Hello FB'})
        self.assertIsInstance(result, dict)


class TestExecInstagramDm(unittest.TestCase):
    @patch(_JS_PATCH)
    def test_returns_dict(self, mock_js):
        mock_js.return_value = (True, json.dumps({'ok': True, 'stage': 'enter_sent'}))
        result = _exec_instagram_dm({'content': 'Hello IG'})
        self.assertIsInstance(result, dict)


class TestExecCalendarCreate(unittest.TestCase):
    @patch(_JS_PATCH)
    def test_returns_error_on_js_failure(self, mock_js):
        mock_js.return_value = (False, '')
        result = _exec_calendar_create({'title': 'Meeting'})
        self.assertFalse(result['ok'])

    @patch(_JS_PATCH)
    def test_returns_dict_on_success(self, mock_js):
        mock_js.side_effect = [
            (True, json.dumps({'ok': True, 'stage': 'new_event_clicked'})),
            (True, json.dumps({'ok': True, 'stage': 'title_filled'})),
            (True, json.dumps({'ok': True, 'stage': 'saved'})),
        ]
        result = _exec_calendar_create({'title': 'Team sync'})
        self.assertIsInstance(result, dict)


class TestExecTelegramSend(unittest.TestCase):
    def test_returns_not_configured_when_no_env(self):
        """When TelegramConfig reports not configured, return error dict."""
        with patch('zamal_core.channel_adapter._load_auth_env', create=True):
            pass
        # Patch the imports inside the function
        mock_tc = MagicMock()
        mock_tc.configured = False

        with patch.dict('sys.modules', {
            'zamal_core.control_center': MagicMock(_load_auth_env=lambda: None),
            'zamal_core.telegram_bot': MagicMock(
                TelegramConfig=MagicMock(return_value=mock_tc),
                TelegramAPI=MagicMock(),
            ),
        }):
            result = _exec_telegram_send({'content': 'hi'})
            self.assertFalse(result['ok'])
            self.assertIn('telegram_not_configured', result['error'])


# ── _get_emergency_stop() ─────────────────────────────────────────────────

class TestGetEmergencyStop(unittest.TestCase):
    # Store is imported inside _get_emergency_stop via `from .store import Store`.
    # Since the function does a fresh local import each call, patch the source module.
    _STORE_PATCH = 'zamal_core.store.Store'

    def test_returns_dict_always(self):
        """_get_emergency_stop() never raises; returns a dict."""
        result = _get_emergency_stop()
        self.assertIsInstance(result, dict)

    def test_returns_empty_on_db_error(self):
        """If Store raises (e.g., permissions), returns {}."""
        with patch(self._STORE_PATCH, side_effect=Exception('db error')):
            result = _get_emergency_stop()
            self.assertEqual(result, {})

    def test_reads_stop_everything_flag(self):
        """When controls row has stop_everything=1, result contains stop_everything key."""
        data = {'stop_everything': 1, 'pause_external_actions': 0}
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda s, key: data[key]
        mock_store = MagicMock()
        mock_store.db.execute.return_value.fetchone.return_value = mock_row
        with patch(self._STORE_PATCH, return_value=mock_store):
            result = _get_emergency_stop()
        self.assertIn('stop_everything', result)
        self.assertTrue(result['stop_everything'])

    def test_reads_pause_external_actions_flag(self):
        """When pause_external_actions=1, result contains it as True."""
        data = {'stop_everything': 0, 'pause_external_actions': 1}
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda s, key: data[key]
        mock_store = MagicMock()
        mock_store.db.execute.return_value.fetchone.return_value = mock_row
        with patch(self._STORE_PATCH, return_value=mock_store):
            result = _get_emergency_stop()
        self.assertTrue(result['pause_external_actions'])

    def test_returns_empty_when_no_row(self):
        """When controls table has no row (id=1 missing), returns {}."""
        mock_store = MagicMock()
        mock_store.db.execute.return_value.fetchone.return_value = None
        with patch(self._STORE_PATCH, return_value=mock_store):
            result = _get_emergency_stop()
        self.assertEqual(result, {})


# ── get_channel_adapter() singleton ──────────────────────────────────────

class TestGetChannelAdapterSingleton(unittest.TestCase):
    def test_returns_channel_adapter_instance(self):
        adapter = get_channel_adapter()
        self.assertIsInstance(adapter, ChannelAdapter)

    def test_returns_same_instance_on_repeated_calls(self):
        a1 = get_channel_adapter()
        a2 = get_channel_adapter()
        self.assertIs(a1, a2)

    def test_singleton_is_thread_safe(self):
        """Multiple threads should all get the exact same instance."""
        results = []

        def fetch():
            results.append(get_channel_adapter())

        threads = [threading.Thread(target=fetch) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 10)
        first = results[0]
        for r in results:
            self.assertIs(r, first)


# ── ChannelItem and ChannelResult dataclasses ─────────────────────────────

class TestDataclasses(unittest.TestCase):
    def test_channel_item_defaults(self):
        item = ChannelItem(channel='gmail', item_id='1', sender='a',
                           subject='s', preview='p', timestamp=1234.0)
        self.assertEqual(item.metadata, {})

    def test_channel_result_defaults(self):
        result = ChannelResult(channel='gmail', items=[], count=0,
                               task_id='t', privacy_level='PUBLIC')
        self.assertEqual(result.provenance, {})
        self.assertEqual(result.error, '')

    def test_action_proposal_defaults(self):
        proposal = ActionProposal(
            proposal_id='p', channel='gmail', action='send_message',
            destination='x@y.com', preview='hi', task_id='t',
            approval_id='a', payload={}, timestamp=1234.0)
        self.assertEqual(proposal.status, 'PENDING')


# ── CHANNELS frozenset ────────────────────────────────────────────────────

class TestChannels(unittest.TestCase):
    def test_all_six_channels_defined(self):
        expected = {'telegram', 'gmail', 'calendar', 'whatsapp', 'facebook', 'instagram'}
        self.assertEqual(ChannelAdapter.CHANNELS, expected)

    def test_channels_is_frozenset(self):
        self.assertIsInstance(ChannelAdapter.CHANNELS, frozenset)


# ── Privacy: no credentials stored in proposals ───────────────────────────

class TestNoCredentialLeakage(unittest.TestCase):
    """Verify that proposals never carry raw secrets in their attributes."""

    @patch.object(ChannelAdapter, '_create_approval', return_value='id-safe')
    def test_proposal_stores_no_raw_tokens(self, _mock):
        adapter = ChannelAdapter()
        # Attempt to slip a GitHub token into content (will be sanitized)
        proposal = adapter.propose_send(
            'gmail', 'user@example.com',
            'Here is your token: ghp_abcdefghijklmnopqrstuvwxyz123456')
        self.assertNotIn('ghp_abcde', proposal.preview)
        self.assertNotIn('ghp_abcde', str(proposal.payload))

    @patch.object(ChannelAdapter, '_create_approval', return_value='id-safe2')
    def test_proposal_payload_has_no_raw_password(self, _mock):
        adapter = ChannelAdapter()
        # password= pattern should be sanitized by privacy filter
        proposal = adapter.propose_send(
            'gmail', 'x@y.com', 'password=supersecretvalue123')
        self.assertNotIn('supersecretvalue123', proposal.preview)
        self.assertNotIn('supersecretvalue123', str(proposal.payload))


if __name__ == '__main__':
    unittest.main()
