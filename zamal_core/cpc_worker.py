"""Phase 7: CPC Autonomous Engineering Worker.

Six capabilities:
1. CpcWriteGateway  — session-scoped CPC write mode; read-only by default
2. CpcTaskPlanner   — CPC context → structured implementation plan template
3. CpcOwnerGuard    — single coding owner per task; concurrent sessions denied
4. CpcScopeGuard    — affected-file enforcement; scope violation detection
5. CpcTestRunner    — approved CPC test commands with recorded results
6. CpcCompletionReport — structured result with all required evidence fields

No commit/push, no destructive git ops, no arbitrary shell, no unrelated writes.
CPC write mode is closed by default; opened only for an approved coding session.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path
from types import MappingProxyType

from .store import Denied, Gate, uid, encode
from .worker import Capability, redact
from .mac_worker import (
    MacWorker, FolderPolicy, ALLOWED_FOLDERS, CPC_ROOT,
    OPERATOR_ROOT, NODE_BIN, _PHASE6_KINDS,
)

# ── Git (read-only) ───────────────────────────────────────────────────────────

_GIT = '/usr/bin/git'
_GIT_ENV = MappingProxyType({
    'PATH': '/usr/bin:/bin',
    'HOME': str(Path.home()),
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_CONFIG_GLOBAL': '/dev/null',
    'LANG': 'C',
    'LC_ALL': 'C',
})

# ── Coding owner allowlist ────────────────────────────────────────────────────

ALLOWED_OWNERS = frozenset({'claude_code', 'codex'})

# ── CPC test command allowlist ────────────────────────────────────────────────

_PYTHON3 = '/usr/bin/python3'

CPC_TEST_COMMANDS = MappingProxyType({
    'python_test': {
        'argv': [_PYTHON3, '-m', 'backend.testing.run_tests'],
        'description': 'Run CPC backend unit tests (Python unittest)',
        'timeout': 120,
        'browser_required': False,
    },
    'playwright_e2e': {
        'argv': [str(NODE_BIN.parent / 'npx'), 'playwright', 'test', '--reporter=line'],
        'description': 'Run CPC e2e tests (Playwright — browser required)',
        'timeout': 300,
        'browser_required': True,
    },
    'none': None,
})

# ── Scope path validation helpers ─────────────────────────────────────────────

_BLOCKED_SCOPE_PARTS = frozenset({
    '..', '.', '',
    '.git', 'node_modules', '.n8n-data', '.venv', '__pycache__',
    'backups', 'database',
})
_SCOPE_MAX_FILES = 50
_SCOPE_MAX_PATH_LEN = 512

# ── Phase 7 action kinds ──────────────────────────────────────────────────────

_PHASE7_KINDS = frozenset({
    'cpc_plan',             # SAFE: CPC project overview + implementation plan template
    'cpc_snapshot',         # SAFE: capture CPC git state (before/during/after)
    'cpc_coding_start',     # PROTECTED: open scoped write-mode session with owner
    'cpc_scope_check',      # SAFE: detect out-of-scope changes vs session baseline
    'cpc_coding_complete',  # PROTECTED: close session, run tests, produce report
})

_PHASE7_FIELDS = MappingProxyType({
    'cpc_plan':            frozenset({'workspace'}),
    'cpc_snapshot':        frozenset({'workspace'}),
    'cpc_coding_start':    frozenset({'workspace', 'task_id', 'scope_files', 'owner'}),
    'cpc_scope_check':     frozenset({'session_id', 'workspace'}),
    'cpc_coding_complete': frozenset({'session_id', 'workspace', 'run_tests'}),
})


# ── CpcGitReader ──────────────────────────────────────────────────────────────

class CpcGitReader:
    """Read-only git operations on CPC. No mutation; no commit, push, reset, revert."""

    def __init__(self, cpc_root=None):
        self._root = str(Path(cpc_root or CPC_ROOT).resolve())

    def _run(self, argv, timeout=15):
        r = subprocess.run(
            argv, capture_output=True, text=True,
            cwd=self._root, env=dict(_GIT_ENV),
            timeout=timeout, shell=False,
        )
        # rstrip only — git status --porcelain lines have significant leading whitespace
        return r.stdout.rstrip(), r.returncode

    def snapshot(self):
        """Return {sha, dirty_files, timestamp}. dirty_files are git-tracked changes."""
        sha_raw, rc = self._run([_GIT, 'rev-parse', 'HEAD'])
        sha = sha_raw.strip() if rc == 0 else 'no_commits'
        status, _ = self._run([_GIT, 'status', '--porcelain'])
        dirty = self._parse_status(status)
        return {'sha': sha, 'dirty_files': sorted(dirty), 'timestamp': time.time()}

    def changed_files(self):
        """Return list of currently changed file paths (relative to CPC root)."""
        status, _ = self._run([_GIT, 'status', '--porcelain'])
        return sorted(self._parse_status(status))

    @staticmethod
    def _parse_status(status_text):
        files = []
        for line in (status_text or '').splitlines():
            if len(line) < 3:
                continue
            raw = line[3:].strip()
            if ' -> ' in raw:
                raw = raw.split(' -> ', 1)[1].strip()
            if raw:
                files.append(raw)
        return files


# ── CpcSessionStore ───────────────────────────────────────────────────────────

class CpcSessionStore:
    """Manages the cpc_coding_sessions table. Enforces one active session per task."""

    def __init__(self, store):
        self.store = store

    def active_session_for_task(self, task_id):
        """Return the active session row for this task, or None."""
        row = self.store.db.execute(
            "SELECT * FROM cpc_coding_sessions WHERE task_id=? AND status='active'",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def session(self, session_id):
        row = self.store.db.execute(
            'SELECT * FROM cpc_coding_sessions WHERE id=?', (session_id,),
        ).fetchone()
        if not row:
            raise Denied(f'Coding session {session_id!r} not found')
        return dict(row)

    def create_session(self, task_id, owner, scope_files, git_before, actor):
        """Create an active session. Raises Denied if task already has one."""
        existing = self.active_session_for_task(task_id)
        if existing:
            raise Denied(
                f'Task {task_id!r} already has an active coding session '
                f'(owner={existing["owner"]!r}); concurrent sessions denied'
            )
        session_id = uid()
        now = time.time()
        self.store.db.execute(
            'INSERT INTO cpc_coding_sessions'
            '(id,task_id,owner,scope_files,git_before,status,actor,created) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (session_id, task_id, owner,
             json.dumps(scope_files, sort_keys=True),
             json.dumps(git_before, sort_keys=True),
             'active', actor, now),
        )
        return self.session(session_id)

    def complete_session(self, session_id, git_after, test_results, status, actor):
        """Mark session completed or failed with results."""
        if status not in ('completed', 'failed'):
            raise ValueError(f'Invalid session status: {status!r}')
        now = time.time()
        self.store.db.execute(
            'UPDATE cpc_coding_sessions SET git_after=?,test_results=?,status=?,'
            'actor=?,completed=? WHERE id=?',
            (json.dumps(git_after, sort_keys=True),
             json.dumps(test_results, sort_keys=True),
             status, actor, now, session_id),
        )
        return self.session(session_id)


# ── CpcScopeGuard ─────────────────────────────────────────────────────────────

class CpcScopeGuard:
    """Validates scope_files lists and detects out-of-scope file modifications."""

    def validate_scope_files(self, scope_files_json):
        """Parse and validate scope_files JSON string. Returns list of path strings."""
        try:
            scope = json.loads(scope_files_json)
        except (json.JSONDecodeError, ValueError):
            raise Denied('scope_files must be a JSON-encoded list of strings')
        if not isinstance(scope, list) or not scope:
            raise Denied('scope_files must be a non-empty JSON list')
        if len(scope) > _SCOPE_MAX_FILES:
            raise Denied(f'scope_files exceeds maximum of {_SCOPE_MAX_FILES} entries')
        validated = []
        for entry in scope:
            if not isinstance(entry, str) or not entry.strip():
                raise Denied('Each scope_files entry must be a non-empty string')
            if len(entry) > _SCOPE_MAX_PATH_LEN:
                raise Denied(f'scope_files path too long: {entry!r}')
            if entry.startswith('/'):
                raise Denied(f'scope_files path must be relative: {entry!r}')
            if '\x00' in entry or '\\' in entry:
                raise Denied(f'scope_files path contains unsafe characters: {entry!r}')
            for part in entry.split('/'):
                if part in _BLOCKED_SCOPE_PARTS or part.startswith('.'):
                    raise Denied(f'scope_files path component not permitted: {part!r}')
            validated.append(entry.strip('/'))
        return validated

    def check_scope(self, current_dirty, before_dirty, scope_files):
        """
        Detect files changed by the coding session that are outside approved scope.

        current_dirty: list of currently changed files (from git status)
        before_dirty:  list of files already dirty at session start
        scope_files:   list of allowed paths/dirs from session
        """
        before_set = set(before_dirty)
        new_changes = [f for f in current_dirty if f not in before_set]
        violations = [f for f in new_changes if not self._in_scope(f, scope_files)]
        return {
            'ok': len(violations) == 0,
            'new_changes': sorted(new_changes),
            'violations': sorted(violations),
            'scope_files': sorted(scope_files),
            'before_dirty_count': len(before_set),
        }

    @staticmethod
    def _in_scope(path, scope_files):
        for s in scope_files:
            if path == s:
                return True
            s_dir = s.rstrip('/')
            if path.startswith(s_dir + '/'):
                return True
        return False


# ── CpcTaskPlanner ────────────────────────────────────────────────────────────

class CpcTaskPlanner:
    """Reads CPC project context and returns structured implementation plan template."""

    def __init__(self, cpc_root=None):
        self._root = Path(cpc_root or CPC_ROOT)

    def plan(self):
        """Produce a CPC project overview + implementation plan template dict."""
        pkg_json = {}
        pkg_path = self._root / 'package.json'
        try:
            if pkg_path.is_file() and not pkg_path.is_symlink():
                pkg_json = json.loads(pkg_path.read_text(encoding='utf-8', errors='replace')[:8192])
        except Exception:
            pass

        top_entries = self._list_top(self._root)
        backend_entries = self._list_top(self._root / 'backend')
        frontend_entries = self._list_top(self._root / 'frontend')

        git = CpcGitReader(self._root)
        snap = git.snapshot()

        return {
            'workspace': 'cpc',
            'project': {
                'name': pkg_json.get('name', 'unknown'),
                'version': pkg_json.get('version', 'unknown'),
                'scripts': list(pkg_json.get('scripts', {}).keys()),
                'top_level': top_entries,
                'backend': backend_entries,
                'frontend': frontend_entries,
            },
            'git_state': {
                'sha': snap['sha'],
                'dirty_files': snap['dirty_files'],
                'dirty_count': len(snap['dirty_files']),
            },
            'approved_test_commands': [k for k in CPC_TEST_COMMANDS if k != 'none'],
            'implementation_plan_template': {
                'actual_problem': None,
                'expected_result': None,
                'affected_area': None,
                'do_not_change': None,
                'acceptance_criteria': None,
            },
            'note': (
                'Fill implementation_plan_template. Then cpc_coding_start with '
                'exact scope_files (relative CPC paths) and owner (claude_code|codex). '
                'cpc_coding_start requires human approval.'
            ),
            'generated_at': snap['timestamp'],
        }

    def _list_top(self, path):
        entries = []
        try:
            p = Path(path)
            if not p.is_dir() or p.is_symlink():
                return entries
            for entry in sorted(p.iterdir()):
                name = entry.name
                if name.startswith('.') or name in ('node_modules', '__pycache__',
                                                     '.venv', 'backups'):
                    continue
                entries.append({'name': name, 'is_dir': entry.is_dir()})
        except (PermissionError, OSError):
            pass
        return entries


# ── CpcTestRunner ─────────────────────────────────────────────────────────────

class CpcTestRunner:
    """Runs approved CPC test commands. Records exact command, exit code and output."""

    def __init__(self, cpc_root=None):
        self._root = str(Path(cpc_root or CPC_ROOT).resolve())

    def run(self, cmd_name, check):
        """Run an approved test command. Returns test result dict."""
        if cmd_name not in CPC_TEST_COMMANDS:
            raise Denied(
                f'Test command {cmd_name!r} is not in the approved list: '
                f'{sorted(k for k in CPC_TEST_COMMANDS if k != "none")}'
            )
        if cmd_name == 'none' or CPC_TEST_COMMANDS[cmd_name] is None:
            return {
                'command': 'none',
                'description': 'No tests requested',
                'passed': None,
                'skipped': True,
                'browser_verification_required': True,
                'note': 'Tests were skipped; browser verification required before marking PASS.',
            }
        cfg = CPC_TEST_COMMANDS[cmd_name]
        argv = cfg['argv']
        exe = Path(argv[0])
        if not exe.is_file() or not os.access(exe, os.X_OK):
            raise Denied(f'Test executable not available: {argv[0]}')
        check()
        env = {
            'PATH': f"{NODE_BIN.parent}:/usr/bin:/bin:/usr/local/bin",
            'HOME': str(Path.home()),
            'CI': '1',
            'PYTHONPATH': self._root,
            'LANG': 'en_US.UTF-8',
            'TZ': 'Asia/Qatar',
        }
        try:
            r = subprocess.run(
                argv, capture_output=True, text=True,
                cwd=self._root, env=env,
                timeout=cfg['timeout'], shell=False,
            )
            check()
            return {
                'command': cmd_name,
                'argv': argv,
                'description': cfg['description'],
                'exit_code': r.returncode,
                'stdout': redact(r.stdout[-4096:]),
                'stderr': redact(r.stderr[-2048:]),
                'passed': r.returncode == 0,
                'browser_verification_required': cfg['browser_required'],
            }
        except subprocess.TimeoutExpired:
            check()
            return {
                'command': cmd_name,
                'argv': argv,
                'description': cfg['description'],
                'exit_code': None,
                'passed': False,
                'error': f'Test timed out after {cfg["timeout"]}s',
                'browser_verification_required': cfg['browser_required'],
            }


# ── CpcCompletionReport ───────────────────────────────────────────────────────

def build_completion_report(session, scope_result, test_result):
    """Return the structured completion report required by Phase 7 spec."""
    scope_ok = scope_result['ok']
    test_passed = test_result.get('passed')
    browser_needed = test_result.get('browser_verification_required', False)

    if not scope_ok:
        status = 'FAIL'
        blockers = [f'Out-of-scope files modified: {scope_result["violations"]}']
    elif test_passed is None or test_result.get('skipped'):
        status = 'NOT_VERIFIED'
        blockers = ['Tests skipped; manual/browser verification required']
    elif not test_passed:
        status = 'FAIL'
        blockers = [f'Test command {test_result.get("command")!r} exited non-zero']
    elif browser_needed:
        status = 'NOT_VERIFIED'
        blockers = ['Tests passed but browser/UI verification still required']
    else:
        status = 'PASS'
        blockers = []

    return {
        'task_id': session['task_id'],
        'session_id': session['id'],
        'coding_owner': session['owner'],
        'files_changed': scope_result['new_changes'],
        'unrelated_change_status': 'VIOLATION' if not scope_ok else 'CLEAN',
        'violations': scope_result['violations'],
        'scope_files': scope_result['scope_files'],
        'tests_run': [test_result],
        'status': status,
        'evidence': {
            'scope_check': scope_result,
            'test_result': test_result,
            'session_created': session['created'],
            'session_completed': time.time(),
        },
        'remaining_blockers': blockers,
        'next_action': (
            'Resolve scope violations and re-run' if not scope_ok else
            'Run browser/UI verification' if status == 'NOT_VERIFIED' else
            'Review report and approve Git commit if satisfied' if status == 'PASS' else
            'Fix failing tests and re-run cpc_coding_complete'
        ),
    }


# ── CpcWorker ─────────────────────────────────────────────────────────────────

class CpcWorker(MacWorker):
    """Phase 7: CPC autonomous engineering worker.

    Extends MacWorker (Phase 6) with CPC pipeline capabilities.
    All Phase 3 and Phase 6 capabilities are fully preserved.
    CPC write mode is OFF by default; opened only via approved cpc_coding_start.
    """

    def __init__(self, store, *, operator=None, cpc=None, node=None):
        super().__init__(store, operator=operator, cpc=cpc, node=node)
        cpc_root = Path(cpc or CPC_ROOT)
        op_root = Path(operator or OPERATOR_ROOT)
        self.cpc_git = CpcGitReader(cpc_root)
        self.cpc_sessions = CpcSessionStore(store)
        self.cpc_scope = CpcScopeGuard()
        self.cpc_planner = CpcTaskPlanner(cpc_root)
        self.cpc_tests = CpcTestRunner(cpc_root)
        # Extend Phase 6 handler registry with Phase 7 capabilities.
        all_handlers = dict(self.handlers)
        all_handlers.update({k: Capability(self, k) for k in _PHASE7_KINDS})
        self.handlers = MappingProxyType(all_handlers)

    def gate(self):
        return Gate(self.store, self.handlers)

    def validate(self, kind, p, project):
        if kind not in _PHASE7_KINDS:
            return super().validate(kind, p, project)
        if not isinstance(p, dict):
            raise Denied('Structured payload required')
        expected = _PHASE7_FIELDS[kind]
        if set(p) != expected:
            raise Denied(f'Payload must have exactly: {sorted(expected)}')

        if kind == 'cpc_plan':
            if p.get('workspace') != 'cpc':
                raise Denied('cpc_plan workspace must be "cpc"')

        elif kind == 'cpc_snapshot':
            if p.get('workspace') != 'cpc':
                raise Denied('cpc_snapshot workspace must be "cpc"')

        elif kind == 'cpc_coding_start':
            if p.get('workspace') != 'cpc':
                raise Denied('cpc_coding_start workspace must be "cpc"')
            if not isinstance(p.get('task_id'), str) or not p['task_id'].strip():
                raise Denied('task_id must be a non-empty string')
            if p.get('owner') not in ALLOWED_OWNERS:
                raise Denied(
                    f'owner must be one of {sorted(ALLOWED_OWNERS)}; got {p.get("owner")!r}'
                )
            # Validate scope_files (JSON-encoded list of paths)
            self.cpc_scope.validate_scope_files(p.get('scope_files', ''))
            # Enforce single active session per task at execute time
            existing = self.cpc_sessions.active_session_for_task(p['task_id'])
            if existing:
                raise Denied(
                    f'Task already has an active coding session '
                    f'(owner={existing["owner"]!r}); concurrent sessions denied'
                )

        elif kind == 'cpc_scope_check':
            if p.get('workspace') != 'cpc':
                raise Denied('cpc_scope_check workspace must be "cpc"')
            if not isinstance(p.get('session_id'), str) or not p['session_id'].strip():
                raise Denied('session_id must be a non-empty string')
            # Verify session exists
            self.cpc_sessions.session(p['session_id'])

        elif kind == 'cpc_coding_complete':
            if p.get('workspace') != 'cpc':
                raise Denied('cpc_coding_complete workspace must be "cpc"')
            if not isinstance(p.get('session_id'), str) or not p['session_id'].strip():
                raise Denied('session_id must be a non-empty string')
            session = self.cpc_sessions.session(p['session_id'])
            if session['status'] != 'active':
                raise Denied(
                    f'Session {p["session_id"]!r} is not active '
                    f'(status={session["status"]!r}); cannot complete'
                )
            if p.get('run_tests') not in CPC_TEST_COMMANDS:
                raise Denied(
                    f'run_tests must be one of {sorted(CPC_TEST_COMMANDS)}; '
                    f'got {p.get("run_tests")!r}'
                )

    def run(self, kind, p, baseline):
        if kind not in _PHASE7_KINDS:
            return super().run(kind, p, baseline)
        check = lambda: self.controls.check(kind, baseline)
        check()
        self.validate(kind, p, 'cpc')

        if kind == 'cpc_plan':
            result = self.cpc_planner.plan()
            check()
            return result

        if kind == 'cpc_snapshot':
            snap = self.cpc_git.snapshot()
            check()
            return {**snap, 'workspace': 'cpc'}

        if kind == 'cpc_coding_start':
            scope_files = self.cpc_scope.validate_scope_files(p['scope_files'])
            git_before = self.cpc_git.snapshot()
            check()
            with self.store.transaction():
                check()
                session = self.cpc_sessions.create_session(
                    task_id=p['task_id'],
                    owner=p['owner'],
                    scope_files=scope_files,
                    git_before=git_before,
                    actor='worker',
                )
            return {
                'session_id': session['id'],
                'task_id': session['task_id'],
                'owner': session['owner'],
                'scope_files': scope_files,
                'git_before_sha': git_before['sha'],
                'dirty_files_at_start': git_before['dirty_files'],
                'status': 'active',
                'write_mode': 'OPEN',
                'note': (
                    'CPC write-mode session opened. Coding worker may now edit '
                    'files listed in scope_files. Run cpc_scope_check to detect '
                    'violations, then cpc_coding_complete (requires approval) to close.'
                ),
            }

        if kind == 'cpc_scope_check':
            session = self.cpc_sessions.session(p['session_id'])
            before_snap = json.loads(session['git_before'])
            scope_files = json.loads(session['scope_files'])
            current_dirty = self.cpc_git.changed_files()
            check()
            scope_result = self.cpc_scope.check_scope(
                current_dirty=current_dirty,
                before_dirty=before_snap['dirty_files'],
                scope_files=scope_files,
            )
            return {
                'session_id': session['id'],
                'owner': session['owner'],
                'task_id': session['task_id'],
                'workspace': 'cpc',
                **scope_result,
            }

        if kind == 'cpc_coding_complete':
            session = self.cpc_sessions.session(p['session_id'])
            before_snap = json.loads(session['git_before'])
            scope_files = json.loads(session['scope_files'])
            current_dirty = self.cpc_git.changed_files()
            check()
            scope_result = self.cpc_scope.check_scope(
                current_dirty=current_dirty,
                before_dirty=before_snap['dirty_files'],
                scope_files=scope_files,
            )
            test_result = self.cpc_tests.run(p['run_tests'], check)
            check()
            git_after = self.cpc_git.snapshot()
            final_status = (
                'completed'
                if scope_result['ok'] and test_result.get('passed') is not False
                else 'failed'
            )
            with self.store.transaction():
                self.cpc_sessions.complete_session(
                    session_id=session['id'],
                    git_after=git_after,
                    test_results=[test_result],
                    status=final_status,
                    actor='worker',
                )
            report = build_completion_report(session, scope_result, test_result)
            report['session_final_status'] = final_status
            return report

        raise Denied('Unimplemented Phase 7 capability')
