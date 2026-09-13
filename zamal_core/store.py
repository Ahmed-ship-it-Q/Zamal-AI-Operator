"""Transactional task context and single-use approvals; standard library only.

Trust boundary: this module, registered handlers and the local OS account are
trusted. Untrusted chat/provider input must never access decide() or SQLite.
The future gateway must authenticate the human before invoking decide().
"""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType


class Conflict(ValueError):
    pass


class Denied(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def uid():
    return str(uuid.uuid4())


STATES = {
    'RECEIVED': {'UNDERSTOOD', 'FAILED'},
    'UNDERSTOOD': {'PLANNED', 'FAILED'},
    'PLANNED': {'RUNNING', 'WAITING_APPROVAL', 'FAILED'},
    'RUNNING': {'VERIFYING', 'RECOVERING', 'FAILED'},
    'VERIFYING': {'COMPLETED', 'FAILED', 'RECOVERING'},
    'WAITING_APPROVAL': {'APPROVED', 'REJECTED', 'PLANNED', 'FAILED'},
    'APPROVED': {'RUNNING', 'FAILED'},
    'REJECTED': set(), 'COMPLETED': set(),
    'FAILED': {'RECOVERING'}, 'RECOVERING': {'FAILED'},
}
CONTEXT_FIELDS = frozenset({
    'inputs', 'rules', 'decisions', 'plan', 'selected_ai_tool', 'affected_files',
    'git_state', 'test_results', 'errors', 'retries', 'final_result', 'next_action',
    'scope', 'restrictions', 'evidence',
})
DEFAULT_CONTEXT = {
    'inputs': [], 'rules': [], 'decisions': [], 'plan': [], 'selected_ai_tool': None,
    'affected_files': [], 'git_state': None, 'test_results': [], 'errors': [],
    'retries': [], 'final_result': None, 'next_action': None,
}


class Policy:
    SAFE = frozenset({'read', 'search', 'inspect', 'analyze', 'research', 'plan',
                      'draft', 'prepare_code', 'test', 'build', 'compare',
                      'diagnose', 'verify',
                      'service_status', 'browser_open', 'terminal_run', 'permission_check',
                      # Phase 7: read-only CPC pipeline actions
                      'cpc_plan', 'cpc_snapshot', 'cpc_scope_check',
                      # Phase 8: read-only verification actions
                      'git_capture', 'diff_review', 'capture_evidence',
                      # Phase 10: read-only connection/event/export actions
                      'connection_status', 'event_inspect', 'workflow_export',
                      # Phase 12: read-only monitor/security/usage actions
                      'monitor_sweep', 'security_audit', 'usage_summary', 'output_list',
                      # Phase 13: read-only automation observation and listing
                      'automation_observe', 'automation_list',
                      # Job application: read-only open/plan/preflight/status
                      'job_app_open', 'job_app_plan', 'job_app_preflight', 'job_app_status'})
    PROTECTED = frozenset({'email_send', 'job_submit', 'profile_mutate', 'git_push',
                           'git_merge', 'deploy', 'delete', 'git_reset', 'git_revert',
                           'payment', 'credential_change', 'security_change',
                           'external_irreversible', 'ai_inference',
                           'service_start', 'service_stop',
                           # Phase 7: write-mode and completion require approval
                           'cpc_coding_start', 'cpc_coding_complete',
                           # Phase 8: protected git and verification operations
                           'verify_tests', 'git_worktree_create', 'git_rollback',
                           # Phase 10: protected connection management
                           'connection_enable', 'connection_disable',
                           # Phase 12: protected recovery and output purge
                           'service_recover', 'output_purge',
                           # Phase 13: protected automation activation
                           'automation_activate'})

    def __init__(self, protect_commits=True):
        self.protect_commits = bool(protect_commits)

    def requires_approval(self, kind):
        if isinstance(kind, str) and kind.startswith('biz_'):
            from .business_ops import SAFE, PROTECTED
            if kind in PROTECTED: return True
            if kind in SAFE: return False
        if isinstance(kind, str) and kind.startswith('ops_'):
            from .personal_ops import _PHASE14_SAFE, _PHASE14_PROTECTED
            if kind in _PHASE14_PROTECTED: return True
            if kind in _PHASE14_SAFE: return False
        if isinstance(kind, str) and kind.startswith('job_app_'):
            from .job_app import JOB_APP_SAFE, JOB_APP_PROTECTED
            if kind in JOB_APP_PROTECTED: return True
            if kind in JOB_APP_SAFE: return False
        if kind == 'git_commit':
            return self.protect_commits
        if kind in self.PROTECTED:
            return True
        if kind in self.SAFE:
            return False
        raise Denied('Unknown action kind; denied by default')


class Store:
    def __init__(self, path=None):
        self.path = Path(path or Path(__file__).resolve().parent.parent / 'state' / 'operator.sqlite3')
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise Denied('Database must not be a symlink')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.execute('PRAGMA synchronous=FULL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5, 6):
            self.close()
            raise Conflict('Unsupported schema version')
        self.db.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
              fingerprint TEXT NOT NULL, command TEXT NOT NULL, task_type TEXT NOT NULL,
              project TEXT NOT NULL, state TEXT NOT NULL, context TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actions (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
              request_key TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
              binding TEXT NOT NULL, protected INTEGER NOT NULL, status TEXT NOT NULL,
              result TEXT, created REAL NOT NULL, UNIQUE(task_id, request_key)
            );
            CREATE TABLE IF NOT EXISTS approvals (
              id TEXT PRIMARY KEY, action_id TEXT NOT NULL UNIQUE REFERENCES actions(id),
              binding TEXT NOT NULL, status TEXT NOT NULL, expires REAL NOT NULL,
              actor TEXT, note TEXT, created REAL NOT NULL, decided REAL
            );
            CREATE TABLE IF NOT EXISTS audit (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
              task_id TEXT NOT NULL REFERENCES tasks(id), event TEXT NOT NULL, details TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS approvals_queue ON approvals(status, created);
            CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
              BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
              BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
            CREATE TABLE IF NOT EXISTS controls (
              id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 0,
              stop_everything INTEGER NOT NULL DEFAULT 0,
              pause_external_actions INTEGER NOT NULL DEFAULT 0,
              read_only_mode INTEGER NOT NULL DEFAULT 0,
              disable_github_push INTEGER NOT NULL DEFAULT 0,
              disable_email_send INTEGER NOT NULL DEFAULT 0,
              stop_everything_epoch INTEGER NOT NULL DEFAULT 0,
              pause_external_actions_epoch INTEGER NOT NULL DEFAULT 0,
              read_only_mode_epoch INTEGER NOT NULL DEFAULT 0,
              disable_github_push_epoch INTEGER NOT NULL DEFAULT 0,
              disable_email_send_epoch INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO controls(id) SELECT 1 WHERE (SELECT user_version FROM pragma_user_version) < 5;
            CREATE TABLE IF NOT EXISTS control_audit (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
              flag TEXT NOT NULL, enabled INTEGER NOT NULL, actor TEXT NOT NULL,
              reason TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS control_audit_no_update BEFORE UPDATE ON control_audit
              BEGIN SELECT RAISE(ABORT, 'control audit is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS control_audit_no_delete BEFORE DELETE ON control_audit
              BEGIN SELECT RAISE(ABORT, 'control audit is append-only'); END;
            CREATE TABLE IF NOT EXISTS worker_outputs (
              id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
              created REAL NOT NULL, deleted REAL
            );
            CREATE TABLE IF NOT EXISTS mac_processes (
              id TEXT PRIMARY KEY, service TEXT NOT NULL UNIQUE,
              pid INTEGER, started REAL, stopped REAL,
              status TEXT NOT NULL DEFAULT 'unknown', actor TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS cpc_coding_sessions (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL REFERENCES tasks(id),
              owner TEXT NOT NULL,
              scope_files TEXT NOT NULL,
              git_before TEXT,
              git_after TEXT,
              test_results TEXT,
              status TEXT NOT NULL DEFAULT 'active',
              actor TEXT NOT NULL,
              created REAL NOT NULL,
              completed REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_session
              ON cpc_coding_sessions(task_id) WHERE status='active';
            CREATE TABLE IF NOT EXISTS git_snapshots (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL REFERENCES tasks(id),
              label TEXT NOT NULL,
              branch TEXT,
              sha TEXT NOT NULL,
              staged TEXT NOT NULL DEFAULT '[]',
              unstaged TEXT NOT NULL DEFAULT '[]',
              untracked TEXT NOT NULL DEFAULT '[]',
              dirty_files TEXT NOT NULL DEFAULT '[]',
              captured_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verification_records (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL REFERENCES tasks(id),
              session_id TEXT REFERENCES cpc_coding_sessions(id),
              verifier TEXT NOT NULL,
              test_command TEXT,
              test_argv TEXT,
              exit_code INTEGER,
              stdout TEXT,
              stderr TEXT,
              passed INTEGER,
              browser_required INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL,
              evidence TEXT,
              verified_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ver_by_session
              ON verification_records(session_id, verified_at);
            CREATE TABLE IF NOT EXISTS task_worktrees (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL REFERENCES tasks(id),
              branch TEXT NOT NULL,
              worktree_path TEXT NOT NULL,
              base_sha TEXT NOT NULL,
              owner TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'active',
              created REAL NOT NULL,
              closed REAL
            );
            CREATE TABLE IF NOT EXISTS rollback_log (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL REFERENCES tasks(id),
              session_id TEXT REFERENCES cpc_coding_sessions(id),
              scope_files TEXT NOT NULL,
              base_sha TEXT NOT NULL,
              actor TEXT NOT NULL,
              reason TEXT NOT NULL,
              rolled_back_at REAL NOT NULL,
              outcome TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inbound_events (
              id TEXT PRIMARY KEY,
              event_key TEXT NOT NULL UNIQUE,
              event_type TEXT NOT NULL,
              source TEXT NOT NULL,
              payload TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'received',
              task_id TEXT REFERENCES tasks(id),
              rejection_reason TEXT,
              received_at REAL NOT NULL,
              processed_at REAL
            );
            CREATE TABLE IF NOT EXISTS n8n_exports (
              id TEXT PRIMARY KEY,
              version TEXT NOT NULL,
              manifest TEXT NOT NULL,
              file_path TEXT NOT NULL UNIQUE,
              sha256 TEXT NOT NULL,
              exported_at REAL NOT NULL
            );
            PRAGMA user_version=6;
            COMMIT;
        ''')

        from .approval_contract import migrate
        migrate(self)
        from .gateway import migrate_gateway
        migrate_gateway(self)
        from .telegram_bot import migrate_telegram
        migrate_telegram(self)
        from .monitor import migrate_monitor
        migrate_monitor(self)
        from .automation import migrate_automation
        migrate_automation(self)
        from .personal_ops import migrate_personal_ops
        migrate_personal_ops(self)
        from .job_app import migrate_job_app
        migrate_job_app(self)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _audit(self, task_id, event, **details):
        self.db.execute('INSERT INTO audit(timestamp,task_id,event,details) VALUES(?,?,?,?)',
                        (time.time(), task_id, event, encode(details)))

    def _row(self, table, record_id):
        # table names are internal constants, never user input.
        row = self.db.execute(f'SELECT * FROM {table} WHERE id=?', (record_id,)).fetchone()
        if row is None:
            raise Conflict('Record not found')
        return dict(row)

    def create_task(self, command, task_type, project, request_key, context=None):
        if not all(isinstance(v, str) and v.strip() for v in (command, task_type, project, request_key)):
            raise ValueError('Non-empty command, task_type, project and request_key required')
        context = context or {}
        if set(context) - CONTEXT_FIELDS:
            raise ValueError('Unknown context fields')
        merged = {**DEFAULT_CONTEXT, **context}
        fingerprint = digest([command, task_type, project, merged])
        with self.transaction():
            previous = self.db.execute('SELECT id,fingerprint FROM tasks WHERE request_key=?', (request_key,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Conflict('Idempotency key reused with different task')
                return self.task(previous['id'])
            task_id, now = uid(), time.time()
            self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (task_id, request_key, fingerprint, command, task_type, project,
                             'RECEIVED', encode(merged), 0, now, now))
            self._audit(task_id, 'TASK_RECEIVED', request_key=request_key)
        return self.task(task_id)

    def task(self, task_id):
        row = self._row('tasks', task_id)
        row['context'] = json.loads(row['context'])
        row['actions'] = [self.action(r[0]) for r in self.db.execute('SELECT id FROM actions WHERE task_id=? ORDER BY created', (task_id,))]
        row['audit'] = [{**dict(r), 'details': json.loads(r['details'])} for r in self.db.execute('SELECT * FROM audit WHERE task_id=? ORDER BY seq', (task_id,))]
        return row

    def tasks(self):
        return [dict(r) for r in self.db.execute('SELECT id,task_type,project,state,revision,created,updated FROM tasks ORDER BY created')]

    def patch_context(self, task_id, patch, expected_revision):
        if not isinstance(patch, dict) or set(patch) - CONTEXT_FIELDS:
            raise ValueError('Only task context fields may be patched')
        with self.transaction():
            task = self._row('tasks', task_id)
            if task['revision'] != expected_revision:
                raise Conflict('Stale task revision')
            if task['state'] in ('COMPLETED', 'REJECTED'):
                raise Conflict('Terminal task is immutable')
            context = {**json.loads(task['context']), **patch}
            self.db.execute('UPDATE tasks SET context=?,revision=revision+1,updated=? WHERE id=?', (encode(context), time.time(), task_id))
            self._audit(task_id, 'CONTEXT_UPDATED', patch=patch)
        return self.task(task_id)

    def _move(self, task_id, state):
        task = self._row('tasks', task_id)
        if state not in STATES.get(task['state'], set()):
            raise Conflict(f"Invalid transition {task['state']} -> {state}")
        self.db.execute('UPDATE tasks SET state=?,revision=revision+1,updated=? WHERE id=?', (state, time.time(), task_id))
        self._audit(task_id, 'STATE_CHANGED', previous=task['state'], state=state)

    def transition(self, task_id, state, expected_revision):
        # Approval/execution states can only be changed by the gate, never this API.
        allowed = {('RECEIVED', 'UNDERSTOOD'), ('UNDERSTOOD', 'PLANNED'), ('VERIFYING', 'COMPLETED')}
        with self.transaction():
            task = self._row('tasks', task_id)
            if task['revision'] != expected_revision:
                raise Conflict('Stale task revision')
            if (task['state'], state) not in allowed:
                raise Denied('Transition is reserved for execution/approval engine')
            if state == 'COMPLETED':
                results = json.loads(task['context'])['test_results']
                if not isinstance(results, list) or not results or not all(isinstance(r, dict) and r.get('passed') is True and r.get('evidence') for r in results):
                    raise Denied('Completion requires passing verification evidence')
            self._move(task_id, state)
        return self.task(task_id)

    def action(self, action_id):
        row = self._row('actions', action_id)
        row['payload'] = json.loads(row['payload'])
        row['result'] = json.loads(row['result']) if row['result'] else None
        approval = self.db.execute('SELECT * FROM approvals WHERE action_id=?', (action_id,)).fetchone()
        row['approval'] = dict(approval) if approval else None
        return row

    def approval(self, approval_id):
        from .approval_contract import lifecycle
        a = self._row('approvals', approval_id)
        action = self.action(a['action_id'])
        row = self.db.execute('SELECT material,hash FROM approval_contracts WHERE approval_id=?', (approval_id,)).fetchone()
        return {**a, 'task_id': action['task_id'], 'lifecycle': lifecycle(action),
                'contract': json.loads(row['material']) if row else None,
                'contract_hash': row['hash'] if row else None}

    def approval_queue(self):
        self.expire_approvals()
        return [dict(r) for r in self.db.execute('''SELECT p.*,a.task_id,a.kind,a.payload,
            CASE WHEN p.expires<=? THEN 1 ELSE 0 END AS expired
            FROM approvals p JOIN actions a ON a.id=p.action_id
            WHERE p.status='PENDING' ORDER BY p.created''', (time.time(),))]

    def expire_approvals(self):
        with self.transaction():
            rows = self.db.execute("SELECT p.id,a.id action_id,a.task_id FROM approvals p JOIN actions a ON a.id=p.action_id WHERE p.status IN ('PENDING','APPROVED') AND p.expires<=? AND a.status='PENDING'", (time.time(),)).fetchall()
            for r in rows:
                self.db.execute("UPDATE approvals SET status='EXPIRED' WHERE id=?", (r['id'],))
                self.db.execute("UPDATE actions SET status='CANCELLED' WHERE id=?", (r['action_id'],))
                task = self._row('tasks', r['task_id'])
                if task['state'] == 'WAITING_APPROVAL':
                    self._move(r['task_id'], 'PLANNED')
                elif task['state'] == 'APPROVED':
                    self._move(r['task_id'], 'FAILED')
                self._audit(r['task_id'], 'APPROVAL_EXPIRED', approval_id=r['id'], action_id=r['action_id'])

    def decide(self, approval_id, choice, *, actor, note=''):
        """Trusted human control plane ONLY; no conversational parser calls this."""
        if choice not in ('APPROVE', 'EDIT', 'REJECT') or not isinstance(actor, str) or not actor.strip():
            raise ValueError('Explicit choice and authenticated actor required')
        if choice == 'EDIT' and not note.strip():
            raise ValueError('EDIT requires requested changes')
        self.expire_approvals()
        with self.transaction():
            approval = self._row('approvals', approval_id)
            if approval['status'] == 'EXPIRED':
                raise Denied('Approval expired; new request required')
            action = self._row('actions', approval['action_id'])
            task = self._row('tasks', action['task_id'])
            if approval['status'] != 'PENDING' or action['status'] != 'PENDING' or task['state'] != 'WAITING_APPROVAL':
                raise Conflict('Approval is no longer pending')
            if approval['binding'] != action['binding']:
                raise Denied('Approval binding mismatch')
            if choice == 'APPROVE':
                from .approval_contract import validate
                validate(self, self.action(action['id']), 'decision')
            if choice == 'APPROVE' and approval['expires'] <= time.time():
                raise Denied('Approval expired; EDIT to prepare a new request')
            status = {'APPROVE': 'APPROVED', 'EDIT': 'EDIT_REQUESTED', 'REJECT': 'REJECTED'}[choice]
            self.db.execute('UPDATE approvals SET status=?,actor=?,note=?,decided=? WHERE id=?', (status, actor, note, time.time(), approval_id))
            if choice != 'APPROVE':
                self.db.execute("UPDATE actions SET status='CANCELLED' WHERE id=?", (action['id'],))
            if choice == 'EDIT':
                context = json.loads(task['context'])
                context['next_action'] = {'requested_edits': note, 'superseded_approval_id': approval_id}
                self.db.execute('UPDATE tasks SET context=? WHERE id=?', (encode(context), task['id']))
            self._move(action['task_id'], {'APPROVE': 'APPROVED', 'EDIT': 'PLANNED', 'REJECT': 'REJECTED'}[choice])
            self._audit(action['task_id'], 'APPROVAL_DECIDED', approval_id=approval_id, choice=choice, actor=actor, note=note)
        return self.action(action['id'])

    def backup(self, destination):
        """Consistent SQLite backup to a NEW file, never overwriting prior backups."""
        destination = Path(destination)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        target = sqlite3.connect(destination)
        try:
            self.db.backup(target)
        finally:
            target.close()
        return str(destination)


class Gate:
    def __init__(self, store, handlers=None, policy=None):
        self.store = store
        self.policy = policy or Policy()
        # Only trusted application startup registers handlers; payloads cannot select shell/code.
        self._handlers = MappingProxyType(dict(handlers or {}))
        for kind, handler in self._handlers.items():
            self.policy.requires_approval(kind)
            if not callable(handler):
                raise ValueError('Handler must be callable')

    def propose(self, task_id, kind, payload, request_key, ttl_seconds=86400):
        protected = self.policy.requires_approval(kind)
        if isinstance(kind,str) and kind.startswith('biz_'):
            from .business_ops import validate
            validate(kind,payload,self.store.task(task_id)['project'])
        if isinstance(kind,str) and kind.startswith('ops_'):
            from .personal_ops import validate_proposal
            validate_proposal(kind,payload,self.store.task(task_id)['project'])
        if isinstance(kind,str) and kind.startswith('job_app_'):
            from .job_app import validate_job_app
            validate_job_app(kind,payload,self.store.task(task_id)['project'])
        if not isinstance(payload, dict) or not isinstance(request_key, str) or not request_key.strip():
            raise ValueError('Structured payload and request_key required')
        if not isinstance(ttl_seconds, (int, float)) or not 0 < ttl_seconds <= 604800:
            raise ValueError('Approval TTL must be >0 and <=7 days')
        binding = digest({'task_id': task_id, 'kind': kind, 'payload': payload})
        s = self.store
        with s.transaction():
            prior = s.db.execute('SELECT id,binding FROM actions WHERE task_id=? AND request_key=?', (task_id, request_key)).fetchone()
            if prior:
                if prior['binding'] != binding:
                    raise Conflict('Action idempotency key reused with different payload')
                return s.action(prior['id'])
            task = s._row('tasks', task_id)
            if task['state'] != 'PLANNED':
                raise Conflict('Action requires a PLANNED task')
            if s.db.execute("SELECT 1 FROM actions WHERE task_id=? AND status IN ('PENDING','EXECUTING')", (task_id,)).fetchone():
                raise Conflict('Task already has an open action')
            action_id, now = uid(), time.time()
            s.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?)', (action_id, task_id, request_key, kind, encode(payload), binding, int(protected), 'PENDING', None, now))
            if protected:
                approval_id = uid()
                s.db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?)', (approval_id, action_id, binding, 'PENDING', now + ttl_seconds, None, None, now, None))
                s._move(task_id, 'WAITING_APPROVAL')
                from .approval_contract import seal
                seal(s, s.action(action_id), self._handlers.get(kind))
                s._audit(task_id, 'APPROVAL_REQUESTED', action_id=action_id, approval_id=approval_id, binding=binding)
            s._audit(task_id, 'ACTION_PROPOSED', action_id=action_id, kind=kind, binding=binding, protected=protected)
        return s.action(action_id)

    def resolve_approval(self, approval_id, choice, *, task_id, action_id, actor, note=''):
        """Explicit authenticated local decision; APPROVE dispatches stored action only."""
        a = self.store.approval(approval_id)
        if a['task_id'] != task_id or a['action_id'] != action_id:
            raise Denied('Approval task/action identity mismatch')
        self.store.decide(approval_id, choice, actor=actor, note=note)
        if choice == 'APPROVE':
            return self.execute(action_id, approval_id=approval_id, task_id=task_id)
        return self.store.approval(approval_id)

    def execute(self, action_id, *, approval_id=None, task_id=None):
        self.store.expire_approvals()
        try:
            current = self.store.action(action_id)
            if task_id is not None and current['task_id'] != task_id:
                raise Denied('Approval task identity mismatch')
            if approval_id is not None and (not current['approval'] or current['approval']['id'] != approval_id):
                raise Denied('Approval action identity mismatch')
            return self._execute(action_id)
        except Denied:
            # Keep rejected attempts visible without recording secret-bearing payload/error text.
            with self.store.transaction():
                action = self.store._row('actions', action_id)
                self.store._audit(action['task_id'], 'EXECUTION_DENIED', action_id=action_id)
            raise

    def _execute(self, action_id):
        from .safety import Controls
        controls = Controls(self.store)
        s = self.store
        with s.transaction():
            action = s.action(action_id)
            if action['status'] != 'PENDING':
                raise Denied('Action already claimed, cancelled or finished; never replay automatically')
            kind = action['kind']
            baseline = controls.check(kind)
            protected = self.policy.requires_approval(kind) or action['protected']
            binding = digest({'task_id': action['task_id'], 'kind': kind, 'payload': action['payload']})
            if binding != action['binding']:
                raise Denied('Action payload integrity failure')
            task = s._row('tasks', action['task_id'])
            if protected:
                a = action['approval']
                if not a or a['status'] != 'APPROVED' or a['binding'] != binding or a['expires'] <= time.time() or not a['actor'] or a['decided'] is None or task['state'] != 'APPROVED':
                    raise Denied('Valid, unexpired, action-bound human approval required')
            elif task['state'] != 'PLANNED':
                raise Denied('Task is not ready')
            handler = self._handlers.get(kind)
            if handler is None:
                raise Denied('No worker connected for this action; approval does not grant a capability')
            if protected:
                from .approval_contract import validate
                validate(s, action, 'execution', handler)
            if hasattr(handler, 'validate'):
                handler.validate(action['payload'], task['project'])
            if protected:
                from .approval_contract import worker_safety
                worker_safety(s, handler, action['payload'])
                validate(s, action, 'execution', handler)
                if action['approval']['expires'] <= time.time():
                    raise Denied('Approval expired during preflight')
            s.db.execute("UPDATE actions SET status='EXECUTING' WHERE id=?", (action_id,))
            if protected:
                s.db.execute("UPDATE approvals SET status='CONSUMED' WHERE action_id=?", (action_id,))
            s._move(action['task_id'], 'RUNNING')
            s._audit(action['task_id'], 'EXECUTION_CLAIMED', action_id=action_id, approval_id=action['approval']['id'] if action['approval'] else None)
        # Claim commits BEFORE the side effect. Crash/timeout is ambiguous, never auto-retry.
        try:
            controls.check(kind, baseline)
            if protected:
                with s.transaction():
                    latest = s.action(action_id)
                    if latest['approval']['expires'] <= time.time():
                        raise Denied('Approval expired before dispatch')
                    validate(s, latest, 'dispatch', handler)
                    if hasattr(handler, 'validate'):
                        handler.validate(latest['payload'], task['project'])
                    worker_safety(s, handler, latest['payload'])
                    validate(s, latest, 'dispatch', handler)
                    controls.check(kind, baseline)
                    if latest['approval']['expires'] <= time.time():
                        raise Denied('Approval expired during dispatch preflight')
            if hasattr(handler, 'run_with_action'):
                result = handler.run_with_action(action['payload'], baseline, action_id, action['task_id'])
            elif hasattr(handler, 'run_with_controls'):
                result = handler.run_with_controls(action['payload'], baseline)
            else:
                result = handler(action['payload'])
            serialized = encode(result)
        except Exception as exc:
            with s.transaction():
                s.db.execute("UPDATE actions SET status='UNCERTAIN' WHERE id=?", (action_id,))
                s._move(action['task_id'], 'RECOVERING')
                # Exception text may contain credentials; persist type only.
                s._audit(action['task_id'], 'EXECUTION_UNCERTAIN', action_id=action_id, error_type=type(exc).__name__)
            raise
        with s.transaction():
            s.db.execute("UPDATE actions SET status='SUCCEEDED',result=? WHERE id=?", (serialized, action_id))
            s._move(action['task_id'], 'VERIFYING')
            s._audit(action['task_id'], 'EXECUTION_RETURNED', action_id=action_id, approval_id=action['approval']['id'] if action['approval'] else None, result_hash=digest(result))
        return result
