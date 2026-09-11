"""Phase 14: Ongoing Personal Operations.

Structured local record-keeping for calendar, finance, documents, research,
news/alerts, subscriptions/bills, CRM, learning, travel, and project portfolio.

SAFE kinds (automatic, no approval): all local record creation, listing, search,
  summarisation, alert checking and acknowledgement.
PROTECTED kinds (approval required): any external submission, financial payment,
  irreversible external cancellation, external invite/send.

All operations are deduplicated, audited, and emergency-stop aware.
CPC remains strictly read-only and unchanged.
No credentials, secrets, or personal data values are ever logged.
"""
import json
import time
import datetime
import re
from contextlib import nullcontext
from pathlib import Path

from .store import Denied, Conflict, uid, encode

# ── Capability sets ──────────────────────────────────────────────────────────

_PHASE14_SAFE = frozenset({
    # Calendar
    'ops_calendar_add', 'ops_calendar_list', 'ops_calendar_update',
    # Finance
    'ops_finance_record', 'ops_finance_summary',
    # Subscriptions/bills
    'ops_subscription_add', 'ops_subscriptions_list', 'ops_alerts_check',
    'ops_alerts_list', 'ops_alerts_acknowledge',
    # Research/notes
    'ops_research_note', 'ops_research_list',
    # Documents
    'ops_document_register', 'ops_documents_list',
    # CRM / contacts
    'ops_contact_add', 'ops_contacts_list',
    # Learning / content / travel / portfolio
    'ops_learning_log', 'ops_learning_list',
    'ops_portfolio_update', 'ops_portfolio_list',
    # Cross-domain summary
    'ops_summary', 'ops_system_status',
    'ops_content_draft', 'ops_content_list', 'ops_news_record', 'ops_news_list',
    'ops_travel_plan', 'ops_travel_list', 'ops_admin_record', 'ops_admin_list',
})

_PHASE14_PROTECTED = frozenset({
    'ops_content_publish', 'ops_travel_submit', 'ops_admin_submit',
    'ops_payment_submit',        # financial payment — external, irreversible
    'ops_subscription_cancel',   # cancellation — external, irreversible
    'ops_calendar_external_send', # external invite/send
})

_PHASE14_KINDS = _PHASE14_SAFE | _PHASE14_PROTECTED

# ── Schema migration ─────────────────────────────────────────────────────────

def _migrate_personal_ops_v1(store):
    """Idempotent Phase 14 schema extension. Uses core_extensions pattern."""
    with store.transaction():
        row = store.db.execute(
            "SELECT version FROM core_extensions WHERE name='personal_ops'"
        ).fetchone()
        if row:
            if row[0] != 1:
                raise Denied('Unknown personal_ops schema version')
            store.db.execute('SELECT id FROM personal_calendar LIMIT 0')
            return
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS personal_calendar (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              start_date TEXT NOT NULL,
              end_date TEXT NOT NULL,
              description TEXT DEFAULT '',
              location TEXT DEFAULT '',
              category TEXT NOT NULL DEFAULT 'general',
              status TEXT NOT NULL DEFAULT 'scheduled',
              created REAL NOT NULL,
              updated REAL NOT NULL,
              task_id TEXT
            )
        ''')
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS personal_finance (
              id TEXT PRIMARY KEY,
              entry_type TEXT NOT NULL,
              amount_cents INTEGER NOT NULL,
              currency TEXT NOT NULL DEFAULT 'USD',
              description TEXT NOT NULL,
              category TEXT NOT NULL DEFAULT 'general',
              entry_date TEXT NOT NULL,
              reference TEXT DEFAULT '',
              status TEXT NOT NULL DEFAULT 'recorded',
              created REAL NOT NULL,
              task_id TEXT
            )
        ''')
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS personal_subscriptions (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL UNIQUE,
              amount_cents INTEGER NOT NULL,
              currency TEXT NOT NULL DEFAULT 'USD',
              billing_cycle TEXT NOT NULL DEFAULT 'monthly',
              next_due TEXT NOT NULL,
              category TEXT NOT NULL DEFAULT 'software',
              status TEXT NOT NULL DEFAULT 'active',
              created REAL NOT NULL,
              updated REAL NOT NULL
            )
        ''')
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS personal_alerts (
              id TEXT PRIMARY KEY,
              alert_type TEXT NOT NULL,
              reference_id TEXT DEFAULT '',
              message TEXT NOT NULL,
              due_date TEXT DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending',
              created REAL NOT NULL
            )
        ''')
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS personal_research (
              id TEXT PRIMARY KEY,
              topic TEXT NOT NULL,
              source TEXT DEFAULT '',
              notes TEXT NOT NULL,
              tags TEXT DEFAULT '[]',
              status TEXT NOT NULL DEFAULT 'in_progress',
              created REAL NOT NULL,
              task_id TEXT
            )
        ''')
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS personal_documents (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              category TEXT NOT NULL DEFAULT 'general',
              description TEXT DEFAULT '',
              file_ref TEXT DEFAULT '',
              sha256 TEXT DEFAULT '',
              created REAL NOT NULL,
              task_id TEXT
            )
        ''')
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS personal_contacts (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              category TEXT NOT NULL DEFAULT 'general',
              notes TEXT DEFAULT '',
              created REAL NOT NULL,
              updated REAL NOT NULL
            )
        ''')
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS personal_learning (
              id TEXT PRIMARY KEY,
              topic TEXT NOT NULL,
              resource TEXT DEFAULT '',
              notes TEXT NOT NULL,
              tags TEXT DEFAULT '[]',
              completed INTEGER NOT NULL DEFAULT 0,
              created REAL NOT NULL
            )
        ''')
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS personal_portfolio (
              id TEXT PRIMARY KEY,
              project_name TEXT NOT NULL UNIQUE,
              description TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'active',
              tags TEXT DEFAULT '[]',
              created REAL NOT NULL,
              updated REAL NOT NULL
            )
        ''')
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS personal_ops_audit (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              timestamp REAL NOT NULL,
              domain TEXT NOT NULL,
              operation TEXT NOT NULL,
              record_id TEXT,
              detail TEXT NOT NULL,
              actor TEXT
            )
        ''')
        store.db.execute('''
            CREATE TRIGGER IF NOT EXISTS personal_ops_audit_no_update
            BEFORE UPDATE ON personal_ops_audit
            BEGIN SELECT RAISE(ABORT, 'personal_ops_audit is append-only'); END
        ''')
        store.db.execute('''
            CREATE TRIGGER IF NOT EXISTS personal_ops_audit_no_delete
            BEFORE DELETE ON personal_ops_audit
            BEGIN SELECT RAISE(ABORT, 'personal_ops_audit is append-only'); END
        ''')
        store.db.execute("INSERT INTO core_extensions VALUES('personal_ops',1)")


# ── Audit helper ─────────────────────────────────────────────────────────────

class OpsAudit:
    def __init__(self, store):
        self._s = store

    def record(self, domain, operation, record_id=None, detail=None, actor=None):
        with (nullcontext() if self._s.db.in_transaction else self._s.transaction()):
            self._s.db.execute(
                'INSERT INTO personal_ops_audit'
                '(timestamp,domain,operation,record_id,detail,actor) VALUES(?,?,?,?,?,?)',
                (time.time(), domain, operation, record_id, encode({'fields': sorted((detail or {}).keys())}), actor)
            )


# ── Domain operations ─────────────────────────────────────────────────────────

class CalendarOps:
    def __init__(self, store):
        self._s = store
        self._audit = OpsAudit(store)

    def add(self, title, start_date, end_date, *, description='', location='',
            category='general', task_id=None):
        if not title or not start_date or not end_date:
            raise Denied('title, start_date and end_date required')
        if start_date > end_date:
            raise Denied('start_date must not be after end_date')
        eid = uid()
        now = time.time()
        with self._s.transaction():
            self._s.db.execute(
                'INSERT INTO personal_calendar'
                '(id,title,start_date,end_date,description,location,category,status,created,updated,task_id)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (eid, title, start_date, end_date, description, location,
                 category, 'scheduled', now, now, task_id)
            )
            self._audit.record('calendar', 'add', eid,
                               {'title': title, 'start': start_date, 'end': end_date,
                                'category': category})
        return {'outcome': 'SUCCESS', 'ok': True, 'id': eid}

    def list(self, *, from_date=None, to_date=None, status=None, limit=50):
        q = 'SELECT * FROM personal_calendar WHERE 1=1'
        params = []
        if from_date:
            q += ' AND end_date >= ?'; params.append(from_date)
        if to_date:
            q += ' AND start_date <= ?'; params.append(to_date)
        if status:
            q += ' AND status = ?'; params.append(status)
        q += ' ORDER BY start_date LIMIT ?'; params.append(limit)
        rows = self._s.db.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def update_status(self, event_id, status):
        if status not in ('scheduled', 'completed', 'cancelled'):
            raise Denied('Invalid calendar event status')
        with self._s.transaction():
            if not self._s.db.execute('SELECT id FROM personal_calendar WHERE id=?',(event_id,)).fetchone():
                raise Conflict('Calendar record not found')
            self._s.db.execute(
                'UPDATE personal_calendar SET status=?, updated=? WHERE id=?',
                (status, time.time(), event_id)
            )
            self._audit.record('calendar', 'update_status', event_id, {'status': status})
        return {'outcome': 'SUCCESS', 'ok': True, 'id': event_id, 'status': status}


class FinanceOps:
    _VALID_TYPES = frozenset({'expense', 'income', 'transfer'})
    _VALID_CYCLES = frozenset({'monthly', 'annual', 'quarterly', 'weekly'})

    def __init__(self, store):
        self._s = store
        self._audit = OpsAudit(store)

    def record_entry(self, entry_type, amount_cents, description, entry_date, *,
                     currency='USD', category='general', reference='', task_id=None):
        if entry_type not in self._VALID_TYPES:
            raise Denied(f'entry_type must be one of {sorted(self._VALID_TYPES)}')
        if not isinstance(amount_cents, int) or amount_cents < 0:
            raise Denied('amount_cents must be a non-negative integer')
        if not description or not entry_date:
            raise Denied('description and entry_date required')
        fid = uid()
        now = time.time()
        with self._s.transaction():
            self._s.db.execute(
                'INSERT INTO personal_finance'
                '(id,entry_type,amount_cents,currency,description,category,'
                'entry_date,reference,status,created,task_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (fid, entry_type, amount_cents, currency, description, category,
                 entry_date, reference, 'recorded', now, task_id)
            )
            self._audit.record('finance', 'record', fid,
                               {'type': entry_type, 'category': category,
                                'date': entry_date})
        return {'outcome': 'SUCCESS', 'ok': True, 'id': fid}

    def summary(self, *, since_date=None, until_date=None):
        q = 'SELECT currency, entry_type, category, SUM(amount_cents) as total, COUNT(*) as n FROM personal_finance WHERE 1=1'
        params = []
        if since_date:
            q += ' AND entry_date >= ?'; params.append(since_date)
        if until_date:
            q += ' AND entry_date <= ?'; params.append(until_date)
        q += ' GROUP BY currency, entry_type, category'
        rows = self._s.db.execute(q, params).fetchall()
        currencies = {}
        for row in rows:
            bucket = currencies.setdefault(row['currency'], {'income_cents': 0, 'expense_cents': 0, 'transfer_cents': 0})
            bucket[row['entry_type'] + '_cents'] += row['total']
        for bucket in currencies.values():
            bucket['net_cents'] = bucket['income_cents'] - bucket['expense_cents']
        return {'outcome': 'SUCCESS', 'ok': True, 'by_currency': currencies,
                'by_type_category': [dict(r) for r in rows], 'ledger_only': True}

    def add_subscription(self, name, amount_cents, billing_cycle, next_due, *,
                         currency='USD', category='software'):
        if billing_cycle not in self._VALID_CYCLES:
            raise Denied(f'billing_cycle must be one of {sorted(self._VALID_CYCLES)}')
        if not isinstance(amount_cents, int) or amount_cents < 0:
            raise Denied('amount_cents must be a non-negative integer')
        sid = uid()
        now = time.time()
        with self._s.transaction():
            try:
                self._s.db.execute(
                    'INSERT INTO personal_subscriptions'
                    '(id,name,amount_cents,currency,billing_cycle,next_due,'
                    'category,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (sid, name, amount_cents, currency, billing_cycle, next_due,
                     category, 'active', now, now)
                )
            except Exception as e:
                if 'UNIQUE' in str(e):
                    raise Conflict(f'Subscription "{name}" already exists')
                raise
            self._audit.record('finance', 'subscription_add', sid,
                               {'name': name, 'cycle': billing_cycle, 'due': next_due})
        return {'outcome': 'SUCCESS', 'ok': True, 'id': sid}

    def list_subscriptions(self, *, status='active'):
        rows = self._s.db.execute(
            'SELECT * FROM personal_subscriptions WHERE status=? ORDER BY next_due',
            (status,)
        ).fetchall()
        return [dict(r) for r in rows]

    def check_alerts(self, *, days_ahead=7):
        today=datetime.date.today()
        cutoff=(today+datetime.timedelta(days=days_ahead)).isoformat()
        alerts=[]
        with self._s.transaction():
            subs=self._s.db.execute('SELECT * FROM personal_subscriptions WHERE status=? AND next_due<=?',('active',cutoff)).fetchall()
            for sub in subs:
                existing=self._s.db.execute('SELECT id FROM personal_alerts WHERE reference_id=? AND due_date=?',(sub['id'],sub['next_due'])).fetchone()
                if existing:
                    alerts.append({'id':existing['id'],'existing':True});continue
                aid=uid()
                self._s.db.execute('INSERT INTO personal_alerts(id,alert_type,reference_id,message,due_date,status,created) VALUES(?,?,?,?,?,?,?)',
                    (aid,'subscription_due',sub['id'],'Subscription payment review due',sub['next_due'],'pending',time.time()))
                self._audit.record('alerts','create',aid)
                alerts.append({'id':aid,'existing':False})
        return {'ok':True,'outcome':'SUCCESS','alerts_generated':alerts,'count':len(alerts)}

    def list_alerts(self, *, status=None):
        q = 'SELECT * FROM personal_alerts WHERE 1=1'
        params = []
        if status:
            q += ' AND status=?'; params.append(status)
        q += ' ORDER BY due_date, created'
        rows = self._s.db.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def acknowledge_alert(self, alert_id):
        row = self._s.db.execute(
            'SELECT * FROM personal_alerts WHERE id=?', (alert_id,)
        ).fetchone()
        if row is None:
            raise Conflict('Alert not found')
        with self._s.transaction():
            self._s.db.execute(
                'UPDATE personal_alerts SET status="acknowledged" WHERE id=?',
                (alert_id,)
            )
            self._audit.record('alerts', 'acknowledge', alert_id,
                               {'type': row['alert_type']})
        return {'outcome': 'SUCCESS', 'ok': True, 'id': alert_id}


class ResearchOps:
    def __init__(self, store):
        self._s = store
        self._audit = OpsAudit(store)

    def add_note(self, topic, notes, *, source='', tags=None, task_id=None):
        if not topic or not notes:
            raise Denied('topic and notes required')
        rid = uid()
        with self._s.transaction():
            self._s.db.execute(
                'INSERT INTO personal_research'
                '(id,topic,source,notes,tags,status,created,task_id) VALUES(?,?,?,?,?,?,?,?)',
                (rid, topic, source, notes, json.dumps(tags or []),
                 'in_progress', time.time(), task_id)
            )
            self._audit.record('research', 'add', rid, {'topic': topic})
        return {'outcome': 'SUCCESS', 'ok': True, 'id': rid}

    def list(self, *, topic_filter=None, status=None, limit=50):
        q = 'SELECT * FROM personal_research WHERE 1=1'
        params = []
        if topic_filter:
            q += ' AND topic LIKE ?'; params.append(f'%{topic_filter}%')
        if status:
            q += ' AND status=?'; params.append(status)
        q += ' ORDER BY created DESC LIMIT ?'; params.append(limit)
        rows = self._s.db.execute(q, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['tags'] = json.loads(d.get('tags') or '[]')
            result.append(d)
        return result


class DocumentOps:
    def __init__(self, store):
        self._s = store
        self._audit = OpsAudit(store)

    def register(self, title, *, category='general', description='',
                 file_ref='', sha256='', task_id=None):
        if not title:
            raise Denied('title required')
        did = uid()
        with self._s.transaction():
            self._s.db.execute(
                'INSERT INTO personal_documents'
                '(id,title,category,description,file_ref,sha256,created,task_id)'
                ' VALUES(?,?,?,?,?,?,?,?)',
                (did, title, category, description, file_ref, sha256,
                 time.time(), task_id)
            )
            self._audit.record('documents', 'register', did,
                               {'title': title, 'category': category})
        return {'outcome': 'SUCCESS', 'ok': True, 'id': did}

    def list(self, *, category=None, limit=50):
        q = 'SELECT * FROM personal_documents WHERE 1=1'
        params = []
        if category:
            q += ' AND category=?'; params.append(category)
        q += ' ORDER BY created DESC LIMIT ?'; params.append(limit)
        rows = self._s.db.execute(q, params).fetchall()
        return [dict(r) for r in rows]


class ContactOps:
    def __init__(self, store):
        self._s = store
        self._audit = OpsAudit(store)

    def add(self, name, *, category='general', notes=''):
        if not name:
            raise Denied('name required')
        cid = uid()
        now = time.time()
        with self._s.transaction():
            self._s.db.execute(
                'INSERT INTO personal_contacts'
                '(id,name,category,notes,created,updated) VALUES(?,?,?,?,?,?)',
                (cid, name, category, notes, now, now)
            )
            self._audit.record('contacts', 'add', cid, {'name': name, 'category': category})
        return {'outcome': 'SUCCESS', 'ok': True, 'id': cid}

    def list(self, *, category=None, limit=50):
        q = 'SELECT * FROM personal_contacts WHERE 1=1'
        params = []
        if category:
            q += ' AND category=?'; params.append(category)
        q += ' ORDER BY name LIMIT ?'; params.append(limit)
        rows = self._s.db.execute(q, params).fetchall()
        return [dict(r) for r in rows]


class LearningOps:
    def __init__(self, store):
        self._s = store
        self._audit = OpsAudit(store)

    def log(self, topic, notes, *, resource='', tags=None, completed=False):
        if not topic or not notes:
            raise Denied('topic and notes required')
        lid = uid()
        with self._s.transaction():
            self._s.db.execute(
                'INSERT INTO personal_learning'
                '(id,topic,resource,notes,tags,completed,created) VALUES(?,?,?,?,?,?,?)',
                (lid, topic, resource, notes, json.dumps(tags or []),
                 int(completed), time.time())
            )
            self._audit.record('learning', 'log', lid, {'topic': topic})
        return {'outcome': 'SUCCESS', 'ok': True, 'id': lid}

    def list(self, *, completed=None, limit=50):
        q = 'SELECT * FROM personal_learning WHERE 1=1'
        params = []
        if completed is not None:
            q += ' AND completed=?'; params.append(int(completed))
        q += ' ORDER BY created DESC LIMIT ?'; params.append(limit)
        rows = self._s.db.execute(q, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['tags'] = json.loads(d.get('tags') or '[]')
            result.append(d)
        return result


class PortfolioOps:
    def __init__(self, store):
        self._s = store
        self._audit = OpsAudit(store)

    def upsert(self, project_name, description, *, status='active', tags=None):
        if not project_name or not description:
            raise Denied('project_name and description required')
        now = time.time()
        existing = self._s.db.execute(
            'SELECT id FROM personal_portfolio WHERE project_name=?',
            (project_name,)
        ).fetchone()
        if existing:
            with self._s.transaction():
                self._s.db.execute(
                    'UPDATE personal_portfolio SET description=?,status=?,tags=?,updated=? WHERE project_name=?',
                    (description, status, json.dumps(tags or []), now, project_name)
                )
                self._audit.record('portfolio', 'update', existing['id'],
                                   {'project': project_name, 'status': status})
            return {'outcome': 'UPDATED', 'ok': True, 'id': existing['id']}
        pid = uid()
        with self._s.transaction():
            self._s.db.execute(
                'INSERT INTO personal_portfolio'
                '(id,project_name,description,status,tags,created,updated) VALUES(?,?,?,?,?,?,?)',
                (pid, project_name, description, status, json.dumps(tags or []), now, now)
            )
            self._audit.record('portfolio', 'add', pid,
                               {'project': project_name, 'status': status})
        return {'outcome': 'CREATED', 'ok': True, 'id': pid}

    def list(self, *, status=None):
        q = 'SELECT * FROM personal_portfolio WHERE 1=1'
        params = []
        if status:
            q += ' AND status=?'; params.append(status)
        q += ' ORDER BY project_name'
        rows = self._s.db.execute(q, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['tags'] = json.loads(d.get('tags') or '[]')
            result.append(d)
        return result


# ── Main worker ───────────────────────────────────────────────────────────────

_PHASE14_FIELDS = {
    # Calendar
    'ops_calendar_add':    frozenset({'title', 'start_date', 'end_date', 'description',
                                      'location', 'category'}),
    'ops_calendar_list':   frozenset({'from_date', 'to_date', 'status'}),
    'ops_calendar_update': frozenset({'event_id', 'status'}),
    # Finance
    'ops_finance_record':  frozenset({'entry_type', 'amount_cents', 'description',
                                      'entry_date', 'currency', 'category', 'reference'}),
    'ops_finance_summary': frozenset({'since_date', 'until_date'}),
    # Subscriptions
    'ops_subscription_add': frozenset({'name', 'amount_cents', 'billing_cycle', 'next_due',
                                       'currency', 'category'}),
    'ops_subscriptions_list': frozenset({'status'}),
    'ops_alerts_check':    frozenset({'days_ahead'}),
    'ops_alerts_list':     frozenset({'status'}),
    'ops_alerts_acknowledge': frozenset({'alert_id'}),
    # Research
    'ops_research_note':   frozenset({'topic', 'notes', 'source', 'tags'}),
    'ops_research_list':   frozenset({'topic_filter', 'status'}),
    # Documents
    'ops_document_register': frozenset({'title', 'category', 'description',
                                        'file_ref', 'sha256'}),
    'ops_documents_list':  frozenset({'category'}),
    # Contacts
    'ops_contact_add':     frozenset({'name', 'category', 'notes'}),
    'ops_contacts_list':   frozenset({'category'}),
    # Learning
    'ops_learning_log':    frozenset({'topic', 'notes', 'resource', 'tags', 'completed'}),
    'ops_learning_list':   frozenset({'completed'}),
    # Portfolio
    'ops_portfolio_update': frozenset({'project_name', 'description', 'status', 'tags'}),
    'ops_portfolio_list':   frozenset({'status'}),
    # Summary
    'ops_summary':          frozenset(),
    # Protected
    'ops_payment_submit':           frozenset({'description', 'amount_cents', 'currency',
                                               'recipient', 'reason'}),
    'ops_subscription_cancel':      frozenset({'subscription_id', 'reason'}),
    'ops_calendar_external_send':   frozenset({'event_id', 'recipient', 'reason'}),
}
# Allowed fields are bounded; required fields are enforced before persistence.
_PHASE14_REQUIRED = {
    'ops_calendar_add':    frozenset({'title', 'start_date', 'end_date'}),
    'ops_calendar_update': frozenset({'event_id', 'status'}),
    'ops_finance_record':  frozenset({'entry_type', 'amount_cents', 'description', 'entry_date'}),
    'ops_subscription_add': frozenset({'name', 'amount_cents', 'billing_cycle', 'next_due'}),
    'ops_alerts_acknowledge': frozenset({'alert_id'}),
    'ops_research_note':   frozenset({'topic', 'notes'}),
    'ops_document_register': frozenset({'title'}),
    'ops_contact_add':     frozenset({'name'}),
    'ops_learning_log':    frozenset({'topic', 'notes'}),
    'ops_portfolio_update': frozenset({'project_name', 'description'}),
    'ops_payment_submit':  frozenset({'description', 'amount_cents', 'currency',
                                      'recipient', 'reason'}),
    'ops_subscription_cancel': frozenset({'subscription_id', 'reason'}),
    'ops_calendar_external_send': frozenset({'event_id', 'recipient', 'reason'}),
}


class PersonalOpsWorker:
    """Phase 14: Personal operations. Extends AutomationWorker via composition."""

    def __init__(self, store, *, operator=None, cpc=None):
        from .automation import AutomationWorker
        self.store = store
        self._task_id = None
        self._automation = AutomationWorker(store, operator=operator, cpc=cpc)
        self.paths = self._automation.paths
        self._calendar = CalendarOps(store)
        self._finance = FinanceOps(store)
        self._research = ResearchOps(store)
        self._documents = DocumentOps(store)
        self._contacts = ContactOps(store)
        self._learning = LearningOps(store)
        self._portfolio = PortfolioOps(store)

    def _domain_record(self, domain, p):
        rid=uid()
        with self.store.transaction():
            self.store.db.execute('INSERT INTO personal_entries(id,domain,title,notes,reference,created,task_id) VALUES(?,?,?,?,?,?,?)',
                (rid,domain,p['title'],p['notes'],p.get('reference',''),time.time(),self._task_id))
            OpsAudit(self.store).record(domain,'record',rid,actor=self._task_id)
        return {'ok':True,'outcome':'SUCCESS','id':rid,'task_id':self._task_id,'local_only':True}

    def _domain_list(self, domain, p):
        rows=self.store.db.execute('SELECT * FROM personal_entries WHERE domain=? AND (title LIKE ? OR notes LIKE ?) ORDER BY created DESC LIMIT 50',
            (domain,'%'+p.get('search','')+'%','%'+p.get('search','')+'%')).fetchall()
        return {'ok':True,'outcome':'SUCCESS','records':[dict(r) for r in rows],'local_only':True}

    def _system_status(self):
        # Reuse Phase 12 usage accounting and persisted monitoring observations.
        from .safety import Controls
        from .monitor import UsageTracker
        root=self.paths.roots['operator'] / 'state/backups'
        backups=[]
        if root.exists() and not root.is_symlink():
            backups=[p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
        return {'ok':True,'outcome':'SUCCESS','controls':Controls(self.store).status(),
                'usage':UsageTracker(self.store).summary(), 'backup_directories':sorted(backups),
                'monitor_observations':[dict(r) for r in self.store.db.execute('SELECT timestamp,category,connector_id,outcome FROM monitor_log ORDER BY seq DESC LIMIT 20')],
                'backup_restore_verified':False,
                'health_action':'monitor_sweep','security_action':'security_audit',
                'recovery_action':'service_recover','automation_action':'automation_observe',
                'cost_limitation':'Existing usage counts; monetary provider cost not inferred',
                'extensions':[dict(r) for r in self.store.db.execute('SELECT * FROM core_extensions')]}

    def gate(self):
        from .store import Gate
        from .worker import Capability
        parent_gate = self._automation.gate()
        p14_handlers = {k: PersonalCapability(self, k) for k in _PHASE14_KINDS}
        all_handlers = dict(parent_gate._handlers)
        all_handlers.update(p14_handlers)
        return Gate(self.store, all_handlers)

    def validate(self, kind, p, project):
        if kind not in _PHASE14_KINDS:
            return self._automation.validate(kind, p, project)
        validate_proposal(kind,p,project)

    def run(self, kind, p, project):
        from .safety import Controls
        Controls(self.store).check(kind, project if isinstance(project, dict) else None)
        if kind in _PHASE14_KINDS:
            if self._task_id is None: raise Denied('Personal operations require canonical Gate execution')
            project = self.store.task(self._task_id)['project']
        self.validate(kind, p, project)
        if kind not in _PHASE14_KINDS:
            return self._automation.run(kind, p, project)
        if kind in _EXTRA_DOMAINS:
            domain, write = _EXTRA_DOMAINS[kind]
            return self._domain_record(domain, p) if write else self._domain_list(domain, p)
        if kind == 'ops_system_status':
            return self._system_status()
        # Calendar
        if kind == 'ops_calendar_add':
            return self._calendar.add(
                p['title'], p['start_date'], p['end_date'],
                description=p.get('description', ''), location=p.get('location', ''),
                category=p.get('category', 'general'), task_id=self._task_id)
        if kind == 'ops_calendar_list':
            return {'outcome': 'SUCCESS', 'ok': True,
                    'events': self._calendar.list(
                        from_date=p.get('from_date'), to_date=p.get('to_date'),
                        status=p.get('status'))}
        if kind == 'ops_calendar_update':
            return self._calendar.update_status(p['event_id'], p['status'])
        # Finance
        if kind == 'ops_finance_record':
            return self._finance.record_entry(
                p['entry_type'], p['amount_cents'], p['description'], p['entry_date'],
                currency=p.get('currency', 'USD'), category=p.get('category', 'general'),
                reference=p.get('reference', ''), task_id=self._task_id)
        if kind == 'ops_finance_summary':
            return self._finance.summary(since_date=p.get('since_date'),
                                         until_date=p.get('until_date'))
        # Subscriptions
        if kind == 'ops_subscription_add':
            return self._finance.add_subscription(
                p['name'], p['amount_cents'], p['billing_cycle'], p['next_due'],
                currency=p.get('currency', 'USD'), category=p.get('category', 'software'))
        if kind == 'ops_subscriptions_list':
            return {'outcome': 'SUCCESS', 'ok': True,
                    'subscriptions': self._finance.list_subscriptions(
                        status=p.get('status', 'active'))}
        if kind == 'ops_alerts_check':
            return self._finance.check_alerts(days_ahead=p.get('days_ahead', 7))
        if kind == 'ops_alerts_list':
            return {'outcome': 'SUCCESS', 'ok': True,
                    'alerts': self._finance.list_alerts(status=p.get('status'))}
        if kind == 'ops_alerts_acknowledge':
            return self._finance.acknowledge_alert(p['alert_id'])
        # Research
        if kind == 'ops_research_note':
            return self._research.add_note(
                p['topic'], p['notes'], source=p.get('source', ''),
                tags=p.get('tags'), task_id=self._task_id)
        if kind == 'ops_research_list':
            return {'outcome': 'SUCCESS', 'ok': True,
                    'notes': self._research.list(
                        topic_filter=p.get('topic_filter'), status=p.get('status'))}
        # Documents
        if kind == 'ops_document_register':
            return self._documents.register(
                p['title'], category=p.get('category', 'general'),
                description=p.get('description', ''), file_ref=p.get('file_ref', ''),
                sha256=p.get('sha256', ''), task_id=self._task_id)
        if kind == 'ops_documents_list':
            return {'outcome': 'SUCCESS', 'ok': True,
                    'documents': self._documents.list(category=p.get('category'))}
        # Contacts
        if kind == 'ops_contact_add':
            return self._contacts.add(p['name'], category=p.get('category', 'general'),
                                      notes=p.get('notes', ''))
        if kind == 'ops_contacts_list':
            return {'outcome': 'SUCCESS', 'ok': True,
                    'contacts': self._contacts.list(category=p.get('category'))}
        # Learning
        if kind == 'ops_learning_log':
            return self._learning.log(p['topic'], p['notes'],
                                       resource=p.get('resource', ''),
                                       tags=p.get('tags'), completed=p.get('completed', False))
        if kind == 'ops_learning_list':
            return {'outcome': 'SUCCESS', 'ok': True,
                    'entries': self._learning.list(completed=p.get('completed'))}
        # Portfolio
        if kind == 'ops_portfolio_update':
            return self._portfolio.upsert(p['project_name'], p['description'],
                                           status=p.get('status', 'active'),
                                           tags=p.get('tags'))
        if kind == 'ops_portfolio_list':
            return {'outcome': 'SUCCESS', 'ok': True,
                    'projects': self._portfolio.list(status=p.get('status'))}
        # Summary
        if kind == 'ops_summary':
            return {
                'outcome': 'SUCCESS', 'ok': True,
                'calendar_count': self.store.db.execute(
                    "SELECT COUNT(*) FROM personal_calendar WHERE status='scheduled'"
                ).fetchone()[0],
                'active_subscriptions': self.store.db.execute(
                    "SELECT COUNT(*) FROM personal_subscriptions WHERE status='active'"
                ).fetchone()[0],
                'pending_alerts': self.store.db.execute(
                    "SELECT COUNT(*) FROM personal_alerts WHERE status='pending'"
                ).fetchone()[0],
                'research_notes': self.store.db.execute(
                    'SELECT COUNT(*) FROM personal_research'
                ).fetchone()[0],
                'documents': self.store.db.execute(
                    'SELECT COUNT(*) FROM personal_documents'
                ).fetchone()[0],
                'portfolio_active': self.store.db.execute(
                    "SELECT COUNT(*) FROM personal_portfolio WHERE status='active'"
                ).fetchone()[0],
            }
        if kind in _PHASE14_PROTECTED:
            raise Denied('External integration not connected; no protected side effect executed')
        raise Denied(f'Unknown personal ops capability: {kind}')

# Continue the existing domain implementation; use the same database and Gate.
_EXTRA_DOMAINS = {
    'ops_content_draft': ('content', True), 'ops_content_list': ('content', False),
    'ops_news_record': ('news', True), 'ops_news_list': ('news', False),
    'ops_travel_plan': ('travel', True), 'ops_travel_list': ('travel', False),
    'ops_admin_record': ('admin', True), 'ops_admin_list': ('admin', False),
}
for _kind, (_, _write) in _EXTRA_DOMAINS.items():
    _PHASE14_FIELDS[_kind] = frozenset({'title', 'notes', 'reference'} if _write else {'search'})
    if _write: _PHASE14_REQUIRED[_kind] = frozenset({'title', 'notes'})
for _kind in ('ops_content_publish', 'ops_travel_submit', 'ops_admin_submit'):
    _PHASE14_FIELDS[_kind] = _PHASE14_REQUIRED[_kind] = frozenset({'record_id', 'recipient', 'reason'})
_PHASE14_FIELDS['ops_system_status'] = frozenset()
_PHASE14_READ_ONLY = frozenset(k for k in _PHASE14_SAFE if k.endswith('_list') or k in ('ops_summary', 'ops_system_status', 'ops_finance_summary'))


def migrate_personal_ops(store):
    row = store.db.execute("SELECT version FROM core_extensions WHERE name='personal_ops'").fetchone()
    if row and row[0] not in (1, 2): raise Denied('Unknown personal_ops schema version')
    if not row or row[0] == 1:
        _migrate_personal_ops_v1(store)
        with store.transaction():
            store.db.execute('''CREATE TABLE personal_entries (
                id TEXT PRIMARY KEY, domain TEXT NOT NULL CHECK(domain IN ('content','news','travel','admin')),
                title TEXT NOT NULL, notes TEXT NOT NULL, reference TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft', created REAL NOT NULL,
                task_id TEXT NOT NULL REFERENCES tasks(id))''')
            store.db.execute("UPDATE core_extensions SET version=2 WHERE name='personal_ops'")
    # Corrupt/missing tables must not silently be recreated once migration is recorded.
    for table in ('calendar','finance','subscriptions','alerts','research','documents','contacts','learning','portfolio','entries'):
        store.db.execute('SELECT id FROM personal_' + table + ' LIMIT 0')
    store.db.execute('SELECT seq FROM personal_ops_audit LIMIT 0')
    triggers = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    if not {'personal_ops_audit_no_update','personal_ops_audit_no_delete'} <= triggers:
        raise Denied('Personal audit integrity unavailable')


def validate_payload(kind, p):
    from .integration_security import validate_data
    validate_data(p)
    for key, value in p.items():
        if key in ('amount_cents', 'days_ahead'):
            maximum = 10**12 if key == 'amount_cents' else 366
            if type(value) is not int or not 0 <= value <= maximum: raise Denied('Invalid bounded integer')
        elif key == 'completed':
            if type(value) is not bool: raise Denied('Boolean required')
        elif key == 'tags':
            if not isinstance(value,list) or len(value)>32 or any(not isinstance(v,str) or not 0<len(v)<=100 for v in value): raise Denied('Invalid tags')
        elif not isinstance(value,str) or len(value)>16000:
            raise Denied('Bounded text required')
        if key in _PHASE14_REQUIRED.get(kind, ()) and isinstance(value,str) and not value.strip(): raise Denied('Required text is empty')
        if key.endswith('_date') or key in ('next_due','from_date','to_date'):
            try:
                if datetime.date.fromisoformat(value).isoformat()!=value: raise ValueError()
            except (ValueError,TypeError): raise Denied('ISO calendar date required')
        if key == 'currency' and not re.fullmatch(r'[A-Z]{3}',value): raise Denied('Three-letter currency required')
        if key == 'sha256' and value and not re.fullmatch(r'[0-9a-f]{64}',value): raise Denied('Invalid document digest')
        if key == 'file_ref' and value:
            # A reference is metadata, never opened or executed by personal operations.
            if value.startswith(('/', '~')) or '..' in Path(value).parts or any(part.startswith('.') for part in Path(value).parts):
                raise Denied('Unsafe document reference')
    if kind=='ops_calendar_add' and p.get('start_date','')>p.get('end_date',''): raise Denied('Invalid calendar interval')
    if kind=='ops_finance_record' and p.get('entry_type') not in FinanceOps._VALID_TYPES: raise Denied('Invalid ledger type')
    if kind=='ops_subscription_add' and p.get('billing_cycle') not in FinanceOps._VALID_CYCLES: raise Denied('Invalid billing cycle')


from .worker import Capability
class PersonalCapability(Capability):
    def run_with_controls(self, payload, baseline):
        raise Denied('Canonical action context required')

    def run_with_action(self, payload, baseline, action_id, task_id):
        from .safety import Controls
        action = self.worker.store.action(action_id)
        if action['status']!='EXECUTING' or action['task_id']!=task_id or action['kind']!=self.kind or action['payload']!=payload:
            raise Denied('Canonical execution claim required')
        Controls(self.worker.store).check(self.kind,baseline)
        self.worker._task_id=task_id
        try:
            return self.worker.run(self.kind,payload,baseline)
        finally:
            self.worker._task_id=None


def validate_proposal(kind,p,project):
    if not isinstance(p, dict):
        raise Denied('Structured payload required')
    if project != 'operator':
        raise Denied('Personal operations require the operator project')
    validate_payload(kind, p)
    allowed = _PHASE14_FIELDS.get(kind, frozenset())
    required = _PHASE14_REQUIRED.get(kind, frozenset())
    if set(p) - allowed:
        raise Denied(f'Unknown payload fields for {kind}: {sorted(set(p) - allowed)}')
    missing = required - set(p)
    if missing:
        raise Denied(f'Required fields missing for {kind}: {sorted(missing)}')
