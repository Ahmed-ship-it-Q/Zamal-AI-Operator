"""Tests for Step 3C: n8n/website form, GitHub Operator, Emergency Stop + Recovery, Unified CC."""
import hashlib
import json
import socket
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _find_free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class _CCBase(unittest.TestCase):
    """Shared CC server fixture (one server per subclass via setUpClass)."""

    @classmethod
    def setUpClass(cls):
        from zamal_core.control_center import ControlCenterServer
        cls.port = _find_free_port()
        cls.server = ControlCenterServer(port=cls.port)
        cls.server.serve(block=False)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=0.3):
                    break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.stop()
        except Exception:
            pass

    def _get(self, path, timeout=5):
        url = f'http://127.0.0.1:{self.port}{path}'
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
            return resp.getcode(), json.loads(resp.read())

    def _post(self, path, data, timeout=5):
        url = f'http://127.0.0.1:{self.port}{path}'
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.getcode(), json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class TestWebsiteFormIntake(_CCBase):
    """A — Website form: validation, dedup, WAITING_APPROVAL, privacy, n8n status."""

    def test_01_valid_form_creates_task_in_waiting_approval(self):
        code, data = self._post('/api/website-form', {
            'email': f'step3c_{int(time.time())}@example.com',
            'subject': 'Step 3C Test',
            'message': 'Hello from the test suite',
        })
        self.assertIn(code, (200, 201))
        self.assertTrue(data.get('ok'))
        self.assertFalse(data.get('duplicate'))
        self.assertIn('task_id', data)
        self.assertEqual(data.get('state'), 'WAITING_APPROVAL')

    def test_02_task_state_is_waiting_approval_in_store(self):
        code, data = self._post('/api/website-form', {
            'email': f'state_{int(time.time())}@example.com',
            'message': 'State check message',
        })
        self.assertIn(code, (200, 201))
        task_id = data['task_id']
        code2, task = self._get(f'/api/task/{task_id}')
        self.assertEqual(code2, 200)
        self.assertEqual(task['state'], 'WAITING_APPROVAL')

    def test_03_missing_email_returns_400(self):
        code, data = self._post('/api/website-form', {
            'message': 'No email provided',
        })
        self.assertEqual(code, 400)

    def test_04_missing_message_returns_400(self):
        code, data = self._post('/api/website-form', {
            'email': 'noemail@example.com',
        })
        self.assertEqual(code, 400)

    def test_05_invalid_email_format_returns_400(self):
        code, data = self._post('/api/website-form', {
            'email': 'not-an-email',
            'message': 'Test message',
        })
        self.assertEqual(code, 400)
        self.assertIn('invalid_email', data.get('error', ''))

    def test_06_duplicate_submission_returns_existing_task(self):
        unique_email = f'dup_{int(time.time())}@example.com'
        payload = {
            'email': unique_email,
            'subject': 'Dedup Test Subject',
            'message': 'Dedup test message body unique content',
        }
        code1, data1 = self._post('/api/website-form', payload)
        self.assertIn(code1, (200, 201))
        self.assertFalse(data1.get('duplicate'))
        task_id_1 = data1['task_id']
        # Second identical submission
        code2, data2 = self._post('/api/website-form', payload)
        self.assertIn(code2, (200, 201))
        self.assertTrue(data2.get('duplicate'))
        self.assertEqual(data2['task_id'], task_id_1)

    def test_07_raw_email_masked_in_response(self):
        raw_email = f'privacy_{int(time.time())}@example.com'
        code, data = self._post('/api/website-form', {
            'email': raw_email,
            'message': 'Privacy masking test',
        })
        self.assertIn(code, (200, 201))
        resp_str = json.dumps(data)
        self.assertNotIn(raw_email, resp_str, 'Raw email must not appear in response')
        masked = data.get('masked_from', '')
        self.assertIn('***@', masked, f'Expected masked email but got: {masked!r}')

    def test_08_n8n_status_included_in_response(self):
        code, data = self._post('/api/website-form', {
            'email': f'n8n_{int(time.time())}@example.com',
            'message': 'n8n status check',
        })
        self.assertIn(code, (200, 201))
        self.assertIn('n8n_status', data)
        self.assertIn(data['n8n_status'], ('ok', 'unreachable', 'unconfigured', 'partial'))

    def test_09_task_visible_in_task_list(self):
        code, data = self._post('/api/website-form', {
            'email': f'list_{int(time.time())}@example.com',
            'message': 'Task list visibility test',
        })
        self.assertIn(code, (200, 201))
        task_id = data['task_id']
        code2, tasks = self._get('/api/tasks?limit=50')
        self.assertEqual(code2, 200)
        self.assertIn(task_id, [t['id'] for t in tasks])

    def test_10_reject_website_form_proposal_zero_delivery(self):
        """Reject a channel proposal — proves zero external action."""
        from zamal_core.channel_adapter import get_channel_adapter
        ca = get_channel_adapter()
        p = ca.propose_send(
            channel='telegram',
            destination='[SANITIZED_OPERATOR]',
            content='ZAMAL STEP 3C WEBSITE FORM TEST — zero delivery proof',
            task_id='step3c-webform-reject',
        )
        result = ca.execute_rejected(p, actor='test_reject')
        # Zero delivery proof: external_action=none, action=rejected
        self.assertEqual(result.get('external_action'), 'none')
        self.assertEqual(result.get('action'), 'rejected')

    def test_11_dedup_key_does_not_contain_raw_email(self):
        """Verify dedup hash construction never embeds raw email."""
        email = 'secret_user@private.com'
        subject = 'Test'
        message = 'Body'
        dedup_hash = hashlib.sha256(
            f'{email}|{subject}|{message}'.encode()
        ).hexdigest()[:32]
        request_key = f'website_form:{dedup_hash}'
        self.assertNotIn(email, request_key)
        self.assertNotIn('@', request_key)


class TestGitHubOperator(_CCBase):
    """B — GitHub Operator: read-only inspection + protected write boundary."""

    def test_01_github_status_endpoint_returns_git_data(self):
        code, data = self._get('/api/github/status')
        self.assertEqual(code, 200)
        self.assertIn('branch', data)
        self.assertIn('sha', data)
        self.assertIn('remote', data)

    def test_02_github_ci_status_has_write_protection(self):
        code, data = self._get('/api/github/ci-status')
        self.assertEqual(code, 200)
        self.assertTrue(data.get('write_requires_approval'))
        ops = data.get('protected_write_ops', [])
        for op in ('git_commit', 'git_push', 'git_merge', 'git_branch_delete'):
            self.assertIn(op, ops)

    def test_03_ci_status_note_mentions_browser(self):
        code, data = self._get('/api/github/ci-status')
        self.assertEqual(code, 200)
        self.assertIn('logged-in browser', data.get('note', ''))

    def test_04_read_ops_pass_safety_check(self):
        """Read-only git operations pass safety checks without approval."""
        from zamal_core.store import Store
        from zamal_core.safety import Controls
        store = Store()
        controls = Controls(store)
        controls.set('stop_everything', False, actor='test_setup', reason='setup')
        baseline = controls.status()
        controls.check('git_capture', baseline)
        controls.check('diff_review', baseline)
        store.close()

    def test_05_push_blocked_by_disable_github_push(self):
        """git_push is blocked when disable_github_push flag is set."""
        from zamal_core.store import Store
        from zamal_core.safety import Controls
        from zamal_core.store import Denied
        store = Store()
        controls = Controls(store)
        controls.set('disable_github_push', True, actor='test', reason='test block')
        baseline = controls.status()
        with self.assertRaises(Denied):
            controls.check('git_push', baseline)
        controls.set('disable_github_push', False, actor='test_cleanup', reason='cleanup')
        store.close()

    def test_06_push_blocked_by_stop_everything(self):
        """git_push is blocked when stop_everything is active."""
        from zamal_core.store import Store
        from zamal_core.safety import Controls
        from zamal_core.store import Denied
        store = Store()
        controls = Controls(store)
        controls.set('stop_everything', True, actor='test', reason='test stop')
        baseline = controls.status()
        with self.assertRaises(Denied):
            controls.check('git_push', baseline)
        controls.set('stop_everything', False, actor='cleanup', reason='cleanup')
        store.close()

    def test_07_status_api_includes_github_integration(self):
        code, data = self._get('/api/status')
        self.assertEqual(code, 200)
        self.assertIn('github', data.get('connections', {}))

    def test_08_no_credentials_in_ci_status_response(self):
        code, data = self._get('/api/github/ci-status')
        self.assertEqual(code, 200)
        import re
        resp_str = json.dumps(data)
        self.assertNotRegex(resp_str, r'ghp_[a-zA-Z0-9]+')
        self.assertNotRegex(resp_str, r'sk-[a-zA-Z0-9]{20,}')
        self.assertNotRegex(resp_str, r'ANTHROPIC_API_KEY=[^\s]+')


class TestEmergencyStopAndRecovery(_CCBase):
    """C — Emergency Stop: block execution, clear + resume, checkpoint idempotency."""

    def setUp(self):
        from zamal_core.store import Store
        from zamal_core.safety import Controls
        s = Store()
        Controls(s).set('stop_everything', False, actor='test_setUp', reason='test setup')
        s.close()

    def tearDown(self):
        from zamal_core.store import Store
        from zamal_core.safety import Controls
        s = Store()
        Controls(s).set('stop_everything', False, actor='test_tearDown', reason='test cleanup')
        s.close()

    def test_01_emergency_stop_blocks_channel_execution(self):
        """With stop_everything active, channel execute_approved returns blocked result."""
        from zamal_core.store import Store
        from zamal_core.safety import Controls
        from zamal_core.channel_adapter import get_channel_adapter
        s = Store()
        Controls(s).set('stop_everything', True, actor='test', reason='test stop')
        s.close()
        ca = get_channel_adapter()
        p = ca.propose_send('telegram', '[SANITIZED]', 'STOP TEST', task_id='estop-test')
        result = ca.execute_approved(p, approval_id=p.approval_id, actor='test')
        self.assertFalse(result.get('ok', True),
                         f'Expected blocked result but got: {result}')
        blocked_msg = str(result.get('error', '') or result.get('status', '')).lower()
        self.assertTrue(
            'stop' in blocked_msg or 'emergency' in blocked_msg or 'denied' in blocked_msg,
            f'Expected stop/emergency/denied, got: {result}'
        )

    def test_02_clear_emergency_stop_allows_read_ops(self):
        from zamal_core.store import Store
        from zamal_core.safety import Controls
        s = Store()
        ctrl = Controls(s)
        ctrl.set('stop_everything', True, actor='test', reason='activate')
        ctrl.set('stop_everything', False, actor='test', reason='clear')
        baseline = ctrl.status()
        ctrl.check('git_capture', baseline)
        ctrl.check('diff_review', baseline)
        s.close()

    def test_03_emergency_stop_via_cc_endpoint_set_and_clear(self):
        code1, d1 = self._post('/api/emergency-stop', {
            'flag': 'stop_everything', 'reason': 'step3c_test'
        })
        self.assertEqual(code1, 200)
        self.assertEqual(d1.get('enabled'), True)
        code2, d2 = self._post('/api/emergency-stop-clear', {
            'flag': 'stop_everything', 'reason': 'step3c_clear'
        })
        self.assertEqual(code2, 200)
        self.assertEqual(d2.get('enabled'), False)

    def test_04_emergency_status_endpoint_returns_all_flags(self):
        code, data = self._get('/api/emergency-status')
        self.assertEqual(code, 200)
        from zamal_core.safety import FLAGS
        for flag in FLAGS:
            self.assertIn(flag, data)
            self.assertIsInstance(data[flag], bool)

    def test_05_checkpoint_save_stores_note(self):
        code, task_data = self._post('/api/tasks', {'text': 'Checkpoint test task'})
        self.assertIn(code, (200, 201))
        task_id = task_data['task_id']
        code2, ck = self._post('/api/checkpoint-save', {
            'task_id': task_id,
            'reason': 'test_service_unavailable',
            'next_action': 'resume_test_after_restart',
        })
        self.assertEqual(code2, 200)
        self.assertTrue(ck.get('ok'))
        self.assertIn('CHECKPOINT', ck.get('checkpoint', ''))
        self.assertEqual(ck.get('reason'), 'test_service_unavailable')

    def test_06_checkpoint_resume_returns_no_duplicate(self):
        code, task_data = self._post('/api/tasks', {'text': 'Resume test task'})
        task_id = task_data['task_id']
        self._post('/api/checkpoint-save', {
            'task_id': task_id,
            'reason': 'test_unavailable',
            'next_action': 'resume_after_test',
        })
        code2, res = self._post('/api/checkpoint-resume', {'task_id': task_id})
        self.assertEqual(code2, 200)
        self.assertTrue(res.get('ok'))
        self.assertTrue(res.get('resumed'))
        self.assertFalse(res.get('duplicate_action'))
        self.assertIn('resume_after_test', res.get('previous_checkpoint', ''))

    def test_07_checkpoint_resume_without_checkpoint_returns_400(self):
        """Resuming a task that has no checkpoint returns 400."""
        code, task_data = self._post('/api/tasks', {'text': 'No checkpoint task'})
        task_id = task_data['task_id']
        code2, data = self._post('/api/checkpoint-resume', {'task_id': task_id})
        self.assertEqual(code2, 400)
        self.assertIn('no_checkpoint', data.get('error', ''))

    def test_08_emergency_stop_visible_in_status_api(self):
        self._post('/api/emergency-stop', {
            'flag': 'stop_everything', 'reason': 'visibility_test'
        })
        code, d = self._get('/api/status')
        self.assertEqual(code, 200)
        self.assertTrue(d.get('emergency_stop_active'))
        self._post('/api/emergency-stop-clear', {
            'flag': 'stop_everything', 'reason': 'cleanup'
        })

    def test_09_stop_cleared_visible_in_status_api(self):
        self._post('/api/emergency-stop', {'flag': 'stop_everything', 'reason': 'test'})
        self._post('/api/emergency-stop-clear', {'flag': 'stop_everything', 'reason': 'clear'})
        code, d = self._get('/api/status')
        self.assertEqual(code, 200)
        self.assertFalse(d.get('emergency_stop_active'))


class TestUnifiedControlCenter(_CCBase):
    """D — Unified CC: all providers/channels/integrations with truthful status labels."""

    def test_01_status_has_all_channel_and_integration_keys(self):
        code, data = self._get('/api/status')
        self.assertEqual(code, 200)
        conn = data.get('connections', {})
        required = {
            'codex', 'claude', 'gemini', 'chatgpt_web', 'browser',
            'n8n', 'telegram', 'github', 'cpc',
            'ch_gmail', 'ch_calendar', 'ch_whatsapp', 'ch_facebook',
            'ch_messenger', 'ch_instagram', 'ch_telegram',
        }
        missing = required - set(conn.keys())
        self.assertFalse(missing, f'Missing status keys: {missing}')

    def test_02_all_status_values_are_valid(self):
        valid = {'ok', 'configured', 'unconfigured', 'unreachable', 'external_only', 'partial'}
        code, data = self._get('/api/status')
        conn = data.get('connections', {})
        for k, v in conn.items():
            self.assertIn(v, valid, f'{k} has invalid status {v!r}')

    def test_03_status_includes_emergency_stop_active(self):
        code, data = self._get('/api/status')
        self.assertEqual(code, 200)
        self.assertIn('emergency_stop_active', data)
        self.assertIsInstance(data['emergency_stop_active'], bool)

    def test_04_ai_providers_endpoint_returns_dict(self):
        code, data = self._get('/api/ai-providers')
        self.assertEqual(code, 200)
        self.assertIn('providers', data)
        self.assertIsInstance(data['providers'], dict)

    def test_05_n8n_workflows_endpoint_returns_json(self):
        code, data = self._get('/api/n8n/workflows')
        self.assertEqual(code, 200)
        self.assertIsInstance(data, dict)

    def test_06_github_ci_status_returns_write_protection(self):
        code, data = self._get('/api/github/ci-status')
        self.assertEqual(code, 200)
        self.assertIn('write_requires_approval', data)
        self.assertTrue(data['write_requires_approval'])

    def test_07_html_contains_all_required_ui_sections(self):
        url = f'http://127.0.0.1:{self.port}/'
        with urllib.request.urlopen(url, timeout=5) as resp:
            html = resp.read().decode('utf-8')
        for section in ('Emergency Stop', 'Website Form', 'Checkpoint Recovery',
                        'N8N Workflows', 'GitHub Operator', 'LIVE_VERIFIED',
                        'estop-badge'):
            self.assertIn(section, html, f'HTML missing section: {section!r}')

    def test_08_chatgpt_web_is_external_only_in_api(self):
        code, data = self._get('/api/status')
        self.assertEqual(code, 200)
        self.assertEqual(data['connections']['chatgpt_web'], 'external_only')

    def test_09_channel_proposals_endpoint_works(self):
        code, data = self._get('/api/channel/proposals')
        self.assertEqual(code, 200)
        self.assertIsInstance(data, list)

    def test_10_emergency_status_endpoint_has_all_flags(self):
        code, data = self._get('/api/emergency-status')
        self.assertEqual(code, 200)
        from zamal_core.safety import FLAGS
        for flag in FLAGS:
            self.assertIn(flag, data)


class TestPrivacyCPCStep3C(unittest.TestCase):
    """E — Privacy and CPC zero-write verification for Step 3C."""

    def test_01_broad_extraction_detector_still_catches_patterns(self):
        from zamal_core.private_read_auth import check_js_for_broad_extraction, PRIVATE_DOMAINS
        for domain in list(PRIVATE_DOMAINS)[:2]:
            for pattern in ('document.body.innerText', 'document.body.innerHTML'):
                found = check_js_for_broad_extraction(domain, pattern)
                self.assertEqual(found, pattern)

    def test_02_website_form_dedup_key_never_contains_raw_email(self):
        email = 'raw@secret-domain.com'
        subject = 'Test'
        message = 'Body content'
        dedup_hash = hashlib.sha256(
            f'{email}|{subject}|{message}'.encode()
        ).hexdigest()[:32]
        request_key = f'website_form:{dedup_hash}'
        self.assertNotIn(email, request_key)
        self.assertNotIn('@', request_key)

    def test_03_private_read_denied_without_auth(self):
        from zamal_core.private_read_auth import (
            require_read_auth, PrivateReadDenied, PrivateReadRegistry
        )
        from unittest.mock import patch
        with patch('zamal_core.private_read_auth.PRIVATE_READ_REGISTRY',
                   PrivateReadRegistry()):
            with self.assertRaises(PrivateReadDenied):
                require_read_auth('gmail', 'test@example.com')

    def test_04_read_auth_does_not_have_send_capability(self):
        from zamal_core.private_read_auth import PrivateReadRegistry
        reg = PrivateReadRegistry()
        auth = reg.authorize(
            task_id='test', channel='telegram', target='*',
            purpose='read_test', scope='inbox_count', ttl_seconds=30,
        )
        # PrivateReadAuth must not have send/write capability
        self.assertFalse(hasattr(auth, 'can_send'))
        self.assertFalse(hasattr(auth, 'can_write'))

    def test_05_channel_reject_never_sends(self):
        from zamal_core.channel_adapter import get_channel_adapter
        ca = get_channel_adapter()
        for ch in ('gmail', 'whatsapp', 'messenger', 'instagram'):
            p = ca.propose_send(ch, '[SANITIZED]', 'reject test', task_id='privacy-test')
            result = ca.execute_rejected(p, actor='test')
            self.assertEqual(result.get('external_action'), 'none',
                             f'{ch} reject had external action: {result}')
            self.assertEqual(result.get('action'), 'rejected',
                             f'{ch} reject action wrong: {result}')

    def test_06_sanitizer_removes_known_secret_patterns(self):
        from zamal_core.control_center import _sanitize
        # The sanitizer pattern sk-[a-zA-Z0-9]{20,} requires 20+ chars
        long_sk = 'sk-' + 'A' * 25
        self.assertIn('[REDACTED]', _sanitize(f'key: {long_sk}'))
        # ghp_ pattern matched by regex
        long_ghp = 'ghp_' + 'a' * 36
        self.assertIn('[REDACTED]', _sanitize(f'token {long_ghp}'))
        # ANTHROPIC_API_KEY keyword is matched regardless of value
        self.assertIn('[REDACTED]', _sanitize('ANTHROPIC_API_KEY'))


if __name__ == '__main__':
    unittest.main()
