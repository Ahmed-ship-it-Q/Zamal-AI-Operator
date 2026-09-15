"""Focused regression for durable Telegram-to-WhatsApp proposal approval."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from zamal_core.channel_adapter import ActionProposal, ChannelAdapter, _exec_whatsapp_send
from zamal_core.store import Store
from zamal_core.telegram_bot import TelegramMessageRouter


PROPOSAL_ID = 'ea934693-3b80-45f7-8b6b-1dcd68c61a17'
APPROVAL_ID = 'a8421126-aa1d-4477-9347-49ac76481207'


def proposal():
    return ActionProposal(
        proposal_id=PROPOSAL_ID, approval_id=APPROVAL_ID,
        channel='whatsapp', action='send_message',
        destination='Jamal BD Number', preview='ZAMAL AI AGENT LIVE TEST',
        task_id='', timestamp=time.time(),
        payload={'channel': 'whatsapp', 'destination': 'Jamal BD Number',
                 'raw_destination': 'Jamal BD Number',
                 'content': 'ZAMAL AI AGENT LIVE TEST',
                 'privacy_level': 'PUBLIC', 'send': True},
    )


class DurableProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'operator.sqlite3'
        self.store_factory = lambda _adapter: Store(self.db)

    def adapter(self):
        with patch.object(ChannelAdapter, '_with_store', self.store_factory):
            adapter = ChannelAdapter(persistent=True)
        adapter._with_store = lambda: Store(self.db)
        return adapter

    def test_existing_proposal_survives_restart_with_exact_binding(self):
        first = self.adapter()
        first.register_existing(proposal())
        second = self.adapter()
        restored = second.get_proposal(PROPOSAL_ID)
        self.assertEqual(restored.approval_id, APPROVAL_ID)
        self.assertEqual(restored.destination, 'Jamal BD Number')
        self.assertEqual(restored.preview, 'ZAMAL AI AGENT LIVE TEST')

    def test_exact_telegram_approval_executes_once_and_restart_denies_retry(self):
        adapter = self.adapter()
        adapter.register_existing(proposal())
        with patch('zamal_core.channel_adapter._get_emergency_stop', return_value={}), \
             patch('zamal_core.channel_adapter._get_executor',
                   return_value=MagicMock(return_value={'ok': True, 'verified_in_dom': True})) as factory:
            first = adapter.execute_telegram_approval(
                PROPOSAL_ID, actor='telegram:authorized', update_id=123)
            second = adapter.execute_telegram_approval(
                PROPOSAL_ID, actor='telegram:authorized', update_id=123)
        self.assertTrue(first['ok'])
        self.assertEqual(second['error'], 'already_executed')
        factory.return_value.assert_called_once()
        restarted = self.adapter()
        self.assertEqual(
            restarted.execute_telegram_approval(
                PROPOSAL_ID, actor='telegram:authorized', update_id=124)['error'],
            'already_executed',
        )

    def test_only_precompose_failure_can_resume_same_approval(self):
        adapter = self.adapter()
        failed = proposal()
        failed.status = 'FAILED'
        failed.executor_result = {'ok': False, 'error': 'wrong_target_chat'}
        adapter.register_existing(failed)
        store = Store(self.db)
        try:
            with store.transaction():
                store.db.execute(
                    'UPDATE channel_proposals SET approved_update_id=?,approved_actor=? WHERE proposal_id=?',
                    (123, 'telegram:authorized', PROPOSAL_ID),
                )
        finally:
            store.close()
        with patch('zamal_core.channel_adapter._get_emergency_stop', return_value={}), \
             patch('zamal_core.channel_adapter._get_executor',
                   return_value=MagicMock(return_value={'ok': True})) as factory:
            result = adapter.resume_safe_approved_failure(
                PROPOSAL_ID, actor='telegram:authorized', update_id=123)
            retry = adapter.resume_safe_approved_failure(
                PROPOSAL_ID, actor='telegram:authorized', update_id=123)
        self.assertTrue(result['ok'])
        self.assertEqual(retry['error'], 'failure_not_safe_to_retry')
        factory.return_value.assert_called_once()

    def test_visible_send_reconciliation_never_resends(self):
        adapter = self.adapter()
        failed = proposal()
        failed.status = 'FAILED'
        failed.executor_result = {'ok': False, 'error': 'send_not_visibly_verified'}
        adapter.register_existing(failed)
        with patch('zamal_core.channel_adapter._get_executor') as executor:
            result = adapter.reconcile_visible_delivery(
                PROPOSAL_ID, actor='operator:uat', target_verified=True,
                exact_outbound_count=1)
            retry = adapter.execute_telegram_approval(
                PROPOSAL_ID, actor='telegram:authorized', update_id=123)
        self.assertTrue(result['ok'])
        self.assertTrue(result['result']['reconciled_without_resend'])
        self.assertEqual(retry['error'], 'already_executed')
        executor.assert_not_called()

    def test_parser_accepts_only_exact_bound_command(self):
        adapter = ChannelAdapter()
        p = proposal()
        adapter._proposals[p.proposal_id] = p
        api = MagicMock()
        store = Store(self.db)
        self.addCleanup(store.close)
        router = TelegramMessageRouter(store, api, Path(self.tmp.name) / 'inbox')
        with patch('zamal_core.channel_adapter.get_channel_adapter', return_value=adapter), \
             patch('zamal_core.channel_adapter._get_emergency_stop', return_value={}), \
             patch('zamal_core.channel_adapter._get_executor',
                   return_value=MagicMock(return_value={'ok': True})) as factory:
            result = router._text('authorized', 123, f'APPROVE {PROPOSAL_ID}')
            retry = router._text('authorized', 124, f'APPROVE {PROPOSAL_ID}')
        self.assertTrue(result['ok'])
        self.assertEqual(retry['error'], 'already_executed')
        factory.return_value.assert_called_once()


class NarrowWhatsAppExecutorTests(unittest.TestCase):
    def test_exact_target_and_one_new_outbound_message_required(self):
        replies = [
            (True, '{"ok":true}'),
            (True, '{"ok":true}'),
            (True, '{"ok":true,"target_verified":true,"exact_outbound_before":0}'),
            (True, '{"ok":true}'),
            (True, '{"ok":true,"stage":"send_clicked"}'),
            (True, '{"ok":true,"target_verified":true,"exact_outbound_delta":1,"verified_in_dom":true}'),
        ]
        with patch('zamal_core.ai_browser_transport._exec_js_in_domain', side_effect=replies) as execute, \
             patch('time.sleep'):
            result = _exec_whatsapp_send(proposal().payload)
        self.assertTrue(result['ok'])
        self.assertEqual(result['exact_outbound_delta'], 1)
        scripts = ' '.join(call.args[1] for call in execute.call_args_list)
        self.assertNotIn('document.body', scripts)
        self.assertNotIn('texts:', scripts)
        self.assertIn('[title="Jamal BD Number"]', scripts)
        self.assertIn("querySelectorAll('#main header span')", scripts)
        self.assertIn("querySelectorAll('#main [data-testid=\"selectable-text\"]')", scripts)

    def test_wrong_destination_fails_before_browser(self):
        payload = proposal().payload.copy()
        payload['raw_destination'] = 'Someone Else'
        with patch('zamal_core.ai_browser_transport._exec_js_in_domain') as execute:
            result = _exec_whatsapp_send(payload)
        self.assertEqual(result['error'], 'unauthorized_test_destination')
        execute.assert_not_called()


class StartupBindingTests(unittest.TestCase):
    def test_startup_enables_receiver(self):
        source = Path('scripts/start_control_center.py').read_text()
        self.assertIn('telegram_receiver=True', source)

    def test_receiver_store_is_created_and_used_on_receiver_thread(self):
        from zamal_core.control_center import ControlCenterServer
        ready = threading.Event()

        class Config:
            configured = True

        class Bot:
            def __init__(self, store):
                self.store = store

            def poll(self, **_kwargs):
                self.store.db.execute('SELECT 1').fetchone()
                ready.set()

        server = ControlCenterServer(port=0, telegram_receiver=True)
        with patch('zamal_core.control_center._load_auth_env'), \
             patch('zamal_core.telegram_bot.TelegramConfig', return_value=Config()), \
             patch('zamal_core.telegram_bot.TelegramBot', Bot):
            server._start_telegram_receiver()
            self.assertTrue(ready.wait(2))
            server._stop_telegram_receiver()


if __name__ == '__main__':
    unittest.main()
