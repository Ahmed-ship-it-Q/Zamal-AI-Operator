"""Focused tests for Job Application Worker (Phase 15).

Covers:
  - Schema/migration idempotency
  - Approval binding (submit creates PENDING approval)
  - No-submit-before-approval (execute without APPROVE raises Denied)
  - Post-approval continuation (APPROVE → handler executes)
  - Replay denial (second execute raises Denied after consume)
  - Emergency stop (stop_everything blocks submit)
  - Evidence logging (job_app_audit entries, response_hash logged)
  - Private-path denial (CV outside outbox rejected)
  - Unsupported board BLOCKED safely
  - candidate.json missing → Denied at submit
  - acknowledge_gaps=false → Denied at propose time
"""
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import sqlite3

from zamal_core.store import Store, Denied, Conflict
from zamal_core.safety import Controls
from zamal_core.job_app import (
    JobApplicationWorker, JobBrowserPolicy, GreenhouseSubmitter,
    JOB_APP_SAFE, JOB_APP_PROTECTED, JOB_APP_KINDS,
    migrate_job_app, validate_job_app, ALLOWED_JOB_DOMAINS,
    SUPPORTED_SUBMIT_BOARDS, _build_multipart,
)


# ── Fixture ───────────────────────────────────────────────────────────────────

class JobAppFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        # Create outbox and config dirs
        self._operator = Path(self._tmp) / 'operator'
        self._outbox = self._operator / 'outbox'
        self._config = self._operator / 'config'
        self._outbox.mkdir(parents=True)
        self._config.mkdir(parents=True)

        # Write a minimal candidate.json
        (self._config / 'candidate.json').write_text(json.dumps({
            'first_name': 'Test', 'last_name': 'User', 'email': 'test@example.com',
        }))

        # Write a minimal CV in outbox
        self._cv = self._outbox / 'cv.pdf'
        self._cv.write_bytes(b'%PDF-1.4 fake pdf content for test')

        db_path = Path(self._tmp) / 'data' / 'store.db'
        db_path.parent.mkdir(parents=True)
        os.environ['ZAMAL_DB'] = str(db_path)
        self._db_path = db_path
        self.s = Store(db_path)
        self.controls = Controls(self.s)
        self.w = JobApplicationWorker(self.s, operator=str(self._operator))
        self.g = self.w.gate()

    def tearDown(self):
        try:
            if self.controls.status().get('stop_everything'):
                self.controls.set('stop_everything', False, actor='teardown', reason='cleanup')
        except Exception:
            pass
        self.s.close()
        import shutil; shutil.rmtree(self._tmp, ignore_errors=True)
        os.environ.pop('ZAMAL_DB', None)

    def _task(self):
        t = self.s.create_task('test job task', 'work', 'operator', 'key-job-' + str(time.time()))
        t = self.s.transition(t['id'], 'UNDERSTOOD', 0)
        t = self.s.transition(t['id'], 'PLANNED', 1)
        return t

    def _biz_jobs_action(self, task_id, job_id='456789'):
        """Create a mock succeeded biz_jobs action with a Greenhouse job in result."""
        job_url = f'https://boards.greenhouse.io/testco/jobs/{job_id}'
        jobs = [{'id': job_id, 'title': 'Engineer', 'company': 'testco',
                  'url': job_url, 'source': 'greenhouse',
                  'requirements_excerpt': 'Python, Go, testing'}]
        from zamal_core.store import uid, encode
        action_id = uid()
        now = time.time()
        self.s.db.execute(
            '''INSERT INTO actions(id,task_id,kind,payload,binding,result,status,
               created,request_key,protected)
               VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (action_id, task_id, 'biz_jobs', encode({'query': 'engineer'}), 'none',
             encode({'jobs': jobs}), 'SUCCEEDED', now, 'rk-' + action_id, 0),
        )
        return action_id, job_url

    def propose(self, task_id, kind, payload):
        return self.g.propose(task_id, kind, payload, 'rk-' + str(time.time()))

    def _fresh_task(self):
        """Create a fresh PLANNED task for a single action."""
        t = self.s.create_task('test action task', 'work', 'operator', 'rk-act-' + str(time.time()))
        t = self.s.transition(t['id'], 'UNDERSTOOD', 0)
        t = self.s.transition(t['id'], 'PLANNED', 1)
        return t


# ── Migration tests ───────────────────────────────────────────────────────────

class MigrationTests(JobAppFixture):
    def test_tables_created(self):
        tables = {r[0] for r in self.s.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertIn('job_applications', tables)
        self.assertIn('job_app_audit', tables)

    def test_core_extension_registered(self):
        row = self.s.db.execute(
            "SELECT version FROM core_extensions WHERE name='job_app'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)

    def test_migration_idempotent(self):
        # Running migrate again must not raise
        migrate_job_app(self.s)
        row = self.s.db.execute(
            "SELECT version FROM core_extensions WHERE name='job_app'"
        ).fetchone()
        self.assertEqual(row[0], 1)

    def test_audit_append_only_update_blocked(self):
        self.s.db.execute(
            "INSERT INTO job_app_audit(timestamp,application_id,operation,detail) VALUES(?,?,?,?)",
            (time.time(), 'test-id', 'test-op', '{}'),
        )
        with self.assertRaises((sqlite3.OperationalError, sqlite3.IntegrityError)):
            self.s.db.execute(
                "UPDATE job_app_audit SET operation='hacked' WHERE operation='test-op'"
            )

    def test_audit_append_only_delete_blocked(self):
        self.s.db.execute(
            "INSERT INTO job_app_audit(timestamp,application_id,operation,detail) VALUES(?,?,?,?)",
            (time.time(), 'test-id2', 'test-op', '{}'),
        )
        with self.assertRaises((sqlite3.OperationalError, sqlite3.IntegrityError)):
            self.s.db.execute("DELETE FROM job_app_audit WHERE operation='test-op'")


# ── Validation tests ──────────────────────────────────────────────────────────

class ValidationTests(JobAppFixture):
    def test_acknowledge_gaps_false_rejected_at_validate(self):
        with self.assertRaises(Denied):
            validate_job_app('job_app_submit',
                             {'application_id': 'x', 'acknowledge_gaps': False},
                             'operator')

    def test_acknowledge_gaps_non_bool_rejected(self):
        with self.assertRaises(Denied):
            validate_job_app('job_app_submit',
                             {'application_id': 'x', 'acknowledge_gaps': 'yes'},
                             'operator')

    def test_unknown_fields_rejected(self):
        with self.assertRaises(Denied):
            validate_job_app('job_app_open',
                             {'url': 'https://boards.greenhouse.io/co/jobs/1',
                              'board_type': 'greenhouse', 'extra': 'field'},
                             'operator')

    def test_wrong_project_rejected(self):
        with self.assertRaises(Denied):
            validate_job_app('job_app_open',
                             {'url': 'https://boards.greenhouse.io/co/jobs/1',
                              'board_type': 'greenhouse'},
                             'personal')

    def test_disallowed_domain_rejected(self):
        with self.assertRaises(Denied):
            validate_job_app('job_app_open',
                             {'url': 'https://evil.com/jobs/1', 'board_type': 'unknown'},
                             'operator')

    def test_allowed_domains_accepted(self):
        for domain in ALLOWED_JOB_DOMAINS:
            validate_job_app('job_app_open',
                             {'url': f'https://{domain}/jobs/123',
                              'board_type': 'greenhouse'},
                             'operator')

    def test_path_traversal_in_cv_rejected(self):
        with self.assertRaises(Denied):
            validate_job_app('job_app_plan',
                             {'source_action_id': 'x', 'job_id': 'y',
                              'cv_file_ref': '../secret/cv.pdf',
                              'candidate': {'name': 'T', 'skills': [], 'experience': ''}},
                             'operator')

    def test_hidden_file_in_cv_rejected(self):
        with self.assertRaises(Denied):
            validate_job_app('job_app_plan',
                             {'source_action_id': 'x', 'job_id': 'y',
                              'cv_file_ref': '.hidden/cv.pdf',
                              'candidate': {'name': 'T', 'skills': [], 'experience': ''}},
                             'operator')


# ── Approval binding tests ────────────────────────────────────────────────────

class ApprovalBindingTests(JobAppFixture):
    def _plan_application(self):
        plan_task = self._fresh_task()
        src_action_id, _ = self._biz_jobs_action(plan_task['id'])
        payload = {
            'source_action_id': src_action_id,
            'job_id': '456789',
            'cv_file_ref': 'cv.pdf',
            'candidate': {'name': 'Test User', 'skills': ['Python'], 'experience': '3 years'},
        }
        action = self.propose(plan_task['id'], 'job_app_plan', payload)
        result = self.g.execute(action['id'])
        return result['application_id']

    def test_submit_requires_approval_creates_pending(self):
        app_id = self._plan_application()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.assertEqual(action['status'], 'PENDING')
        self.assertIsNotNone(action.get('approval'))

    def test_submit_kind_in_protected(self):
        self.assertIn('job_app_submit', JOB_APP_PROTECTED)

    def test_submit_requires_approval_flag(self):
        self.assertTrue(self.g.policy.requires_approval('job_app_submit'))

    def test_safe_kinds_do_not_require_approval(self):
        for kind in JOB_APP_SAFE:
            self.assertFalse(self.g.policy.requires_approval(kind), f'{kind} should be SAFE')


# ── No-submit-before-approval ─────────────────────────────────────────────────

class NoSubmitBeforeApprovalTests(JobAppFixture):
    def _plan_application(self):
        plan_task = self._fresh_task()
        src_action_id, _ = self._biz_jobs_action(plan_task['id'])
        payload = {
            'source_action_id': src_action_id,
            'job_id': '456789',
            'cv_file_ref': 'cv.pdf',
            'candidate': {'name': 'Test User', 'skills': ['Python'], 'experience': '3 years'},
        }
        action = self.propose(plan_task['id'], 'job_app_plan', payload)
        result = self.g.execute(action['id'])
        return result['application_id']

    def test_execute_without_approve_raises_denied(self):
        app_id = self._plan_application()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        with self.assertRaises(Denied):
            self.g.execute(action['id'])

    def test_rejected_approval_blocks_execute(self):
        app_id = self._plan_application()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'REJECT', actor='owner', note='nope')
        with self.assertRaises(Denied):
            self.g.execute(action['id'])


# ── Post-approval continuation ────────────────────────────────────────────────

class PostApprovalContinuationTests(JobAppFixture):
    def _plan_application(self):
        plan_task = self._fresh_task()
        src_action_id, _ = self._biz_jobs_action(plan_task['id'])
        payload = {
            'source_action_id': src_action_id,
            'job_id': '456789',
            'cv_file_ref': 'cv.pdf',
            'candidate': {'name': 'Test User', 'skills': ['Python'], 'experience': '3 years'},
        }
        action = self.propose(plan_task['id'], 'job_app_plan', payload)
        result = self.g.execute(action['id'])
        return result['application_id']

    def _mock_gh_submit(self, status=200):
        return patch.object(
            GreenhouseSubmitter, 'submit',
            return_value=(status, 'abc123hash', status in (200, 201), {'id': 'ext-456'}),
        )

    def test_approve_then_execute_succeeds(self):
        app_id = self._plan_application()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='go')
        with self._mock_gh_submit(200):
            result = self.g.execute(action['id'])
        self.assertTrue(result['ok'])
        self.assertEqual(result['outcome'], 'SUBMITTED')
        self.assertIn('response_hash', result)
        self.assertTrue(result['evidence_logged'])

    def test_approved_action_updates_application_status(self):
        app_id = self._plan_application()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='go')
        with self._mock_gh_submit(200):
            self.g.execute(action['id'])
        row = self.s.db.execute('SELECT status FROM job_applications WHERE id=?', (app_id,)).fetchone()
        self.assertEqual(row['status'], 'submitted')

    def test_telegram_approval_continues_exact_action_and_denies_replay(self):
        from zamal_core.telegram_bot import TelegramCallbackHandler
        app_id = self._plan_application()
        task = self._fresh_task()
        action = self.propose(task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        with patch('zamal_core.job_app.JobApplicationWorker', return_value=self.w), self._mock_gh_submit(200) as submit:
            with self.assertRaises(Denied):
                self.g.execute(action['id'])
            submit.assert_not_called()
            handler = TelegramCallbackHandler(self.s)
            callback = 'approval:' + action['approval']['id'] + ':APPROVE'
            self.assertTrue(handler.handle('fixture-callback', 'fixture-owner', callback)['ok'])
            self.assertFalse(handler.handle('fixture-replay', 'fixture-owner', callback)['ok'])
            submit.assert_called_once()


# ── Replay denial ─────────────────────────────────────────────────────────────

class ReplayDenialTests(JobAppFixture):
    def _plan_application(self):
        plan_task = self._fresh_task()
        src_action_id, _ = self._biz_jobs_action(plan_task['id'])
        payload = {
            'source_action_id': src_action_id,
            'job_id': '456789',
            'cv_file_ref': 'cv.pdf',
            'candidate': {'name': 'Test User', 'skills': ['Python'], 'experience': '3 years'},
        }
        action = self.propose(plan_task['id'], 'job_app_plan', payload)
        result = self.g.execute(action['id'])
        return result['application_id']

    def test_second_execute_raises_after_consume(self):
        app_id = self._plan_application()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='ok')
        with patch.object(GreenhouseSubmitter, 'submit',
                          return_value=(200, 'h', True, {'id': 'ext-1'})):
            self.g.execute(action['id'])
        # Second execute on same action raises
        with self.assertRaises(Denied):
            self.g.execute(action['id'])

    def test_application_already_submitted_cannot_resubmit(self):
        app_id = self._plan_application()
        # First submit
        submit_task1 = self._fresh_task()
        action1 = self.propose(submit_task1['id'], 'job_app_submit',
                               {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action1['approval']['id'], 'APPROVE', actor='owner', note='ok')
        with patch.object(GreenhouseSubmitter, 'submit',
                          return_value=(200, 'h', True, {'id': 'ext-1'})):
            self.g.execute(action1['id'])
        # Second propose for same app on a fresh task: execute should fail with Conflict
        submit_task2 = self._fresh_task()
        action2 = self.propose(submit_task2['id'], 'job_app_submit',
                               {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action2['approval']['id'], 'APPROVE', actor='owner', note='retry')
        with patch.object(GreenhouseSubmitter, 'submit',
                          return_value=(200, 'h2', True, {})):
            with self.assertRaises(Conflict):
                self.g.execute(action2['id'])


# ── Emergency stop ────────────────────────────────────────────────────────────

class EmergencyStopTests(JobAppFixture):
    def _plan_application(self):
        plan_task = self._fresh_task()
        src_action_id, _ = self._biz_jobs_action(plan_task['id'])
        payload = {
            'source_action_id': src_action_id,
            'job_id': '456789',
            'cv_file_ref': 'cv.pdf',
            'candidate': {'name': 'Test User', 'skills': ['Python'], 'experience': '3 years'},
        }
        action = self.propose(plan_task['id'], 'job_app_plan', payload)
        result = self.g.execute(action['id'])
        return result['application_id']

    def test_stop_everything_blocks_submit_execute(self):
        app_id = self._plan_application()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='go')
        self.controls.set('stop_everything', True, actor='owner', reason='test emergency')
        try:
            with self.assertRaises(Denied) as cm:
                self.g.execute(action['id'])
            self.assertIn('stop_everything', str(cm.exception))
        finally:
            self.controls.set('stop_everything', False, actor='owner', reason='test cleanup')

    def test_pause_external_actions_blocks_submit(self):
        app_id = self._plan_application()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='go')
        self.controls.set('pause_external_actions', True, actor='owner', reason='pause test')
        try:
            with self.assertRaises(Denied) as cm:
                self.g.execute(action['id'])
            self.assertIn('pause_external_actions', str(cm.exception))
        finally:
            self.controls.set('pause_external_actions', False, actor='owner', reason='test cleanup')

    def test_stop_everything_blocks_safe_kinds_too(self):
        task_id = self._task()['id']
        action = self.propose(task_id, 'job_app_open',
                              {'url': 'https://boards.greenhouse.io/co/jobs/1',
                               'board_type': 'greenhouse'})
        self.controls.set('stop_everything', True, actor='owner', reason='test stop')
        try:
            with self.assertRaises(Denied):
                self.g.execute(action['id'])
        finally:
            self.controls.set('stop_everything', False, actor='owner', reason='cleanup')


# ── Evidence logging ──────────────────────────────────────────────────────────

class EvidenceLoggingTests(JobAppFixture):
    def _plan_application(self):
        plan_task = self._fresh_task()
        src_action_id, _ = self._biz_jobs_action(plan_task['id'])
        payload = {
            'source_action_id': src_action_id,
            'job_id': '456789',
            'cv_file_ref': 'cv.pdf',
            'candidate': {'name': 'Test User', 'skills': ['Python'], 'experience': '3 years'},
        }
        action = self.propose(plan_task['id'], 'job_app_plan', payload)
        result = self.g.execute(action['id'])
        return result['application_id']

    def test_plan_creates_audit_entry(self):
        app_id = self._plan_application()
        rows = self.s.db.execute(
            "SELECT * FROM job_app_audit WHERE application_id=? AND operation='plan'", (app_id,)
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_submit_creates_audit_entry_with_response_hash(self):
        app_id = self._plan_application()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='go')
        with patch.object(GreenhouseSubmitter, 'submit',
                          return_value=(200, 'abc123hash', True, {'id': 'ext-1'})):
            self.g.execute(action['id'])
        rows = self.s.db.execute(
            "SELECT * FROM job_app_audit WHERE application_id=? AND operation='submit_submitted'",
            (app_id,)
        ).fetchall()
        self.assertEqual(len(rows), 1)
        detail = json.loads(rows[0]['detail'])
        self.assertEqual(detail['response_hash'], 'abc123hash')
        self.assertEqual(detail['http_status'], 200)

    def test_response_hash_persisted_in_job_applications(self):
        app_id = self._plan_application()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='go')
        with patch.object(GreenhouseSubmitter, 'submit',
                          return_value=(200, 'testhash456', True, {})):
            self.g.execute(action['id'])
        row = self.s.db.execute(
            'SELECT response_hash FROM job_applications WHERE id=?', (app_id,)
        ).fetchone()
        self.assertEqual(row['response_hash'], 'testhash456')

    def test_blocked_submit_creates_audit_entry(self):
        plan_task = self._fresh_task()
        src_action_id, _ = self._biz_jobs_action(plan_task['id'], job_id='lever-999')
        jobs = [{'id': 'lever-999', 'title': 'Engineer', 'company': 'leverco',
                  'url': 'https://jobs.lever.co/leverco/lever-999', 'source': 'lever',
                  'requirements_excerpt': 'Python'}]
        from zamal_core.store import encode
        self.s.db.execute(
            'UPDATE actions SET result=? WHERE id=?',
            (encode({'jobs': jobs}), src_action_id),
        )
        payload = {
            'source_action_id': src_action_id,
            'job_id': 'lever-999',
            'cv_file_ref': 'cv.pdf',
            'candidate': {'name': 'Test User', 'skills': ['Python'], 'experience': '3 years'},
        }
        action = self.propose(plan_task['id'], 'job_app_plan', payload)
        result = self.g.execute(action['id'])
        app_id = result['application_id']

        submit_task = self._fresh_task()
        action2 = self.propose(submit_task['id'], 'job_app_submit',
                               {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action2['approval']['id'], 'APPROVE', actor='owner', note='go')
        result2 = self.g.execute(action2['id'])
        self.assertEqual(result2['outcome'], 'BLOCKED')

        rows = self.s.db.execute(
            "SELECT * FROM job_app_audit WHERE application_id=? AND operation='submit_blocked'",
            (app_id,)
        ).fetchall()
        self.assertEqual(len(rows), 1)


# ── Private-path denial ───────────────────────────────────────────────────────

class PrivatePathDenialTests(JobAppFixture):
    def _plan_application(self, task_id, cv_file_ref='cv.pdf'):
        src_action_id, _ = self._biz_jobs_action(task_id)
        payload = {
            'source_action_id': src_action_id,
            'job_id': '456789',
            'cv_file_ref': cv_file_ref,
            'candidate': {'name': 'Test User', 'skills': ['Python'], 'experience': '3 years'},
        }
        action = self.propose(task_id, 'job_app_plan', payload)
        return self.g.execute(action['id'])

    def test_cv_outside_outbox_rejected_at_validate(self):
        with self.assertRaises(Denied):
            validate_job_app('job_app_plan',
                             {'source_action_id': 'x', 'job_id': 'y',
                              'cv_file_ref': '../../../etc/passwd',
                              'candidate': {'name': 'T', 'skills': [], 'experience': ''}},
                             'operator')

    def test_resolve_outbox_rejects_escape(self):
        # Bypass validate and test _resolve_outbox directly
        with self.assertRaises(Denied):
            self.w._resolve_outbox('../../../etc/passwd')

    def test_absolute_path_outside_outbox_rejected(self):
        # Absolute paths pass schema validation but are caught by _resolve_outbox containment check
        with self.assertRaises(Denied):
            self.w._resolve_outbox('/etc/passwd')


# ── Unsupported board ─────────────────────────────────────────────────────────

class UnsupportedBoardTests(JobAppFixture):
    def _make_lever_app(self):
        plan_task = self._fresh_task()
        src_action_id, _ = self._biz_jobs_action(plan_task['id'])
        jobs = [{'id': 'lv-1', 'title': 'Engineer', 'company': 'leverco',
                  'url': 'https://jobs.lever.co/leverco/lv-1', 'source': 'lever',
                  'requirements_excerpt': 'Python'}]
        from zamal_core.store import encode
        self.s.db.execute(
            'UPDATE actions SET result=? WHERE id=?',
            (encode({'jobs': jobs}), src_action_id),
        )
        payload = {
            'source_action_id': src_action_id,
            'job_id': 'lv-1',
            'cv_file_ref': 'cv.pdf',
            'candidate': {'name': 'Test User', 'skills': ['Python'], 'experience': '3 years'},
        }
        action = self.propose(plan_task['id'], 'job_app_plan', payload)
        result = self.g.execute(action['id'])
        return result['application_id']

    def test_lever_submit_blocked_safely(self):
        app_id = self._make_lever_app()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='ok')
        result = self.g.execute(action['id'])
        self.assertEqual(result['outcome'], 'BLOCKED')
        self.assertFalse(result.get('ok', True))

    def test_lever_submit_does_not_call_http(self):
        app_id = self._make_lever_app()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='ok')
        with patch.object(GreenhouseSubmitter, 'submit') as mock_submit:
            self.g.execute(action['id'])
            mock_submit.assert_not_called()

    def test_lever_app_status_set_to_blocked(self):
        app_id = self._make_lever_app()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='ok')
        self.g.execute(action['id'])
        row = self.s.db.execute('SELECT status FROM job_applications WHERE id=?', (app_id,)).fetchone()
        self.assertEqual(row['status'], 'blocked')


# ── candidate.json missing ────────────────────────────────────────────────────

class CandidateJsonMissingTests(JobAppFixture):
    def _plan_application(self):
        plan_task = self._fresh_task()
        src_action_id, _ = self._biz_jobs_action(plan_task['id'])
        payload = {
            'source_action_id': src_action_id,
            'job_id': '456789',
            'cv_file_ref': 'cv.pdf',
            'candidate': {'name': 'Test User', 'skills': ['Python'], 'experience': '3 years'},
        }
        action = self.propose(plan_task['id'], 'job_app_plan', payload)
        result = self.g.execute(action['id'])
        return result['application_id']

    def test_missing_candidate_json_raises_denied_at_submit(self):
        app_id = self._plan_application()
        (self._config / 'candidate.json').unlink()
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='go')
        with self.assertRaises(Denied) as cm:
            self.g.execute(action['id'])
        self.assertIn('candidate.json', str(cm.exception))

    def test_incomplete_candidate_json_raises_denied(self):
        app_id = self._plan_application()
        (self._config / 'candidate.json').write_text(json.dumps({'first_name': 'Test', 'last_name': 'User'}))
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='go')
        with self.assertRaises(Denied) as cm:
            self.g.execute(action['id'])
        self.assertIn('email', str(cm.exception))


# ── Multipart helper ──────────────────────────────────────────────────────────

class MultipartTests(unittest.TestCase):
    def test_build_multipart_fields(self):
        body = _build_multipart('BOUNDARY', {'first_name': 'Alice', 'email': 'a@b.com'}, {})
        self.assertIn(b'name="first_name"', body)
        self.assertIn(b'Alice', body)
        self.assertIn(b'--BOUNDARY--', body)

    def test_build_multipart_file(self):
        pdf = b'%PDF fake'
        body = _build_multipart('BOUND', {}, {'resume': ('cv.pdf', 'application/pdf', pdf)})
        self.assertIn(b'filename="cv.pdf"', body)
        self.assertIn(b'%PDF fake', body)

    def test_build_multipart_empty(self):
        body = _build_multipart('B', {}, {})
        self.assertIn(b'--B--', body)


# ── Browser policy ────────────────────────────────────────────────────────────

class BrowserPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = JobBrowserPolicy()

    def test_allowed_domain_validates(self):
        for domain in ALLOWED_JOB_DOMAINS:
            self.policy.validate(f'https://{domain}/jobs/1')  # Must not raise

    def test_disallowed_domain_raises(self):
        with self.assertRaises(Denied):
            self.policy.validate('https://indeed.com/jobs/1')

    def test_http_url_raises(self):
        with self.assertRaises(Denied):
            self.policy.validate('http://boards.greenhouse.io/co/jobs/1')

    def test_credentials_in_url_raises(self):
        with self.assertRaises(Denied):
            self.policy.validate('https://user:pass@boards.greenhouse.io/co/jobs/1')

    @patch('subprocess.run')
    def test_open_launches_subprocess(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        result = self.policy.open('https://boards.greenhouse.io/co/jobs/1')
        self.assertTrue(result['opened'])
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertIn('/usr/bin/open', args[0])


# ── Browser → Approval → Resume → Evidence (local fixture E2E) ────────────────

class BrowserApprovalResumeTests(JobAppFixture):
    """Proves the complete safe browser workflow on local fixture.

    open → navigate → detect/fill fields → synthetic file-upload → stop before
    protected submit → create Telegram APPROVE/EDIT/REJECT via real Operator Gate
    → after APPROVE resume automatically → evidence saved.

    Browser subprocess (job_browser_fixture.run) is synthetic.
    Gate / Store / TelegramCallbackHandler / Controls use real operator machinery.
    """

    _PREPARED = {
        'phase': 'PREPARED',
        'navigated': True,
        'filled': True,
        'submissions': 0,
        'fields': ['first_name', 'last_name', 'email', 'resume'],
        'upload_sha256': 'aabb' * 16,
        'contract': 'fixture-test-contract-v1',
        'bridge_identity': 'test-bridge-identity-abcd1234',
    }

    def _make_submitted(self):
        prepared = dict(self._PREPARED)
        return {
            'phase': 'VERIFIED',
            'navigated': True,
            'filled': True,
            'submissions': 1,
            'contract': prepared['contract'],
            'bridge_identity': prepared['bridge_identity'],
            'prepared': prepared,
        }

    def _mock_fixture_run(self):
        """Returns (patch_ctx, calls_list). fake_run calls real check() for binding proof."""
        prepared = dict(self._PREPARED)
        submitted = self._make_submitted()
        calls = []

        def fake_run(operator, operation, check, expected=None):
            check()  # real approval/control binding verified here
            calls.append(operation)
            return prepared if operation == 'prepare' else submitted

        return patch('zamal_core.job_browser_fixture.run', side_effect=fake_run), calls

    def _do_prepare(self, task_id):
        """Execute fixture_prepare; return (result, calls)."""
        action = self.propose(task_id, 'job_app_fixture_prepare', {'fixture': 'local-browser-v1'})
        mock_ctx, calls = self._mock_fixture_run()
        with mock_ctx:
            result = self.g.execute(action['id'])
        return result, calls

    def test_prepare_creates_local_fixture_application(self):
        task = self._fresh_task()
        result, calls = self._do_prepare(task['id'])
        self.assertTrue(result['ok'])
        self.assertEqual(result['outcome'], 'APPROVAL_REQUIRED')
        self.assertTrue(result['synthetic_only'])
        self.assertIn('application_id', result)
        self.assertEqual(calls, ['prepare'])
        row = self.s.db.execute(
            'SELECT status, board_type FROM job_applications WHERE id=?',
            (result['application_id'],),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['board_type'], 'local_fixture')
        self.assertEqual(row['status'], 'ready')

    def test_prepare_logs_browser_prepared_audit(self):
        task = self._fresh_task()
        result, _ = self._do_prepare(task['id'])
        ops = [r['operation'] for r in self.s.db.execute(
            'SELECT operation FROM job_app_audit WHERE application_id=?',
            (result['application_id'],),
        ).fetchall()]
        self.assertIn('browser_prepared', ops)

    def test_submit_creates_pending_approval(self):
        task = self._fresh_task()
        result, _ = self._do_prepare(task['id'])
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': result['application_id'], 'acknowledge_gaps': True})
        self.assertEqual(action['approval']['status'], 'PENDING')

    def test_execute_without_approve_denied(self):
        task = self._fresh_task()
        result, _ = self._do_prepare(task['id'])
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': result['application_id'], 'acknowledge_gaps': True})
        with self.assertRaises(Denied):
            self.g.execute(action['id'])

    def test_telegram_approve_resumes_and_saves_evidence(self):
        from zamal_core.telegram_bot import TelegramCallbackHandler
        task = self._fresh_task()
        result, _ = self._do_prepare(task['id'])
        app_id = result['application_id']
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        approval_id = action['approval']['id']
        mock_ctx, calls = self._mock_fixture_run()
        with patch('zamal_core.job_app.JobApplicationWorker', return_value=self.w), mock_ctx:
            handler = TelegramCallbackHandler(self.s)
            cb = f'approval:{approval_id}:APPROVE'
            res = handler.handle('upd-approve-1', 'fixture-owner', cb)
        self.assertTrue(res['ok'])
        self.assertEqual(res.get('choice'), 'APPROVE')
        self.assertEqual(calls, ['submit'])
        row = self.s.db.execute(
            'SELECT status FROM job_applications WHERE id=?', (app_id,),
        ).fetchone()
        self.assertEqual(row['status'], 'submitted')
        ops = [r['operation'] for r in self.s.db.execute(
            'SELECT operation FROM job_app_audit WHERE application_id=?', (app_id,),
        ).fetchall()]
        self.assertIn('browser_prepared', ops)
        self.assertIn('browser_submit_verified', ops)

    def test_telegram_approve_replay_denied(self):
        from zamal_core.telegram_bot import TelegramCallbackHandler
        task = self._fresh_task()
        result, _ = self._do_prepare(task['id'])
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': result['application_id'], 'acknowledge_gaps': True})
        approval_id = action['approval']['id']
        mock_ctx, _ = self._mock_fixture_run()
        handler = TelegramCallbackHandler(self.s)
        cb = f'approval:{approval_id}:APPROVE'
        with patch('zamal_core.job_app.JobApplicationWorker', return_value=self.w), mock_ctx:
            first = handler.handle('upd-first', 'owner', cb)
        self.assertTrue(first['ok'])
        replay = handler.handle('upd-replay', 'owner', cb)
        self.assertFalse(replay['ok'])

    def test_telegram_reject_prevents_submit(self):
        from zamal_core.telegram_bot import TelegramCallbackHandler
        task = self._fresh_task()
        result, _ = self._do_prepare(task['id'])
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': result['application_id'], 'acknowledge_gaps': True})
        approval_id = action['approval']['id']
        handler = TelegramCallbackHandler(self.s)
        cb = f'approval:{approval_id}:REJECT'
        with patch('zamal_core.job_app.JobApplicationWorker', return_value=self.w):
            res = handler.handle('upd-reject', 'fixture-owner', cb)
        self.assertTrue(res['ok'])
        self.assertEqual(res.get('choice'), 'REJECT')
        ap = self.s.approval(approval_id)
        self.assertEqual(ap['status'], 'REJECTED')
        row = self.s.db.execute(
            'SELECT status FROM job_applications WHERE id=?',
            (result['application_id'],),
        ).fetchone()
        self.assertNotEqual(row['status'], 'submitted')

    def test_telegram_edit_leaves_application_unsubmitted(self):
        from zamal_core.telegram_bot import TelegramCallbackHandler
        task = self._fresh_task()
        result, _ = self._do_prepare(task['id'])
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': result['application_id'], 'acknowledge_gaps': True})
        approval_id = action['approval']['id']
        handler = TelegramCallbackHandler(self.s)
        cb = f'approval:{approval_id}:EDIT'
        with patch('zamal_core.job_app.JobApplicationWorker', return_value=self.w):
            res = handler.handle('upd-edit', 'fixture-owner', cb)
        self.assertTrue(res['ok'])
        self.assertEqual(res.get('choice'), 'EDIT')
        row = self.s.db.execute(
            'SELECT status FROM job_applications WHERE id=?',
            (result['application_id'],),
        ).fetchone()
        self.assertNotEqual(row['status'], 'submitted')

    def test_emergency_stop_blocks_fixture_prepare(self):
        self.controls.set('stop_everything', True, actor='test', reason='stop test')
        task = self._fresh_task()
        action = self.propose(task['id'], 'job_app_fixture_prepare', {'fixture': 'local-browser-v1'})
        mock_ctx, _ = self._mock_fixture_run()
        with mock_ctx:
            with self.assertRaises(Denied):
                self.g.execute(action['id'])

    def test_emergency_stop_blocks_fixture_submit_after_approve(self):
        task = self._fresh_task()
        result, _ = self._do_prepare(task['id'])
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': result['application_id'], 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='go')
        self.controls.set('stop_everything', True, actor='test', reason='stop test')
        with self.assertRaises(Denied):
            self.g.execute(action['id'])

    def test_approval_binding_verified_mid_execution(self):
        """check() passed to fixture run is called with real Gate state: action=EXECUTING, approval=CONSUMED."""
        task = self._fresh_task()
        result, _ = self._do_prepare(task['id'])
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': result['application_id'], 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='go')
        binding_checks = []
        submitted = self._make_submitted()

        def fake_run_verify(operator, operation, check, expected=None):
            try:
                check()
                binding_checks.append('ok')
            except Exception as exc:
                binding_checks.append(f'FAIL:{exc}')
            return submitted

        with patch('zamal_core.job_browser_fixture.run', side_effect=fake_run_verify):
            self.g.execute(action['id'])

        self.assertTrue(binding_checks, 'check() was never called by fixture run')
        self.assertEqual(binding_checks, ['ok'] * len(binding_checks),
                         f'Approval binding check failed: {binding_checks}')


# ── H-1 Greenhouse board slug regression tests ────────────────────────────────

class GreenhouseBoardSlugRegressionTests(JobAppFixture):
    """Regression tests for H-1: board slug must come from URL, not company display name."""

    def _make_gh_app_with_url(self, company_display, job_url, job_id='456789'):
        """Create a planned GH application whose job_url may differ from company display name."""
        plan_task = self._fresh_task()
        src_action_id, _ = self._biz_jobs_action(plan_task['id'], job_id=job_id)
        # Override the biz_jobs result with custom company/url
        jobs = [{'id': job_id, 'title': 'Engineer', 'company': company_display,
                  'url': job_url, 'source': 'greenhouse',
                  'requirements_excerpt': 'Python'}]
        from zamal_core.store import encode
        self.s.db.execute(
            'UPDATE actions SET result=? WHERE id=?',
            (encode({'jobs': jobs}), src_action_id),
        )
        payload = {
            'source_action_id': src_action_id,
            'job_id': job_id,
            'cv_file_ref': 'cv.pdf',
            'candidate': {'name': 'Test User', 'skills': ['Python'], 'experience': '3 years'},
        }
        action = self.propose(plan_task['id'], 'job_app_plan', payload)
        result = self.g.execute(action['id'])
        return result['application_id']

    def test_slug_from_url_differs_from_display_name(self):
        """When company display name != URL slug, submission must use the URL slug."""
        company_display = 'Acme Corp'
        url_slug = 'acmecorp'
        job_id = '999001'
        job_url = f'https://boards.greenhouse.io/{url_slug}/jobs/{job_id}'

        app_id = self._make_gh_app_with_url(company_display, job_url, job_id=job_id)
        submit_task = self._fresh_task()
        action = self.propose(submit_task['id'], 'job_app_submit',
                              {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action['approval']['id'], 'APPROVE', actor='owner', note='go')

        submitted_board = []

        def capture_submit(board, gh_job_id, candidate_contact, cv_path, cl_path=None):
            submitted_board.append(board)
            return (200, 'testhash', True, {'id': 'ext-captured'})

        with patch.object(GreenhouseSubmitter, 'submit', side_effect=capture_submit):
            result = self.g.execute(action['id'])

        self.assertTrue(result.get('ok'), f"Expected ok=True, got: {result}")
        self.assertEqual(len(submitted_board), 1, 'submit() was not called exactly once')
        self.assertEqual(submitted_board[0], url_slug,
                         f'Board slug must be URL slug "{url_slug}", not display name "{company_display}"')
        self.assertNotEqual(submitted_board[0], company_display,
                            'Board slug must NOT be the display name')

    def test_malformed_url_fails_closed_no_http_call(self):
        """Malformed or missing Greenhouse URL must fail closed: return BLOCKED, no HTTP call made."""
        plan_task = self._fresh_task()
        src_action_id, _ = self._biz_jobs_action(plan_task['id'], job_id='999002')
        # Override with a malformed URL (no board slug parseable)
        jobs = [{'id': '999002', 'title': 'Engineer', 'company': 'SomeCo',
                  'url': 'https://boards.greenhouse.io/jobs/999002',  # missing board slug segment
                  'source': 'greenhouse',
                  'requirements_excerpt': 'Python'}]
        from zamal_core.store import encode
        self.s.db.execute(
            'UPDATE actions SET result=? WHERE id=?',
            (encode({'jobs': jobs}), src_action_id),
        )
        payload = {
            'source_action_id': src_action_id,
            'job_id': '999002',
            'cv_file_ref': 'cv.pdf',
            'candidate': {'name': 'Test User', 'skills': ['Python'], 'experience': '3 years'},
        }
        action = self.propose(plan_task['id'], 'job_app_plan', payload)
        result = self.g.execute(action['id'])
        app_id = result['application_id']

        submit_task = self._fresh_task()
        action2 = self.propose(submit_task['id'], 'job_app_submit',
                               {'application_id': app_id, 'acknowledge_gaps': True})
        self.s.decide(action2['approval']['id'], 'APPROVE', actor='owner', note='go')

        http_called = []

        def should_not_be_called(*args, **kwargs):
            http_called.append(True)
            return (200, 'shouldnotreach', True, {})

        with patch.object(GreenhouseSubmitter, 'submit', side_effect=should_not_be_called):
            result2 = self.g.execute(action2['id'])

        self.assertEqual(result2['outcome'], 'BLOCKED',
                         f'Expected BLOCKED for malformed URL, got: {result2}')
        self.assertFalse(result2.get('ok', True),
                         'ok must be False/absent when URL is malformed')
        self.assertEqual(http_called, [],
                         'HTTP submit() must NOT be called for malformed URL (fail-closed)')


# ── Greenhouse URL structured parser (ITEM 1 re-hardening) ───────────────────

class GreenhouseURLParserTests(unittest.TestCase):
    """Tests for parse_greenhouse_url() structured validation."""

    def _import(self):
        from zamal_core.job_app import parse_greenhouse_url, GreenhouseURLError
        return parse_greenhouse_url, GreenhouseURLError

    def test_valid_boards_host(self):
        parse_greenhouse_url, _ = self._import()
        slug, job_id = parse_greenhouse_url(
            'https://boards.greenhouse.io/acmecorp/jobs/123456')
        self.assertEqual(slug, 'acmecorp')
        self.assertEqual(job_id, '123456')

    def test_valid_job_boards_host(self):
        parse_greenhouse_url, _ = self._import()
        slug, job_id = parse_greenhouse_url(
            'https://job-boards.greenhouse.io/myco/jobs/999')
        self.assertEqual(slug, 'myco')
        self.assertEqual(job_id, '999')

    def test_valid_app_host(self):
        parse_greenhouse_url, _ = self._import()
        slug, job_id = parse_greenhouse_url(
            'https://app.greenhouse.io/myco/jobs/777')
        self.assertEqual(slug, 'myco')
        self.assertEqual(job_id, '777')

    def test_http_scheme_rejected(self):
        parse_greenhouse_url, GreenhouseURLError = self._import()
        with self.assertRaises(GreenhouseURLError):
            parse_greenhouse_url('http://boards.greenhouse.io/co/jobs/1')

    def test_ftp_scheme_rejected(self):
        parse_greenhouse_url, GreenhouseURLError = self._import()
        with self.assertRaises(GreenhouseURLError):
            parse_greenhouse_url('ftp://boards.greenhouse.io/co/jobs/1')

    def test_lookalike_domain_rejected(self):
        parse_greenhouse_url, GreenhouseURLError = self._import()
        # greenhouse.io embedded in a different domain
        with self.assertRaises(GreenhouseURLError):
            parse_greenhouse_url('https://evil-greenhouse.io/co/jobs/1')

    def test_subdomain_lookalike_rejected(self):
        parse_greenhouse_url, GreenhouseURLError = self._import()
        with self.assertRaises(GreenhouseURLError):
            parse_greenhouse_url('https://boards.greenhouse.io.attacker.com/co/jobs/1')

    def test_credentials_in_url_rejected(self):
        parse_greenhouse_url, GreenhouseURLError = self._import()
        with self.assertRaises(GreenhouseURLError):
            parse_greenhouse_url('https://user:pw@boards.greenhouse.io/co/jobs/1')

    def test_unexpected_port_rejected(self):
        parse_greenhouse_url, GreenhouseURLError = self._import()
        with self.assertRaises(GreenhouseURLError):
            parse_greenhouse_url('https://boards.greenhouse.io:8080/co/jobs/1')

    def test_missing_board_slug_rejected(self):
        parse_greenhouse_url, GreenhouseURLError = self._import()
        # URL path: /jobs/123 — no board slug segment
        with self.assertRaises(GreenhouseURLError):
            parse_greenhouse_url('https://boards.greenhouse.io/jobs/123')

    def test_missing_job_id_rejected(self):
        parse_greenhouse_url, GreenhouseURLError = self._import()
        with self.assertRaises(GreenhouseURLError):
            parse_greenhouse_url('https://boards.greenhouse.io/myco/jobs/')

    def test_slug_with_special_chars_rejected(self):
        parse_greenhouse_url, GreenhouseURLError = self._import()
        with self.assertRaises(GreenhouseURLError):
            parse_greenhouse_url('https://boards.greenhouse.io/my co/jobs/1')

    def test_slug_underscore_hyphen_allowed(self):
        parse_greenhouse_url, _ = self._import()
        slug, job_id = parse_greenhouse_url(
            'https://boards.greenhouse.io/my_co-inc/jobs/42')
        self.assertEqual(slug, 'my_co-inc')
        self.assertEqual(job_id, '42')

    def test_greenhouse_url_error_is_subclass_of_denied(self):
        from zamal_core.job_app import GreenhouseURLError
        from zamal_core.store import Denied
        self.assertTrue(issubclass(GreenhouseURLError, Denied))

    def test_empty_url_rejected(self):
        parse_greenhouse_url, GreenhouseURLError = self._import()
        with self.assertRaises(GreenhouseURLError):
            parse_greenhouse_url('')

    def test_non_string_rejected(self):
        parse_greenhouse_url, GreenhouseURLError = self._import()
        with self.assertRaises(GreenhouseURLError):
            parse_greenhouse_url(None)


if __name__ == '__main__':
    unittest.main()
