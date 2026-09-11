"""Phase 12: Monitoring, Recovery, Cost/Usage and Security.

Architecture:
  MonitorWorker → extends GatewayWorker (full Phase 1–11 capability chain)
  ServiceMonitor  — health sweep + safe service recovery with 60s idempotency guard
  SecurityAuditor — credential/permission/secret checks; key names only, never values
  UsageTracker    — action/event counts from existing SQLite tables; no external API
  OutputReaper    — eligible-output listing and sha256-verified deletion

SAFE kinds (no approval): monitor_sweep, security_audit, usage_summary, output_list
PROTECTED kinds (approval required): service_recover, output_purge
Emergency controls block all mutating operations.
"""
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

from .store import Denied, Conflict, uid, encode
from .integration_security import SECRET_KEY, failure

_PHASE12_SAFE = frozenset({'monitor_sweep', 'security_audit', 'usage_summary', 'output_list'})
_PHASE12_PROTECTED = frozenset({'service_recover', 'output_purge'})
_PHASE12_KINDS = _PHASE12_SAFE | _PHASE12_PROTECTED

_PHASE12_FIELDS = {
    'monitor_sweep':    frozenset(),
    'security_audit':   frozenset(),
    'usage_summary':    frozenset({'since_hours'}),
    'output_list':      frozenset(),
    'service_recover':  frozenset({'service', 'reason'}),
    'output_purge':     frozenset({'output_id', 'sha256', 'reason'}),
}

# Keys that are known-safe in gateway auth files (key names only, never values)
_KNOWN_GATEWAY_KEYS = frozenset({
    'ZAMAL_N8N_GATEWAY_SECRET', 'ZAMAL_TELEGRAM_TOKEN', 'ZAMAL_TELEGRAM_CHAT_ID',
})
# Allowed gateway binding host
_LOOPBACK_HOST = '127.0.0.1'

_RECOVER_COOLDOWN_SECONDS = 60


def migrate_monitor(store):
    """Idempotent Phase 12 schema extension. Uses core_extensions pattern."""
    with store.transaction():
        row = store.db.execute("SELECT version FROM core_extensions WHERE name='monitor'").fetchone()
        if row:
            if row[0] != 1:
                raise Denied('Unknown monitor schema version')
            # Verify tables exist
            store.db.execute('SELECT seq FROM monitor_log LIMIT 0')
            store.db.execute('SELECT seq FROM security_audit_log LIMIT 0')
            return
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS monitor_log (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              timestamp REAL NOT NULL,
              category TEXT NOT NULL,
              connector_id TEXT,
              detail TEXT NOT NULL,
              actor TEXT,
              outcome TEXT NOT NULL
            )
        ''')
        store.db.execute('''
            CREATE TRIGGER IF NOT EXISTS monitor_log_no_update
            BEFORE UPDATE ON monitor_log
            BEGIN SELECT RAISE(ABORT, 'monitor_log is append-only'); END
        ''')
        store.db.execute('''
            CREATE TRIGGER IF NOT EXISTS monitor_log_no_delete
            BEFORE DELETE ON monitor_log
            BEGIN SELECT RAISE(ABORT, 'monitor_log is append-only'); END
        ''')
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS security_audit_log (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              timestamp REAL NOT NULL,
              check_name TEXT NOT NULL,
              result TEXT NOT NULL,
              findings TEXT NOT NULL
            )
        ''')
        store.db.execute('''
            CREATE TRIGGER IF NOT EXISTS security_audit_log_no_update
            BEFORE UPDATE ON security_audit_log
            BEGIN SELECT RAISE(ABORT, 'security_audit_log is append-only'); END
        ''')
        store.db.execute('''
            CREATE TRIGGER IF NOT EXISTS security_audit_log_no_delete
            BEFORE DELETE ON security_audit_log
            BEGIN SELECT RAISE(ABORT, 'security_audit_log is append-only'); END
        ''')
        store.db.execute("INSERT INTO core_extensions VALUES('monitor',1)")


class MonitorLog:
    def __init__(self, store):
        self._s = store

    def record(self, category, outcome, *, connector_id=None, detail=None, actor=None):
        with self._s.transaction():
            self._s.db.execute(
                'INSERT INTO monitor_log(timestamp,category,connector_id,detail,actor,outcome) '
                'VALUES(?,?,?,?,?,?)',
                (time.time(), category, connector_id, encode(detail or {}), actor, outcome)
            )

    def recent(self, limit=50):
        rows = self._s.db.execute(
            'SELECT * FROM monitor_log ORDER BY seq DESC LIMIT ?', (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


class ServiceMonitor:
    """Health sweep and safe service recovery."""

    def __init__(self, store):
        self._s = store
        self._log = MonitorLog(store)

    def sweep(self) -> dict:
        """Check all registered connectors. Writes audit row. Never raises."""
        from .connection_registry import ConnectionRegistry, ToolHealthGate
        registry = ConnectionRegistry(store=self._s)
        gate = ToolHealthGate(registry)
        results = []
        try:
            connectors = registry.all()
        except Exception as exc:
            return {'outcome': 'FAILED', 'error': 'registry_unavailable'}
        for rec in connectors:
            health = gate.check(rec.id)
            results.append({'connector_id': rec.id, 'tier': rec.tier,
                            'status': health['status']})
            self._log.record('health_sweep', health['status'],
                             connector_id=rec.id,
                             detail={'tier': rec.tier, 'health': health})
        return {'outcome': 'SUCCESS', 'ok': True, 'connectors': results,
                'swept_at': time.time(), 'count': len(results)}

    def recover_service(self, service, *, actor, reason) -> dict:
        """Safe restart via ProcessControl. Idempotency: one recovery per 60s per service."""
        from .safety import Controls
        from .mac_worker import ProcessControl, APPROVED_SERVICES
        Controls(self._s).check('service_recover')
        if service not in APPROVED_SERVICES:
            raise Denied(f'Service {service!r} not in approved list')
        # Idempotency guard: no duplicate restart within cooldown window
        cutoff = time.time() - _RECOVER_COOLDOWN_SECONDS
        recent = self._s.db.execute(
            "SELECT seq FROM monitor_log WHERE category='recovery' AND connector_id=? "
            "AND timestamp > ? AND outcome NOT IN ('ALREADY_RUNNING')",
            (service, cutoff)
        ).fetchone()
        if recent:
            raise Denied(f'Recovery for {service!r} already attempted within {_RECOVER_COOLDOWN_SECONDS}s')
        ctrl = ProcessControl(self._s)
        st = ctrl.status(service)
        if st['running']:
            self._log.record('recovery', 'ALREADY_RUNNING',
                             connector_id=service, actor=actor,
                             detail={'reason': reason, 'status': st})
            return {'outcome': 'ALREADY_RUNNING', 'ok': True, 'service': service, 'status': st}
        try:
            result = ctrl.start(service, actor)
        except Exception as exc:
            self._log.record('recovery', 'FAILED', connector_id=service, actor=actor,
                             detail={'reason': reason, 'error': str(exc)})
            raise
        self._log.record('recovery', 'SUCCESS', connector_id=service, actor=actor,
                         detail={'reason': reason, 'result': result})
        return {'outcome': 'SUCCESS', 'ok': True, 'service': service, 'result': result}


class SecurityAuditor:
    """Credential/permission/secret checks. Reports key names only, never values."""

    def __init__(self, store, *, gateway_config_path=None, operator_root=None):
        self._s = store
        self._gateway_config = Path(gateway_config_path or 'state/gateway/auth.env')
        self._operator_root = Path(operator_root or Path(__file__).resolve().parent.parent)

    def check_env_secrets(self) -> dict:
        """Report env key names that match the SECRET_KEY pattern. Never logs values."""
        flagged = [k for k in os.environ if SECRET_KEY.search(k) and k not in _KNOWN_GATEWAY_KEYS]
        # Also check if known gateway keys are unexpectedly absent
        configured = {k: k in os.environ for k in _KNOWN_GATEWAY_KEYS}
        result = 'warn' if flagged else 'pass'
        return {'check': 'env_secrets', 'result': result,
                'flagged_key_names': flagged, 'configured_keys': configured}

    def check_gateway_config(self) -> dict:
        """Verify gateway config: exists, mode 600, no embedded credentials, loopback only."""
        findings = []
        result = 'pass'
        p = self._gateway_config
        if not p.exists():
            return {'check': 'gateway_config', 'result': 'warn',
                    'findings': ['config_not_found']}
        try:
            mode = oct(stat.S_IMODE(p.stat().st_mode))
            if mode != '0o600':
                findings.append(f'file_mode_{mode}_not_600')
                result = 'warn'
        except OSError:
            findings.append('stat_failed')
            result = 'warn'
        # Read and check key names only — check no unexpected keys
        try:
            lines = p.read_text().splitlines()
            for line in lines:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                key = line.split('=', 1)[0].strip()
                if key not in _KNOWN_GATEWAY_KEYS:
                    findings.append(f'unexpected_key:{key}')
                    result = 'warn'
        except OSError:
            findings.append('read_failed')
            result = 'fail'
        return {'check': 'gateway_config', 'result': result, 'findings': findings}

    def check_outbox_permissions(self) -> dict:
        """Spot-check that worker outbox files are not world-readable."""
        outbox = self._operator_root / 'outbox'
        findings = []
        checked = 0
        if outbox.exists():
            for p in outbox.rglob('*'):
                if p.is_file():
                    checked += 1
                    try:
                        mode = p.stat().st_mode
                        if mode & 0o044:  # world or group readable
                            findings.append(f'world_readable:{p.relative_to(self._operator_root)}')
                    except OSError:
                        pass
                    if checked >= 100:
                        break
        result = 'warn' if findings else 'pass'
        return {'check': 'outbox_permissions', 'result': result,
                'checked': checked, 'findings': findings[:10]}

    def full_audit(self) -> dict:
        checks = [
            self.check_env_secrets(),
            self.check_gateway_config(),
            self.check_outbox_permissions(),
        ]
        overall = 'pass'
        for c in checks:
            if c['result'] == 'fail':
                overall = 'fail'
            elif c['result'] == 'warn' and overall == 'pass':
                overall = 'warn'
        with self._s.transaction():
            for c in checks:
                self._s.db.execute(
                    'INSERT INTO security_audit_log(timestamp,check_name,result,findings) VALUES(?,?,?,?)',
                    (time.time(), c['check'], c['result'], json.dumps(c.get('findings', [])))
                )
        return {'outcome': 'SUCCESS', 'ok': True, 'overall': overall,
                'checks': checks, 'audited_at': time.time()}


class UsageTracker:
    """Cost/usage visibility from existing SQLite tables. No external API."""

    def __init__(self, store):
        self._s = store

    def action_summary(self, since_hours=None) -> dict:
        since = (time.time() - since_hours * 3600) if since_hours else 0
        rows = self._s.db.execute(
            'SELECT kind, status, COUNT(*) as n FROM actions WHERE created >= ? GROUP BY kind, status',
            (since,)
        ).fetchall()
        by_kind = {}
        total = 0
        for r in rows:
            by_kind.setdefault(r['kind'], {})[r['status']] = r['n']
            total += r['n']
        return {'total_actions': total, 'by_kind': by_kind,
                'since_hours': since_hours, 'queried_at': time.time()}

    def event_summary(self, since_hours=None) -> dict:
        since = (time.time() - since_hours * 3600) if since_hours else 0
        rows = self._s.db.execute(
            'SELECT source, event_type, COUNT(*) as n FROM inbound_events WHERE received_at >= ? GROUP BY source, event_type',
            (since,)
        ).fetchall()
        by_source = {}
        total = 0
        for r in rows:
            by_source.setdefault(r['source'], {})[r['event_type']] = r['n']
            total += r['n']
        return {'total_events': total, 'by_source': by_source,
                'since_hours': since_hours, 'queried_at': time.time()}

    def summary(self, since_hours=None) -> dict:
        return {'actions': self.action_summary(since_hours),
                'events': self.event_summary(since_hours)}


class OutputReaper:
    """Eligible worker-output listing and sha256-verified deletion."""

    def __init__(self, store, *, policy_days=3, operator_root=None):
        self._s = store
        self._policy_days = policy_days
        self._op_root = Path(operator_root or Path(__file__).resolve().parent.parent)
        self._log = MonitorLog(store)

    def list_eligible(self) -> list:
        cutoff = time.time() - self._policy_days * 86400
        rows = self._s.db.execute(
            'SELECT id, path, sha256, created FROM worker_outputs '
            'WHERE deleted IS NULL AND created < ?',
            (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]

    def purge(self, output_id, *, actor, reason, sha256) -> dict:
        """PROTECTED. Verify eligibility, re-verify sha256, delete, mark deleted, audit."""
        from .safety import Controls
        Controls(self._s).check('output_purge')
        if not all(isinstance(v, str) and v.strip() for v in (output_id, actor, reason, sha256)):
            raise Denied('output_id, actor, reason and sha256 required')
        cutoff = time.time() - self._policy_days * 86400
        row = self._s.db.execute(
            'SELECT * FROM worker_outputs WHERE id=? AND deleted IS NULL AND created < ?',
            (output_id, cutoff)
        ).fetchone()
        if row is None:
            raise Denied('Output not eligible for purge: not found, already deleted, or too recent')
        if row['sha256'] != sha256:
            raise Denied('sha256 mismatch: output changed or wrong ID provided')
        file_path = self._op_root / row['path']
        if not file_path.exists():
            raise Denied('Output file not found on disk; cannot verify before deletion')
        actual_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual_hash != sha256:
            raise Denied('File content sha256 does not match record; deletion denied')
        file_path.unlink()
        with self._s.transaction():
            self._s.db.execute(
                'UPDATE worker_outputs SET deleted=? WHERE id=?', (time.time(), output_id)
            )
        self._log.record('output_purge', 'SUCCESS', actor=actor,
                         detail={'output_id': output_id, 'path': row['path'], 'reason': reason})
        return {'outcome': 'SUCCESS', 'ok': True, 'output_id': output_id,
                'path': row['path'], 'purged_at': time.time()}


class MonitorWorker:
    """Phase 12 monitor capabilities; extends GatewayWorker via composition."""

    def __init__(self, store, *, operator=None, cpc=None):
        from .gateway import GatewayWorker
        self.store = store
        self._gateway = GatewayWorker(store, operator=operator, cpc=cpc)
        self.paths = self._gateway.paths
        self._service_monitor = ServiceMonitor(store)
        self._security = SecurityAuditor(store)
        self._usage = UsageTracker(store)
        self._reaper = OutputReaper(store)

    def gate(self):
        from .store import Gate
        from .worker import Capability
        gw_gate = self._gateway.gate()
        p12_handlers = {k: Capability(self, k) for k in _PHASE12_KINDS}
        all_handlers = dict(gw_gate._handlers)
        all_handlers.update(p12_handlers)
        return Gate(self.store, all_handlers)

    def validate(self, kind, p, project):
        if kind not in _PHASE12_KINDS:
            return self._gateway.validate(kind, p, project)
        if not isinstance(p, dict):
            raise Denied('Structured payload required')
        expected = _PHASE12_FIELDS[kind]
        optional = frozenset({'since_hours'}) if kind == 'usage_summary' else frozenset()
        if set(p) - expected - optional or (expected - optional) - set(p):
            raise Denied(f'Payload must have exactly: {sorted(expected)}')
        if kind == 'service_recover':
            if not isinstance(p.get('service'), str) or not p['service'].strip():
                raise Denied('service must be a non-empty string')
            if not isinstance(p.get('reason'), str) or not p['reason'].strip():
                raise Denied('reason required')
        if kind == 'output_purge':
            for k in ('output_id', 'sha256', 'reason'):
                if not isinstance(p.get(k), str) or not p[k].strip():
                    raise Denied(f'{k} must be a non-empty string')

    def run(self, kind, p, project):
        from .safety import Controls
        Controls(self.store).check(kind)
        self.validate(kind, p, project)
        if kind not in _PHASE12_KINDS:
            return self._gateway.run(kind, p, project)
        if kind == 'monitor_sweep':
            return self._service_monitor.sweep()
        if kind == 'security_audit':
            return self._security.full_audit()
        if kind == 'usage_summary':
            return self._usage.summary(since_hours=p.get('since_hours'))
        if kind == 'output_list':
            return {'outcome': 'SUCCESS', 'ok': True,
                    'eligible': self._reaper.list_eligible()}
        if kind == 'service_recover':
            actor = f'gate:{project}'
            return self._service_monitor.recover_service(
                p['service'], actor=actor, reason=p['reason'])
        if kind == 'output_purge':
            actor = f'gate:{project}'
            return self._reaper.purge(
                p['output_id'], actor=actor, reason=p['reason'], sha256=p['sha256'])
        raise Denied('Unknown monitor capability')
