"""Versioned n8n Workflow Export — Phase 10.

Exports redacted n8n workflow audit snapshots from the existing read-only SQLite source.
Strips ALL credentials before writing. Never overwrites a prior export.
The encryptionKey and any credential material must NEVER appear in output.
"""
import hashlib
import json
import os
import sqlite3
import time
import urllib.request
import urllib.error
import uuid
from .integration_security import SECRET_KEY, SECRET_TEXT
from .store import Denied
from pathlib import Path


_STRIP_KEYS = frozenset({
    'encryptionKey', 'encryption_key', 'encryptionkey',
    'password', 'secret', 'token', 'apiKey', 'api_key',
    'accessToken', 'access_token', 'clientSecret', 'client_secret',
    'privateKey', 'private_key', 'credential', 'credentials',
    'Authorization', 'authorization', 'auth',
})

_N8N_API_BASE = 'http://127.0.0.1:5678/api/v1'
_N8N_DB_CANDIDATES = [
    Path.home() / 'zamal-ai-operator' / '.n8n-data' / '.n8n' / 'database.sqlite',
    Path('/Users/book/zamal-ai-operator/.n8n-data/.n8n/database.sqlite'),
]
_OUTBOX = Path('/Users/book/zamal-ai-operator/state/n8n_exports')


def _strip_credentials(obj, depth=0):
    """Recursively remove credential-bearing keys. Returns sanitized copy."""
    if depth > 20:
        raise Denied('Workflow export nesting limit')
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if SECRET_KEY.search(k) or k in ('parameters','staticData','static_data','pinData'):
                result[k] = '***REDACTED***'
            else:
                result[k] = _strip_credentials(v, depth + 1)
        return result
    if isinstance(obj, list):
        return [_strip_credentials(item, depth + 1) for item in obj]
    if isinstance(obj,str) and SECRET_TEXT.search(obj): return '***REDACTED***'
    return obj


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _version_tag() -> str:
    return time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex


class WorkflowExporter:
    def __init__(self, store=None, outbox=None, database=None):
        self._database = Path(database) if database else None
        self._store = store
        self._outbox = Path(outbox) if outbox else _OUTBOX
        if self._outbox.is_symlink() or any(p.is_symlink() for p in self._outbox.parents):
            raise Denied('Export directory must not contain symlinks')
        self._outbox.mkdir(mode=0o700, parents=True, exist_ok=True)

    def export(self):
        workflows = self._fetch_workflows()
        safe = _strip_credentials(workflows)
        payload_bytes = json.dumps(safe, indent=2, ensure_ascii=False, sort_keys=True).encode()
        sha = _sha256(payload_bytes)
        version = _version_tag()
        filename = f'n8n_workflows_{version}.json'
        out_path = self._outbox / filename
        if out_path.exists():
            raise FileExistsError(f'Export already exists: {out_path}')
        manifest = {
            'version': version,
            'sha256': sha,
            'workflow_count': len(safe) if isinstance(safe, list) else 1,
            'exported_at': time.time(),
            'source': self._source_label,
            'file': filename,
            'restore_ready': False,
            'redaction': 'Credentials, parameters, runtime/static and pinned data removed; review and rebind before restore',
        }
        fd = os.open(out_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(payload_bytes)
            f.flush()
            os.fsync(f.fileno())
        manifest_path=self._outbox / (filename + '.manifest.json')
        with os.fdopen(os.open(manifest_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as f:
            json.dump(manifest,f,sort_keys=True);f.flush();os.fsync(f.fileno())
        if self._store:
            self._record_export(version, manifest, str(out_path), sha)
        return {'outcome': 'SUCCESS', 'version': version, 'sha256': sha, 'file': str(out_path), 'manifest': manifest}

    def _fetch_workflows(self):
        # Reuse the local n8n database read-only; no API key, fallback, pagination
        # truncation, credential-table reads or live workflow writes.
        self._source_label='n8n_sqlite_readonly'
        return self._fetch_via_sqlite()

    def _fetch_via_sqlite(self):
        for candidate in ([self._database] if self._database else _N8N_DB_CANDIDATES):
            if candidate.exists():
                if candidate.is_symlink(): raise Denied('n8n database symlink denied')
                conn = sqlite3.connect(candidate.absolute().as_uri() + '?mode=ro', uri=True, timeout=5)
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute('SELECT id, name, active, nodes, connections, settings, createdAt, updatedAt FROM workflow_entity').fetchall()
                    workflows = []
                    for row in rows:
                        w = dict(row)
                        for key in ('nodes', 'connections', 'settings', 'static_data'):
                            if w.get(key) and isinstance(w[key], str):
                                try:
                                    w[key] = json.loads(w[key])
                                except Exception:
                                    pass
                        workflows.append(w)
                    return workflows
                finally:
                    conn.close()
        raise FileNotFoundError('n8n SQLite database not found')

    def _record_export(self, version, manifest, file_path, sha):
        from .store import uid
        export_id = uid()
        self._store.db.execute(
            'INSERT INTO n8n_exports(id,version,manifest,file_path,sha256,exported_at) VALUES(?,?,?,?,?,?)',
            (export_id, version, json.dumps(manifest), file_path, sha, time.time())
        )
