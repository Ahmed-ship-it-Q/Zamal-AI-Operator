"""Phase 13: Automation Discovery / Improvement.

Observes patterns in existing task/action/event/approval history,
ranks opportunities by frequency, reliability, risk and expected benefit,
and records deduplicated proposals. Nothing is ever activated silently.

SAFE kinds (no approval): automation_observe, automation_list
PROTECTED kinds (approval required): automation_activate

The observer NEVER mutates tasks, actions, approvals or n8n workflows.
Emergency controls always override. CPC stays strictly read-only.
"""
import hashlib
import json
import time
from pathlib import Path

from .store import Denied, Conflict, uid, encode

_PHASE13_SAFE = frozenset({'automation_observe', 'automation_list'})
_PHASE13_PROTECTED = frozenset({'automation_activate'})
_PHASE13_KINDS = _PHASE13_SAFE | _PHASE13_PROTECTED

_PHASE13_FIELDS = {
    'automation_observe': frozenset({'since_hours'}),
    'automation_list':    frozenset({'status'}),
    'automation_activate': frozenset({'proposal_id', 'reason'}),
}
_PHASE13_OPTIONAL = {
    'automation_observe': frozenset({'since_hours'}),
    'automation_list':    frozenset({'status'}),
}

RISK_LOW = 'low'
RISK_MEDIUM = 'medium'
RISK_HIGH = 'high'

STATUS_PENDING = 'pending'
STATUS_APPROVED = 'approved'
STATUS_REJECTED = 'rejected'
STATUS_ACTIVATED = 'activated'

# Minimum frequency to surface a proposal
_MIN_FREQUENCY = 3
# Minimum failure rate (0.0–1.0) to surface a failure pattern
_MIN_FAILURE_RATE = 0.25


def _sig(pattern_type, pattern_key):
    """Stable deduplication key for a proposal."""
    raw = f'{pattern_type}:{pattern_key}'
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def migrate_automation(store):
    """Idempotent Phase 13 schema extension. Uses core_extensions pattern."""
    with store.transaction():
        row = store.db.execute(
            "SELECT version FROM core_extensions WHERE name='automation'"
        ).fetchone()
        if row:
            if row[0] != 1:
                raise Denied('Unknown automation schema version')
            store.db.execute('SELECT id FROM automation_proposals LIMIT 0')
            return
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS automation_proposals (
              id TEXT PRIMARY KEY,
              signature TEXT NOT NULL UNIQUE,
              pattern_type TEXT NOT NULL,
              pattern_key TEXT NOT NULL,
              evidence TEXT NOT NULL,
              recommendation TEXT NOT NULL,
              risk_level TEXT NOT NULL,
              benefit_score REAL NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              created REAL NOT NULL,
              updated REAL NOT NULL,
              actor TEXT,
              note TEXT
            )
        ''')
        store.db.execute('''
            CREATE INDEX IF NOT EXISTS automation_proposals_status
            ON automation_proposals(status, created)
        ''')
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS automation_audit (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              timestamp REAL NOT NULL,
              proposal_id TEXT NOT NULL,
              event TEXT NOT NULL,
              detail TEXT NOT NULL,
              actor TEXT
            )
        ''')
        store.db.execute('''
            CREATE TRIGGER IF NOT EXISTS automation_audit_no_update
            BEFORE UPDATE ON automation_audit
            BEGIN SELECT RAISE(ABORT, 'automation_audit is append-only'); END
        ''')
        store.db.execute('''
            CREATE TRIGGER IF NOT EXISTS automation_audit_no_delete
            BEFORE DELETE ON automation_audit
            BEGIN SELECT RAISE(ABORT, 'automation_audit is append-only'); END
        ''')
        store.db.execute("INSERT INTO core_extensions VALUES('automation',1)")


class ProposalStore:
    """Persist, deduplicate and retrieve automation proposals."""

    def __init__(self, store):
        self._s = store

    def upsert(self, pattern_type, pattern_key, evidence, recommendation,
               risk_level, benefit_score):
        """Insert new proposal or update evidence on an existing one (by signature)."""
        sig = _sig(pattern_type, pattern_key)
        now = time.time()
        existing = self._s.db.execute(
            'SELECT id, status FROM automation_proposals WHERE signature=?', (sig,)
        ).fetchone()
        if existing:
            if existing['status'] in (STATUS_REJECTED, STATUS_ACTIVATED):
                return {'outcome': 'SKIPPED', 'proposal_id': existing['id'],
                        'reason': f'already_{existing["status"]}'}
            with self._s.transaction():
                self._s.db.execute(
                    'UPDATE automation_proposals SET evidence=?, benefit_score=?, updated=? '
                    'WHERE signature=?',
                    (encode(evidence), benefit_score, now, sig)
                )
            return {'outcome': 'UPDATED', 'proposal_id': existing['id']}
        proposal_id = uid()
        with self._s.transaction():
            self._s.db.execute(
                'INSERT INTO automation_proposals'
                '(id,signature,pattern_type,pattern_key,evidence,recommendation,'
                'risk_level,benefit_score,status,created,updated) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (proposal_id, sig, pattern_type, pattern_key,
                 encode(evidence), recommendation, risk_level,
                 benefit_score, STATUS_PENDING, now, now)
            )
            self._audit(proposal_id, 'created',
                        {'pattern_type': pattern_type, 'risk': risk_level,
                         'benefit': benefit_score})
        return {'outcome': 'CREATED', 'proposal_id': proposal_id}

    def list(self, status=None):
        if status:
            rows = self._s.db.execute(
                'SELECT * FROM automation_proposals WHERE status=? ORDER BY benefit_score DESC',
                (status,)
            ).fetchall()
        else:
            rows = self._s.db.execute(
                'SELECT * FROM automation_proposals ORDER BY benefit_score DESC'
            ).fetchall()
        return [self._decode(r) for r in rows]

    def get(self, proposal_id):
        row = self._s.db.execute(
            'SELECT * FROM automation_proposals WHERE id=?', (proposal_id,)
        ).fetchone()
        if row is None:
            raise Conflict('Proposal not found')
        return self._decode(row)

    def decide(self, proposal_id, status, *, actor, note=''):
        if status not in (STATUS_APPROVED, STATUS_REJECTED, STATUS_ACTIVATED):
            raise Denied('Invalid proposal status transition')
        row = self.get(proposal_id)
        if row['status'] == STATUS_ACTIVATED:
            raise Conflict('Proposal already activated')
        if row['status'] == STATUS_REJECTED and status != STATUS_APPROVED:
            raise Conflict('Rejected proposal; create a new observation to reconsider')
        if status == STATUS_ACTIVATED and row['status'] != STATUS_APPROVED:
            raise Denied('Proposal must be approved before activation')
        with self._s.transaction():
            self._s.db.execute(
                'UPDATE automation_proposals SET status=?,updated=?,actor=?,note=? WHERE id=?',
                (status, time.time(), actor, note, proposal_id)
            )
            self._audit(proposal_id, status, {'actor': actor, 'note': note})
        return self.get(proposal_id)

    def _audit(self, proposal_id, event, detail, actor=None):
        self._s.db.execute(
            'INSERT INTO automation_audit(timestamp,proposal_id,event,detail,actor) VALUES(?,?,?,?,?)',
            (time.time(), proposal_id, event, encode(detail), actor)
        )

    def _decode(self, row):
        r = dict(row)
        r['evidence'] = json.loads(r['evidence'])
        return r


class PatternObserver:
    """Scans task/action/event history for automation opportunities.
    Read-only — never mutates any existing table.
    """

    def __init__(self, store):
        self._s = store

    def observe(self, since_epoch=None):
        """Return list of raw pattern dicts from history scans."""
        since = since_epoch or 0
        patterns = []
        patterns.extend(self._repeated_task_kinds(since))
        patterns.extend(self._repeated_action_kinds(since))
        patterns.extend(self._action_failures(since))
        patterns.extend(self._frequent_approval_kinds(since))
        patterns.extend(self._frequent_event_sources(since))
        return patterns

    def _repeated_task_kinds(self, since):
        rows = self._s.db.execute(
            'SELECT task_type, project, COUNT(*) as n FROM tasks '
            'WHERE created >= ? GROUP BY task_type, project HAVING n >= ?',
            (since, _MIN_FREQUENCY)
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                'pattern_type': 'repeated_task_kind',
                'pattern_key': f'task_type:{r["task_type"]}:project:{r["project"]}',
                'frequency': r['n'],
                'evidence': {'task_type': r['task_type'], 'project': r['project'],
                             'count': r['n']},
                'recommendation': (
                    f'Task type "{r["task_type"]}" has been created {r["n"]} times '
                    f'in project "{r["project"]}". Consider an n8n workflow trigger '
                    f'or Telegram shortcut to automate intake for this pattern.'
                ),
                'risk_level': RISK_LOW,
                'benefit_score': min(1.0, r['n'] / 20.0),
            })
        return out

    def _repeated_action_kinds(self, since):
        rows = self._s.db.execute(
            'SELECT kind, COUNT(*) as n, '
            'SUM(CASE WHEN status="EXECUTED" THEN 1 ELSE 0 END) as succeeded '
            'FROM actions WHERE created >= ? AND protected=0 '
            'GROUP BY kind HAVING n >= ?',
            (since, _MIN_FREQUENCY)
        ).fetchall()
        out = []
        for r in rows:
            reliability = r['succeeded'] / r['n'] if r['n'] else 0
            out.append({
                'pattern_type': 'repeated_safe_action',
                'pattern_key': f'action_kind:{r["kind"]}:unprotected',
                'frequency': r['n'],
                'evidence': {'kind': r['kind'], 'count': r['n'],
                             'succeeded': r['succeeded'],
                             'reliability': round(reliability, 3)},
                'recommendation': (
                    f'SAFE action "{r["kind"]}" has run {r["n"]} times '
                    f'(reliability {reliability:.0%}). '
                    f'If this is routine, consider an automated trigger via n8n.'
                ),
                'risk_level': RISK_LOW,
                'benefit_score': min(1.0, reliability * r['n'] / 15.0),
            })
        return out

    def _action_failures(self, since):
        rows = self._s.db.execute(
            'SELECT kind, COUNT(*) as n, '
            'SUM(CASE WHEN status IN ("FAILED","UNCERTAIN") THEN 1 ELSE 0 END) as failed '
            'FROM actions WHERE created >= ? GROUP BY kind HAVING n >= ?',
            (since, _MIN_FREQUENCY)
        ).fetchall()
        out = []
        for r in rows:
            fail_rate = r['failed'] / r['n'] if r['n'] else 0
            if fail_rate >= _MIN_FAILURE_RATE:
                out.append({
                    'pattern_type': 'repeated_failure',
                    'pattern_key': f'action_kind:{r["kind"]}:failure',
                    'frequency': r['n'],
                    'evidence': {'kind': r['kind'], 'count': r['n'],
                                 'failed': r['failed'],
                                 'failure_rate': round(fail_rate, 3)},
                    'recommendation': (
                        f'Action "{r["kind"]}" has a {fail_rate:.0%} failure rate '
                        f'over {r["n"]} attempts. Review logs, add health checks, '
                        f'or add automated alerting via monitor_sweep.'
                    ),
                    'risk_level': RISK_MEDIUM,
                    'benefit_score': min(1.0, fail_rate * 0.8 + r['n'] / 30.0),
                })
        return out

    def _frequent_approval_kinds(self, since):
        rows = self._s.db.execute(
            '''SELECT a.kind, COUNT(*) as n,
               SUM(CASE WHEN ap.status="APPROVED" THEN 1 ELSE 0 END) as approved
               FROM actions a JOIN approvals ap ON ap.action_id=a.id
               WHERE a.created >= ?
               GROUP BY a.kind HAVING n >= ?''',
            (since, _MIN_FREQUENCY)
        ).fetchall()
        out = []
        for r in rows:
            approval_rate = r['approved'] / r['n'] if r['n'] else 0
            if approval_rate >= 0.8:
                out.append({
                    'pattern_type': 'frequent_approval',
                    'pattern_key': f'action_kind:{r["kind"]}:approval',
                    'frequency': r['n'],
                    'evidence': {'kind': r['kind'], 'count': r['n'],
                                 'approved': r['approved'],
                                 'approval_rate': round(approval_rate, 3)},
                    'recommendation': (
                        f'Action "{r["kind"]}" is approved {approval_rate:.0%} of the time '
                        f'over {r["n"]} attempts. Review whether the risk profile still '
                        f'warrants manual approval, or consider pre-approval templates. '
                        f'NOTE: do not bypass approval gates without an explicit policy change.'
                    ),
                    'risk_level': RISK_MEDIUM,
                    'benefit_score': min(1.0, approval_rate * r['n'] / 20.0),
                })
        return out

    def _frequent_event_sources(self, since):
        rows = self._s.db.execute(
            'SELECT source, event_type, COUNT(*) as n FROM inbound_events '
            'WHERE received_at >= ? GROUP BY source, event_type HAVING n >= ?',
            (since, _MIN_FREQUENCY)
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                'pattern_type': 'frequent_event_source',
                'pattern_key': f'source:{r["source"]}:type:{r["event_type"]}',
                'frequency': r['n'],
                'evidence': {'source': r['source'], 'event_type': r['event_type'],
                             'count': r['n']},
                'recommendation': (
                    f'Event source "{r["source"]}" fires "{r["event_type"]}" '
                    f'{r["n"]} times. If handling is repetitive, consider a '
                    f'dedicated n8n subworkflow or Telegram command shortcut.'
                ),
                'risk_level': RISK_LOW,
                'benefit_score': min(1.0, r['n'] / 25.0),
            })
        return out


class AutomationEngine:
    """Orchestrates observation → deduplication → proposal storage."""

    def __init__(self, store):
        self._s = store
        self._observer = PatternObserver(store)
        self._proposals = ProposalStore(store)

    def observe_and_propose(self, since_hours=None):
        """Scan history, generate deduplicated proposals, return summary."""
        from .safety import Controls
        Controls(self._s).check('automation_observe')
        since_epoch = (time.time() - since_hours * 3600) if since_hours else 0
        patterns = self._observer.observe(since_epoch=since_epoch)
        created = updated = skipped = 0
        proposals = []
        for p in patterns:
            r = self._proposals.upsert(
                p['pattern_type'], p['pattern_key'], p['evidence'],
                p['recommendation'], p['risk_level'], p['benefit_score'],
            )
            if r['outcome'] == 'CREATED':
                created += 1
            elif r['outcome'] == 'UPDATED':
                updated += 1
            else:
                skipped += 1
            proposals.append({**r, 'pattern_type': p['pattern_type'],
                              'pattern_key': p['pattern_key'],
                              'risk': p['risk_level'],
                              'benefit': p['benefit_score']})
        return {
            'outcome': 'SUCCESS', 'ok': True,
            'patterns_found': len(patterns),
            'created': created, 'updated': updated, 'skipped': skipped,
            'proposals': proposals,
            'observed_at': time.time(),
        }

    def list_proposals(self, status=None):
        from .safety import Controls
        Controls(self._s).check('automation_list')
        return {
            'outcome': 'SUCCESS', 'ok': True,
            'proposals': self._proposals.list(status=status),
        }

    def activate(self, proposal_id, *, actor, reason):
        """PROTECTED. Record activation decision after approval; never auto-execute."""
        from .safety import Controls
        Controls(self._s).check('automation_activate')
        row = self._proposals.get(proposal_id)
        if row['status'] != STATUS_APPROVED:
            raise Denied('Proposal must be approved before activation; use the Gate')
        result = self._proposals.decide(proposal_id, STATUS_ACTIVATED, actor=actor, note=reason)
        return {'outcome': 'SUCCESS', 'ok': True, 'proposal': result,
                'note': 'Activation recorded. Implement the suggested workflow change manually.'}


class AutomationWorker:
    """Phase 13 automation capabilities; extends MonitorWorker via composition."""

    def __init__(self, store, *, operator=None, cpc=None):
        from .monitor import MonitorWorker
        self.store = store
        self._monitor = MonitorWorker(store, operator=operator, cpc=cpc)
        self.paths = self._monitor.paths
        self._engine = AutomationEngine(store)
        self._proposals = ProposalStore(store)

    def gate(self):
        from .store import Gate
        from .worker import Capability
        monitor_gate = self._monitor.gate()
        p13_handlers = {k: Capability(self, k) for k in _PHASE13_KINDS}
        all_handlers = dict(monitor_gate._handlers)
        all_handlers.update(p13_handlers)
        return Gate(self.store, all_handlers)

    def validate(self, kind, p, project):
        if kind not in _PHASE13_KINDS:
            return self._monitor.validate(kind, p, project)
        if not isinstance(p, dict):
            raise Denied('Structured payload required')
        required = _PHASE13_FIELDS[kind] - _PHASE13_OPTIONAL.get(kind, frozenset())
        optional = _PHASE13_OPTIONAL.get(kind, frozenset())
        if set(p) - _PHASE13_FIELDS[kind] or required - set(p):
            raise Denied(f'Payload fields: required={sorted(required)} optional={sorted(optional)}')
        if kind == 'automation_activate':
            for k in ('proposal_id', 'reason'):
                if not isinstance(p.get(k), str) or not p[k].strip():
                    raise Denied(f'{k} must be a non-empty string')
        if kind == 'automation_list':
            if 'status' in p and p['status'] not in (
                    STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_ACTIVATED, None):
                raise Denied('Invalid status filter')

    def run(self, kind, p, project):
        from .safety import Controls
        Controls(self.store).check(kind)
        self.validate(kind, p, project)
        if kind not in _PHASE13_KINDS:
            return self._monitor.run(kind, p, project)
        if kind == 'automation_observe':
            return self._engine.observe_and_propose(since_hours=p.get('since_hours'))
        if kind == 'automation_list':
            return self._engine.list_proposals(status=p.get('status'))
        if kind == 'automation_activate':
            actor = f'gate:{project}'
            return self._engine.activate(
                p['proposal_id'], actor=actor, reason=p['reason'])
        raise Denied('Unknown automation capability')
