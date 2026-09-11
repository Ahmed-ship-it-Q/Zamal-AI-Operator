"""Connection Registry and Tool Health Gate — Phase 10.

Canonical registry of all tools/services/connectors.
Credentials MUST NOT be stored here; only connectivity metadata.
"""
import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field, replace
from .store import Denied
from .integration_security import validate_data
from typing import Optional

HEALTH_OK = 'OK'
HEALTH_DEGRADED = 'DEGRADED'
HEALTH_UNAVAILABLE = 'UNAVAILABLE'
HEALTH_UNCONFIGURED = 'UNCONFIGURED'
HEALTH_BLOCKED = 'BLOCKED'

TIER_SAFE = 'SAFE'
TIER_PROTECTED = 'PROTECTED'
TIER_BLOCKED = 'BLOCKED'


@dataclass(frozen=True)
class ConnectionRecord:
    id: str
    display_name: str
    tier: str
    health_url: Optional[str] = None
    enabled: bool = True
    notes: str = ''
    _blocked_reason: str = field(default='', compare=False)

    def to_dict(self):
        return {
            'id': self.id,
            'display_name': self.display_name,
            'tier': self.tier,
            'enabled': self.enabled,
            'health_url': self.health_url,
            'notes': self.notes,
            'blocked_reason': self._blocked_reason if self.tier == TIER_BLOCKED else None,
        }


class ToolHealthGate:
    """Pre-dispatch health check: registered? configured? enabled? healthy? permitted?
    Checks are bounded (<= 5s), non-destructive, and have no side effects.
    """

    def __init__(self, registry):
        self._registry = registry

    def check(self, connector_id):
        record = self._registry.get(connector_id)
        if record is None:
            return {'connector_id': connector_id, 'status': HEALTH_UNCONFIGURED, 'reason': 'Not registered'}
        if record.tier == TIER_BLOCKED:
            return {'connector_id': connector_id, 'status': HEALTH_BLOCKED, 'reason': record._blocked_reason or 'Administratively blocked'}
        if not record.enabled:
            return {'connector_id': connector_id, 'status': HEALTH_UNAVAILABLE, 'reason': 'Connector disabled'}
        if record.health_url:
            return self._http_check(connector_id, record.health_url)
        return {'connector_id': connector_id, 'status': HEALTH_UNCONFIGURED, 'reason': 'No verified availability probe'}

    def _http_check(self, connector_id, url):
        try:
            if url not in ('http://127.0.0.1:5678/healthz/readiness', 'http://127.0.0.1:8787/health'):
                return {'connector_id':connector_id,'status':HEALTH_BLOCKED,'reason':'Health endpoint not allowlisted'}
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs): return None
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            req = urllib.request.Request(url, method='GET')
            with opener.open(req, timeout=5) as resp:
                status = resp.status
                data = json.loads(resp.read(4097))
                valid = data.get('status') == 'ok' if connector_id == 'n8n' else data.get('ok') is True and data.get('readOnly') is True
                if 200 <= status < 300 and valid:
                    return {'connector_id': connector_id, 'status': HEALTH_OK, 'http_status': status}
                if 500 <= status < 600:
                    return {'connector_id': connector_id, 'status': HEALTH_DEGRADED, 'http_status': status}
                return {'connector_id': connector_id, 'status': HEALTH_UNAVAILABLE, 'http_status': status}
        except urllib.error.URLError as exc:
            return {'connector_id': connector_id, 'status': HEALTH_UNAVAILABLE, 'reason': 'health_request_failed'}
        except TimeoutError:
            return {'connector_id': connector_id, 'status': HEALTH_UNAVAILABLE, 'reason': 'timeout'}
        except Exception as exc:
            return {'connector_id': connector_id, 'status': HEALTH_UNAVAILABLE, 'reason': 'invalid_health_response'}


class ConnectionRegistry:
    """Canonical, operator-authoritative registry.  Immutable after construction."""

    _BUILTINS = [
        ConnectionRecord('n8n', 'n8n Workflow Engine', TIER_SAFE,
                         health_url='http://127.0.0.1:5678/healthz/readiness'),
        ConnectionRecord('cpc_inspector', 'CPC Inspector', TIER_SAFE,
                         health_url='http://127.0.0.1:8787/health'),
        ConnectionRecord('mac_worker', 'MacOS Local Worker', TIER_SAFE),
        ConnectionRecord('browser_worker', 'Browser Automation Worker', TIER_SAFE),
        ConnectionRecord('local_worker', 'Local Execution Worker', TIER_SAFE),
        ConnectionRecord('cpc_worker', 'CPC Coding Pipeline Worker', TIER_PROTECTED),
        ConnectionRecord('verifier_worker', 'Verifier / Git Safety Worker', TIER_PROTECTED),
        ConnectionRecord('ai_inference', 'AI Inference (HandoffEngine)', TIER_PROTECTED),
        ConnectionRecord('codex', 'Codex CLI', TIER_BLOCKED,
                         enabled=False,
                         _blocked_reason='unified_exec=true safety contract; DO NOT reclassify or bypass'),
        ConnectionRecord('telegram_bot', 'Telegram Remote Gateway', TIER_SAFE,
                         notes='Health derived from ZAMAL_TELEGRAM_TOKEN env; no HTTP probe'),
    ]

    def __init__(self, extra=None, store=None):
        self.store = store
        self._records = {r.id: r for r in self._BUILTINS}
        for r in (extra or []):
            if not isinstance(r, ConnectionRecord):
                raise ValueError('Registry entries must be ConnectionRecord instances')
            if r.id in self._records:
                raise Denied('Duplicate connector identity')
            validate_data({'id':r.id,'display_name':r.display_name,'notes':r.notes})
            if r.tier not in (TIER_SAFE,TIER_PROTECTED,TIER_BLOCKED):
                raise Denied('Unknown connector tier')
            self._records[r.id] = r

    def get(self, connector_id):
        record = self._records.get(connector_id)
        if record is not None and self.store is not None:
            row = self.store.db.execute('SELECT * FROM connection_controls WHERE connector_id=?',(connector_id,)).fetchone()
            if row is None or row['enabled'] not in (0,1):
                raise Denied('Missing or corrupt connector state')
            record = replace(record,enabled=bool(row['enabled']))
        return record

    def all(self):
        return [self.get(cid) for cid in self._records]

    def status(self):
        gate = ToolHealthGate(self)
        results = []
        for r in self.all():
            health = gate.check(r.id)
            results.append({**r.to_dict(), 'health': health['status'], 'health_detail': health})
        return results

    def capability_status(self, root=None):
        """Read the evidence-indexed report; it never enables a connection or action."""
        import hashlib
        from pathlib import Path
        from .worker import Paths
        root=Path(root or Path(__file__).resolve().parent.parent)
        paths=Paths(root, '/Users/book/Desktop/AI-GROCERY-DECISION-PLATFORM')
        report=json.loads(paths.read('operator','knowledge/CAPABILITY_STATUS.json'))
        allowed={'LIVE_VERIFIED','CONNECTED','IMPLEMENTED_AWAITING_AUTH','AVAILABLE_NOT_ACTIVE','BLOCKED','OPTIONAL_EXPANSION'}
        if report.get('schema')!=1 or not isinstance(report.get('capabilities'),list):raise Denied('Invalid capability report')
        names=set()
        for item in report['capabilities']:
            if item.get('status') not in allowed or not item.get('name') or item['name'] in names:raise Denied('Invalid capability classification')
            names.add(item['name'])
            if item['status']=='LIVE_VERIFIED' and not item.get('evidence'):raise Denied('Live classification requires evidence')
            for ref in item.get('evidence',[]):
                if not isinstance(ref,dict) or set(ref)!={'path','sha256'} or not ref['path'].startswith('outbox/'):raise Denied('Invalid capability evidence reference')
                if hashlib.sha256(paths.read('operator',ref['path'])).hexdigest()!=ref['sha256']:raise Denied('Capability evidence changed; reverify status')
        validate_data(report)
        return report
