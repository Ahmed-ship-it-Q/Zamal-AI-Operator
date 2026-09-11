"""Phase 8: Verification, Browser QA, Git Safety & Rollback.

Nine capabilities:
1. git_capture        — SAFE: snapshot branch/sha/staged/unstaged/untracked
2. diff_review        — SAFE: exact scope diff vs session baseline
3. verify_tests       — PROTECTED: independent test run; coder PASS is not evidence
4. capture_evidence   — SAFE: associate before/after git snapshots with result
5. git_worktree_create — PROTECTED: task-specific branch/worktree; ownership tracked
6. git_rollback       — PROTECTED: restore task-created files only; auditable; never global reset
7. git_commit         — PROTECTED: commit only if independent PASS + scope CLEAN + diff verified
8. git_push           — PROTECTED: push; no force; requires commit; blocked by disable_github_push
9. git_merge          — PROTECTED: merge with approval; no auto-merge on conflict

MANDATORY: Verifier is independent from coding-worker PASS claims (separate table, verifier='independent').
CPC remains read-only outside an approved coding session.
No force push, no destructive reset, no auto-merge.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path
from types import MappingProxyType

from .store import Denied, Gate, uid
from .worker import Capability, redact
from .cpc_worker import (
    CpcWorker, CpcGitReader, CpcSessionStore, CpcScopeGuard, CpcTestRunner,
    _GIT, _GIT_ENV, CPC_TEST_COMMANDS, _PHASE7_KINDS,
)
from .mac_worker import CPC_ROOT, OPERATOR_ROOT

# ── Phase 8 constants ─────────────────────────────────────────────────────────

_PHASE8_KINDS = frozenset({
    'git_capture',
    'diff_review',
    'verify_tests',
    'capture_evidence',
    'git_worktree_create',
    'git_rollback',
    'git_commit',
    'git_push',
    'git_merge',
})
_PHASE8_SAFE = frozenset({'git_capture', 'diff_review', 'capture_evidence'})
_PHASE8_PROTECTED = _PHASE8_KINDS - _PHASE8_SAFE

_PHASE8_FIELDS = MappingProxyType({
    'git_capture':         frozenset({'workspace', 'task_id', 'label'}),
    'diff_review':         frozenset({'workspace', 'session_id'}),
    'verify_tests':        frozenset({'workspace', 'session_id', 'run_tests'}),
    'capture_evidence':    frozenset({'workspace', 'task_id', 'session_id', 'label'}),
    'git_worktree_create': frozenset({'workspace', 'task_id', 'base_sha'}),
    'git_rollback':        frozenset({'workspace', 'session_id', 'reason'}),
    'git_commit':          frozenset({'workspace', 'session_id', 'message'}),
    'git_push':            frozenset({'workspace', 'task_id', 'remote', 'branch'}),
    'git_merge':           frozenset({'workspace', 'task_id', 'source_branch', 'target_branch'}),
})

_LABEL_RE = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
_SHA_RE = re.compile(r'^[0-9a-f]{40}$')
_BRANCH_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9/._-]{0,99}$')
_REMOTE_RE = re.compile(r'^[a-zA-Z0-9_-]{1,50}$')
_MESSAGE_MAX = 512
_DIFF_MAX = 32768
_PROTECTED_BRANCHES = frozenset({'main', 'master'})

WORKTREE_ROOT = OPERATOR_ROOT / 'state' / 'worktrees'


# ── VerifierGitReader ─────────────────────────────────────────────────────────

class VerifierGitReader(CpcGitReader):
    """Extends CpcGitReader with staged/unstaged/untracked separation and branch detection."""

    def branch(self):
        raw, rc = self._run([_GIT, 'rev-parse', '--abbrev-ref', 'HEAD'])
        return raw.strip() if rc == 0 else 'DETACHED'

    def snapshot_detailed(self):
        """Return {branch, sha, staged, unstaged, untracked, dirty_files, timestamp}."""
        sha_raw, rc = self._run([_GIT, 'rev-parse', 'HEAD'])
        sha = sha_raw.strip() if rc == 0 else 'no_commits'
        branch = self.branch()
        status_raw, _ = self._run([_GIT, 'status', '--porcelain'])
        staged, unstaged, untracked = [], [], []
        for line in (status_raw or '').splitlines():
            if len(line) < 3:
                continue
            x, y = line[0], line[1]
            path = line[3:]
            if ' -> ' in path:
                path = path.split(' -> ', 1)[1]
            path = path.strip()
            if not path:
                continue
            if x == '?' and y == '?':
                untracked.append(path)
            else:
                if x != ' ':
                    staged.append(path)
                if y != ' ':
                    unstaged.append(path)
        dirty = sorted(set(staged + unstaged))
        return {
            'sha': sha,
            'branch': branch,
            'staged': sorted(set(staged)),
            'unstaged': sorted(set(unstaged)),
            'untracked': sorted(untracked),
            'dirty_files': dirty,
            'timestamp': time.time(),
        }

    def diff_scope(self, scope_files, base_sha='HEAD'):
        """Return exact diff for scope_files vs base_sha (unstaged + staged combined)."""
        if not scope_files:
            return {'diff': '', 'stat': '', 'files': [], 'base_sha': base_sha}
        files = list(scope_files)
        diff_raw, _ = self._run([_GIT, 'diff', base_sha, '--'] + files, timeout=30)
        staged_raw, _ = self._run([_GIT, 'diff', '--cached', base_sha, '--'] + files, timeout=30)
        stat_raw, _ = self._run([_GIT, 'diff', '--stat', base_sha, '--'] + files, timeout=15)
        combined = (diff_raw + staged_raw)[:_DIFF_MAX]
        return {
            'diff': combined,
            'stat': stat_raw[:4096],
            'files': sorted(files),
            'base_sha': base_sha,
            'truncated': len(diff_raw + staged_raw) > _DIFF_MAX,
        }


# ── GitSnapshotStore ──────────────────────────────────────────────────────────

class GitSnapshotStore:
    """Manages the git_snapshots table. Records before/after/verification state."""

    def __init__(self, store):
        self.store = store

    def create(self, task_id, label, branch, sha, staged, unstaged, untracked):
        snapshot_id = uid()
        now = time.time()
        dirty = sorted(set(list(staged) + list(unstaged)))
        self.store.db.execute(
            'INSERT INTO git_snapshots'
            '(id,task_id,label,branch,sha,staged,unstaged,untracked,dirty_files,captured_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?)',
            (snapshot_id, task_id, label, branch, sha,
             json.dumps(sorted(staged)),
             json.dumps(sorted(unstaged)),
             json.dumps(sorted(untracked)),
             json.dumps(dirty),
             now),
        )
        return self._decode(self.store.db.execute(
            'SELECT * FROM git_snapshots WHERE id=?', (snapshot_id,)
        ).fetchone())

    def get(self, snapshot_id):
        row = self.store.db.execute(
            'SELECT * FROM git_snapshots WHERE id=?', (snapshot_id,)
        ).fetchone()
        if not row:
            raise Denied(f'Git snapshot {snapshot_id!r} not found')
        return self._decode(row)

    def list_for_task(self, task_id):
        rows = self.store.db.execute(
            'SELECT * FROM git_snapshots WHERE task_id=? ORDER BY captured_at',
            (task_id,),
        ).fetchall()
        return [self._decode(r) for r in rows]

    @staticmethod
    def _decode(row):
        d = dict(row)
        for f in ('staged', 'unstaged', 'untracked', 'dirty_files'):
            d[f] = json.loads(d[f])
        return d


# ── VerificationRecordStore ───────────────────────────────────────────────────

class VerificationRecordStore:
    """Manages the verification_records table. Independent of coding-worker self-reports."""

    def __init__(self, store):
        self.store = store

    def create(self, task_id, session_id, verifier, test_command, test_argv,
               exit_code, stdout, stderr, passed, browser_required, status, evidence):
        record_id = uid()
        now = time.time()
        self.store.db.execute(
            'INSERT INTO verification_records'
            '(id,task_id,session_id,verifier,test_command,test_argv,exit_code,'
            'stdout,stderr,passed,browser_required,status,evidence,verified_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (record_id, task_id, session_id, verifier, test_command,
             json.dumps(test_argv) if test_argv is not None else None,
             exit_code,
             (stdout or '')[:4096],
             (stderr or '')[:2048],
             (1 if passed else 0) if passed is not None else None,
             int(bool(browser_required)),
             status,
             json.dumps(evidence) if evidence is not None else None,
             now),
        )
        return self.get(record_id)

    def get(self, record_id):
        row = self.store.db.execute(
            'SELECT * FROM verification_records WHERE id=?', (record_id,)
        ).fetchone()
        if not row:
            raise Denied(f'Verification record {record_id!r} not found')
        return self._decode(row)

    def latest_for_session(self, session_id):
        row = self.store.db.execute(
            'SELECT * FROM verification_records WHERE session_id=?'
            ' ORDER BY verified_at DESC LIMIT 1',
            (session_id,),
        ).fetchone()
        return self._decode(row) if row else None

    def has_pass(self, session_id):
        """Return True only if an INDEPENDENT verifier recorded PASS for this session."""
        row = self.store.db.execute(
            "SELECT 1 FROM verification_records"
            " WHERE session_id=? AND status='PASS' AND verifier='independent'",
            (session_id,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _decode(row):
        d = dict(row)
        if d.get('test_argv'):
            d['test_argv'] = json.loads(d['test_argv'])
        if d.get('evidence'):
            d['evidence'] = json.loads(d['evidence'])
        return d


# ── WorktreeStore ─────────────────────────────────────────────────────────────

class WorktreeStore:
    """Manages the task_worktrees table. Tracks task-to-worktree ownership."""

    def __init__(self, store):
        self.store = store

    def create(self, task_id, branch, worktree_path, base_sha, owner):
        wt_id = uid()
        now = time.time()
        self.store.db.execute(
            'INSERT INTO task_worktrees'
            '(id,task_id,branch,worktree_path,base_sha,owner,status,created)'
            ' VALUES(?,?,?,?,?,?,?,?)',
            (wt_id, task_id, branch, str(worktree_path), base_sha, owner, 'active', now),
        )
        return self.get(wt_id)

    def get(self, wt_id):
        row = self.store.db.execute(
            'SELECT * FROM task_worktrees WHERE id=?', (wt_id,)
        ).fetchone()
        if not row:
            raise Denied(f'Worktree record {wt_id!r} not found')
        return dict(row)

    def active_for_task(self, task_id):
        row = self.store.db.execute(
            "SELECT * FROM task_worktrees WHERE task_id=? AND status='active'",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def close(self, wt_id):
        now = time.time()
        self.store.db.execute(
            "UPDATE task_worktrees SET status='closed', closed=? WHERE id=?",
            (now, wt_id),
        )
        return self.get(wt_id)


# ── RollbackLog ───────────────────────────────────────────────────────────────

class RollbackLog:
    """Manages the rollback_log table. Every rollback is auditable."""

    def __init__(self, store):
        self.store = store

    def create(self, task_id, session_id, scope_files, base_sha, actor, reason, outcome):
        log_id = uid()
        now = time.time()
        self.store.db.execute(
            'INSERT INTO rollback_log'
            '(id,task_id,session_id,scope_files,base_sha,actor,reason,rolled_back_at,outcome)'
            ' VALUES(?,?,?,?,?,?,?,?,?)',
            (log_id, task_id, session_id,
             json.dumps(sorted(scope_files)),
             base_sha, actor, reason, now, outcome),
        )
        return self.get(log_id)

    def get(self, log_id):
        row = self.store.db.execute(
            'SELECT * FROM rollback_log WHERE id=?', (log_id,)
        ).fetchone()
        if not row:
            raise Denied(f'Rollback log {log_id!r} not found')
        d = dict(row)
        d['scope_files'] = json.loads(d['scope_files'])
        return d


# ── WorktreeManager ───────────────────────────────────────────────────────────

class WorktreeManager:
    """Creates and removes task-specific git worktrees. Never discards user work."""

    def __init__(self, cpc_root=None, worktree_root=None):
        self._cpc = Path(cpc_root or CPC_ROOT).resolve()
        self._wt_base = Path(worktree_root or WORKTREE_ROOT).resolve()

    def _run(self, argv, timeout=30):
        r = subprocess.run(
            argv, capture_output=True, text=True,
            cwd=str(self._cpc), env=dict(_GIT_ENV),
            timeout=timeout, shell=False,
        )
        return r.stdout.rstrip(), r.stderr.rstrip(), r.returncode

    def branch_name(self, task_id):
        short = task_id.replace('-', '')[:12]
        return f'zamal/task-{short}'

    def worktree_path(self, task_id):
        short = task_id.replace('-', '')[:12]
        return self._wt_base / f'task-{short}'

    def create(self, task_id, base_sha):
        """Create a task-specific branch and worktree. Pre-existing CPC changes are unaffected."""
        path = self.worktree_path(task_id)
        branch = self.branch_name(task_id)
        if path.exists():
            raise Denied(f'Worktree path already exists: {path}; cannot overwrite')
        _, _, rc = self._run([_GIT, 'rev-parse', '--verify', branch])
        if rc == 0:
            raise Denied(f'Branch {branch!r} already exists; task worktree may already be active')
        sha_out, _, rc = self._run([_GIT, 'rev-parse', '--verify', base_sha])
        if rc != 0:
            raise Denied(f'Base SHA {base_sha!r} not found in repository')
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        out, err, rc = self._run(
            [_GIT, 'worktree', 'add', str(path), '-b', branch, base_sha],
        )
        if rc != 0:
            raise Denied(f'git worktree add failed (exit {rc}): {err[:512]}')
        return {
            'worktree_path': str(path),
            'branch': branch,
            'base_sha': base_sha,
        }

    def remove(self, worktree_path):
        """Remove a worktree. Path must be under the managed worktree base."""
        path = Path(worktree_path).resolve()
        if not str(path).startswith(str(self._wt_base)):
            raise Denied('Worktree path is outside the managed worktree directory; cannot remove')
        _, err, rc = self._run(
            [_GIT, 'worktree', 'remove', '--force', str(path)],
        )
        if rc != 0:
            raise Denied(f'git worktree remove failed: {err[:512]}')


# ── RollbackEngine ────────────────────────────────────────────────────────────

class RollbackEngine:
    """Restores task-created files to base_sha. Pre-existing dirty files are never touched."""

    def __init__(self, cpc_root=None):
        self._root = str(Path(cpc_root or CPC_ROOT).resolve())
        self._git_reader = VerifierGitReader(cpc_root)

    def _run(self, argv, timeout=15):
        r = subprocess.run(
            argv, capture_output=True, text=True,
            cwd=self._root, env=dict(_GIT_ENV),
            timeout=timeout, shell=False,
        )
        return r.stdout.rstrip(), r.stderr.rstrip(), r.returncode

    def rollback(self, scope_files, base_sha, before_dirty):
        """
        Restore task-created changes to base_sha. Pre-existing dirty files are skipped.

        Algorithm:
          - current dirty = git status now
          - new changes = current dirty - before_dirty
          - eligible = new changes that are in scope_files
          - For each eligible file: git checkout <base_sha> -- <file>
          - Files in before_dirty are never touched (pre-existing work preserved)
        """
        current_dirty = self._git_reader.changed_files()
        before_set = set(before_dirty)
        scope_set = set(scope_files)
        restored, skipped_preexisting, skipped_clean, failed = [], [], [], []

        for f in scope_files:
            if f in before_set:
                skipped_preexisting.append(f)
                continue
            if f not in current_dirty:
                skipped_clean.append(f)
                continue
            _, err, rc = self._run([_GIT, 'checkout', base_sha, '--', f])
            if rc == 0:
                restored.append(f)
            else:
                failed.append({'file': f, 'error': err[:256]})

        if not failed:
            outcome = 'SUCCESS'
        elif restored:
            outcome = 'PARTIAL'
        else:
            outcome = 'FAILED'

        return {
            'files_restored': restored,
            'skipped_preexisting': skipped_preexisting,
            'skipped_clean': skipped_clean,
            'failed': failed,
            'base_sha': base_sha,
            'outcome': outcome,
        }


# ── Branch/SHA validation helpers ─────────────────────────────────────────────

def _validate_branch(name, field='branch'):
    if not isinstance(name, str) or not _BRANCH_RE.match(name):
        raise Denied(f'{field} must be a safe branch name (alphanumeric + /._-)')
    if '..' in name or name.endswith('.lock') or name.endswith('.'):
        raise Denied(f'{field} {name!r} is not valid git syntax')


# ── VerifierWorker ────────────────────────────────────────────────────────────

class VerifierWorker(CpcWorker):
    """Phase 8: Verification, Browser QA, Git Safety & Rollback.

    Extends CpcWorker (Phase 7) with independent verification and protected git ops.
    All Phase 3, 6, and 7 capabilities are fully preserved.
    Verifier records to verification_records (separate from coding-worker self-reports).
    """

    def __init__(self, store, *, operator=None, cpc=None, node=None):
        super().__init__(store, operator=operator, cpc=cpc, node=node)
        cpc_root = self.paths.roots['cpc']
        op_root = self.paths.roots['operator']
        self.ver_git = VerifierGitReader(cpc_root)
        self.git_snapshots = GitSnapshotStore(store)
        self.ver_records = VerificationRecordStore(store)
        self.worktrees_store = WorktreeStore(store)
        self.rollback_log_store = RollbackLog(store)
        self.wt_manager = WorktreeManager(cpc_root, op_root / 'state' / 'worktrees')
        self.rollback_engine = RollbackEngine(cpc_root)
        all_handlers = dict(self.handlers)
        all_handlers.update({k: Capability(self, k) for k in _PHASE8_KINDS})
        self.handlers = MappingProxyType(all_handlers)

    def gate(self):
        return Gate(self.store, self.handlers)

    def _git_cpc(self, argv, timeout=30):
        """Run a git command in the CPC directory. Returns (stdout, stderr, returncode)."""
        r = subprocess.run(
            argv, capture_output=True, text=True,
            cwd=str(self.paths.roots['cpc']),
            env=dict(_GIT_ENV),
            timeout=timeout, shell=False,
        )
        return r.stdout.rstrip(), r.stderr.rstrip(), r.returncode

    def validate(self, kind, p, project):
        if kind not in _PHASE8_KINDS:
            return super().validate(kind, p, project)
        if not isinstance(p, dict):
            raise Denied('Structured payload required')
        expected = _PHASE8_FIELDS[kind]
        if set(p) != expected:
            raise Denied(f'Payload must have exactly: {sorted(expected)}')

        if kind == 'git_capture':
            if p.get('workspace') != 'cpc':
                raise Denied('git_capture workspace must be "cpc"')
            if not isinstance(p.get('task_id'), str) or not p['task_id'].strip():
                raise Denied('task_id must be a non-empty string')
            if not isinstance(p.get('label'), str) or not _LABEL_RE.match(p['label']):
                raise Denied('label must match [a-z][a-z0-9_-]{0,63}')

        elif kind == 'diff_review':
            if p.get('workspace') != 'cpc':
                raise Denied('diff_review workspace must be "cpc"')
            if not isinstance(p.get('session_id'), str) or not p['session_id'].strip():
                raise Denied('session_id must be a non-empty string')
            self.cpc_sessions.session(p['session_id'])

        elif kind == 'verify_tests':
            if p.get('workspace') != 'cpc':
                raise Denied('verify_tests workspace must be "cpc"')
            if not isinstance(p.get('session_id'), str) or not p['session_id'].strip():
                raise Denied('session_id must be a non-empty string')
            self.cpc_sessions.session(p['session_id'])
            if p.get('run_tests') not in CPC_TEST_COMMANDS:
                raise Denied(f'run_tests must be one of {sorted(CPC_TEST_COMMANDS)}')

        elif kind == 'capture_evidence':
            if p.get('workspace') != 'cpc':
                raise Denied('capture_evidence workspace must be "cpc"')
            if not isinstance(p.get('task_id'), str) or not p['task_id'].strip():
                raise Denied('task_id must be a non-empty string')
            if not isinstance(p.get('session_id'), str) or not p['session_id'].strip():
                raise Denied('session_id must be a non-empty string')
            if not isinstance(p.get('label'), str) or not _LABEL_RE.match(p['label']):
                raise Denied('label must match [a-z][a-z0-9_-]{0,63}')

        elif kind == 'git_worktree_create':
            if p.get('workspace') != 'cpc':
                raise Denied('git_worktree_create workspace must be "cpc"')
            if not isinstance(p.get('task_id'), str) or not p['task_id'].strip():
                raise Denied('task_id must be a non-empty string')
            if not isinstance(p.get('base_sha'), str) or not _SHA_RE.match(p.get('base_sha', '')):
                raise Denied('base_sha must be a 40-character lowercase hex SHA-1')
            existing = self.worktrees_store.active_for_task(p['task_id'])
            if existing:
                raise Denied(
                    f'Task already has an active worktree at {existing["worktree_path"]!r}'
                )

        elif kind == 'git_rollback':
            if p.get('workspace') != 'cpc':
                raise Denied('git_rollback workspace must be "cpc"')
            if not isinstance(p.get('session_id'), str) or not p['session_id'].strip():
                raise Denied('session_id must be a non-empty string')
            if not isinstance(p.get('reason'), str) or not p['reason'].strip():
                raise Denied('reason must be a non-empty string')
            self.cpc_sessions.session(p['session_id'])

        elif kind == 'git_commit':
            if p.get('workspace') != 'cpc':
                raise Denied('git_commit workspace must be "cpc"')
            if not isinstance(p.get('session_id'), str) or not p['session_id'].strip():
                raise Denied('session_id must be a non-empty string')
            if not isinstance(p.get('message'), str) or not p['message'].strip():
                raise Denied('message must be a non-empty commit message string')
            if len(p['message']) > _MESSAGE_MAX:
                raise Denied(f'message exceeds {_MESSAGE_MAX} characters')
            self.cpc_sessions.session(p['session_id'])
            if not self.ver_records.has_pass(p['session_id']):
                raise Denied(
                    'git_commit requires an independent verification PASS record; '
                    'run verify_tests first and confirm status=PASS before requesting commit'
                )

        elif kind == 'git_push':
            if p.get('workspace') != 'cpc':
                raise Denied('git_push workspace must be "cpc"')
            if not isinstance(p.get('task_id'), str) or not p['task_id'].strip():
                raise Denied('task_id must be a non-empty string')
            if not isinstance(p.get('remote'), str) or not _REMOTE_RE.match(p.get('remote', '')):
                raise Denied('remote must match [a-zA-Z0-9_-]{1,50}')
            _validate_branch(p.get('branch', ''), 'branch')
            if p['branch'].lower() in _PROTECTED_BRANCHES:
                raise Denied(
                    f'Direct push to {p["branch"]!r} is denied; '
                    'push to a task branch and open a pull request'
                )

        elif kind == 'git_merge':
            if p.get('workspace') != 'cpc':
                raise Denied('git_merge workspace must be "cpc"')
            if not isinstance(p.get('task_id'), str) or not p['task_id'].strip():
                raise Denied('task_id must be a non-empty string')
            _validate_branch(p.get('source_branch', ''), 'source_branch')
            _validate_branch(p.get('target_branch', ''), 'target_branch')
            if p.get('source_branch') == p.get('target_branch'):
                raise Denied('source_branch and target_branch must differ')

    def run(self, kind, p, baseline):
        if kind not in _PHASE8_KINDS:
            return super().run(kind, p, baseline)
        check = lambda: self.controls.check(kind, baseline)
        check()
        self.validate(kind, p, 'cpc')

        if kind == 'git_capture':
            snap = self.ver_git.snapshot_detailed()
            check()
            with self.store.transaction():
                record = self.git_snapshots.create(
                    task_id=p['task_id'],
                    label=p['label'],
                    branch=snap['branch'],
                    sha=snap['sha'],
                    staged=snap['staged'],
                    unstaged=snap['unstaged'],
                    untracked=snap['untracked'],
                )
            return record

        if kind == 'diff_review':
            session = self.cpc_sessions.session(p['session_id'])
            scope_files = json.loads(session['scope_files'])
            before_snap = json.loads(session['git_before'])
            base_sha = before_snap['sha']
            diff_result = self.ver_git.diff_scope(scope_files, base_sha)
            check()
            current_dirty = self.ver_git.changed_files()
            before_dirty = before_snap['dirty_files']
            scope_result = self.cpc_scope.check_scope(current_dirty, before_dirty, scope_files)
            return {
                'session_id': session['id'],
                'task_id': session['task_id'],
                'scope_files': scope_files,
                'base_sha': base_sha,
                'diff': diff_result,
                'scope_status': scope_result,
                'pre_existing_dirty': sorted(before_dirty),
                'current_dirty': sorted(current_dirty),
                'note': 'pre_existing_dirty files are never committed or reset by Phase 8.',
            }

        if kind == 'verify_tests':
            session = self.cpc_sessions.session(p['session_id'])
            # Run tests INDEPENDENTLY — separate from coding-worker's own test run.
            test_result = self.cpc_tests.run(p['run_tests'], check)
            check()
            if test_result.get('skipped'):
                status = 'NOT_VERIFIED'
            elif test_result.get('browser_verification_required'):
                status = 'NOT_VERIFIED'
            elif test_result.get('passed') is True:
                status = 'PASS'
            else:
                status = 'FAIL'
            with self.store.transaction():
                record = self.ver_records.create(
                    task_id=session['task_id'],
                    session_id=session['id'],
                    verifier='independent',
                    test_command=test_result.get('command'),
                    test_argv=test_result.get('argv'),
                    exit_code=test_result.get('exit_code'),
                    stdout=test_result.get('stdout'),
                    stderr=test_result.get('stderr'),
                    passed=test_result.get('passed'),
                    browser_required=test_result.get('browser_verification_required', False),
                    status=status,
                    evidence={'test_result': test_result, 'verified_at': time.time()},
                )
            return {
                'session_id': session['id'],
                'task_id': session['task_id'],
                'verifier': 'independent',
                'status': status,
                'record_id': record['id'],
                'test_result': test_result,
                'note': (
                    'Recorded independently of coding-worker self-report. '
                    'Only this independent PASS authorizes git_commit.'
                ),
            }

        if kind == 'capture_evidence':
            snap = self.ver_git.snapshot_detailed()
            check()
            with self.store.transaction():
                snapshot = self.git_snapshots.create(
                    task_id=p['task_id'],
                    label=p['label'],
                    branch=snap['branch'],
                    sha=snap['sha'],
                    staged=snap['staged'],
                    unstaged=snap['unstaged'],
                    untracked=snap['untracked'],
                )
            latest_record = self.ver_records.latest_for_session(p['session_id'])
            return {
                'task_id': p['task_id'],
                'session_id': p['session_id'],
                'label': p['label'],
                'snapshot_id': snapshot['id'],
                'snapshot': snapshot,
                'latest_verification_status': (
                    latest_record['status'] if latest_record else None
                ),
                'latest_verification_id': (
                    latest_record['id'] if latest_record else None
                ),
                'captured_at': snap['timestamp'],
            }

        if kind == 'git_worktree_create':
            existing = self.worktrees_store.active_for_task(p['task_id'])
            if existing:
                raise Denied(
                    f'Task already has an active worktree at {existing["worktree_path"]!r}'
                )
            wt = self.wt_manager.create(p['task_id'], p['base_sha'])
            check()
            with self.store.transaction():
                record = self.worktrees_store.create(
                    task_id=p['task_id'],
                    branch=wt['branch'],
                    worktree_path=wt['worktree_path'],
                    base_sha=wt['base_sha'],
                    owner='operator',
                )
            return {
                'task_id': p['task_id'],
                'worktree_id': record['id'],
                'branch': wt['branch'],
                'worktree_path': wt['worktree_path'],
                'base_sha': wt['base_sha'],
                'status': 'active',
                'note': (
                    'Task-specific worktree created from base_sha. '
                    'Pre-existing changes in the main CPC working tree are unaffected.'
                ),
            }

        if kind == 'git_rollback':
            session = self.cpc_sessions.session(p['session_id'])
            before_snap = json.loads(session['git_before'])
            scope_files = json.loads(session['scope_files'])
            base_sha = before_snap['sha']
            before_dirty = before_snap['dirty_files']
            check()
            rollback_result = self.rollback_engine.rollback(scope_files, base_sha, before_dirty)
            check()
            with self.store.transaction():
                log_entry = self.rollback_log_store.create(
                    task_id=session['task_id'],
                    session_id=session['id'],
                    scope_files=rollback_result['files_restored'],
                    base_sha=base_sha,
                    actor='operator',
                    reason=p['reason'],
                    outcome=rollback_result['outcome'],
                )
            return {
                'session_id': session['id'],
                'task_id': session['task_id'],
                'rollback_id': log_entry['id'],
                **rollback_result,
                'note': (
                    'Only task-created changes were eligible for rollback. '
                    'Pre-existing dirty files (skipped_preexisting) were not modified.'
                ),
            }

        if kind == 'git_commit':
            session = self.cpc_sessions.session(p['session_id'])
            before_snap = json.loads(session['git_before'])
            scope_files = json.loads(session['scope_files'])
            if not self.ver_records.has_pass(p['session_id']):
                raise Denied('Independent verification PASS required; cannot commit')
            current_dirty = self.ver_git.changed_files()
            scope_result = self.cpc_scope.check_scope(
                current_dirty, before_snap['dirty_files'], scope_files
            )
            if not scope_result['ok']:
                raise Denied(
                    f'Scope violations present; commit denied: {scope_result["violations"]}'
                )
            check()
            modified_scope = [f for f in scope_files if f in set(current_dirty)]
            if not modified_scope:
                raise Denied('No scope files are currently modified; nothing to commit')
            _, err, rc = self._git_cpc([_GIT, 'add', '--'] + modified_scope)
            if rc != 0:
                raise Denied(f'git add failed (exit {rc}): {err[:512]}')
            out, err, rc = self._git_cpc([_GIT, 'commit', '-m', p['message']])
            if rc != 0:
                raise Denied(f'git commit failed (exit {rc}): {err[:512]}')
            new_sha, _, _ = self._git_cpc([_GIT, 'rev-parse', 'HEAD'])
            return {
                'session_id': session['id'],
                'task_id': session['task_id'],
                'committed': True,
                'sha': new_sha.strip(),
                'files_committed': modified_scope,
                'message': p['message'],
                'scope_status': 'CLEAN',
                'verification_basis': 'independent_pass',
            }

        if kind == 'git_push':
            check()
            out, err, rc = self._git_cpc(
                [_GIT, 'push', p['remote'], p['branch']],
                timeout=60,
            )
            return {
                'task_id': p['task_id'],
                'remote': p['remote'],
                'branch': p['branch'],
                'pushed': rc == 0,
                'exit_code': rc,
                'output': out[:2048] if rc == 0 else '',
                'error': err[:1024] if rc != 0 else '',
                'note': 'No force push. Protected branches (main/master) are denied at validate time.',
            }

        if kind == 'git_merge':
            check()
            out, err, rc = self._git_cpc(
                [_GIT, 'merge', '--no-ff', p['source_branch']],
                timeout=60,
            )
            if rc != 0:
                self._git_cpc([_GIT, 'merge', '--abort'], timeout=15)
                return {
                    'task_id': p['task_id'],
                    'merged': False,
                    'status': 'CONFLICT',
                    'source_branch': p['source_branch'],
                    'target_branch': p['target_branch'],
                    'error': err[:1024],
                    'note': 'Merge aborted. No auto-resolution of conflicts.',
                }
            return {
                'task_id': p['task_id'],
                'merged': True,
                'status': 'SUCCESS',
                'source_branch': p['source_branch'],
                'target_branch': p['target_branch'],
                'output': out[:2048],
            }

        raise Denied('Unimplemented Phase 8 capability')
