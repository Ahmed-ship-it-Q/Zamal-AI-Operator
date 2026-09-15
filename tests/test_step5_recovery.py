"""Real persistence/HTTP-handler recovery proof; all delivery and browser I/O stubbed."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from zamal_core import checkpoint, control_center as cc
from zamal_core.safety import Controls
from zamal_core.store import Store, Conflict, Denied, Gate


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'fixture.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.task = self.store.create_task('Synthetic recovery', 'local', 'operator', 'recovery-fixture')
        self.tid = self.task['id']

    def save(self):
        return checkpoint.save(self.store, self.tid, 'session_expired', 'resume_after:session_expired', 'claude')

    def test_duplicate_and_restart_claim_once(self):
        first, claim = self.save()
        self.assertTrue(claim)
        with_store = Store(self.path)
        try:
            second, claim = checkpoint.save(with_store, self.tid, 'session_expired', 'resume_after:session_expired', 'claude')
        finally:
            with_store.close()
        self.assertEqual(first, second)
        self.assertFalse(claim)
        self.assertEqual(len(self.store.task(self.tid)['context']['evidence']), 1)

    def test_concurrent_saves_claim_once(self):
        def save(_):
            store = Store(self.path)
            try:
                return checkpoint.save(store, self.tid, 'session_expired', 'resume_after:session_expired', 'claude')[1]
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=3) as executor:
            self.assertEqual(sum(executor.map(save, range(3))), 1)

    def test_cannot_replace_pending_session(self):
        self.save()
        with self.assertRaises(Conflict):
            checkpoint.save(self.store, self.tid, 'session_expired', 'resume_after:session_expired', None)

    def test_expired_captcha_otp_pause_then_same_task_resume(self):
        self.save()
        for blocker in ('expired_login', 'captcha', 'otp'):
            before = self.store.task(self.tid)
            result = checkpoint.resume(self.store, self.tid, health_check=Mock(return_value=False))
            self.assertFalse(result['resumed'], blocker)
            self.assertEqual(result['status'], 'SESSION_REQUIRED')
            self.assertEqual(before, self.store.task(self.tid))
        health = Mock(return_value=True)
        result = checkpoint.resume(self.store, self.tid, health_check=health)
        health.assert_called_once_with('claude')
        self.assertTrue(result['resumed'])
        self.assertEqual(result['task_id'], self.tid)
        self.assertEqual(len(self.store.tasks()), 1)
        self.assertEqual(self.store.task(self.tid)['actions'], [])
        with self.assertRaises(Conflict):
            checkpoint.resume(self.store, self.tid, health_check=health)

    def test_pending_checkpoint_blocks_action_then_executes_once(self):
        task = self.task
        for state in ('UNDERSTOOD', 'PLANNED'):
            task = self.store.transition(self.tid, state, task['revision'])
        effect = Mock(spec=lambda payload: None, return_value={'ok': True})
        gate = Gate(self.store, handlers={'read': effect})
        action = gate.propose(self.tid, 'read', {'fixture': True}, 'fixture-action')
        self.save()
        with self.assertRaises(Denied):
            gate.execute(action['id'])
        effect.assert_not_called()
        checkpoint.resume(self.store, self.tid, health_check=Mock(return_value=True))
        gate.execute(action['id'])
        effect.assert_called_once()
        with self.assertRaises(Denied):
            gate.execute(action['id'])

    def test_probe_exception_remains_checkpointed(self):
        self.save()
        result = checkpoint.resume(self.store, self.tid, health_check=Mock(side_effect=RuntimeError))
        self.assertFalse(result['ok'])
        self.assertTrue(self.store.task(self.tid)['context']['checkpoint']['pending'])

    def test_stop_blocks_resume_clear_allows_retry(self):
        self.save()
        controls = Controls(self.store)
        controls.set('stop_everything', True, actor='fixture', reason='test')
        health = Mock(return_value=True)
        with self.assertRaises(Denied):
            checkpoint.resume(self.store, self.tid, health_check=health)
        health.assert_not_called()
        controls.set('stop_everything', False, actor='fixture', reason='test')
        self.assertTrue(checkpoint.resume(self.store, self.tid, health_check=health)['resumed'])

    def test_stop_clear_during_probe_still_blocks(self):
        self.save()
        def health(_):
            controls = Controls(self.store)
            controls.set('stop_everything', True, actor='fixture', reason='test')
            controls.set('stop_everything', False, actor='fixture', reason='test')
            return True
        with self.assertRaises(Denied):
            checkpoint.resume(self.store, self.tid, health_check=health)
        self.assertTrue(self.store.task(self.tid)['context']['checkpoint']['pending'])

    def test_handler_dispatches_one_consolidated_notice(self):
        handler = object.__new__(cc._Handler)
        handler._send_json = Mock()
        handler._error = Mock()
        def run_thread(**kwargs):
            thread = Mock()
            thread.start.side_effect = lambda: kwargs['target'](*kwargs['args'])
            return thread
        with patch.object(cc, '_get_store', side_effect=lambda: Store(self.path)), \
             patch.object(cc, '_send_telegram_notify') as notify, \
             patch.object(cc.threading, 'Thread', side_effect=run_thread):
            for _ in range(3):
                handler._handle_checkpoint_save({'task_id': self.tid, 'reason': 'session_expired', 'required_session': 'claude'})
            notify.assert_called_once()
            self.assertIn('ACTION_REQUIRED', notify.call_args.args[1])
            handler._error.assert_not_called()

    def test_unspecified_session_cannot_resume(self):
        self.assertFalse(checkpoint.session_ready('unspecified'))

    def test_narrow_probe_blocks_challenge_and_requires_positive_login(self):
        with patch('zamal_core.ai_browser_transport._exec_js_in_domain') as probe:
            probe.return_value = (True, '{"blocked":true}')
            self.assertFalse(checkpoint.session_ready('claude'))
            self.assertEqual(probe.call_count, 1)
            self.assertNotIn('document.body', probe.call_args.args[1])
            probe.side_effect = [(True, '{"blocked":false}'), (True, '{"logged_in":false}')]
            self.assertFalse(checkpoint.session_ready('claude'))
            probe.side_effect = [(True, '{"blocked":false}'), (True, '{"logged_in":true,"has_input":true}')]
            self.assertTrue(checkpoint.session_ready('claude'))

    def test_ui_does_not_claim_blocked_resume_succeeded(self):
        source = Path(cc.__file__).read_text()
        self.assertIn("if(!d.resumed){status.textContent=d.status||'ACTION_REQUIRED';return;}", source)

    def test_http_session_recovery_preserves_task_and_notice_count(self):
        import json
        import threading
        import urllib.request
        delivered = threading.Event()
        server = cc.ControlCenterServer(port=0)
        # Bind an ephemeral local HTTP fixture and exercise the actual endpoints.
        server.serve(block=False)
        self.addCleanup(server.stop)
        port = server._server.server_address[1]
        def post(path, body):
            request = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.load(response)
        with patch.object(cc, '_get_store', side_effect=lambda: Store(self.path)), \
             patch.object(cc, '_send_telegram_notify') as notice, \
             patch('zamal_core.ai_browser_transport._exec_js_in_domain') as probe:
            body = {'task_id': self.tid, 'reason': 'session_expired', 'required_session': 'claude'}
            notice.side_effect = lambda *args: delivered.set()
            post('/api/checkpoint-save', body)
            self.assertTrue(delivered.wait(1))
            post('/api/checkpoint-save', body)
            probe.return_value = (True, '{"blocked":true}')
            blocked = post('/api/checkpoint-resume', {'task_id': self.tid})
            self.assertFalse(blocked['resumed'])
            self.assertEqual(blocked['status'], 'SESSION_REQUIRED')
            probe.side_effect = [(True, '{"blocked":false}'), (True, '{"logged_in":true,"has_input":true}')]
            resumed = post('/api/checkpoint-resume', {'task_id': self.tid})
            self.assertTrue(resumed['resumed'])
            self.assertEqual(resumed['task_id'], self.tid)
            notice.assert_called_once()
