"""Mac Safety Operations verification.

Covers the seven required scenarios:
1. Allowed safe action executes without approval (service_status)
2. Private-path denial — paths outside ALLOWED_FOLDERS blocked at policy layer
3. Approval-required action stops at PENDING (service_start)
4. Emergency Stop — stop_everything=True blocks all Mac ops
5. Blocked execution during stop — queued action cannot execute
6. Resume — stop_everything=False, actions execute again
7. Evidence logging — control_audit records every stop/resume with actor+reason

All subprocess, socket and process calls mocked; no real services started.
No CPC writes, commit, push, or external actions performed.
"""
import socket
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from zamal_core.store import Store, Denied, uid
from zamal_core.safety import Controls
from zamal_core.mac_worker import (
    FolderPolicy, MacWorker, ALLOWED_FOLDERS, APPROVED_SERVICES,
)


def _planned_task(store, project='operator'):
    t = store.create_task('Mac safety fixture', 'mac_ops', project, uid())
    t = store.transition(t['id'], 'UNDERSTOOD', t['revision'])
    t = store.transition(t['id'], 'PLANNED', t['revision'])
    return t


def _mock_socket_refused():
    err = OSError('Connection refused')
    m = MagicMock(side_effect=err)
    return m


class MacSafetyFixture(unittest.TestCase):
    """Base fixture: isolated Store + MacWorker with mocked subprocesses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-mac-safety-')
        root = Path(self.tmp.name)
        self.op = root / 'operator'
        self.cpc = root / 'cpc'
        self.op.mkdir()
        self.cpc.mkdir()
        self.s = Store(self.op / 'state' / 'operator.sqlite3')
        self.w = MacWorker(self.s, operator=self.op, cpc=self.cpc)
        self.g = self.w.gate()
        self.controls = Controls(self.s)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def task(self, project='operator'):
        return _planned_task(self.s, project)

    def propose(self, kind, payload):
        t = self.task()
        return self.g.propose(t['id'], kind, payload, uid())


# ─── 1. Allowed safe action ───────────────────────────────────────────────────

class AllowedSafeActionTests(MacSafetyFixture):
    """service_status is SAFE: no approval required; executes immediately."""

    @patch('socket.create_connection', side_effect=OSError('refused'))
    def test_service_status_no_approval(self, _sock):
        action = self.propose('service_status', {'service': 'n8n'})
        # SAFE actions produce no approval row
        self.assertIsNone(action['approval'])
        result = self.g.execute(action['id'])
        self.assertEqual(result['service'], 'n8n')
        self.assertIn('running', result)
        self.assertEqual(self.s.action(action['id'])['status'], 'SUCCEEDED')

    @patch('socket.create_connection', side_effect=OSError('refused'))
    def test_service_status_cpc_inspector_no_approval(self, _sock):
        action = self.propose('service_status', {'service': 'cpc_inspector'})
        self.assertIsNone(action['approval'])
        result = self.g.execute(action['id'])
        self.assertEqual(result['service'], 'cpc_inspector')

    def test_terminal_run_no_approval(self):
        argv = ['/usr/bin/python3', '--version']
        fake = MagicMock(return_value={'exit_code': 0, 'output': 'Python 3.x'})
        with patch.object(self.w.mac_runner, 'run', fake):
            action = self.propose('terminal_run', {'command': 'python_version'})
            self.assertIsNone(action['approval'])
            result = self.g.execute(action['id'])
            self.assertTrue(result['passed'])

    def test_permission_check_no_approval(self):
        with patch('ctypes.cdll.LoadLibrary') as mock_lib:
            mock_lib.return_value.AXIsProcessTrusted.return_value = 0
            action = self.propose('permission_check', {'permission': 'accessibility'})
            self.assertIsNone(action['approval'])
            result = self.g.execute(action['id'])
            self.assertIn(result['status'], ('authorized', 'not_determined', 'unavailable'))


# ─── 2. Private-path denial ───────────────────────────────────────────────────

class PrivatePathDenialTests(MacSafetyFixture):
    """Paths outside ALLOWED_FOLDERS are rejected before any execution."""

    def test_home_ssh_outside_allowed(self):
        policy = FolderPolicy()
        with self.assertRaises(Denied):
            policy.resolve('operator_root', '../../../.ssh/id_rsa')

    def test_desktop_outside_allowed(self):
        policy = FolderPolicy()
        with self.assertRaises(Denied):
            policy.resolve('operator_root', '../Desktop/secret.txt')

    def test_home_documents_outside_allowed(self):
        policy = FolderPolicy()
        with self.assertRaises(Denied):
            policy.resolve('operator_root', '../Documents/private.pdf')

    def test_unknown_folder_key_denied(self):
        policy = FolderPolicy()
        with self.assertRaises(Denied):
            policy.resolve('home', 'file.txt')

    def test_etc_passwd_denied(self):
        policy = FolderPolicy()
        with self.assertRaises(Denied):
            policy.resolve('operator_root', '../../../etc/passwd')

    def test_state_dir_blocked_component(self):
        """'state' is in _BLOCKED_COMPONENTS — prevents DB access via path."""
        policy = FolderPolicy()
        with self.assertRaises(Denied):
            policy.resolve('operator_root', 'state/operator.sqlite3')

    def test_sqlite_extension_blocked(self):
        policy = FolderPolicy()
        with self.assertRaises(Denied):
            policy.resolve('operator_root', 'data.sqlite3')

    def test_dotenv_blocked(self):
        policy = FolderPolicy()
        with self.assertRaises(Denied):
            policy.resolve('operator_root', '.env')

    def test_ssh_folder_blocked_component(self):
        policy = FolderPolicy()
        with self.assertRaises(Denied):
            policy.resolve('operator_root', '.ssh/id_rsa')

    def test_symlink_denied(self):
        import os
        allowed = {'op': self.op}
        policy = FolderPolicy(allowed=allowed)
        link = self.op / 'link.txt'
        link.symlink_to(self.op / 'real.txt')
        (self.op / 'real.txt').write_text('data')
        with self.assertRaises(Denied):
            policy.resolve('op', 'link.txt')

    def test_cpc_write_denied_by_policy(self):
        policy = FolderPolicy()
        with self.assertRaises(Denied):
            policy.check_writable('cpc_root')

    def test_null_byte_path_denied(self):
        policy = FolderPolicy()
        with self.assertRaises(Denied):
            policy.resolve('operator_root', 'file\x00name')

    def test_credential_name_denied(self):
        policy = FolderPolicy()
        with self.assertRaises(Denied):
            policy.resolve('operator_root', 'token.txt')


# ─── 3. Approval-required action ─────────────────────────────────────────────

class ApprovalRequiredTests(MacSafetyFixture):
    """service_start is PROTECTED: stops at PENDING; cannot execute without approval."""

    def test_service_start_requires_approval(self):
        action = self.propose('service_start', {'service': 'n8n'})
        self.assertIsNotNone(action['approval'])
        self.assertEqual(action['approval']['status'], 'PENDING')

    def test_service_start_cannot_execute_without_approval(self):
        action = self.propose('service_start', {'service': 'n8n'})
        with self.assertRaises(Denied):
            self.g.execute(action['id'])
        self.assertEqual(self.s.action(action['id'])['status'], 'PENDING')
        self.assertIsNone(self.s.action(action['id'])['result'])

    def test_service_stop_requires_approval(self):
        action = self.propose('service_stop', {'service': 'n8n'})
        self.assertIsNotNone(action['approval'])
        self.assertEqual(action['approval']['status'], 'PENDING')

    def test_unapproved_service_rejected_at_validate(self):
        # validate() is called at execute time (not propose time) for non-ops/biz kinds.
        # Directly call the worker's validate to confirm the service deny-by-default.
        with self.assertRaises(Denied):
            self.w.validate('service_start', {'service': 'unknown_service'}, 'operator')

    def test_approval_reject_cancels_action(self):
        action = self.propose('service_start', {'service': 'n8n'})
        self.s.decide(action['approval']['id'], 'REJECT', actor='owner')
        with self.assertRaises(Denied):
            self.g.execute(action['id'])
        self.assertEqual(self.s.action(action['id'])['status'], 'CANCELLED')

    @patch('socket.create_connection', side_effect=OSError('refused'))
    @patch('subprocess.Popen')
    def test_approved_service_start_executes(self, mock_popen, _sock):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_popen.return_value = mock_proc
        action = self.propose('service_start', {'service': 'n8n'})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner')
        result = self.g.execute(action['id'])
        self.assertEqual(result['service'], 'n8n')
        self.assertEqual(self.s.action(action['id'])['status'], 'SUCCEEDED')


# ─── 4 & 5. Emergency Stop — blocks execution ─────────────────────────────────

class EmergencyStopTests(MacSafetyFixture):
    """stop_everything=True blocks all Mac ops dispatch."""

    @patch('socket.create_connection', side_effect=OSError('refused'))
    def test_stop_blocks_safe_action(self, _sock):
        action = self.propose('service_status', {'service': 'n8n'})
        self.controls.set('stop_everything', True, actor='owner', reason='test stop')
        with self.assertRaises(Denied):
            self.g.execute(action['id'])

    def test_stop_blocks_protected_action(self):
        action = self.propose('service_start', {'service': 'n8n'})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner')
        self.controls.set('stop_everything', True, actor='owner', reason='test stop')
        with self.assertRaises(Denied):
            self.g.execute(action['id'])

    def test_stop_blocks_terminal_run(self):
        action = self.propose('terminal_run', {'command': 'python_version'})
        self.controls.set('stop_everything', True, actor='owner', reason='test stop')
        with self.assertRaises(Denied):
            self.g.execute(action['id'])

    def test_stop_blocks_permission_check(self):
        action = self.propose('permission_check', {'permission': 'accessibility'})
        self.controls.set('stop_everything', True, actor='owner', reason='test stop')
        with self.assertRaises(Denied):
            self.g.execute(action['id'])

    def test_action_remains_pending_after_stop_denial(self):
        """Blocked execution must not corrupt action state."""
        action = self.propose('service_status', {'service': 'n8n'})
        self.controls.set('stop_everything', True, actor='owner', reason='test stop')
        try:
            self.g.execute(action['id'])
        except Denied:
            pass
        self.assertEqual(self.s.action(action['id'])['status'], 'PENDING')

    def test_stop_epoch_latches_even_after_resume(self):
        """Epoch increments when stopped; re-enabling does not roll back epoch."""
        before = self.controls.status()['stop_everything_epoch']
        self.controls.set('stop_everything', True, actor='owner', reason='latch test')
        self.controls.set('stop_everything', False, actor='owner', reason='latch test off')
        after = self.controls.status()['stop_everything_epoch']
        self.assertEqual(after, before + 1)


# ─── 6. Resume after stop ─────────────────────────────────────────────────────

class ResumeAfterStopTests(MacSafetyFixture):
    """After stop_everything=False, new proposals execute normally."""

    @patch('socket.create_connection', side_effect=OSError('refused'))
    def test_new_action_executes_after_resume(self, _sock):
        self.controls.set('stop_everything', True, actor='owner', reason='pause')
        self.controls.set('stop_everything', False, actor='owner', reason='resume')
        action = self.propose('service_status', {'service': 'n8n'})
        result = self.g.execute(action['id'])
        self.assertEqual(result['service'], 'n8n')
        self.assertEqual(self.s.action(action['id'])['status'], 'SUCCEEDED')

    @patch('socket.create_connection', side_effect=OSError('refused'))
    def test_epoch_latch_detected_in_execute_window(self, _sock):
        """Epoch latch fires if stop is toggled ON then OFF between claim and dispatch.

        The baseline is captured at CLAIM time (start of _execute transaction).
        Controls.check(kind, baseline) at dispatch compares epoch to baseline;
        any on/off toggle AFTER the baseline is captured increments the epoch and raises.
        We simulate this by patching run_with_controls to toggle stop mid-execution.
        """
        action = self.propose('service_status', {'service': 'n8n'})
        original = self.w.run

        def toggle_and_run(kind, p, baseline):
            self.controls.set('stop_everything', True, actor='owner', reason='latch test')
            self.controls.set('stop_everything', False, actor='owner', reason='latch test')
            return original(kind, p, baseline)

        with patch.object(self.w, 'run', side_effect=toggle_and_run):
            with self.assertRaises(Denied):
                self.g.execute(action['id'])

    def test_stop_status_clears_after_resume(self):
        self.controls.set('stop_everything', True, actor='owner', reason='pause')
        self.assertEqual(self.controls.status()['stop_everything'], 1)
        self.controls.set('stop_everything', False, actor='owner', reason='resume')
        self.assertEqual(self.controls.status()['stop_everything'], 0)


# ─── 7. Evidence logging ──────────────────────────────────────────────────────

class EvidenceLoggingTests(MacSafetyFixture):
    """control_audit records every flag change with actor, reason and timestamp."""

    def test_stop_logged_to_audit(self):
        self.controls.set('stop_everything', True, actor='owner', reason='safety test')
        audit = self.controls.history()
        entry = next(e for e in audit if e['flag'] == 'stop_everything' and e['enabled'] == 1)
        self.assertEqual(entry['actor'], 'owner')
        self.assertEqual(entry['reason'], 'safety test')
        self.assertGreater(entry['timestamp'], 0)

    def test_resume_logged_to_audit(self):
        self.controls.set('stop_everything', True, actor='owner', reason='pause')
        self.controls.set('stop_everything', False, actor='owner', reason='cleared by owner')
        audit = self.controls.history()
        stop = [e for e in audit if e['flag'] == 'stop_everything' and e['enabled'] == 1]
        resume = [e for e in audit if e['flag'] == 'stop_everything' and e['enabled'] == 0]
        self.assertTrue(stop)
        self.assertTrue(resume)
        self.assertEqual(resume[-1]['reason'], 'cleared by owner')

    def test_audit_append_only(self):
        """control_audit rows cannot be deleted or updated."""
        import sqlite3
        self.controls.set('stop_everything', True, actor='owner', reason='audit test')
        with self.assertRaises(sqlite3.Error):
            self.s.db.execute('DELETE FROM control_audit')
        with self.assertRaises(sqlite3.Error):
            self.s.db.execute("UPDATE control_audit SET actor='attacker'")

    def test_multiple_flag_changes_all_logged(self):
        flags = ['stop_everything', 'pause_external_actions', 'read_only_mode']
        for flag in flags:
            self.controls.set(flag, True, actor='owner', reason=f'{flag} test')
        audit = self.controls.history()
        logged_flags = {e['flag'] for e in audit if e['enabled'] == 1}
        for flag in flags:
            self.assertIn(flag, logged_flags)

    def test_audit_persists_across_restart(self):
        self.controls.set('stop_everything', True, actor='owner', reason='persist test')
        path = self.s.path
        self.s.close()
        s2 = Store(path)
        try:
            audit = Controls(s2).history()
            entry = next(e for e in audit if e['flag'] == 'stop_everything')
            self.assertEqual(entry['actor'], 'owner')
        finally:
            s2.close()

    @patch('socket.create_connection', side_effect=OSError('refused'))
    def test_safe_action_execution_after_stop_denied_leaves_no_audit_gap(self, _sock):
        """Denied execution must not create a false success audit entry."""
        action = self.propose('service_status', {'service': 'n8n'})
        self.controls.set('stop_everything', True, actor='owner', reason='audit gap test')
        try:
            self.g.execute(action['id'])
        except Denied:
            pass
        task_id = self.s.action(action['id'])['task_id']
        task = self.s.task(task_id)
        success_events = [e for e in task['audit'] if e['event'] == 'EXECUTION_RETURNED']
        self.assertEqual(len(success_events), 0)


# ─── Composite: full stop→block→resume→execute cycle ─────────────────────────

class FullCycleTests(MacSafetyFixture):
    """Integration: propose → stop → block → resume → new action succeeds."""

    @patch('socket.create_connection', side_effect=OSError('refused'))
    def test_full_stop_resume_cycle(self, _sock):
        # 1. Propose safe action
        pre_stop_action = self.propose('service_status', {'service': 'n8n'})
        self.assertIsNone(pre_stop_action['approval'])

        # 2. Stop everything
        self.controls.set('stop_everything', True, actor='owner', reason='full cycle test')
        self.assertEqual(self.controls.status()['stop_everything'], 1)

        # 3. Existing action blocked
        with self.assertRaises(Denied):
            self.g.execute(pre_stop_action['id'])

        # 4. Resume
        self.controls.set('stop_everything', False, actor='owner', reason='resume for test')
        self.assertEqual(self.controls.status()['stop_everything'], 0)

        # 5. New action after resume executes
        post_resume = self.propose('service_status', {'service': 'n8n'})
        result = self.g.execute(post_resume['id'])
        self.assertEqual(result['service'], 'n8n')
        self.assertEqual(self.s.action(post_resume['id'])['status'], 'SUCCEEDED')

        # 6. Evidence: stop and resume both in audit
        audit = self.controls.history()
        stop_entries = [e for e in audit if e['flag'] == 'stop_everything' and e['enabled'] == 1]
        resume_entries = [e for e in audit if e['flag'] == 'stop_everything' and e['enabled'] == 0]
        self.assertTrue(stop_entries)
        self.assertTrue(resume_entries)


if __name__ == '__main__':
    import unittest
    unittest.main()
