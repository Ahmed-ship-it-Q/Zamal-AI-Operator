"""Phase 7 tests: CPC Autonomous Engineering Worker.

Tests cover:
- CpcGitReader (snapshot, changed_files, _parse_status)
- CpcSessionStore (create, complete, active session lookup, concurrency block)
- CpcScopeGuard (validate scope_files, check_scope, directory prefix match)
- CpcTaskPlanner (plan output structure, missing package.json)
- CpcTestRunner (none skips, known command mock, unknown denied, timeout)
- CpcWorker validate (all kinds, invalid payloads, workspace checks)
- CpcWorker gate (full SAFE and PROTECTED flows)
- Phase 7 controls (STOP blocks all, READ_ONLY blocks writes)
- Schema v4 (cpc_coding_sessions table, unique active index)
- CPC read-only preserved (read/inspect still work, write via draft still denied)
- CLI smoke tests
- Concurrent session enforcement
- Completion report structure
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zamal_core.store import Store, Gate, Denied, Policy, Conflict
from zamal_core.cpc_worker import (
    CpcGitReader, CpcSessionStore, CpcScopeGuard, CpcTaskPlanner,
    CpcTestRunner, CpcWorker, build_completion_report,
    ALLOWED_OWNERS, CPC_TEST_COMMANDS, _PHASE7_KINDS,
    _PHASE7_FIELDS, _BLOCKED_SCOPE_PARTS,
)
from zamal_core.mac_worker import OPERATOR_ROOT, CPC_ROOT


# ── Helpers ────────────────────────────────────────────────────────────────────

def _tmp_store():
    """Create an isolated Store in a temp directory."""
    tmp = tempfile.mkdtemp()
    state_dir = Path(tmp) / 'state'
    state_dir.mkdir(mode=0o700)
    store = Store(state_dir / 'operator.sqlite3')
    return store, tmp


def _planned_task(store, *, project='cpc', task_type='test'):
    t = store.create_task(
        f'test-{uuid.uuid4().hex[:6]}', task_type, project,
        f'k-{uuid.uuid4().hex[:8]}',
    )
    store.transition(t['id'], 'UNDERSTOOD', t['revision'])
    t = store.task(t['id'])
    store.transition(t['id'], 'PLANNED', t['revision'])
    return store.task(t['id'])


def _make_approved_action(store, gate, task_id, kind, payload):
    """Propose a PROTECTED action, approve it, return action id."""
    action = gate.propose(task_id, kind, payload, f'k-{uuid.uuid4().hex[:8]}')
    approval_id = action['approval']['id']
    store.decide(approval_id, 'APPROVE', actor='local-os:test', note='')
    return action['id']


# ── CpcGitReaderTests ──────────────────────────────────────────────────────────

class CpcGitReaderTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Init a bare git repo in temp dir
        import subprocess
        subprocess.run(['/usr/bin/git', 'init', '-q', '--initial-branch=main', str(self.root)],
                       check=True, capture_output=True)
        subprocess.run(['/usr/bin/git', 'config', 'user.email', 'test@test.com'],
                       check=True, cwd=str(self.root), capture_output=True)
        subprocess.run(['/usr/bin/git', 'config', 'user.name', 'Test'],
                       check=True, cwd=str(self.root), capture_output=True)
        self.reader = CpcGitReader(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_no_commits(self):
        snap = self.reader.snapshot()
        self.assertEqual(snap['sha'], 'no_commits')
        self.assertIsInstance(snap['dirty_files'], list)
        self.assertIn('timestamp', snap)

    def test_snapshot_with_commit(self):
        import subprocess
        (self.root / 'file.txt').write_text('hello')
        subprocess.run(['/usr/bin/git', 'add', '.'],
                       check=True, cwd=str(self.root), capture_output=True)
        subprocess.run(['/usr/bin/git', 'commit', '-m', 'init', '-q'],
                       check=True, cwd=str(self.root), capture_output=True)
        snap = self.reader.snapshot()
        self.assertNotEqual(snap['sha'], 'no_commits')
        self.assertEqual(len(snap['sha']), 40)
        self.assertEqual(snap['dirty_files'], [])

    def test_changed_files_detects_modification(self):
        import subprocess
        (self.root / 'app.py').write_text('x=1')
        subprocess.run(['/usr/bin/git', 'add', '.'],
                       check=True, cwd=str(self.root), capture_output=True)
        subprocess.run(['/usr/bin/git', 'commit', '-m', 'init', '-q'],
                       check=True, cwd=str(self.root), capture_output=True)
        (self.root / 'app.py').write_text('x=2')  # modify after commit
        changed = self.reader.changed_files()
        self.assertIn('app.py', changed)

    def test_parse_status_rename(self):
        lines = 'R  old.py -> new.py\nM  modified.py\n?? untracked.py'
        files = CpcGitReader._parse_status(lines)
        self.assertIn('new.py', files)
        self.assertIn('modified.py', files)
        self.assertIn('untracked.py', files)
        self.assertNotIn('old.py', files)

    def test_parse_status_empty(self):
        self.assertEqual(CpcGitReader._parse_status(''), [])
        self.assertEqual(CpcGitReader._parse_status(None), [])


# ── CpcSessionStoreTests ───────────────────────────────────────────────────────

class CpcSessionStoreTests(unittest.TestCase):

    def setUp(self):
        self.store, self.tmp_dir = _tmp_store()
        self.sessions = CpcSessionStore(self.store)
        t = _planned_task(self.store)
        self.task_id = t['id']

    def tearDown(self):
        self.store.close()
        import shutil; shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _git_before(self):
        return {'sha': 'abc123', 'dirty_files': [], 'timestamp': time.time()}

    def test_create_session_stores_record(self):
        with self.store.transaction():
            s = self.sessions.create_session(
                task_id=self.task_id,
                owner='claude_code',
                scope_files=['frontend/app.js'],
                git_before=self._git_before(),
                actor='test',
            )
        self.assertEqual(s['owner'], 'claude_code')
        self.assertEqual(s['status'], 'active')
        self.assertEqual(json.loads(s['scope_files']), ['frontend/app.js'])
        self.assertEqual(s['task_id'], self.task_id)

    def test_active_session_for_task_returns_session(self):
        with self.store.transaction():
            self.sessions.create_session(
                task_id=self.task_id, owner='codex',
                scope_files=['backend/main.py'], git_before=self._git_before(),
                actor='test',
            )
        row = self.sessions.active_session_for_task(self.task_id)
        self.assertIsNotNone(row)
        self.assertEqual(row['owner'], 'codex')

    def test_duplicate_active_session_denied(self):
        with self.store.transaction():
            self.sessions.create_session(
                task_id=self.task_id, owner='claude_code',
                scope_files=['a.py'], git_before=self._git_before(),
                actor='test',
            )
        with self.assertRaises(Denied):
            with self.store.transaction():
                self.sessions.create_session(
                    task_id=self.task_id, owner='codex',
                    scope_files=['b.py'], git_before=self._git_before(),
                    actor='test',
                )

    def test_complete_session_updates_status(self):
        with self.store.transaction():
            s = self.sessions.create_session(
                task_id=self.task_id, owner='claude_code',
                scope_files=['app.py'], git_before=self._git_before(),
                actor='test',
            )
        git_after = {'sha': 'def456', 'dirty_files': ['app.py'], 'timestamp': time.time()}
        self.sessions.complete_session(s['id'], git_after, [{'passed': True}], 'completed', 'test')
        completed = self.sessions.session(s['id'])
        self.assertEqual(completed['status'], 'completed')
        self.assertIsNotNone(completed['completed'])

    def test_active_session_none_after_complete(self):
        with self.store.transaction():
            s = self.sessions.create_session(
                task_id=self.task_id, owner='claude_code',
                scope_files=['app.py'], git_before=self._git_before(),
                actor='test',
            )
        self.sessions.complete_session(s['id'], {}, [], 'completed', 'test')
        self.assertIsNone(self.sessions.active_session_for_task(self.task_id))

    def test_session_not_found_raises(self):
        with self.assertRaises(Denied):
            self.sessions.session('nonexistent-id')

    def test_invalid_complete_status_raises(self):
        with self.store.transaction():
            s = self.sessions.create_session(
                task_id=self.task_id, owner='claude_code',
                scope_files=['app.py'], git_before=self._git_before(),
                actor='test',
            )
        with self.assertRaises(ValueError):
            self.sessions.complete_session(s['id'], {}, [], 'invalid', 'test')


# ── CpcScopeGuardTests ────────────────────────────────────────────────────────

class CpcScopeGuardTests(unittest.TestCase):

    def setUp(self):
        self.guard = CpcScopeGuard()

    def test_validate_valid_paths(self):
        result = self.guard.validate_scope_files(
            '["frontend/app.js", "backend/api/routes.py"]'
        )
        self.assertEqual(result, ['frontend/app.js', 'backend/api/routes.py'])

    def test_validate_empty_list_denied(self):
        with self.assertRaises(Denied):
            self.guard.validate_scope_files('[]')

    def test_validate_invalid_json_denied(self):
        with self.assertRaises(Denied):
            self.guard.validate_scope_files('not-json')

    def test_validate_traversal_denied(self):
        with self.assertRaises(Denied):
            self.guard.validate_scope_files('["../zamal_core/store.py"]')

    def test_validate_double_dot_component_denied(self):
        with self.assertRaises(Denied):
            self.guard.validate_scope_files('["frontend/../backend/secret.py"]')

    def test_validate_hidden_component_denied(self):
        with self.assertRaises(Denied):
            self.guard.validate_scope_files('[".env"]')

    def test_validate_node_modules_blocked(self):
        with self.assertRaises(Denied):
            self.guard.validate_scope_files('["node_modules/evil.js"]')

    def test_validate_absolute_path_denied(self):
        with self.assertRaises(Denied):
            self.guard.validate_scope_files('["/etc/passwd"]')

    def test_validate_too_many_files_denied(self):
        big = json.dumps([f'file{i}.py' for i in range(51)])
        with self.assertRaises(Denied):
            self.guard.validate_scope_files(big)

    def test_check_scope_no_violations(self):
        result = self.guard.check_scope(
            current_dirty=['frontend/app.js', 'backend/main.py'],
            before_dirty=[],
            scope_files=['frontend/app.js', 'backend/main.py'],
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['violations'], [])

    def test_check_scope_detects_violation(self):
        result = self.guard.check_scope(
            current_dirty=['frontend/app.js', 'backend/secret_config.py'],
            before_dirty=[],
            scope_files=['frontend/app.js'],
        )
        self.assertFalse(result['ok'])
        self.assertIn('backend/secret_config.py', result['violations'])

    def test_check_scope_preexisting_dirty_not_flagged(self):
        result = self.guard.check_scope(
            current_dirty=['frontend/app.js', 'preexisting.py'],
            before_dirty=['preexisting.py'],  # was already dirty at session start
            scope_files=['frontend/app.js'],
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['violations'], [])

    def test_check_scope_directory_prefix_match(self):
        result = self.guard.check_scope(
            current_dirty=['frontend/components/Header.js', 'frontend/utils/helper.js'],
            before_dirty=[],
            scope_files=['frontend'],
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['violations'], [])

    def test_check_scope_directory_with_trailing_slash(self):
        result = self.guard.check_scope(
            current_dirty=['backend/api/endpoint.py'],
            before_dirty=[],
            scope_files=['backend/api/'],
        )
        self.assertTrue(result['ok'])

    def test_check_scope_empty_new_changes_is_clean(self):
        result = self.guard.check_scope(
            current_dirty=['preexisting.py'],
            before_dirty=['preexisting.py'],
            scope_files=['frontend/app.js'],
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['new_changes'], [])


# ── CpcTaskPlannerTests ────────────────────────────────────────────────────────

class CpcTaskPlannerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_package_json(self, name='test-project', scripts=None):
        pkg = {'name': name, 'version': '1.0.0', 'scripts': scripts or {'test': 'python3 -m tests'}}
        (self.root / 'package.json').write_text(json.dumps(pkg))

    def test_plan_reads_package_json(self):
        self._make_package_json('my-project', {'test': 'pytest'})
        (self.root / 'frontend').mkdir()
        planner = CpcTaskPlanner(self.root)
        result = planner.plan()
        self.assertEqual(result['project']['name'], 'my-project')
        self.assertIn('test', result['project']['scripts'])

    def test_plan_returns_template_fields(self):
        self._make_package_json()
        planner = CpcTaskPlanner(self.root)
        result = planner.plan()
        tpl = result['implementation_plan_template']
        for field in ('actual_problem', 'expected_result', 'affected_area',
                      'do_not_change', 'acceptance_criteria'):
            self.assertIn(field, tpl)
            self.assertIsNone(tpl[field])

    def test_plan_handles_missing_package_json(self):
        planner = CpcTaskPlanner(self.root)
        result = planner.plan()
        self.assertEqual(result['project']['name'], 'unknown')
        self.assertIn('implementation_plan_template', result)

    def test_plan_has_workspace_cpc(self):
        planner = CpcTaskPlanner(self.root)
        result = planner.plan()
        self.assertEqual(result['workspace'], 'cpc')

    def test_plan_lists_approved_test_commands(self):
        planner = CpcTaskPlanner(self.root)
        result = planner.plan()
        self.assertIn('python_test', result['approved_test_commands'])
        self.assertNotIn('none', result['approved_test_commands'])

    def test_plan_includes_git_state(self):
        planner = CpcTaskPlanner(self.root)
        with patch.object(CpcGitReader, 'snapshot', return_value={'sha': 'abc123', 'dirty_files': [], 'timestamp': 1.0}):
            result = planner.plan()
        self.assertEqual(result['git_state']['sha'], 'abc123')


# ── CpcTestRunnerTests ────────────────────────────────────────────────────────

class CpcTestRunnerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runner = CpcTestRunner(self.tmp.name)
        self.check = lambda: None

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_none_skips_tests(self):
        result = self.runner.run('none', self.check)
        self.assertTrue(result['skipped'])
        self.assertIsNone(result['passed'])
        self.assertTrue(result['browser_verification_required'])

    def test_run_unknown_command_denied(self):
        with self.assertRaises(Denied):
            self.runner.run('npm_test', self.check)

    def test_run_python_test_executable_missing_denied(self):
        # _PYTHON3 should exist, but argv[0] check
        with patch('os.access', return_value=False):
            with self.assertRaises(Denied):
                self.runner.run('python_test', self.check)

    def test_run_python_test_with_mock_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = 'OK\n'
        mock_result.stderr = ''
        with patch('subprocess.run', return_value=mock_result):
            with patch('os.access', return_value=True):
                with patch.object(Path, 'is_file', return_value=True):
                    result = self.runner.run('python_test', self.check)
        self.assertTrue(result['passed'])
        self.assertEqual(result['command'], 'python_test')

    def test_run_python_test_with_mock_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = 'FAILED\n'
        mock_result.stderr = ''
        with patch('subprocess.run', return_value=mock_result):
            with patch('os.access', return_value=True):
                with patch.object(Path, 'is_file', return_value=True):
                    result = self.runner.run('python_test', self.check)
        self.assertFalse(result['passed'])

    def test_run_playwright_marks_browser_required(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = 'passed\n'
        mock_result.stderr = ''
        with patch('subprocess.run', return_value=mock_result):
            with patch('os.access', return_value=True):
                with patch.object(Path, 'is_file', return_value=True):
                    result = self.runner.run('playwright_e2e', self.check)
        self.assertTrue(result['browser_verification_required'])

    def test_run_timeout_handled(self):
        with patch('subprocess.run', side_effect=__import__('subprocess').TimeoutExpired('cmd', 120)):
            with patch('os.access', return_value=True):
                with patch.object(Path, 'is_file', return_value=True):
                    result = self.runner.run('python_test', self.check)
        self.assertFalse(result['passed'])
        self.assertIn('timed out', result['error'])


# ── CpcWorkerValidateTests ────────────────────────────────────────────────────

class CpcWorkerValidateTests(unittest.TestCase):

    def setUp(self):
        self.store, self.tmp_dir = _tmp_store()
        self.tmp = tempfile.TemporaryDirectory()
        self.worker = CpcWorker(
            self.store,
            operator=str(OPERATOR_ROOT),
            cpc=self.tmp.name,
        )
        # Ensure temp has minimal git setup
        import subprocess
        subprocess.run(['/usr/bin/git', 'init', '-q', str(self.tmp.name)], capture_output=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        import shutil; shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _task_id(self):
        t = _planned_task(self.store)
        return t['id']

    def test_cpc_plan_valid(self):
        self.worker.validate('cpc_plan', {'workspace': 'cpc'}, 'cpc')

    def test_cpc_plan_wrong_workspace_denied(self):
        with self.assertRaises(Denied):
            self.worker.validate('cpc_plan', {'workspace': 'operator'}, 'cpc')

    def test_cpc_plan_wrong_fields_denied(self):
        with self.assertRaises(Denied):
            self.worker.validate('cpc_plan', {'workspace': 'cpc', 'extra': 'x'}, 'cpc')

    def test_cpc_snapshot_valid(self):
        self.worker.validate('cpc_snapshot', {'workspace': 'cpc'}, 'cpc')

    def test_cpc_snapshot_wrong_workspace(self):
        with self.assertRaises(Denied):
            self.worker.validate('cpc_snapshot', {'workspace': 'operator'}, 'cpc')

    def test_cpc_coding_start_valid(self):
        tid = self._task_id()
        self.worker.validate('cpc_coding_start', {
            'workspace': 'cpc',
            'task_id': tid,
            'scope_files': '["frontend/app.js"]',
            'owner': 'claude_code',
        }, 'cpc')

    def test_cpc_coding_start_bad_owner_denied(self):
        tid = self._task_id()
        with self.assertRaises(Denied):
            self.worker.validate('cpc_coding_start', {
                'workspace': 'cpc',
                'task_id': tid,
                'scope_files': '["frontend/app.js"]',
                'owner': 'human',
            }, 'cpc')

    def test_cpc_coding_start_traversal_scope_denied(self):
        tid = self._task_id()
        with self.assertRaises(Denied):
            self.worker.validate('cpc_coding_start', {
                'workspace': 'cpc',
                'task_id': tid,
                'scope_files': '["../zamal_core/store.py"]',
                'owner': 'claude_code',
            }, 'cpc')

    def test_cpc_coding_start_empty_scope_denied(self):
        tid = self._task_id()
        with self.assertRaises(Denied):
            self.worker.validate('cpc_coding_start', {
                'workspace': 'cpc',
                'task_id': tid,
                'scope_files': '[]',
                'owner': 'claude_code',
            }, 'cpc')

    def test_cpc_scope_check_invalid_session_denied(self):
        with self.assertRaises(Denied):
            self.worker.validate('cpc_scope_check', {
                'session_id': 'nonexistent',
                'workspace': 'cpc',
            }, 'cpc')

    def test_cpc_coding_complete_bad_run_tests_denied(self):
        # First create a session to validate against
        store, tmp_dir = _tmp_store()
        try:
            sessions = CpcSessionStore(store)
            tid = _planned_task(store)['id']
            with store.transaction():
                s = sessions.create_session(tid, 'claude_code', ['app.py'],
                                            {'sha': 'x', 'dirty_files': [], 'timestamp': 1.0}, 'test')
            w = CpcWorker(store, operator=str(OPERATOR_ROOT), cpc=self.tmp.name)
            with self.assertRaises(Denied):
                w.validate('cpc_coding_complete', {
                    'session_id': s['id'],
                    'workspace': 'cpc',
                    'run_tests': 'npm_jest',  # not in allowlist
                }, 'cpc')
        finally:
            store.close()
            import shutil; shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_phase3_kind_passes_to_superclass(self):
        # 'read' should still validate via superclass
        with self.assertRaises(Denied):
            self.worker.validate('read', {'workspace': 'unknown_ws', 'path': 'x'}, 'unknown')

    def test_phase6_kind_passes_to_superclass(self):
        self.worker.validate('service_status', {'service': 'n8n'}, 'operator')


# ── CpcWorkerGateTests ────────────────────────────────────────────────────────

class CpcWorkerGateTests(unittest.TestCase):

    def setUp(self):
        self.store, self.tmp_dir = _tmp_store()
        self.tmp = tempfile.TemporaryDirectory()
        # Init git repo in tmp (acts as CPC root)
        import subprocess
        subprocess.run(['/usr/bin/git', 'init', '-q', str(self.tmp.name)], capture_output=True)
        subprocess.run(['/usr/bin/git', 'config', 'user.email', 'test@t.com'],
                       cwd=self.tmp.name, capture_output=True)
        subprocess.run(['/usr/bin/git', 'config', 'user.name', 'T'],
                       cwd=self.tmp.name, capture_output=True)
        self.worker = CpcWorker(
            self.store,
            operator=str(OPERATOR_ROOT),
            cpc=self.tmp.name,
        )
        self.gate = self.worker.gate()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        import shutil; shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _planned_cpc_task(self):
        return _planned_task(self.store, project='cpc')

    def _planned_op_task(self):
        return _planned_task(self.store, project='operator')

    def test_cpc_plan_executes_safely(self):
        t = self._planned_cpc_task()
        a = self.gate.propose(t['id'], 'cpc_plan', {'workspace': 'cpc'}, f'k-{uuid.uuid4().hex[:8]}')
        self.assertEqual(a['status'], 'PENDING')
        result = self.gate.execute(a['id'])
        self.assertIn('implementation_plan_template', result)
        self.assertEqual(result['workspace'], 'cpc')

    def test_cpc_snapshot_executes_safely(self):
        t = self._planned_cpc_task()
        a = self.gate.propose(t['id'], 'cpc_snapshot', {'workspace': 'cpc'}, f'k-{uuid.uuid4().hex[:8]}')
        result = self.gate.execute(a['id'])
        self.assertIn('sha', result)
        self.assertIn('dirty_files', result)
        self.assertEqual(result['workspace'], 'cpc')

    def test_cpc_coding_start_requires_approval(self):
        t = self._planned_op_task()
        a = self.gate.propose(t['id'], 'cpc_coding_start', {
            'workspace': 'cpc',
            'task_id': t['id'],
            'scope_files': '["frontend/app.js"]',
            'owner': 'claude_code',
        }, f'k-{uuid.uuid4().hex[:8]}')
        self.assertEqual(a['status'], 'PENDING')
        self.assertIsNotNone(a['approval'])
        # Execute without approval → Denied
        with self.assertRaises(Denied):
            self.gate.execute(a['id'])

    def test_cpc_coding_start_approved_creates_session(self):
        t = self._planned_op_task()
        action_id = _make_approved_action(self.store, self.gate, t['id'], 'cpc_coding_start', {
            'workspace': 'cpc',
            'task_id': t['id'],
            'scope_files': '["frontend/app.js"]',
            'owner': 'claude_code',
        })
        result = self.gate.execute(action_id)
        self.assertIn('session_id', result)
        self.assertEqual(result['owner'], 'claude_code')
        self.assertEqual(result['write_mode'], 'OPEN')
        self.assertEqual(result['scope_files'], ['frontend/app.js'])

    def test_concurrent_coding_start_denied(self):
        t1 = self._planned_op_task()
        action_id_1 = _make_approved_action(self.store, self.gate, t1['id'], 'cpc_coding_start', {
            'workspace': 'cpc',
            'task_id': t1['id'],
            'scope_files': '["frontend/app.js"]',
            'owner': 'claude_code',
        })
        self.gate.execute(action_id_1)  # first session created

        # Second session for SAME task — duplicate check is at execute() time (validate())
        t2 = _planned_task(self.store, project='operator')
        action_id_2 = _make_approved_action(self.store, self.gate, t2['id'], 'cpc_coding_start', {
            'workspace': 'cpc',
            'task_id': t1['id'],  # same task_id as first session
            'scope_files': '["backend/main.py"]',
            'owner': 'codex',
        })
        with self.assertRaises(Denied):
            self.gate.execute(action_id_2)

    def test_cpc_scope_check_no_violations(self):
        # Create a session first
        t = self._planned_op_task()
        action_id = _make_approved_action(self.store, self.gate, t['id'], 'cpc_coding_start', {
            'workspace': 'cpc',
            'task_id': t['id'],
            'scope_files': '["frontend/app.js"]',
            'owner': 'claude_code',
        })
        start_result = self.gate.execute(action_id)
        session_id = start_result['session_id']

        # Create a new planned task for scope_check
        t2 = self._planned_cpc_task()
        a2 = self.gate.propose(t2['id'], 'cpc_scope_check', {
            'session_id': session_id,
            'workspace': 'cpc',
        }, f'k-{uuid.uuid4().hex[:8]}')
        result = self.gate.execute(a2['id'])
        self.assertIn('ok', result)
        self.assertEqual(result['session_id'], session_id)
        self.assertIn('violations', result)

    def test_cpc_scope_check_detects_violation(self):
        t = self._planned_op_task()
        action_id = _make_approved_action(self.store, self.gate, t['id'], 'cpc_coding_start', {
            'workspace': 'cpc',
            'task_id': t['id'],
            'scope_files': '["frontend/app.js"]',
            'owner': 'claude_code',
        })
        self.gate.execute(action_id)
        session_row = self.store.db.execute(
            "SELECT id FROM cpc_coding_sessions WHERE task_id=? AND status='active'",
            (t['id'],)
        ).fetchone()
        session_id = session_row['id']

        # Simulate out-of-scope change
        (Path(self.tmp.name) / 'secret.py').write_text('x=1')

        t2 = self._planned_cpc_task()
        a2 = self.gate.propose(t2['id'], 'cpc_scope_check', {
            'session_id': session_id,
            'workspace': 'cpc',
        }, f'k-{uuid.uuid4().hex[:8]}')
        result = self.gate.execute(a2['id'])
        self.assertFalse(result['ok'])
        self.assertIn('secret.py', result['violations'])

    def test_cpc_coding_complete_requires_approval(self):
        # Start a session
        t = self._planned_op_task()
        action_id = _make_approved_action(self.store, self.gate, t['id'], 'cpc_coding_start', {
            'workspace': 'cpc',
            'task_id': t['id'],
            'scope_files': '["app.py"]',
            'owner': 'claude_code',
        })
        start_result = self.gate.execute(action_id)
        session_id = start_result['session_id']

        # Propose cpc_coding_complete (PROTECTED)
        t2 = self._planned_op_task()
        a2 = self.gate.propose(t2['id'], 'cpc_coding_complete', {
            'session_id': session_id,
            'workspace': 'cpc',
            'run_tests': 'none',
        }, f'k-{uuid.uuid4().hex[:8]}')
        self.assertIsNotNone(a2['approval'])
        with self.assertRaises(Denied):
            self.gate.execute(a2['id'])

    def test_cpc_coding_complete_approved_closes_session(self):
        # Start a session
        t = self._planned_op_task()
        action_id = _make_approved_action(self.store, self.gate, t['id'], 'cpc_coding_start', {
            'workspace': 'cpc',
            'task_id': t['id'],
            'scope_files': '["app.py"]',
            'owner': 'codex',
        })
        start_result = self.gate.execute(action_id)
        session_id = start_result['session_id']

        # Complete it
        t2 = self._planned_op_task()
        action_id_2 = _make_approved_action(self.store, self.gate, t2['id'], 'cpc_coding_complete', {
            'session_id': session_id,
            'workspace': 'cpc',
            'run_tests': 'none',
        })
        report = self.gate.execute(action_id_2)
        self.assertIn('task_id', report)
        self.assertIn('coding_owner', report)
        self.assertIn('files_changed', report)
        self.assertIn('tests_run', report)
        self.assertIn('status', report)
        self.assertIn('evidence', report)
        self.assertIn('remaining_blockers', report)
        self.assertIn('next_action', report)
        self.assertIn('unrelated_change_status', report)
        # Session should be closed
        session = CpcSessionStore(self.store).session(session_id)
        self.assertNotEqual(session['status'], 'active')


# ── Phase7ControlsTests ────────────────────────────────────────────────────────

class Phase7ControlsTests(unittest.TestCase):

    def setUp(self):
        self.store, self.tmp_dir = _tmp_store()
        self.tmp = tempfile.TemporaryDirectory()
        import subprocess
        subprocess.run(['/usr/bin/git', 'init', '-q', str(self.tmp.name)], capture_output=True)
        self.worker = CpcWorker(
            self.store,
            operator=str(OPERATOR_ROOT),
            cpc=self.tmp.name,
        )
        self.gate = self.worker.gate()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        import shutil; shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _set_control(self, flag, on):
        from zamal_core.safety import Controls
        Controls(self.store).set(flag, on, actor='test', reason='testing')

    def test_stop_everything_blocks_cpc_plan(self):
        self._set_control('stop_everything', True)
        t = _planned_task(self.store, project='cpc')
        a = self.gate.propose(t['id'], 'cpc_plan', {'workspace': 'cpc'}, f'k-{uuid.uuid4().hex[:8]}')
        with self.assertRaises(Denied):
            self.gate.execute(a['id'])

    def test_stop_everything_blocks_cpc_snapshot(self):
        self._set_control('stop_everything', True)
        t = _planned_task(self.store, project='cpc')
        a = self.gate.propose(t['id'], 'cpc_snapshot', {'workspace': 'cpc'}, f'k-{uuid.uuid4().hex[:8]}')
        with self.assertRaises(Denied):
            self.gate.execute(a['id'])

    def test_stop_everything_blocks_cpc_scope_check(self):
        self._set_control('stop_everything', True)
        with self.assertRaises(Denied):
            from zamal_core.safety import Controls
            Controls(self.store).check('cpc_scope_check')

    def test_read_only_blocks_cpc_coding_start(self):
        self._set_control('read_only_mode', True)
        with self.assertRaises(Denied):
            from zamal_core.safety import Controls
            Controls(self.store).check('cpc_coding_start')

    def test_read_only_allows_cpc_plan(self):
        self._set_control('read_only_mode', True)
        from zamal_core.safety import Controls
        Controls(self.store).check('cpc_plan')  # should NOT raise


# ── SchemaV4Tests ─────────────────────────────────────────────────────────────

class SchemaV4Tests(unittest.TestCase):

    def setUp(self):
        self.store, self.tmp_dir = _tmp_store()

    def tearDown(self):
        self.store.close()
        import shutil; shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_fresh_db_is_v5(self):
        v = self.store.db.execute('PRAGMA user_version').fetchone()[0]
        self.assertEqual(v, 6)

    def test_cpc_coding_sessions_table_exists(self):
        tables = [r[0] for r in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        self.assertIn('cpc_coding_sessions', tables)

    def test_unique_active_session_index_exists(self):
        indexes = [r[0] for r in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()]
        self.assertIn('one_active_session', indexes)

    def test_mac_processes_table_preserved(self):
        tables = [r[0] for r in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        self.assertIn('mac_processes', tables)

    def test_unique_active_session_constraint_enforced(self):
        t = _planned_task(self.store, project='cpc')
        task_id = t['id']
        now = time.time()
        self.store.db.execute(
            'INSERT INTO cpc_coding_sessions(id,task_id,owner,scope_files,status,actor,created) '
            'VALUES(?,?,?,?,?,?,?)',
            (str(uuid.uuid4()), task_id, 'claude_code', '["app.py"]', 'active', 'test', now)
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute(
                'INSERT INTO cpc_coding_sessions(id,task_id,owner,scope_files,status,actor,created) '
                'VALUES(?,?,?,?,?,?,?)',
                (str(uuid.uuid4()), task_id, 'codex', '["b.py"]', 'active', 'test', now)
            )


# ── CpcReadonlyPreservedTests ─────────────────────────────────────────────────

@unittest.skipUnless(sys.platform == 'darwin', 'macOS only')
class CpcReadonlyPreservedTests(unittest.TestCase):

    def setUp(self):
        self.store, self.tmp_dir = _tmp_store()
        self.tmp = tempfile.TemporaryDirectory()
        self.worker = CpcWorker(
            self.store,
            operator=str(OPERATOR_ROOT),
            cpc=str(CPC_ROOT),
        )
        self.gate = self.worker.gate()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
        import shutil; shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_cpc_read_still_works(self):
        t = _planned_task(self.store, project='cpc')
        a = self.gate.propose(t['id'], 'read', {
            'workspace': 'cpc', 'path': 'package.json',
        }, f'k-{uuid.uuid4().hex[:8]}')
        result = self.gate.execute(a['id'])
        self.assertIn('text', result)
        self.assertIn('sha256', result)

    def test_cpc_inspect_still_works(self):
        t = _planned_task(self.store, project='cpc')
        a = self.gate.propose(t['id'], 'inspect', {
            'workspace': 'cpc', 'path': 'frontend',
        }, f'k-{uuid.uuid4().hex[:8]}')
        result = self.gate.execute(a['id'])
        self.assertIn('entries', result)

    def test_cpc_write_via_draft_still_denied(self):
        # draft/prepare_code only allowed in operator workspace; validated at execute time
        t = _planned_task(self.store, project='cpc')
        name = f'evil-{uuid.uuid4().hex[:6]}.py'
        a = self.gate.propose(t['id'], 'draft', {
            'workspace': 'cpc', 'name': name, 'content': 'x',
        }, f'k-{uuid.uuid4().hex[:8]}')
        with self.assertRaises(Denied):
            self.gate.execute(a['id'])

    def test_unapproved_cpc_write_action_denied(self):
        # CPC draft requires operator workspace — denied at execute time
        t = _planned_task(self.store, project='cpc')
        name = f'evil-{uuid.uuid4().hex[:6]}.md'
        a = self.gate.propose(t['id'], 'draft', {
            'workspace': 'cpc', 'name': name, 'content': 'bad',
        }, f'k-{uuid.uuid4().hex[:8]}')
        with self.assertRaises(Denied):
            self.gate.execute(a['id'])


# ── CompletionReportTests ─────────────────────────────────────────────────────

class CompletionReportTests(unittest.TestCase):

    def _session(self, task_id='tid-1', owner='claude_code'):
        return {'id': 'sid-1', 'task_id': task_id, 'owner': owner, 'created': 1.0}

    def _test_result(self, passed, browser=False, skipped=False):
        r = {'command': 'python_test', 'passed': passed,
             'browser_verification_required': browser}
        if skipped:
            r['skipped'] = True
            r['passed'] = None
            r['browser_verification_required'] = True
        return r

    def _scope_ok(self):
        return {'ok': True, 'new_changes': ['app.py'], 'violations': [],
                'scope_files': ['app.py'], 'before_dirty_count': 0}

    def _scope_fail(self, violations=None):
        v = violations or ['secret.py']
        return {'ok': False, 'new_changes': ['app.py', 'secret.py'], 'violations': v,
                'scope_files': ['app.py'], 'before_dirty_count': 0}

    def test_pass_status_when_scope_ok_tests_pass(self):
        report = build_completion_report(self._session(), self._scope_ok(),
                                         self._test_result(True))
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['unrelated_change_status'], 'CLEAN')
        self.assertEqual(report['remaining_blockers'], [])

    def test_fail_status_when_scope_violation(self):
        report = build_completion_report(self._session(), self._scope_fail(),
                                         self._test_result(True))
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['unrelated_change_status'], 'VIOLATION')
        self.assertTrue(len(report['remaining_blockers']) > 0)

    def test_fail_status_when_test_fails(self):
        report = build_completion_report(self._session(), self._scope_ok(),
                                         self._test_result(False))
        self.assertEqual(report['status'], 'FAIL')

    def test_not_verified_when_tests_skipped(self):
        report = build_completion_report(self._session(), self._scope_ok(),
                                         self._test_result(None, skipped=True))
        self.assertEqual(report['status'], 'NOT_VERIFIED')

    def test_not_verified_when_browser_required(self):
        report = build_completion_report(self._session(), self._scope_ok(),
                                         self._test_result(True, browser=True))
        self.assertEqual(report['status'], 'NOT_VERIFIED')

    def test_report_has_all_required_fields(self):
        report = build_completion_report(self._session(), self._scope_ok(),
                                         self._test_result(True))
        for field in ('task_id', 'session_id', 'coding_owner', 'files_changed',
                      'unrelated_change_status', 'violations', 'scope_files',
                      'tests_run', 'status', 'evidence', 'remaining_blockers', 'next_action'):
            self.assertIn(field, report)


# ── Phase7PolicyTests ─────────────────────────────────────────────────────────

class Phase7PolicyTests(unittest.TestCase):

    def setUp(self):
        self.policy = Policy()

    def test_cpc_plan_is_safe(self):
        self.assertFalse(self.policy.requires_approval('cpc_plan'))

    def test_cpc_snapshot_is_safe(self):
        self.assertFalse(self.policy.requires_approval('cpc_snapshot'))

    def test_cpc_scope_check_is_safe(self):
        self.assertFalse(self.policy.requires_approval('cpc_scope_check'))

    def test_cpc_coding_start_is_protected(self):
        self.assertTrue(self.policy.requires_approval('cpc_coding_start'))

    def test_cpc_coding_complete_is_protected(self):
        self.assertTrue(self.policy.requires_approval('cpc_coding_complete'))


# ── Phase7CliTests ─────────────────────────────────────────────────────────────

@unittest.skipUnless(sys.platform == 'darwin', 'macOS only')
class Phase7CliTests(unittest.TestCase):
    """Smoke tests for Phase 7 CLI commands via direct module calls (not subprocess)."""

    def setUp(self):
        self.store, self.tmp_dir = _tmp_store()

    def tearDown(self):
        self.store.close()
        import shutil; shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_schema_version_is_5(self):
        v = self.store.db.execute('PRAGMA user_version').fetchone()[0]
        self.assertEqual(v, 6)

    def test_cpc_sessions_returns_list(self):
        sessions = CpcSessionStore(self.store)
        # With no sessions, result is empty
        rows = self.store.db.execute('SELECT * FROM cpc_coding_sessions').fetchall()
        self.assertIsInstance(rows, list)

    def test_cpc_plan_cli_structure(self):
        planner = CpcTaskPlanner()
        result = planner.plan()
        self.assertIn('implementation_plan_template', result)
        self.assertEqual(result['workspace'], 'cpc')

    def test_init_command_smoke(self):
        # Verify Store opens cleanly and reports v5
        s = Store(Path(self.tmp_dir) / 'state' / 'operator.sqlite3')
        version = s.db.execute('PRAGMA user_version').fetchone()[0]
        integrity = s.db.execute('PRAGMA integrity_check').fetchone()[0]
        s.close()
        self.assertEqual(version, 6)
        self.assertEqual(integrity, 'ok')


if __name__ == '__main__':
    unittest.main()
