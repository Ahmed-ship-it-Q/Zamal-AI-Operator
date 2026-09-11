"""Controlled Webhook / Event Gateway — Phase 10.

Validates, deduplicates, normalizes, and routes inbound events.
Payload is DATA only — never authorization.
"approve" verbs in payload are explicitly denied.
Unknown sources are denied.
"""
import json
import time
import uuid
import sqlite3
from .store import digest
from .integration_security import validate_data, failure

from .store import Denied, Conflict, encode, uid


ALLOWED_SOURCES = frozenset({'n8n', 'webhook', 'cli', 'intake', 'test', 'telegram'})
ALLOWED_EVENT_TYPES = frozenset({
    'task_create', 'task_update', 'intake_document', 'intake_screenshot',
    'intake_url', 'intake_voice', 'health_check', 'workflow_trigger',
    'notification', 'audit_event', 'error_report',
})

_APPROVAL_VERBS = frozenset({'approve', 'approved', 'authorization', 'authorize', 'grant_approval'})

_OUTCOME_SUCCESS = 'SUCCESS'
_OUTCOME_FAILED = 'FAILED'
_OUTCOME_UNAVAILABLE = 'UNAVAILABLE'
_OUTCOME_DEGRADED = 'DEGRADED'
_OUTCOME_DENIED = 'DENIED'
_OUTCOME_APPROVAL_REQUIRED = 'APPROVAL_REQUIRED'
_OUTCOME_NOT_CONFIGURED = 'NOT_CONFIGURED'
_OUTCOME_TIMEOUT = 'TIMEOUT'
_OUTCOME_NOT_VERIFIED = 'NOT_VERIFIED'
_OUTCOME_DUPLICATE = 'DUPLICATE'


def migrate_gateway(store):
    with store.transaction():
        row = store.db.execute("SELECT version FROM core_extensions WHERE name='gateway'").fetchone()
        if row:
            if row[0] != 1: raise Denied('Unknown gateway schema')
            store.db.execute('SELECT connector_id,enabled,revision FROM connection_controls LIMIT 0')
            store.db.execute('SELECT result FROM inbound_events LIMIT 0')
            return
        store.db.execute('CREATE TABLE connection_controls (connector_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), revision INTEGER NOT NULL DEFAULT 0)')
        from .connection_registry import ConnectionRegistry
        for r in ConnectionRegistry().all():
            store.db.execute('INSERT INTO connection_controls VALUES(?,?,0)',(r.id,int(r.enabled)))
        if 'result' not in [r[1] for r in store.db.execute('pragma table_info(inbound_events)')]:
            store.db.execute('ALTER TABLE inbound_events ADD COLUMN result TEXT')
        store.db.execute("INSERT INTO core_extensions VALUES('gateway',1)")


class InboundEventStore:
    def __init__(self, store):
        self._s = store

    def record(self, event_type, source, payload, event_key=None):
        now = time.time()
        event_id = uid()
        key = source + ':' + (event_key or digest([event_type,payload]))
        try:
            with self._s.transaction():
                self._s.db.execute(
                    'INSERT INTO inbound_events(id,event_key,event_type,source,payload,status,received_at) '
                    'VALUES(?,?,?,?,?,?,?)',
                    (event_id, key, event_type, source, encode(payload), 'received', now)
                )
        except sqlite3.IntegrityError:
            existing = self._s.db.execute('SELECT * FROM inbound_events WHERE event_key=?',(key,)).fetchone()
            if existing is None: raise
            if existing['event_type'] != event_type or existing['payload'] != encode(payload) or existing['source'] != source:
                raise Denied('Event key payload conflict')
            return {'outcome':_OUTCOME_DUPLICATE,'event_id':existing['id'],'event_key':key,
                    'previous':json.loads(existing['result']) if existing['result'] else None,
                    'status':existing['status']}
        return {'outcome': _OUTCOME_SUCCESS, 'event_id': event_id, 'event_key': key}

    def get(self, event_id):
        row = self._s.db.execute('SELECT * FROM inbound_events WHERE id=?', (event_id,)).fetchone()
        if row is None:
            raise Conflict('Event not found')
        r = dict(row)
        r['payload'] = json.loads(r['payload'])
        return r

    def list_recent(self, limit=50):
        rows = self._s.db.execute(
            'SELECT * FROM inbound_events ORDER BY received_at DESC LIMIT ?', (limit,)
        ).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            r['payload'] = json.loads(r['payload'])
            result.append(r)
        return result

    def mark_processed(self, event_id, task_id=None):
        with self._s.transaction():
            self._s.db.execute(
                'UPDATE inbound_events SET status=?,task_id=?,processed_at=? WHERE id=?',
                ('processed', task_id, time.time(), event_id)
            )

    def mark_rejected(self, event_id, reason):
        with self._s.transaction():
            self._s.db.execute(
                'UPDATE inbound_events SET status=?,rejection_reason=?,processed_at=? WHERE id=?',
                ('rejected', reason, time.time(), event_id)
            )


class EventValidator:
    """Validates inbound events. Payload is DATA; cannot authorize anything."""

    def validate(self, event_type, source, payload):
        if source not in ALLOWED_SOURCES:
            raise Denied(f'Unknown event source denied: {source}')
        if event_type not in ALLOWED_EVENT_TYPES:
            raise Denied(f'Unknown event type denied: {event_type}')
        if not isinstance(payload, dict):
            raise ValueError('Event payload must be a JSON object')
        validate_data(payload)
        self._check_approval_verbs(payload)

    def _check_approval_verbs(self, obj, depth=0):
        if depth > 12:
            raise Denied('Event nesting limit')
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in _APPROVAL_VERBS:
                    raise Denied(f'Payload key "{k}" looks like an approval verb; payload is DATA not authorization')
                self._check_approval_verbs(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                self._check_approval_verbs(item, depth + 1)


class EventNormalizer:
    """Normalizes a validated event into operator intake format."""

    def normalize(self, event_type, source, payload):
        return {
            'event_type': event_type,
            'source': source,
            'payload': payload,
            'normalized_at': time.time(),
        }


class EventRouter:
    """Routes normalized events to operator intake."""

    def __init__(self, store):
        self._s = store

    def route(self, event_id, normalized):
        event_type = normalized['event_type']
        payload = normalized['payload']
        if event_type == 'task_create':
            return self._route_task_create(event_id, payload)
        if event_type == 'health_check':
            from .connection_registry import ConnectionRegistry, ToolHealthGate
            health = ToolHealthGate(ConnectionRegistry(store=self._s)).check(payload.get('connector_id',''))
            return {'outcome':'SUCCESS' if health['status']=='OK' else 'UNAVAILABLE','ok':health['status']=='OK','health':health}
        if event_type == 'workflow_trigger':
            return self._route_action(event_id,payload)
        if event_type in ('notification','audit_event','error_report'):
            return {'outcome':'RECORDED','ok':True,'event_id':event_id}
        return failure('NOT_CONFIGURED','event_handler_not_connected',event_id=event_id)

    def _route_task_create(self, event_id, payload):
        text = payload.get('text', '')
        task_type = payload.get('type', 'generic')
        project = payload.get('project', 'default')
        request_key = f'gateway:{event_id}'
        if not text:
            return {'outcome': _OUTCOME_FAILED, 'event_id': event_id, 'reason': 'task_create requires text field'}
        task = self._s.create_task(text, task_type, project, request_key)
        return {'outcome': _OUTCOME_SUCCESS, 'ok':True, 'event_id': event_id, 'task_id': task['id']}

    def _route_action(self,event_id,payload):
        from .connection_registry import ToolHealthGate, ConnectionRegistry
        if set(payload) != {'connector_id','kind','arguments','text','project'}:
            return failure('DENIED','invalid_action_envelope')
        health = ToolHealthGate(ConnectionRegistry(store=self._s)).check(payload['connector_id'])
        if health['status'] != 'OK': return failure('UNAVAILABLE','connector_unavailable')
        # This integration is an intake/proposal boundary, never a remote executor.
        from .personal_ops import PersonalOpsWorker
        from .business_ops import BusinessWorker
        worker=BusinessWorker(self._s) if payload['kind'].startswith('biz_') else PersonalOpsWorker(self._s) if payload['kind'].startswith('ops_') else GatewayWorker(self._s)
        gate=worker.gate()
        gate.policy.requires_approval(payload['kind'])
        task=self._s.create_task(payload['text'],'integration',payload['project'],f'gateway:{event_id}')
        for state in ('UNDERSTOOD','PLANNED'):
            task=self._s.transition(task['id'],state,task['revision'])
        action=gate.propose(task['id'],payload['kind'],payload['arguments'],f'event:{event_id}')
        return {'outcome':'APPROVAL_REQUIRED' if action['approval'] else 'PLANNED','ok':True,
                'task_id':task['id'],'action_id':action['id'],
                'approval_id':action['approval']['id'] if action['approval'] else None}


class InboundEventGateway:
    """Main entry point for inbound events from n8n or other sources."""

    def __init__(self, store):
        self._s = store
        self._event_store = InboundEventStore(store)
        self._validator = EventValidator()
        self._normalizer = EventNormalizer()
        self._router = EventRouter(store)

    def receive(self, event_type, source, payload, event_key=None):
        from .safety import Controls
        try:
            Controls(self._s).check('inspect')
            if event_key is not None and (not isinstance(event_key,str) or not 1<=len(event_key)<=128):
                raise Denied('Invalid event key')
            validate_data({'event_key':event_key})
            self._validator.validate(event_type,source,payload)
            rec=self._event_store.record(event_type,source,payload,event_key)
        except (Denied,ValueError,TypeError,RecursionError):
            return failure('DENIED','invalid_event')
        except sqlite3.Error:
            return failure('FAILED','event_store_unavailable')
        if rec['outcome']==_OUTCOME_DUPLICATE: return rec
        eid=rec['event_id']
        try:
            result=self._router.route(eid,self._normalizer.normalize(event_type,source,payload))
        except (Denied,ValueError,TypeError):
            result=failure('DENIED','action_policy_denied')
        except Exception:
            result=failure('FAILED','operator_route_failed')
        result={**result,'event_id':eid}
        try:
            with self._s.transaction():
                self._s.db.execute('UPDATE inbound_events SET status=?,task_id=?,processed_at=?,result=? WHERE id=? AND status=?',
                    ('processed' if result.get('ok') else 'rejected',result.get('task_id'),time.time(),encode(result),eid,'received'))
        except sqlite3.Error:
            # A task/action may already exist. Keep the durable event claim and
            # never retry it automatically or return the unpersisted success.
            return failure('FAILED','event_result_not_persisted',event_id=eid)
        return result


_PHASE10_KINDS = frozenset({
    'connection_status', 'event_inspect', 'workflow_export',
    'connection_enable', 'connection_disable',
})
_PHASE10_SAFE = frozenset({'connection_status', 'event_inspect', 'workflow_export'})
_PHASE10_PROTECTED = frozenset({'connection_enable', 'connection_disable'})

_PHASE10_FIELDS = {
    'connection_status':  frozenset({'connector_id'}),
    'event_inspect':      frozenset({'event_id'}),
    'workflow_export':    frozenset({}),
    'connection_enable':  frozenset({'connector_id', 'reason'}),
    'connection_disable': frozenset({'connector_id', 'reason'}),
}


class GatewayWorker:
    """Phase 10: Tool & Connection Gateway capabilities.
    Extends VerifierWorker via composition to preserve the full worker chain.
    """

    def __init__(self, store, *, operator=None, cpc=None):
        from .verifier import VerifierWorker
        from .connection_registry import ConnectionRegistry, ToolHealthGate
        from .store import Gate
        from .worker import Capability, MappingProxyType
        self.store = store
        self._verifier = VerifierWorker(store,operator=operator,cpc=cpc)
        self.paths = self._verifier.paths
        self._registry = ConnectionRegistry(store=store)
        self._health_gate = ToolHealthGate(self._registry)
        self._event_store = InboundEventStore(store)
        self._gateway = InboundEventGateway(store)
        self._Capability = Capability
        self._Gate = Gate
        self._MappingProxyType = MappingProxyType

    def gate(self):
        from .store import Gate
        from .worker import Capability
        from types import MappingProxyType
        verifier_gate = self._verifier.gate()
        p10_handlers = {k: Capability(self, k) for k in _PHASE10_KINDS}
        all_handlers = dict(verifier_gate._handlers)
        all_handlers.update(p10_handlers)
        return Gate(self.store, all_handlers)

    def validate(self, kind, p, project):
        if kind not in _PHASE10_KINDS:
            return self._verifier.validate(kind, p, project)
        if not isinstance(p, dict):
            raise Denied('Structured payload required')
        expected = _PHASE10_FIELDS[kind]
        if set(p) != expected:
            raise Denied(f'Payload must have exactly: {sorted(expected)}')
        if kind in ('connection_status', 'connection_enable', 'connection_disable'):
            cid = p.get('connector_id', '')
            if not isinstance(cid, str) or not cid.strip():
                raise Denied('connector_id must be a non-empty string')
        if kind in ('connection_enable','connection_disable'):
            if not isinstance(p['reason'],str) or not p['reason'].strip(): raise Denied('Reason required')
            validate_data(p)
            r=self._registry.get(p['connector_id'])
            if r is None or r.tier=='BLOCKED': raise Denied('Unknown or permanently blocked connector')
        if kind == 'event_inspect':
            eid = p.get('event_id', '')
            if not isinstance(eid, str) or not eid.strip():
                raise Denied('event_id must be a non-empty string')

    def run(self, kind, p, project):
        from .safety import Controls
        Controls(self.store).check(kind,project)
        self.validate(kind,p,'operator')
        if kind not in _PHASE10_KINDS:
            return self._verifier.run(kind, p, project)
        if kind == 'connection_status':
            return self._health_gate.check(p['connector_id'])
        if kind == 'event_inspect':
            return self._event_store.get(p['event_id'])
        if kind == 'workflow_export':
            from .workflow_export import WorkflowExporter
            return WorkflowExporter(store=self.store).export()
        if kind in ('connection_enable', 'connection_disable'):
            with self.store.transaction():
                Controls(self.store).check(kind,project)
                self.validate(kind,p,'operator')
                self.store.db.execute('UPDATE connection_controls SET enabled=?,revision=revision+1 WHERE connector_id=?',
                    (int(kind=='connection_enable'),p['connector_id']))
            return {'outcome':'SUCCESS','ok':True,'connector_id':p['connector_id'],'enabled':kind=='connection_enable'}
        raise Denied('Unknown gateway capability')
