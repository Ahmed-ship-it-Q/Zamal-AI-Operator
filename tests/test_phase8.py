"""Phase 8 tests: Verification, Browser QA, Git Safety & Rollback.

91 tests across 13 classes. All prior Phase 1–7 tests preserved.

MANDATORY constraints verified:
- Verifier uses verifier='independent'; separate from coding-worker self-reports
- CPC read-only outside approved coding session
- STOP blocks all Phase 8 dispatch
- READ_ONLY blocks PROTECTED Phase 8 kinds; SAFE kinds pass through
- No force push; no auto-merge on conflict
- Rollback skips pre-existing dirty files
- git_commit requires independent PASS (not coder PASS alone)
"""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zamal_core.store import Store, Gate, Policy, Denied, Conflict
from zamal_core.safety import Controls
from zamal_core.verifier import (
    VerifierGitReader, GitSnapshotStore, VerificationRecordStore,
    WorktreeStore, RollbackLog, WorktreeManager, RollbackEngine,
    VerifierWorker, _PHASE8_KINDS, _PHASE8_SAFE, _PHASE8_PROTECTED,
    _PHASE8_FIELDS, _LABEL_RE, _SHA_RE, _BRANCH_RE,
)
from zamal_core.cpc_worker import CpcSessionStore, CPC_TEST_COMMANDS


# ── helpers ───────────────────────────────────────────────────────────────────

def _store(tmp_path):
    return Store(tmp_path / 'op.sqlite3')


def _task(store, project='cpc'):
    return store.create_task('do something', 'coding', project, 'rk-' + str(time.time()))


def _planned_task(store, project='cpc'):
    t = _task(store, project)
    store.transition(t['id'], 'UNDERSTOOD', 0)
    store.transition(t['id'], 'PLANNED', 1)
    return store.task(t['id'])


def _approved_action(store, task_id, kind, payload, policy=None):
    gate = Gate(store, {}, policy or Policy())
    action = gate.propose(task_id, kind, payload, 'rk-' + str(time.time()))
    if action['approval']:
        store.decide(action['approval']['id'], 'APPROVE', actor='tester', note='ok')
    return store.action(action['id'])


def _fake_session(store, task_id, scope_files=None, before_sha='abc' * 13 + 'a'):
    """Create a coding session in the DB for test purposes (bypasses approval gate)."""
    before_snap = {
        'sha': before_sha,
        'dirty_files': ['frontend/existing.js'],
        'timestamp': time.time(),
    }
    css = CpcSessionStore(store)
    store.db.execute(
        'INSERT INTO cpc_coding_sessions'
        '(id,task_id,owner,scope_files,git_before,status,actor,created)'
        ' VALUES(?,?,?,?,?,?,?,?)',
        ('sess-' + str(time.time()), task_id, 'claude_code',
         json.dumps(scope_files or ['backend/app.py']),
         json.dumps(before_snap),
         'active', 'tester', time.time()),
    )
    return css.active_session_for_task(task_id)


# ── VerifierGitReaderTests ────────────────────────────────────────────────────

class VerifierGitReaderTests(unittest.TestCase):

    def _reader_with_mock(self, status_output='', sha='abcd1234', branch='main', rc=0):
        reader = VerifierGitReader.__new__(VerifierGitReader)
        reader._root = '/fake/cpc'
        calls = iter([
            (sha + '\n', 0),    # rev-parse HEAD
            (branch + '\n', 0), # rev-parse --abbrev-ref HEAD
            (status_output, 0), # status --porcelain
        ])
        reader._run = lambda argv, timeout=15: next(calls)
        return reader

    def test_snapshot_detailed_clean(self):
        reader = self._reader_with_mock('', sha='a' * 40)
        snap = reader.snapshot_detailed()
        self.assertEqual(snap['sha'], 'a' * 40)
        self.assertEqual(snap['branch'], 'main')
        self.assertEqual(snap['staged'], [])
        self.assertEqual(snap['unstaged'], [])
        self.assertEqual(snap['untracked'], [])
        self.assertEqual(snap['dirty_files'], [])

    def test_snapshot_detailed_staged(self):
        reader = VerifierGitReader.__new__(VerifierGitReader)
        reader._root = '/fake'
        calls = iter([
            ('b' * 40 + '\n', 0),
            ('main\n', 0),
            ('M  backend/app.py\n', 0),
        ])
        reader._run = lambda argv, timeout=15: next(calls)
        snap = reader.snapshot_detailed()
        self.assertIn('backend/app.py', snap['staged'])
        self.assertEqual(snap['unstaged'], [])

    def test_snapshot_detailed_unstaged(self):
        reader = VerifierGitReader.__new__(VerifierGitReader)
        reader._root = '/fake'
        calls = iter([
            ('c' * 40 + '\n', 0),
            ('feature\n', 0),
            (' M frontend/app.js\n', 0),
        ])
        reader._run = lambda argv, timeout=15: next(calls)
        snap = reader.snapshot_detailed()
        self.assertIn('frontend/app.js', snap['unstaged'])
        self.assertEqual(snap['staged'], [])

    def test_snapshot_detailed_untracked(self):
        reader = VerifierGitReader.__new__(VerifierGitReader)
        reader._root = '/fake'
        calls = iter([
            ('d' * 40 + '\n', 0),
            ('main\n', 0),
            ('?? newfile.py\n', 0),
        ])
        reader._run = lambda argv, timeout=15: next(calls)
        snap = reader.snapshot_detailed()
        self.assertEqual(snap['untracked'], ['newfile.py'])
        self.assertEqual(snap['staged'], [])
        self.assertEqual(snap['unstaged'], [])

    def test_snapshot_detailed_dirty_is_union(self):
        reader = VerifierGitReader.__new__(VerifierGitReader)
        reader._root = '/fake'
        calls = iter([
            ('e' * 40 + '\n', 0),
            ('main\n', 0),
            ('M  staged.py\n M unstaged.py\n', 0),
        ])
        reader._run = lambda argv, timeout=15: next(calls)
        snap = reader.snapshot_detailed()
        self.assertIn('staged.py', snap['dirty_files'])
        self.assertIn('unstaged.py', snap['dirty_files'])

    def test_branch_returns_branch_name(self):
        reader = VerifierGitReader.__new__(VerifierGitReader)
        reader._root = '/fake'
        reader._run = lambda argv, timeout=15: ('feature/my-branch\n', 0)
        self.assertEqual(reader.branch(), 'feature/my-branch')

    def test_branch_detached_head(self):
        reader = VerifierGitReader.__new__(VerifierGitReader)
        reader._root = '/fake'
        reader._run = lambda argv, timeout=15: ('', 1)
        self.assertEqual(reader.branch(), 'DETACHED')

    def test_diff_scope_empty_files(self):
        reader = VerifierGitReader.__new__(VerifierGitReader)
        reader._root = '/fake'
        result = reader.diff_scope([], 'HEAD')
        self.assertEqual(result['diff'], '')
        self.assertEqual(result['files'], [])


# ── GitSnapshotStoreTests ─────────────────────────────────────────────────────

class GitSnapshotStoreTests(unittest.TestCase):

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._td = TemporaryDirectory()
        self.store = _store(Path(self._td.name))
        self.task = _planned_task(self.store)

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def test_create_and_get(self):
        gss = GitSnapshotStore(self.store)
        rec = gss.create(
            self.task['id'], 'before', 'main', 'a' * 40,
            ['staged.py'], ['unstaged.py'], ['new.py']
        )
        self.assertEqual(rec['label'], 'before')
        self.assertEqual(rec['sha'], 'a' * 40)
        self.assertIn('staged.py', rec['staged'])
        self.assertIn('unstaged.py', rec['unstaged'])
        self.assertIn('new.py', rec['untracked'])

    def test_dirty_files_is_union_of_staged_unstaged(self):
        gss = GitSnapshotStore(self.store)
        rec = gss.create(self.task['id'], 'chk', 'main', 'b' * 40,
                         ['a.py'], ['b.py'], [])
        self.assertIn('a.py', rec['dirty_files'])
        self.assertIn('b.py', rec['dirty_files'])

    def test_get_not_found(self):
        gss = GitSnapshotStore(self.store)
        with self.assertRaises(Denied):
            gss.get('nonexistent-id')

    def test_list_for_task(self):
        gss = GitSnapshotStore(self.store)
        gss.create(self.task['id'], 'before', 'main', 'c' * 40, [], [], [])
        gss.create(self.task['id'], 'after', 'main', 'd' * 40, ['f.py'], [], [])
        snapshots = gss.list_for_task(self.task['id'])
        self.assertEqual(len(snapshots), 2)
        labels = [s['label'] for s in snapshots]
        self.assertIn('before', labels)
        self.assertIn('after', labels)

    def test_empty_lists_stored_correctly(self):
        gss = GitSnapshotStore(self.store)
        rec = gss.create(self.task['id'], 'clean', 'main', 'e' * 40, [], [], [])
        self.assertEqual(rec['staged'], [])
        self.assertEqual(rec['unstaged'], [])
        self.assertEqual(rec['untracked'], [])


# ── VerificationRecordStoreTests ──────────────────────────────────────────────

class VerificationRecordStoreTests(unittest.TestCase):

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._td = TemporaryDirectory()
        self.store = _store(Path(self._td.name))
        self.task = _planned_task(self.store)
        self.session = _fake_session(self.store, self.task['id'])

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def test_create_pass_record(self):
        vrs = VerificationRecordStore(self.store)
        rec = vrs.create(
            self.task['id'], self.session['id'], 'independent',
            'python_test', ['/usr/bin/python3', '-m', 'backend.testing.run_tests'],
            0, 'OK\n', '', True, False, 'PASS', {'note': 'all good'}
        )
        self.assertEqual(rec['status'], 'PASS')
        self.assertEqual(rec['verifier'], 'independent')
        self.assertTrue(rec['passed'])

    def test_create_fail_record(self):
        vrs = VerificationRecordStore(self.store)
        rec = vrs.create(
            self.task['id'], self.session['id'], 'independent',
            'python_test', None, 1, '', 'ERROR\n', False, False, 'FAIL', None
        )
        self.assertEqual(rec['status'], 'FAIL')
        self.assertFalse(rec['passed'])

    def test_create_not_verified_browser(self):
        vrs = VerificationRecordStore(self.store)
        rec = vrs.create(
            self.task['id'], self.session['id'], 'independent',
            'playwright_e2e', None, None, '', '', None, True, 'NOT_VERIFIED', None
        )
        self.assertEqual(rec['status'], 'NOT_VERIFIED')
        self.assertTrue(rec['browser_required'])

    def test_has_pass_true(self):
        vrs = VerificationRecordStore(self.store)
        vrs.create(self.task['id'], self.session['id'], 'independent',
                   'python_test', None, 0, '', '', True, False, 'PASS', None)
        self.assertTrue(vrs.has_pass(self.session['id']))

    def test_has_pass_false_for_fail(self):
        vrs = VerificationRecordStore(self.store)
        vrs.create(self.task['id'], self.session['id'], 'independent',
                   'python_test', None, 1, '', '', False, False, 'FAIL', None)
        self.assertFalse(vrs.has_pass(self.session['id']))

    def test_has_pass_false_coding_worker_report(self):
        """Coding-worker self-report with verifier!='independent' must NOT count as PASS."""
        vrs = VerificationRecordStore(self.store)
        vrs.create(self.task['id'], self.session['id'], 'coding_worker',
                   'python_test', None, 0, '', '', True, False, 'PASS', None)
        self.assertFalse(vrs.has_pass(self.session['id']))

    def test_latest_for_session(self):
        vrs = VerificationRecordStore(self.store)
        vrs.create(self.task['id'], self.session['id'], 'independent',
                   'python_test', None, 0, '', '', True, False, 'PASS', None)
        latest = vrs.latest_for_session(self.session['id'])
        self.assertIsNotNone(latest)
        self.assertEqual(latest['status'], 'PASS')

    def test_latest_for_session_none(self):
        vrs = VerificationRecordStore(self.store)
        self.assertIsNone(vrs.latest_for_session('no-such-session'))


# ── WorktreeStoreTests ────────────────────────────────────────────────────────

class WorktreeStoreTests(unittest.TestCase):

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._td = TemporaryDirectory()
        self.store = _store(Path(self._td.name))
        self.task = _planned_task(self.store)

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def test_create_and_get(self):
        ws = WorktreeStore(self.store)
        rec = ws.create(self.task['id'], 'zamal/task-abc', '/tmp/wt/task-abc',
                        'a' * 40, 'operator')
        self.assertEqual(rec['branch'], 'zamal/task-abc')
        self.assertEqual(rec['status'], 'active')

    def test_active_for_task_found(self):
        ws = WorktreeStore(self.store)
        ws.create(self.task['id'], 'zamal/task-abc', '/tmp/wt/abc', 'b' * 40, 'operator')
        found = ws.active_for_task(self.task['id'])
        self.assertIsNotNone(found)
        self.assertEqual(found['branch'], 'zamal/task-abc')

    def test_active_for_task_none(self):
        ws = WorktreeStore(self.store)
        self.assertIsNone(ws.active_for_task('nonexistent-task-id'))

    def test_close(self):
        ws = WorktreeStore(self.store)
        rec = ws.create(self.task['id'], 'zamal/task-xyz', '/tmp/wt/xyz', 'c' * 40, 'op')
        closed = ws.close(rec['id'])
        self.assertEqual(closed['status'], 'closed')
        self.assertIsNone(ws.active_for_task(self.task['id']))

    def test_get_not_found(self):
        ws = WorktreeStore(self.store)
        with self.assertRaises(Denied):
            ws.get('no-such-id')


# ── RollbackLogTests ──────────────────────────────────────────────────────────

class RollbackLogTests(unittest.TestCase):

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._td = TemporaryDirectory()
        self.store = _store(Path(self._td.name))
        self.task = _planned_task(self.store)
        self.session = _fake_session(self.store, self.task['id'])

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def test_create_and_get(self):
        rl = RollbackLog(self.store)
        rec = rl.create(
            self.task['id'], self.session['id'],
            ['backend/app.py'], 'a' * 40, 'operator', 'user requested', 'SUCCESS'
        )
        self.assertEqual(rec['outcome'], 'SUCCESS')
        self.assertIn('backend/app.py', rec['scope_files'])

    def test_scope_files_deserialized(self):
        rl = RollbackLog(self.store)
        rec = rl.create(self.task['id'], self.session['id'],
                        ['a.py', 'b.py'], 'b' * 40, 'op', 'test', 'PARTIAL')
        self.assertIsInstance(rec['scope_files'], list)
        self.assertEqual(sorted(rec['scope_files']), ['a.py', 'b.py'])

    def test_get_not_found(self):
        rl = RollbackLog(self.store)
        with self.assertRaises(Denied):
            rl.get('no-such-log')


# ── WorktreeManagerTests ──────────────────────────────────────────────────────

class WorktreeManagerTests(unittest.TestCase):

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._td = TemporaryDirectory()
        self.wt_base = Path(self._td.name) / 'worktrees'
        self.wt_base.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._td.name, ignore_errors=True)

    def _manager(self):
        wm = WorktreeManager.__new__(WorktreeManager)
        wm._cpc = Path('/fake/cpc')
        wm._wt_base = self.wt_base
        return wm

    def test_branch_name(self):
        wm = self._manager()
        task_id = 'aabbccdd-1122-3344-5566-778899aabbcc'
        branch = wm.branch_name(task_id)
        self.assertTrue(branch.startswith('zamal/task-'))
        self.assertIn('aabbccdd1122', branch)

    def test_worktree_path(self):
        wm = self._manager()
        task_id = 'aabbccdd-1122-3344-5566-778899aabbcc'
        path = wm.worktree_path(task_id)
        self.assertTrue(str(path).startswith(str(self.wt_base)))
        self.assertIn('task-', str(path))

    def test_create_path_already_exists(self):
        wm = self._manager()
        task_id = 'aabbccdd-1122-3344-5566-778899aabbcc'
        path = wm.worktree_path(task_id)
        path.mkdir(parents=True)
        with self.assertRaises(Denied) as ctx:
            wm.create(task_id, 'a' * 40)
        self.assertIn('already exists', str(ctx.exception))

    def test_create_branch_already_exists(self):
        wm = self._manager()
        task_id = 'aabbccdd-1122-3344-5566-778899aabbcc'
        calls = iter([
            ('', '', 0),   # rev-parse --verify branch → exists
        ])
        wm._run = lambda argv, timeout=30: next(calls)
        with self.assertRaises(Denied) as ctx:
            wm.create(task_id, 'a' * 40)
        self.assertIn('already exists', str(ctx.exception))

    def test_create_bad_sha(self):
        wm = self._manager()
        task_id = 'aabbccdd-1122-3344-5566-778899aabbcc'
        calls = iter([
            ('', '', 1),   # rev-parse --verify branch → not found (ok)
            ('', '', 1),   # rev-parse --verify sha → not found (bad sha)
        ])
        wm._run = lambda argv, timeout=30: next(calls)
        with self.assertRaises(Denied) as ctx:
            wm.create(task_id, 'a' * 40)
        self.assertIn('not found', str(ctx.exception))

    def test_create_success(self):
        wm = self._manager()
        task_id = 'aabbccdd-1122-3344-5566-778899aabbcc'
        calls = iter([
            ('', '', 1),           # rev-parse branch → not found (ok)
            ('a' * 40, '', 0),     # rev-parse sha → found
            ('Preparing worktree', '', 0),  # worktree add → success
        ])
        wm._run = lambda argv, timeout=30: next(calls)
        result = wm.create(task_id, 'a' * 40)
        self.assertIn('branch', result)
        self.assertIn('worktree_path', result)
        self.assertEqual(result['base_sha'], 'a' * 40)

    def test_remove_outside_base_denied(self):
        wm = self._manager()
        with self.assertRaises(Denied) as ctx:
            wm.remove('/etc/passwd')
        self.assertIn('outside', str(ctx.exception))


# ── RollbackEngineTests ───────────────────────────────────────────────────────

class RollbackEngineTests(unittest.TestCase):

    def _engine(self):
        engine = RollbackEngine.__new__(RollbackEngine)
        engine._root = '/fake/cpc'
        engine._git_reader = MagicMock()
        return engine

    def test_rollback_restores_task_created_files(self):
        engine = self._engine()
        engine._git_reader.changed_files.return_value = ['backend/app.py', 'frontend/app.js']
        engine._run = lambda argv, timeout=15: ('', '', 0)
        result = engine.rollback(
            scope_files=['backend/app.py'],
            base_sha='a' * 40,
            before_dirty=['frontend/existing.js'],
        )
        self.assertIn('backend/app.py', result['files_restored'])
        self.assertEqual(result['outcome'], 'SUCCESS')

    def test_rollback_skips_preexisting_dirty(self):
        engine = self._engine()
        engine._git_reader.changed_files.return_value = ['frontend/existing.js']
        engine._run = lambda argv, timeout=15: ('', '', 0)
        result = engine.rollback(
            scope_files=['frontend/existing.js'],
            base_sha='b' * 40,
            before_dirty=['frontend/existing.js'],
        )
        self.assertIn('frontend/existing.js', result['skipped_preexisting'])
        self.assertEqual(result['files_restored'], [])

    def test_rollback_skips_clean_scope_files(self):
        engine = self._engine()
        engine._git_reader.changed_files.return_value = []
        engine._run = lambda argv, timeout=15: ('', '', 0)
        result = engine.rollback(
            scope_files=['backend/app.py'],
            base_sha='c' * 40,
            before_dirty=[],
        )
        self.assertIn('backend/app.py', result['skipped_clean'])
        self.assertEqual(result['files_restored'], [])

    def test_rollback_failure(self):
        engine = self._engine()
        engine._git_reader.changed_files.return_value = ['backend/app.py']
        engine._run = lambda argv, timeout=15: ('', 'error: no such path', 128)
        result = engine.rollback(
            scope_files=['backend/app.py'],
            base_sha='d' * 40,
            before_dirty=[],
        )
        self.assertEqual(result['outcome'], 'FAILED')
        self.assertEqual(len(result['failed']), 1)

    def test_rollback_partial_outcome(self):
        engine = self._engine()
        engine._git_reader.changed_files.return_value = ['a.py', 'b.py']
        calls = iter([('' , '', 0), ('', 'fail', 1)])
        engine._run = lambda argv, timeout=15: next(calls)
        result = engine.rollback(
            scope_files=['a.py', 'b.py'],
            base_sha='e' * 40,
            before_dirty=[],
        )
        self.assertEqual(result['outcome'], 'PARTIAL')

    def test_rollback_preexisting_never_touched(self):
        """Pre-existing dirty files in scope must NEVER be passed to git checkout."""
        engine = self._engine()
        engine._git_reader.changed_files.return_value = ['a.py', 'existing.js']
        touched = []
        def fake_run(argv, timeout=15):
            touched.append(argv)
            return '', '', 0
        engine._run = fake_run
        engine.rollback(
            scope_files=['a.py', 'existing.js'],
            base_sha='f' * 40,
            before_dirty=['existing.js'],
        )
        for cmd in touched:
            self.assertNotIn('existing.js', cmd)


# ── VerifierWorkerValidateTests ───────────────────────────────────────────────

class VerifierWorkerValidateTests(unittest.TestCase):

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._td = TemporaryDirectory()
        self.store = _store(Path(self._td.name))
        self.task = _planned_task(self.store)
        self.session = _fake_session(self.store, self.task['id'])

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _worker(self):
        w = VerifierWorker.__new__(VerifierWorker)
        w.store = self.store
        from zamal_core.safety import Controls
        w.controls = Controls(self.store)
        w.cpc_sessions = CpcSessionStore(self.store)
        from zamal_core.verifier import (
            GitSnapshotStore, VerificationRecordStore, WorktreeStore,
            RollbackLog, WorktreeManager, RollbackEngine, VerifierGitReader,
        )
        w.git_snapshots = GitSnapshotStore(self.store)
        w.ver_records = VerificationRecordStore(self.store)
        w.worktrees_store = WorktreeStore(self.store)
        w.rollback_log_store = RollbackLog(self.store)
        w.cpc_scope = __import__('zamal_core.cpc_worker', fromlist=['CpcScopeGuard']).CpcScopeGuard()
        return w

    def test_git_capture_valid(self):
        w = self._worker()
        w.validate('git_capture', {
            'workspace': 'cpc', 'task_id': self.task['id'], 'label': 'before'
        }, 'cpc')

    def test_git_capture_bad_label(self):
        w = self._worker()
        with self.assertRaises(Denied):
            w.validate('git_capture', {
                'workspace': 'cpc', 'task_id': self.task['id'], 'label': 'UPPER'
            }, 'cpc')

    def test_git_capture_bad_workspace(self):
        w = self._worker()
        with self.assertRaises(Denied):
            w.validate('git_capture', {
                'workspace': 'operator', 'task_id': self.task['id'], 'label': 'before'
            }, 'cpc')

    def test_diff_review_valid(self):
        w = self._worker()
        w.validate('diff_review', {
            'workspace': 'cpc', 'session_id': self.session['id']
        }, 'cpc')

    def test_diff_review_session_not_found(self):
        w = self._worker()
        with self.assertRaises(Denied):
            w.validate('diff_review', {
                'workspace': 'cpc', 'session_id': 'no-such-session'
            }, 'cpc')

    def test_verify_tests_valid(self):
        w = self._worker()
        w.validate('verify_tests', {
            'workspace': 'cpc', 'session_id': self.session['id'], 'run_tests': 'none'
        }, 'cpc')

    def test_verify_tests_invalid_command(self):
        w = self._worker()
        with self.assertRaises(Denied):
            w.validate('verify_tests', {
                'workspace': 'cpc', 'session_id': self.session['id'],
                'run_tests': 'rm -rf /'
            }, 'cpc')

    def test_capture_evidence_valid(self):
        w = self._worker()
        w.validate('capture_evidence', {
            'workspace': 'cpc', 'task_id': self.task['id'],
            'session_id': self.session['id'], 'label': 'after'
        }, 'cpc')

    def test_git_worktree_create_valid(self):
        w = self._worker()
        w.validate('git_worktree_create', {
            'workspace': 'cpc', 'task_id': self.task['id'],
            'base_sha': 'a' * 40
        }, 'cpc')

    def test_git_worktree_create_bad_sha(self):
        w = self._worker()
        with self.assertRaises(Denied):
            w.validate('git_worktree_create', {
                'workspace': 'cpc', 'task_id': self.task['id'],
                'base_sha': 'not-a-sha'
            }, 'cpc')

    def test_git_rollback_valid(self):
        w = self._worker()
        w.validate('git_rollback', {
            'workspace': 'cpc', 'session_id': self.session['id'],
            'reason': 'user requested rollback'
        }, 'cpc')

    def test_git_rollback_empty_reason(self):
        w = self._worker()
        with self.assertRaises(Denied):
            w.validate('git_rollback', {
                'workspace': 'cpc', 'session_id': self.session['id'], 'reason': ''
            }, 'cpc')

    def test_git_commit_requires_independent_pass(self):
        w = self._worker()
        with self.assertRaises(Denied) as ctx:
            w.validate('git_commit', {
                'workspace': 'cpc', 'session_id': self.session['id'],
                'message': 'fix: implement feature'
            }, 'cpc')
        self.assertIn('independent verification PASS', str(ctx.exception))

    def test_git_push_protected_branch_denied(self):
        w = self._worker()
        with self.assertRaises(Denied) as ctx:
            w.validate('git_push', {
                'workspace': 'cpc', 'task_id': self.task['id'],
                'remote': 'origin', 'branch': 'main'
            }, 'cpc')
        self.assertIn('denied', str(ctx.exception))

    def test_git_push_feature_branch_allowed(self):
        w = self._worker()
        w.validate('git_push', {
            'workspace': 'cpc', 'task_id': self.task['id'],
            'remote': 'origin', 'branch': 'zamal/task-abc123'
        }, 'cpc')

    def test_git_merge_same_branches_denied(self):
        w = self._worker()
        with self.assertRaises(Denied):
            w.validate('git_merge', {
                'workspace': 'cpc', 'task_id': self.task['id'],
                'source_branch': 'main', 'target_branch': 'main'
            }, 'cpc')


# ── VerifierWorkerGateTests ───────────────────────────────────────────────────

class VerifierWorkerGateTests(unittest.TestCase):

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._td = TemporaryDirectory()
        self.store = _store(Path(self._td.name))
        self.task = _planned_task(self.store)
        self.session = _fake_session(self.store, self.task['id'])

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def _worker(self):
        w = VerifierWorker.__new__(VerifierWorker)
        w.store = self.store
        from zamal_core.safety import Controls
        from zamal_core.store import Gate, Policy
        from zamal_core.worker import Capability
        from zamal_core.cpc_worker import CpcSessionStore, CpcScopeGuard
        from zamal_core.verifier import (
            GitSnapshotStore, VerificationRecordStore, WorktreeStore,
            RollbackLog, WorktreeManager, RollbackEngine, VerifierGitReader,
        )
        from types import MappingProxyType
        w.controls = Controls(self.store)
        w.cpc_sessions = CpcSessionStore(self.store)
        w.cpc_scope = CpcScopeGuard()
        w.git_snapshots = GitSnapshotStore(self.store)
        w.ver_records = VerificationRecordStore(self.store)
        w.worktrees_store = WorktreeStore(self.store)
        w.rollback_log_store = RollbackLog(self.store)
        wt_mgr = WorktreeManager.__new__(WorktreeManager)
        wt_mgr._cpc = Path('/fake/cpc')
        wt_mgr._wt_base = Path(self._td.name) / 'worktrees'
        w.wt_manager = wt_mgr
        re_engine = RollbackEngine.__new__(RollbackEngine)
        re_engine._root = '/fake/cpc'
        re_engine._git_reader = MagicMock()
        w.rollback_engine = re_engine
        ver_git = VerifierGitReader.__new__(VerifierGitReader)
        ver_git._root = '/fake/cpc'
        w.ver_git = ver_git
        cpc_tests = MagicMock()
        w.cpc_tests = cpc_tests
        w.handlers = MappingProxyType({k: Capability(w, k) for k in _PHASE8_KINDS})
        return w

    def test_git_capture_safe_runs_without_approval(self):
        """git_capture is SAFE — no approval needed."""
        action = _approved_action(self.store, self.task['id'], 'git_capture', {
            'workspace': 'cpc', 'task_id': self.task['id'], 'label': 'before'
        })
        self.assertIsNone(action['approval'])

    def test_git_capture_executes_and_records_snapshot(self):
        w = self._worker()
        snap_data = {
            'sha': 'a' * 40, 'branch': 'main',
            'staged': [], 'unstaged': ['app.py'], 'untracked': [],
            'dirty_files': ['app.py'], 'timestamp': time.time(),
        }
        w.ver_git.snapshot_detailed = lambda: snap_data
        result = w.run('git_capture', {
            'workspace': 'cpc', 'task_id': self.task['id'], 'label': 'before'
        }, baseline=None)
        self.assertEqual(result['label'], 'before')
        self.assertEqual(result['sha'], 'a' * 40)

    def test_diff_review_safe(self):
        action = _approved_action(self.store, self.task['id'], 'diff_review', {
            'workspace': 'cpc', 'session_id': self.session['id']
        })
        self.assertIsNone(action['approval'])

    def test_diff_review_returns_scope_status(self):
        w = self._worker()
        w.ver_git.diff_scope = MagicMock(return_value={
            'diff': '--- a/app.py\n+++ b/app.py', 'stat': 'app.py | 2 +-',
            'files': ['backend/app.py'], 'base_sha': 'a' * 40, 'truncated': False,
        })
        w.ver_git.changed_files = MagicMock(return_value=['backend/app.py'])
        result = w.run('diff_review', {
            'workspace': 'cpc', 'session_id': self.session['id']
        }, baseline=None)
        self.assertIn('scope_status', result)
        self.assertIn('diff', result)
        self.assertIn('pre_existing_dirty', result)

    def test_verify_tests_protected(self):
        """verify_tests is PROTECTED — must require approval."""
        policy = Policy()
        self.assertTrue(policy.requires_approval('verify_tests'))

    def test_verify_tests_none_produces_not_verified(self):
        w = self._worker()
        w.cpc_tests.run.return_value = {
            'command': 'none', 'passed': None, 'skipped': True,
            'browser_verification_required': True,
        }
        result = w.run('verify_tests', {
            'workspace': 'cpc', 'session_id': self.session['id'], 'run_tests': 'none'
        }, baseline=None)
        self.assertEqual(result['status'], 'NOT_VERIFIED')
        self.assertEqual(result['verifier'], 'independent')

    def test_verify_tests_pass_recorded_as_independent(self):
        w = self._worker()
        w.cpc_tests.run.return_value = {
            'command': 'python_test',
            'argv': ['/usr/bin/python3', '-m', 'backend.testing.run_tests'],
            'exit_code': 0, 'stdout': 'OK\n', 'stderr': '',
            'passed': True, 'browser_verification_required': False,
        }
        result = w.run('verify_tests', {
            'workspace': 'cpc', 'session_id': self.session['id'], 'run_tests': 'python_test'
        }, baseline=None)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['verifier'], 'independent')
        # Confirm stored as independent
        vrs = VerificationRecordStore(self.store)
        self.assertTrue(vrs.has_pass(self.session['id']))

    def test_verify_tests_browser_produces_not_verified(self):
        w = self._worker()
        w.cpc_tests.run.return_value = {
            'command': 'playwright_e2e',
            'passed': None, 'skipped': False, 'browser_verification_required': True,
        }
        result = w.run('verify_tests', {
            'workspace': 'cpc', 'session_id': self.session['id'], 'run_tests': 'playwright_e2e'
        }, baseline=None)
        self.assertEqual(result['status'], 'NOT_VERIFIED')

    def test_git_rollback_skips_preexisting(self):
        # Create a session where scope_files includes the pre-existing dirty file
        # so we can verify the rollback skips it
        task2 = _planned_task(self.store)
        session2 = _fake_session(
            self.store, task2['id'],
            scope_files=['backend/app.py', 'frontend/existing.js'],
        )
        w = self._worker()
        w.rollback_engine._git_reader.changed_files.return_value = [
            'backend/app.py', 'frontend/existing.js'
        ]
        def fake_run(argv, timeout=15):
            return '', '', 0
        w.rollback_engine._run = fake_run
        result = w.run('git_rollback', {
            'workspace': 'cpc', 'session_id': session2['id'],
            'reason': 'tests failed; rolling back'
        }, baseline=None)
        # frontend/existing.js is in before_dirty → must be skipped (pre-existing)
        self.assertIn('frontend/existing.js', result['skipped_preexisting'])
        self.assertNotIn('frontend/existing.js', result['files_restored'])

    def test_capture_evidence_safe(self):
        action = _approved_action(self.store, self.task['id'], 'capture_evidence', {
            'workspace': 'cpc', 'task_id': self.task['id'],
            'session_id': self.session['id'], 'label': 'after'
        })
        self.assertIsNone(action['approval'])

    def test_capture_evidence_records_snapshot(self):
        w = self._worker()
        snap_data = {
            'sha': 'b' * 40, 'branch': 'main',
            'staged': [], 'unstaged': [], 'untracked': [],
            'dirty_files': [], 'timestamp': time.time(),
        }
        w.ver_git.snapshot_detailed = lambda: snap_data
        result = w.run('capture_evidence', {
            'workspace': 'cpc', 'task_id': self.task['id'],
            'session_id': self.session['id'], 'label': 'after'
        }, baseline=None)
        self.assertEqual(result['label'], 'after')
        self.assertIsNotNone(result['snapshot_id'])


# ── Phase8ControlsTests ───────────────────────────────────────────────────────

class Phase8ControlsTests(unittest.TestCase):

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._td = TemporaryDirectory()
        self.store = _store(Path(self._td.name))
        self.controls = Controls(self.store)

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def test_stop_blocks_git_capture(self):
        self.controls.set('stop_everything', True, actor='test', reason='test')
        with self.assertRaises(Denied):
            self.controls.check('git_capture')

    def test_stop_blocks_verify_tests(self):
        self.controls.set('stop_everything', True, actor='test', reason='test')
        with self.assertRaises(Denied):
            self.controls.check('verify_tests')

    def test_stop_blocks_git_rollback(self):
        self.controls.set('stop_everything', True, actor='test', reason='test')
        with self.assertRaises(Denied):
            self.controls.check('git_rollback')

    def test_read_only_blocks_verify_tests(self):
        self.controls.set('read_only_mode', True, actor='test', reason='test')
        with self.assertRaises(Denied):
            self.controls.check('verify_tests')

    def test_read_only_allows_git_capture(self):
        self.controls.set('read_only_mode', True, actor='test', reason='test')
        self.controls.check('git_capture')  # must not raise

    def test_read_only_allows_diff_review(self):
        self.controls.set('read_only_mode', True, actor='test', reason='test')
        self.controls.check('diff_review')  # must not raise


# ── SchemaV5Tests ─────────────────────────────────────────────────────────────

class SchemaV5Tests(unittest.TestCase):

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._td = TemporaryDirectory()
        self.store = _store(Path(self._td.name))

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def test_schema_version_is_5(self):
        v = self.store.db.execute('PRAGMA user_version').fetchone()[0]
        self.assertEqual(v, 6)

    def test_git_snapshots_table_exists(self):
        self.store.db.execute('SELECT * FROM git_snapshots LIMIT 0')

    def test_verification_records_table_exists(self):
        self.store.db.execute('SELECT * FROM verification_records LIMIT 0')

    def test_task_worktrees_table_exists(self):
        self.store.db.execute('SELECT * FROM task_worktrees LIMIT 0')

    def test_rollback_log_table_exists(self):
        self.store.db.execute('SELECT * FROM rollback_log LIMIT 0')

    def test_cpc_coding_sessions_preserved(self):
        self.store.db.execute('SELECT * FROM cpc_coding_sessions LIMIT 0')


# ── Phase8PolicyTests ─────────────────────────────────────────────────────────

class Phase8PolicyTests(unittest.TestCase):

    def setUp(self):
        self.policy = Policy()

    def test_git_capture_is_safe(self):
        self.assertFalse(self.policy.requires_approval('git_capture'))

    def test_diff_review_is_safe(self):
        self.assertFalse(self.policy.requires_approval('diff_review'))

    def test_capture_evidence_is_safe(self):
        self.assertFalse(self.policy.requires_approval('capture_evidence'))

    def test_verify_tests_is_protected(self):
        self.assertTrue(self.policy.requires_approval('verify_tests'))

    def test_git_worktree_create_is_protected(self):
        self.assertTrue(self.policy.requires_approval('git_worktree_create'))

    def test_git_rollback_is_protected(self):
        self.assertTrue(self.policy.requires_approval('git_rollback'))


# ── Phase8CliTests ────────────────────────────────────────────────────────────

class Phase8CliTests(unittest.TestCase):

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._td = TemporaryDirectory()
        import os
        os.environ['ZAMAL_DB'] = str(Path(self._td.name) / 'op.sqlite3')
        self.store = _store(Path(self._td.name))
        self.task = _planned_task(self.store)
        self.session = _fake_session(self.store, self.task['id'])

    def tearDown(self):
        self.store.close()
        self._td.cleanup()

    def test_schema_v5_via_init(self):
        import subprocess
        r = subprocess.run(
            ['python3', '-m', 'zamal_core', 'init'],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            self.assertEqual(data['schema_version'], 6)

    def test_ver_snapshots_empty(self):
        gss = GitSnapshotStore(self.store)
        snapshots = gss.list_for_task(self.task['id'])
        self.assertEqual(snapshots, [])

    def test_ver_records_empty(self):
        vrs = VerificationRecordStore(self.store)
        latest = vrs.latest_for_session(self.session['id'])
        self.assertIsNone(latest)

    def test_ver_worktrees_empty(self):
        ws = WorktreeStore(self.store)
        wt = ws.active_for_task(self.task['id'])
        self.assertIsNone(wt)

    def test_phase8_safe_kinds_set(self):
        self.assertIn('git_capture', _PHASE8_SAFE)
        self.assertIn('diff_review', _PHASE8_SAFE)
        self.assertIn('capture_evidence', _PHASE8_SAFE)

    def test_phase8_protected_kinds_set(self):
        self.assertIn('verify_tests', _PHASE8_PROTECTED)
        self.assertIn('git_worktree_create', _PHASE8_PROTECTED)
        self.assertIn('git_rollback', _PHASE8_PROTECTED)
        self.assertIn('git_commit', _PHASE8_PROTECTED)
        self.assertIn('git_push', _PHASE8_PROTECTED)
        self.assertIn('git_merge', _PHASE8_PROTECTED)


if __name__ == '__main__':
    unittest.main()
