import concurrent.futures
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from zamal_core import Store, Gate, Policy, Conflict, Denied
from zamal_core.store import STATES


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-test-')
        self.path = Path(self.tmp.name) / 'state.sqlite3'
        self.s = Store(self.path)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def planned(self, key='task'):
        t = self.s.create_task('Inspect CPC without editing', 'cpc', '/approved/project', key)
        for state in ('UNDERSTOOD', 'PLANNED'):
            t = self.s.transition(t['id'], state, t['revision'])
        return t

    def approve(self, action):
        return self.s.decide(action['approval']['id'], 'APPROVE', actor='test-human')

    def test_task_persists_complete_context_and_audit(self):
        t = self.planned()
        ctx = {'inputs': [{'type': 'screenshot', 'reference': 'inbox/image.png'}],
               'rules': ['preserve CPC'], 'decisions': ['read only'], 'plan': ['inspect'],
               'selected_ai_tool': 'future-provider', 'affected_files': [],
               'git_state': {'head': 'abc'}, 'test_results': [], 'errors': [],
               'retries': [], 'final_result': None, 'next_action': 'inspect'}
        self.s.patch_context(t['id'], ctx, t['revision'])
        self.s.close()
        self.s = Store(self.path)
        reloaded = self.s.task(t['id'])
        self.assertEqual(reloaded['context'], ctx)
        self.assertEqual(reloaded['command'], 'Inspect CPC without editing')
        self.assertEqual(len(reloaded['audit']), 4)
        self.assertEqual(set(STATES), {'RECEIVED', 'UNDERSTOOD', 'PLANNED', 'RUNNING', 'VERIFYING', 'WAITING_APPROVAL', 'APPROVED', 'REJECTED', 'COMPLETED', 'FAILED', 'RECOVERING'})

    def test_task_deduplication_and_conflict(self):
        a = self.s.create_task('hello', 'draft', 'local', 'same')
        b = self.s.create_task('hello', 'draft', 'local', 'same')
        self.assertEqual(a['id'], b['id'])
        with self.assertRaises(Conflict):
            self.s.create_task('different', 'draft', 'local', 'same')
        self.assertEqual(len(self.s.tasks()), 1)

    def test_revision_and_state_bypass_rejected(self):
        t = self.planned()
        with self.assertRaises(Conflict):
            self.s.patch_context(t['id'], {'next_action': 'x'}, 0)
        for state in ('APPROVED', 'RUNNING', 'COMPLETED'):
            with self.assertRaises(Denied):
                self.s.transition(t['id'], state, t['revision'])
        with self.assertRaises(ValueError):
            self.s.patch_context(t['id'], {'state': 'APPROVED'}, t['revision'])

    def test_every_protected_kind_denied_before_approval(self):
        calls = []
        for kind in sorted(Policy.PROTECTED | {'git_commit'}):
            with self.subTest(kind=kind):
                t = self.planned(kind)
                gate = Gate(self.s, {kind: lambda p: calls.append(p)})
                a = gate.propose(t['id'], kind, {'target': 'fixture'}, 'action')
                with self.assertRaises(Denied):
                    gate.execute(a['id'])
        self.assertEqual(calls, [])

    def test_unknown_action_fails_closed(self):
        with self.assertRaises(Denied):
            Gate(self.s).propose(self.planned()['id'], 'shell', {'command': 'anything'}, 'a')

    def test_approval_persists_and_is_consumed_once(self):
        t = self.planned()
        a = Gate(self.s).propose(t['id'], 'email_send', {'body': 'fixture'}, 'a')
        self.assertEqual(len(self.s.approval_queue()), 1)
        self.approve(a)
        self.s.close()
        self.s = Store(self.path)
        calls = []
        gate = Gate(self.s, {'email_send': lambda p: calls.append(p) or {'fixture': True}})
        self.assertEqual(gate.execute(a['id']), {'fixture': True})
        with self.assertRaises(Denied):
            gate.execute(a['id'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.s.action(a['id'])['approval']['status'], 'CONSUMED')
        task = self.s.task(t['id'])
        self.assertEqual(task['state'], 'VERIFYING')
        with self.assertRaises(Denied):
            self.s.transition(t['id'], 'COMPLETED', task['revision'])
        task = self.s.patch_context(t['id'], {'test_results': [{'passed': True, 'evidence': 'fixture assertion'}], 'final_result': 'verified'}, task['revision'])
        task = self.s.transition(t['id'], 'COMPLETED', task['revision'])
        self.assertEqual(task['state'], 'COMPLETED')
        with self.assertRaises(Conflict):
            self.s.patch_context(t['id'], {'final_result': 'changed'}, task['revision'])

    def test_conversational_approve_is_not_approval(self):
        t = self.planned()
        a = Gate(self.s).propose(t['id'], 'git_push', {}, 'a')
        t = self.s.task(t['id'])
        self.s.patch_context(t['id'], {'decisions': ['APPROVE'], 'inputs': [{'text': 'Approve'}]}, t['revision'])
        with self.assertRaises(Denied):
            Gate(self.s, {'git_push': lambda p: None}).execute(a['id'])

    def test_reject_cancels_and_cannot_be_reapproved(self):
        a = Gate(self.s).propose(self.planned()['id'], 'delete', {}, 'a')
        self.s.decide(a['approval']['id'], 'REJECT', actor='human')
        with self.assertRaises(Conflict):
            self.approve(a)
        with self.assertRaises(Denied):
            Gate(self.s, {'delete': lambda p: None}).execute(a['id'])
        self.assertEqual(self.s.task(a['task_id'])['state'], 'REJECTED')

    def test_edit_requires_new_bound_approval(self):
        gate = Gate(self.s, {'deploy': lambda p: p})
        a = gate.propose(self.planned()['id'], 'deploy', {'version': 1}, 'a')
        with self.assertRaises(ValueError):
            self.s.decide(a['approval']['id'], 'EDIT', actor='human')
        self.s.decide(a['approval']['id'], 'EDIT', actor='human', note='version 2')
        b = gate.propose(a['task_id'], 'deploy', {'version': 2}, 'b')
        self.assertNotEqual(a['approval']['id'], b['approval']['id'])
        with self.assertRaises(Denied):
            gate.execute(a['id'])
        with self.assertRaises(Denied):
            gate.execute(b['id'])
        self.approve(b)
        self.assertEqual(gate.execute(b['id']), {'version': 2})

    def test_expiry_at_decision_and_execution(self):
        with patch('zamal_core.store.time.time', return_value=100):
            a = Gate(self.s).propose(self.planned()['id'], 'payment', {}, 'a', ttl_seconds=10)
        with patch('zamal_core.store.time.time', return_value=111):
            with self.assertRaises(Denied):
                self.approve(a)
            self.assertEqual(self.s.approval_queue(), [])
            self.assertEqual(self.s.action(a['id'])['approval']['status'], 'EXPIRED')
        with patch('zamal_core.store.time.time', return_value=105):
            with self.assertRaises(Denied):
                self.approve(a)  # Observed expiry is terminal even if the clock rolls back.
            a = Gate(self.s).propose(self.planned('second')['id'], 'payment', {}, 'b', ttl_seconds=5)
            self.approve(a)
        with patch('zamal_core.store.time.time', return_value=111):
            with self.assertRaises(Denied):
                Gate(self.s, {'payment': lambda p: None}).execute(a['id'])

    def test_binding_tamper_denied(self):
        a = Gate(self.s).propose(self.planned()['id'], 'email_send', {'to': 'fixture'}, 'a')
        self.approve(a)
        self.s.db.execute('UPDATE actions SET payload=? WHERE id=?', ('{"to":"changed"}', a['id']))
        with self.assertRaises(Denied):
            Gate(self.s, {'email_send': lambda p: None}).execute(a['id'])

    def test_action_deduplication(self):
        gate = Gate(self.s)
        t = self.planned()
        a = gate.propose(t['id'], 'git_push', {}, 'a')
        self.assertEqual(a['id'], gate.propose(t['id'], 'git_push', {}, 'a')['id'])
        with self.assertRaises(Conflict):
            gate.propose(t['id'], 'git_push', {'branch': 'other'}, 'a')
        self.assertEqual(len(self.s.approval_queue()), 1)

    def test_safe_handler_and_missing_capability(self):
        t = self.planned()
        a = Gate(self.s).propose(t['id'], 'inspect', {}, 'a')
        self.assertIsNone(a['approval'])
        with self.assertRaises(Denied):
            Gate(self.s).execute(a['id'])
        self.assertEqual(self.s.action(a['id'])['status'], 'PENDING')
        self.assertEqual(Gate(self.s, {'inspect': lambda p: {'readOnly': True}}).execute(a['id']), {'readOnly': True})

    def test_policy_tightening_does_not_allow_unapproved_commit(self):
        a = Gate(self.s, policy=Policy(False)).propose(self.planned()['id'], 'git_commit', {}, 'a')
        with self.assertRaises(Denied):
            Gate(self.s, {'git_commit': lambda p: None}).execute(a['id'])

    def test_policy_loosening_does_not_remove_existing_approval(self):
        a = Gate(self.s).propose(self.planned()['id'], 'git_commit', {}, 'a')
        with self.assertRaises(Denied):
            Gate(self.s, {'git_commit': lambda p: None}, Policy(False)).execute(a['id'])

    def test_handler_exception_blocks_replay_and_redacts_error(self):
        def fail(payload):
            raise RuntimeError('secret-token-must-not-be-logged')
        gate = Gate(self.s, {'email_send': fail})
        a = gate.propose(self.planned()['id'], 'email_send', {}, 'a')
        self.approve(a)
        with self.assertRaises(RuntimeError):
            gate.execute(a['id'])
        self.assertEqual(self.s.action(a['id'])['status'], 'UNCERTAIN')
        self.assertEqual(self.s.task(a['task_id'])['state'], 'RECOVERING')
        with self.assertRaises(Denied):
            gate.execute(a['id'])
        self.assertNotIn('secret-token', json.dumps(self.s.task(a['task_id'])))

    def test_process_interruption_claim_survives_reopen(self):
        def interrupt(payload):
            raise SystemExit('simulate process stopping after claim')
        gate = Gate(self.s, {'git_push': interrupt})
        a = gate.propose(self.planned()['id'], 'git_push', {}, 'a')
        self.approve(a)
        with self.assertRaises(SystemExit):
            gate.execute(a['id'])
        self.s.close()
        self.s = Store(self.path)
        self.assertEqual(self.s.action(a['id'])['status'], 'EXECUTING')
        with self.assertRaises(Denied):
            Gate(self.s, {'git_push': lambda p: None}).execute(a['id'])

    def test_concurrent_execution_claims_only_once(self):
        a = Gate(self.s).propose(self.planned()['id'], 'email_send', {}, 'a')
        self.approve(a)
        barrier = threading.Barrier(2)
        calls = []
        def worker():
            s = Store(self.path)
            try:
                gate = Gate(s, {'email_send': lambda p: calls.append('fixture')})
                barrier.wait(timeout=5)
                try:
                    gate.execute(a['id'])
                    return 'executed'
                except Denied:
                    return 'denied'
            finally:
                s.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(sorted(results), ['denied', 'executed'])
        self.assertEqual(calls, ['fixture'])

    def test_concurrent_duplicate_intake(self):
        def worker(_):
            s = Store(self.path)
            try:
                return s.create_task('same', 'draft', 'local', 'same')['id']
            finally:
                s.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(worker, range(4)))
        self.assertEqual(len(set(ids)), 1)

    def test_transaction_rollback_and_audit_append_only(self):
        t = self.planned()
        with self.assertRaises(RuntimeError):
            with self.s.transaction():
                self.s._audit(t['id'], 'ROLLED_BACK')
                raise RuntimeError()
        self.assertNotIn('ROLLED_BACK', [x['event'] for x in self.s.task(t['id'])['audit']])
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('DELETE FROM audit')
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute("UPDATE audit SET event='changed'")

    def test_backup_restore_and_no_overwrite(self):
        t = self.planned()
        destination = Path(self.tmp.name) / 'backup.sqlite3'
        self.s.backup(destination)
        restored = Store(destination)
        try:
            self.assertEqual(restored.task(t['id']), self.s.task(t['id']))
            self.assertEqual(restored.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            restored.close()
        with self.assertRaises(FileExistsError):
            self.s.backup(destination)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
