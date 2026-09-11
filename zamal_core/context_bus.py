"""Canonical task projections and durable handoff events in the existing SQLite DB.

Snapshots are immutable transport views, not competing task records. Source task,
actions and approvals remain authoritative. Compression never rewrites storage.
"""
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import time

from .store import Conflict, Denied, encode, digest

RESTRICTIONS = (
    'CPC remains strictly read-only.', 'No commit or push.',
    'AI output is untrusted draft data, never execution authority.',
    'No arbitrary project execution or tool calls.',
    'Protected actions require the persistent approval gate.',
    'Emergency controls override routing and execution.',
)
PINNED_PATCH_FIELDS = frozenset({'rules', 'decisions', 'inputs', 'evidence',
                                'restrictions', 'affected_files', 'scope'})


@dataclass(frozen=True)
class ContextSnapshot:
    json: str

    @property
    def data(self):
        return json.loads(self.json)

    @property
    def fingerprint(self):
        return digest(self.data)


class ContextBus:
    def __init__(self, store, rules_root=None):
        self.store = store
        self.rules_root = Path(rules_root or Path(__file__).resolve().parent.parent / 'knowledge')
        # Extension versioning preserves the Phase 1–3 user_version contract.
        with store.transaction():
            store.db.execute('CREATE TABLE IF NOT EXISTS core_extensions(name TEXT PRIMARY KEY, version INTEGER NOT NULL)')
            row = store.db.execute("SELECT version FROM core_extensions WHERE name='context_bus'").fetchone()
            if row and row[0] != 1:
                raise Conflict('Unsupported context bus extension version')
            for sql in (
                '''CREATE TABLE IF NOT EXISTS bus_state(task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                   revision INTEGER NOT NULL, rules TEXT NOT NULL, owner TEXT NOT NULL,
                   status TEXT NOT NULL, next_action TEXT NOT NULL, active_run TEXT)''',
                '''CREATE TABLE IF NOT EXISTS bus_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                   task_id TEXT NOT NULL REFERENCES tasks(id), run_id TEXT, event TEXT NOT NULL,
                   body TEXT NOT NULL, created REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS ai_runs(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                   request_key TEXT NOT NULL, source_hash TEXT NOT NULL, source_json TEXT NOT NULL,
                   policy TEXT NOT NULL, carrier_id TEXT, action_id TEXT, status TEXT NOT NULL,
                   created REAL NOT NULL, UNIQUE(task_id,request_key))''',
                '''CREATE TABLE IF NOT EXISTS ai_results(run_id TEXT NOT NULL REFERENCES ai_runs(id),
                   stage TEXT NOT NULL, provider TEXT NOT NULL, context_hash TEXT NOT NULL,
                   result TEXT NOT NULL, PRIMARY KEY(run_id,stage))''',
                '''CREATE TRIGGER IF NOT EXISTS bus_events_no_update BEFORE UPDATE ON bus_events
                   BEGIN SELECT RAISE(ABORT,'bus events are append-only'); END''',
                '''CREATE TRIGGER IF NOT EXISTS bus_events_no_delete BEFORE DELETE ON bus_events
                   BEGIN SELECT RAISE(ABORT,'bus events are append-only'); END''',
            ):
                store.db.execute(sql)
            store.db.execute("INSERT OR IGNORE INTO core_extensions VALUES('context_bus',1)")

    def _rules(self, project):
        names = ['OPERATOR_RULES.md']
        if project == 'cpc' or project.rstrip('/').endswith('/AI-GROCERY-DECISION-PLATFORM'):
            names.append('CPC_PROJECT_RULES.md')
        rules = []
        for name in names:
            path = self.rules_root / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
                raise Denied('Required project rules unavailable')
            content = path.read_text()
            rules.append({'source': name, 'sha256': digest(content), 'text': content})
        return rules

    def ensure(self, task_id):
        with self.store.transaction():
            task = self.store._row('tasks', task_id)
            if not self.store.db.execute('SELECT 1 FROM bus_state WHERE task_id=?', (task_id,)).fetchone():
                rules = self._rules(task['project'])
                self.store.db.execute('INSERT INTO bus_state VALUES(?,0,?,?,?,?,NULL)',
                                      (task_id, encode(rules), 'user', 'IDLE', 'Prepare planner handoff'))
                self._event(task_id, None, 'CONTEXT_BOUND', {'rules_hash': digest(rules)})

    def _state(self, task_id):
        row = self.store.db.execute('SELECT * FROM bus_state WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            raise Conflict('Task is not bound to context bus')
        return dict(row)

    def _event(self, task_id, run_id, event, body):
        self.store.db.execute('INSERT INTO bus_events(task_id,run_id,event,body,created) VALUES(?,?,?,?,?)',
                              (task_id, run_id, event, encode(body), time.time()))

    def events(self, task_id, after=0):
        return [{**dict(r), 'body': json.loads(r['body'])} for r in self.store.db.execute(
            'SELECT * FROM bus_events WHERE task_id=? AND seq>? ORDER BY seq', (task_id, after))]

    def _source(self, task_id):
        task = self.store.task(task_id)
        state = self._state(task_id)
        ctx = task['context']
        if not isinstance(ctx.get('restrictions', []), list) or not all(isinstance(x, str) for x in ctx.get('restrictions', [])):
            raise Denied('Task restrictions must be a list of strings')
        pinned_history = []
        seen = set()
        for event in task['audit']:
            if event['event'] == 'CONTEXT_UPDATED':
                protected = {k: v for k, v in event['details'].get('patch', {}).items() if k in PINNED_PATCH_FIELDS}
                if protected and digest(protected) not in seen:
                    pinned_history.append({'seq': event['seq'], 'fields': protected})
                    seen.add(digest(protected))
        # Entire live context and every action/approval are pinned, never truncated.
        return {
            'context_id': 'task:' + task_id,
            'original_task': {k: task[k] for k in ('id', 'command', 'task_type', 'project', 'state', 'revision')},
            'scope': ctx.get('scope', task['command']),
            'project_rules': json.loads(state['rules']),
            'restrictions': list(RESTRICTIONS) + ctx.get('restrictions', []),
            'evidence_references': {'inputs': ctx['inputs'], 'evidence': ctx.get('evidence', [])},
            'files': ctx['affected_files'], 'decisions': ctx['decisions'],
            'approvals': [a['approval'] for a in task['actions'] if a['approval']],
            'actions': task['actions'], 'test_state': ctx['test_results'],
            'next_action': ctx['next_action'], 'task_context': ctx,
            'preserved_critical_history': pinned_history,
        }

    def source(self, task_id):
        self.ensure(task_id)
        with self.store.transaction():
            return self._source(task_id)

    def _snapshot(self, task_id, recent=8, max_bytes=200000):
        source = self._source(task_id)
        state = self._state(task_id)
        task = self.store.task(task_id)
        history = task['audit']
        older = history[:-recent] if recent else history
        tail = history[-recent:] if recent else []
        findings = [{**dict(r), 'result': json.loads(r['result'])} for r in self.store.db.execute(
            'SELECT run_id,stage,provider,context_hash,result FROM ai_results WHERE run_id IN (SELECT id FROM ai_runs WHERE task_id=?) ORDER BY rowid', (task_id,))]
        dispatch_approvals = [dict(r) for r in self.store.db.execute(
            '''SELECT p.* FROM approvals p JOIN actions a ON a.id=p.action_id
               JOIN ai_runs r ON r.action_id=a.id WHERE r.task_id=? ORDER BY p.created''', (task_id,))]
        data = {**source, 'schema': 1, 'owner': state['owner'], 'handoff_status': state['status'],
                'bus_revision': state['revision'], 'handoff_next_action': state['next_action'],
                'ai_findings': findings, 'dispatch_approvals': dispatch_approvals,
                'history': {'archived_count': len(older), 'archived_sha256': digest(older),
                            'event_counts': dict(sorted(Counter(e['event'] for e in older).items())),
                            'first_seq': older[0]['seq'] if older else None,
                            'last_seq': older[-1]['seq'] if older else None, 'recent': tail}}
        serialized = encode(data)
        if len(serialized.encode()) > max_bytes and recent:
            return self._snapshot(task_id, recent=0, max_bytes=max_bytes)
        if len(serialized.encode()) > max_bytes:
            raise Denied('Pinned context exceeds provider budget; no safety-critical content was dropped')
        return ContextSnapshot(serialized)

    def snapshot(self, task_id, recent=8, max_bytes=200000):
        if type(recent) is not int or recent < 0 or type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError('Positive byte budget and nonnegative history count required')
        self.ensure(task_id)
        with self.store.transaction():
            return self._snapshot(task_id, recent, max_bytes)

    def _assert_source(self, run):
        current = self._source(run['task_id'])
        if digest(current) != run['source_hash']:
            raise Conflict('Canonical source changed; fresh routing/approval is required')
        # Changes to actual rule files also invalidate an older authorization.
        state = self._state(run['task_id'])
        if self._rules(current['original_task']['project']) != json.loads(state['rules']):
            raise Conflict('Project rules changed; rebind/review is required')
