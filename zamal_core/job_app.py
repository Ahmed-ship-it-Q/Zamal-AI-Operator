"""Job Application Browser Worker: end-to-end application flow with Telegram approval gate.

Flow:
  1. job_app_open   (SAFE)      — open job URL in default browser for human review
  2. job_app_plan   (SAFE)      — prepare application from CV + job discovery result
  3. job_app_preflight (SAFE)   — validate all required files/fields before submit
  4. job_app_submit (PROTECTED) — HTTP POST to Greenhouse API; Telegram APPROVE required
  5. Evidence in job_app_audit (append-only); status in job_applications table.

Board support:
  greenhouse — full API submission via public boards-api.greenhouse.io multipart form
  lever, linkedin, others — BLOCKED, fail safely, blocker recorded in job_applications

Deny-by-default:
  - Only ALLOWED_JOB_DOMAINS accepted for browser open
  - Candidate contact info read from config/candidate.json only at submit time (never in payload)
  - CV/cover-letter files must be in the operator outbox; path containment enforced
  - No credential/cookie harvesting; no platform security bypass
  - Emergency Stop blocks all dispatch; pause_external_actions blocks submit

stdlib only — no new dependencies.
"""
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .store import Gate, Denied, Conflict, digest, encode, uid
from .integration_security import validate_data, failure
from .safety import Controls
from .worker import Capability


class GreenhouseURLError(Denied):
    """Raised when a Greenhouse job URL fails structured validation.

    Always treated as BLOCKED before any HTTP call.  Inherits from Denied so
    existing exception-handling catch clauses remain correct.
    """

# ── Capability sets ───────────────────────────────────────────────────────────

JOB_APP_SAFE = frozenset({
    'job_app_fixture_prepare',
    'job_app_open',
    'job_app_plan',
    'job_app_preflight',
    'job_app_status',
})
JOB_APP_PROTECTED = frozenset({'job_app_submit'})
JOB_APP_KINDS = JOB_APP_SAFE | JOB_APP_PROTECTED
JOB_APP_READ_ONLY = frozenset({'job_app_open', 'job_app_preflight', 'job_app_status'})

# ── Board support ─────────────────────────────────────────────────────────────

ALLOWED_JOB_DOMAINS = frozenset({
    'boards.greenhouse.io',
    'jobs.greenhouse.io',
    'jobs.lever.co',
    'app.lever.co',
    'jobs.ashbyhq.com',
})
SUPPORTED_SUBMIT_BOARDS = frozenset({'greenhouse'})
_GREENHOUSE_SUBMIT = 'https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}/applications'
_JOB_URL_RE = re.compile(r'^https://([a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)*)(/[^\s<>"]*)?$', re.I)
_CREDENTIAL_URL_RE = re.compile(r'(https?://)[^\s/@:]+:[^\s/@]+@', re.I)
_GH_JOB_ID_RE = re.compile(r'/jobs/(\d+)')
_GH_BOARD_SLUG_RE = re.compile(r'greenhouse\.io/([^/]+)/jobs/', re.I)

# Greenhouse structured URL validation
_GREENHOUSE_HOSTNAMES = frozenset({
    'boards.greenhouse.io',
    'job-boards.greenhouse.io',
    'app.greenhouse.io',
})
_GH_SLUG_RE = re.compile(r'^[a-zA-Z0-9_-]+$')


def parse_greenhouse_url(url):
    """Structured parse of a Greenhouse job URL.

    Enforces:
      - https scheme only
      - Exact hostname from _GREENHOUSE_HOSTNAMES allowlist
      - No userinfo (credentials) in URL
      - No non-standard port
      - Board slug matches ^[a-zA-Z0-9_-]+$ and is non-empty
      - Job ID is numeric and non-empty

    Returns (board_slug, job_id) on success.
    Raises GreenhouseURLError with a descriptive message on any violation.
    Never makes an HTTP call.
    """
    if not isinstance(url, str) or not url.strip():
        raise GreenhouseURLError('Greenhouse URL must be a non-empty string')
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        raise GreenhouseURLError('Greenhouse URL could not be parsed')

    # Scheme must be exactly https
    if parsed.scheme != 'https':
        raise GreenhouseURLError(
            f'Greenhouse URL scheme must be https, got: {parsed.scheme!r}')

    # No credentials embedded in URL
    if parsed.username or parsed.password:
        raise GreenhouseURLError('Greenhouse URL must not contain embedded credentials')

    # No unexpected port
    if parsed.port is not None:
        raise GreenhouseURLError(
            f'Greenhouse URL must not specify a port, got: {parsed.port}')

    # Hostname must be in the exact allowlist (no lookalike domains)
    hostname = (parsed.hostname or '').lower()
    if hostname not in _GREENHOUSE_HOSTNAMES:
        raise GreenhouseURLError(
            f'Greenhouse URL hostname {hostname!r} not in allowlist '
            f'{sorted(_GREENHOUSE_HOSTNAMES)}')

    # Extract slug and job ID from path
    m_board = _GH_BOARD_SLUG_RE.search(url)
    m_job = _GH_JOB_ID_RE.search(url)
    missing = []
    if not m_board:
        missing.append('board slug')
    if not m_job:
        missing.append('job ID')
    if missing:
        raise GreenhouseURLError(
            f'Cannot extract {" and ".join(missing)} from Greenhouse URL: {url!r}')

    slug = m_board.group(1)
    job_id = m_job.group(1)

    if not slug or not _GH_SLUG_RE.match(slug):
        raise GreenhouseURLError(
            f'Greenhouse board slug {slug!r} contains invalid characters '
            f'(must match ^[a-zA-Z0-9_-]+$)')

    return slug, job_id
_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

# ── Payload schema ────────────────────────────────────────────────────────────

_JOB_APP_FIELDS = {
    'job_app_fixture_prepare': frozenset({'fixture'}),
    'job_app_open':      frozenset({'url', 'board_type'}),
    'job_app_plan':      frozenset({'source_action_id', 'job_id', 'cv_file_ref',
                                    'cover_letter_file_ref', 'candidate'}),
    'job_app_preflight': frozenset({'application_id'}),
    'job_app_status':    frozenset({'application_id'}),
    'job_app_submit':    frozenset({'application_id', 'acknowledge_gaps'}),
}
_JOB_APP_REQUIRED = {
    'job_app_fixture_prepare': frozenset({'fixture'}),
    'job_app_open':      frozenset({'url', 'board_type'}),
    'job_app_plan':      frozenset({'source_action_id', 'job_id', 'cv_file_ref', 'candidate'}),
    'job_app_preflight': frozenset({'application_id'}),
    'job_app_status':    frozenset({'application_id'}),
    'job_app_submit':    frozenset({'application_id', 'acknowledge_gaps'}),
}


def validate_job_app(kind, p, project):
    if kind not in JOB_APP_KINDS or project != 'operator' or not isinstance(p, dict):
        raise Denied('Job application operation outside approved Operator scope')
    validate_data(p)
    extra = set(p) - _JOB_APP_FIELDS[kind]
    if extra:
        raise Denied(f'Unknown fields for {kind}: {sorted(extra)}')
    missing = _JOB_APP_REQUIRED[kind] - set(p)
    if missing:
        raise Denied(f'Required fields missing for {kind}: {sorted(missing)}')
    if kind == 'job_app_open':
        _check_job_url(p['url'])
        if p['board_type'] not in ('greenhouse', 'lever', 'ashby', 'unknown'):
            raise Denied('board_type must be greenhouse, lever, ashby or unknown')
    if kind == 'job_app_plan':
        _check_candidate(p['candidate'])
        _check_file_ref(p['cv_file_ref'], 'cv_file_ref')
        if 'cover_letter_file_ref' in p:
            _check_file_ref(p['cover_letter_file_ref'], 'cover_letter_file_ref')
    if kind == 'job_app_fixture_prepare' and p['fixture'] != 'local-browser-v1':
        raise Denied('Only the fixed synthetic local browser fixture is supported')
    if kind == 'job_app_submit':
        if not isinstance(p['acknowledge_gaps'], bool):
            raise Denied('acknowledge_gaps must be a boolean')
        if not p['acknowledge_gaps']:
            raise Denied('Owner must set acknowledge_gaps=true to confirm eligibility before submission')


def _check_job_url(url):
    if not isinstance(url, str) or not url.strip():
        raise Denied('url must be a non-empty string')
    if len(url) > 2048:
        raise Denied('url exceeds maximum length')
    if _CREDENTIAL_URL_RE.search(url):
        raise Denied('url must not contain embedded credentials')
    if not url.lower().startswith('https://'):
        raise Denied('Only https:// job board URLs are permitted')
    m = _JOB_URL_RE.match(url)
    if not m:
        raise Denied('Malformed URL')
    domain = m.group(1).lower()
    if domain not in ALLOWED_JOB_DOMAINS:
        raise Denied(f'Domain {domain!r} not in job board allowlist: {sorted(ALLOWED_JOB_DOMAINS)}')


def _check_candidate(c):
    if (not isinstance(c, dict)
            or not {'name', 'skills', 'experience'} <= set(c)
            or set(c) - {'name', 'skills', 'experience'}):
        raise Denied('candidate must have exactly: name, skills, experience')
    if not isinstance(c['name'], str) or not c['name'].strip():
        raise Denied('candidate.name required')
    if (not isinstance(c['skills'], list) or len(c['skills']) > 40
            or any(not isinstance(x, str) for x in c['skills'])):
        raise Denied('candidate.skills must be a bounded list of strings')
    if not isinstance(c['experience'], str) or len(c['experience']) > 4000:
        raise Denied('candidate.experience must be a bounded string')


def _check_file_ref(ref, field):
    if not isinstance(ref, str) or not ref:
        raise Denied(f'{field} must be a non-empty string')
    parts = Path(ref).parts
    if any(p in ('..', '.', '') or p.startswith('.') for p in parts):
        raise Denied(f'{field}: path traversal or hidden component not permitted')
    if len(ref) > 500:
        raise Denied(f'{field}: path too long')


# ── Schema migration ──────────────────────────────────────────────────────────

def migrate_job_app(store):
    """Idempotent Phase job_app schema extension. Uses core_extensions pattern."""
    with store.transaction():
        row = store.db.execute(
            "SELECT version FROM core_extensions WHERE name='job_app'"
        ).fetchone()
        if row:
            if row[0] != 1:
                raise Denied('Unknown job_app schema version')
            store.db.execute('SELECT id FROM job_applications LIMIT 0')
            return
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS job_applications (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                board_type TEXT NOT NULL,
                job_id TEXT NOT NULL,
                job_title TEXT NOT NULL,
                company TEXT NOT NULL,
                job_url TEXT NOT NULL,
                cv_file_ref TEXT NOT NULL DEFAULT '',
                cover_letter_file_ref TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planned',
                submit_action_id TEXT,
                http_status INTEGER,
                response_hash TEXT NOT NULL DEFAULT '',
                blocker TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '{}',
                created REAL NOT NULL,
                updated REAL NOT NULL
            )
        ''')
        store.db.execute('''
            CREATE TABLE IF NOT EXISTS job_app_audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                application_id TEXT,
                operation TEXT NOT NULL,
                detail TEXT NOT NULL
            )
        ''')
        store.db.execute('''
            CREATE TRIGGER IF NOT EXISTS job_app_audit_no_update
            BEFORE UPDATE ON job_app_audit
            BEGIN SELECT RAISE(ABORT, 'job_app_audit is append-only'); END
        ''')
        store.db.execute('''
            CREATE TRIGGER IF NOT EXISTS job_app_audit_no_delete
            BEFORE DELETE ON job_app_audit
            BEGIN SELECT RAISE(ABORT, 'job_app_audit is append-only'); END
        ''')
        store.db.execute("INSERT INTO core_extensions VALUES('job_app', 1)")


# ── Job browser policy ────────────────────────────────────────────────────────

_OPEN_BIN = '/usr/bin/open'


class JobBrowserPolicy:
    """Open job board URLs in the system browser. External domain allowlist only."""

    def validate(self, url):
        _check_job_url(url)

    def open(self, url):
        self.validate(url)
        open_bin = Path(_OPEN_BIN)
        if not open_bin.is_file() or not os.access(open_bin, os.X_OK):
            raise Denied('/usr/bin/open is unavailable')
        result = subprocess.run(
            [_OPEN_BIN, url],
            capture_output=True, text=True, timeout=10, shell=False,
            env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home())},
        )
        return {'url': url, 'exit_code': result.returncode, 'opened': result.returncode == 0}


# ── Greenhouse HTTP submission ─────────────────────────────────────────────────

def _build_multipart(boundary, fields, files):
    """Build a multipart/form-data body using stdlib only."""
    buf = bytearray()
    crlf = b'\r\n'
    b = boundary.encode('ascii')
    for name, value in fields.items():
        buf += b'--' + b + crlf
        buf += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        buf += value.encode('utf-8') + crlf
    for name, (filename, content_type, data) in files.items():
        buf += b'--' + b + crlf
        buf += f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode() + crlf
        buf += f'Content-Type: {content_type}'.encode() + crlf + crlf
        buf += data + crlf
    buf += b'--' + b + b'--' + crlf
    return bytes(buf)


def _detect_content_type(path):
    ext = path.suffix.lower()
    return {
        '.pdf':  'application/pdf',
        '.doc':  'application/msword',
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.txt':  'text/plain',
        '.md':   'text/plain',
    }.get(ext, 'application/octet-stream')


class GreenhouseSubmitter:
    """Submit to Greenhouse public applications API via multipart POST."""

    def _read_file_safe(self, path, label):
        if not path.is_file():
            raise Denied(f'{label} not found: {path.name}')
        size = path.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise Denied(f'{label} exceeds 5 MB limit: {size} bytes')
        if path.is_symlink():
            raise Denied(f'{label} must not be a symlink')
        return path.read_bytes()

    def submit(self, board, gh_job_id, candidate_contact, cv_path,
               cover_letter_path=None):
        """POST application to Greenhouse. Returns (http_status, response_hash, success, parsed)."""
        cv_data = self._read_file_safe(cv_path, 'CV')
        files = {
            'resume': (cv_path.name, _detect_content_type(cv_path), cv_data),
        }
        if cover_letter_path:
            cl_data = self._read_file_safe(cover_letter_path, 'cover letter')
            files['cover_letter'] = (cover_letter_path.name, _detect_content_type(cover_letter_path), cl_data)

        fields = {k: str(v) for k, v in candidate_contact.items() if v}
        boundary = 'ZamalBoundary' + hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]
        body = _build_multipart(boundary, fields, files)

        url = _GREENHOUSE_SUBMIT.format(board=board, job_id=gh_job_id)
        req = urllib.request.Request(
            url, data=body, method='POST',
            headers={
                'Content-Type': f'multipart/form-data; boundary={boundary}',
                'User-Agent': 'ZamalOperator/1.0 (automated job application; owner-approved)',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.getcode()
                raw = resp.read(8192)
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read(8192)

        resp_hash = hashlib.sha256(raw).hexdigest()
        success = status in (200, 201)
        parsed = {}
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            pass
        return status, resp_hash, success, parsed


# ── Audit helper ──────────────────────────────────────────────────────────────

class JobAppAudit:
    def __init__(self, store):
        self._s = store

    def record(self, application_id, operation, detail=None):
        self._s.db.execute(
            'INSERT INTO job_app_audit(timestamp,application_id,operation,detail) VALUES(?,?,?,?)',
            (time.time(), application_id, operation, encode(detail or {})),
        )


# ── Main worker ───────────────────────────────────────────────────────────────

class JobApplicationWorker:
    """Job application worker. Extends BusinessWorker via composition.

    SAFE: job_app_open, job_app_plan, job_app_preflight, job_app_status
    PROTECTED: job_app_submit (Telegram APPROVE required)
    """

    def __init__(self, store, *, operator=None, cpc=None, web=None):
        from .business_ops import BusinessWorker
        self.store = store
        self._biz = BusinessWorker(store, operator=operator, cpc=cpc, web=web)
        self.paths = self._biz.paths
        op = Path(operator or Path(__file__).parent.parent)
        self._outbox = op / 'outbox'
        self._config = op / 'config'
        self._browser = JobBrowserPolicy()
        self._gh = GreenhouseSubmitter()

    def gate(self):
        parent = self._biz.gate()
        handlers = {**parent._handlers, **{k: JobAppCapability(self, k) for k in JOB_APP_KINDS}}
        return Gate(self.store, handlers)

    def validate(self, kind, p, project):
        if kind in JOB_APP_KINDS:
            validate_job_app(kind, p, project)
        else:
            self._biz.validate(kind, p, project)

    def run(self, kind, p, baseline, task_id, action_id):
        Controls(self.store).check(kind, baseline)
        validate_job_app(kind, p, self.store.task(task_id)['project'])
        if kind == 'job_app_fixture_prepare':
            return self._fixture_prepare(baseline, task_id)
        if kind == 'job_app_open':
            return self._open(p)
        if kind == 'job_app_plan':
            return self._plan(p, task_id, action_id)
        if kind == 'job_app_preflight':
            return self._preflight(p)
        if kind == 'job_app_status':
            return self._status(p)
        if kind == 'job_app_submit':
            return self._submit(p, task_id, action_id, baseline)
        raise Denied(f'Unknown job application capability: {kind}')

    # ── SAFE handlers ─────────────────────────────────────────────────────────

    def _fixture_prepare(self, baseline, task_id):
        from .job_browser_fixture import run
        result=run(self._outbox.parent,'prepare',lambda:Controls(self.store).check('job_app_fixture_prepare',baseline))
        app_id=uid();now=time.time()
        with self.store.transaction():
            self.store.db.execute('''INSERT INTO job_applications
                (id,task_id,board_type,job_id,job_title,company,job_url,status,evidence,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (app_id,task_id,'local_fixture','local-browser-v1','Synthetic form','Local fixture',
                 'local-fixture:job-browser-v1','ready',encode(result),now,now))
            JobAppAudit(self.store).record(app_id,'browser_prepared',result)
        return {'ok':True,'outcome':'APPROVAL_REQUIRED','application_id':app_id,
                'synthetic_only':True,'protected_actions':0,'evidence':result}

    def _fixture_submit(self, app, baseline, task_id, action_id):
        from .job_browser_fixture import run
        original=digest(app)
        def check():
            Controls(self.store).check('job_app_submit',baseline)
            a=self.store.action(action_id)
            if (a['task_id']!=task_id or a['status']!='EXECUTING' or not a['approval']
                    or a['approval']['status']!='CONSUMED' or digest(self._get_app(app['id']))!=original):
                raise Denied('Browser final action lost approval/application binding')
        result=run(self._outbox.parent,'submit',check,json.loads(app['evidence']))
        check()
        self._update_app(app['id'],'submitted',{'submit_action_id':action_id,'response_hash':digest(result),'evidence':encode(result)})
        with self.store.transaction():
            JobAppAudit(self.store).record(app['id'],'browser_submit_verified',{'action_id':action_id,**result})
        return {'ok':True,'outcome':'SUBMITTED','synthetic_only':True,'application_id':app['id'],'evidence':result}

    def _open(self, p):
        result = self._browser.open(p['url'])
        return {
            'ok': result['opened'],
            'outcome': 'OPENED' if result['opened'] else 'OPEN_FAILED',
            'url': p['url'],
            'board_type': p['board_type'],
            'exit_code': result['exit_code'],
            'note': 'URL opened in system browser for human review; no automated interaction performed',
        }

    def _plan(self, p, task_id, action_id):
        src = self.store.action(p['source_action_id'])
        if src['kind'] != 'biz_jobs' or src['status'] != 'SUCCEEDED' or not src['result']:
            raise Denied('source_action_id must reference a succeeded biz_jobs action')
        job = next((j for j in src['result'].get('jobs', []) if j['id'] == p['job_id']), None)
        if not job:
            raise Denied(f'Job {p["job_id"]!r} not found in source biz_jobs result')

        candidate = p['candidate']
        matched = [s for s in candidate['skills'] if s.lower() in job.get('requirements_excerpt', '').lower()]
        gaps = ['Confirm every requirement, qualification, date and eligibility before submission']
        gaps += [f'Skill not mentioned in job description: {s}' for s in candidate['skills'] if s not in matched]

        app_id = uid()
        now = time.time()
        with self.store.transaction():
            self.store.db.execute(
                '''INSERT INTO job_applications
                   (id,task_id,board_type,job_id,job_title,company,job_url,
                    cv_file_ref,cover_letter_file_ref,status,created,updated)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (app_id, task_id, job.get('source', 'unknown'), job['id'],
                 job['title'], job['company'], job['url'],
                 p['cv_file_ref'], p.get('cover_letter_file_ref', ''),
                 'planned', now, now),
            )
            JobAppAudit(self.store).record(app_id, 'plan', {'action_id': action_id})

        return {
            'ok': True,
            'outcome': 'PLANNED',
            'application_id': app_id,
            'job_title': job['title'],
            'company': job['company'],
            'board_type': job.get('source', 'unknown'),
            'job_url': job['url'],
            'candidate_name': candidate['name'],
            'skills_matched': matched,
            'gaps': gaps,
            'cv_file_ref': p['cv_file_ref'],
            'cover_letter_file_ref': p.get('cover_letter_file_ref', ''),
            'next_action': (
                'Review gaps, then propose job_app_preflight to validate files; '
                'then propose job_app_submit with acknowledge_gaps=true after owner confirms eligibility'
            ),
        }

    def _preflight(self, p):
        app = self._get_app(p['application_id'])
        issues = []
        warnings = []

        # Check board support
        if app['board_type'] not in SUPPORTED_SUBMIT_BOARDS:
            warnings.append(f'board_type={app["board_type"]!r} is not supported for automated submit; will be BLOCKED at submit time')

        # Check candidate.json exists
        candidate_path = self._config / 'candidate.json'
        if not candidate_path.is_file():
            issues.append('config/candidate.json not found; required for contact info at submit time')
        else:
            try:
                c = json.loads(candidate_path.read_text())
                for field in ('first_name', 'last_name', 'email'):
                    if not c.get(field, '').strip():
                        issues.append(f'config/candidate.json missing required field: {field}')
            except (ValueError, OSError):
                issues.append('config/candidate.json is not valid JSON')

        # Check CV file
        if app['cv_file_ref']:
            cv = self._resolve_outbox(app['cv_file_ref'])
            if not cv.is_file():
                issues.append(f'CV file not found in outbox: {app["cv_file_ref"]}')
            elif cv.stat().st_size > _MAX_FILE_BYTES:
                issues.append(f'CV file exceeds 5 MB: {cv.stat().st_size} bytes')
        else:
            issues.append('No cv_file_ref in application plan')

        # Check cover letter (optional)
        if app['cover_letter_file_ref']:
            cl = self._resolve_outbox(app['cover_letter_file_ref'])
            if not cl.is_file():
                warnings.append(f'Cover letter not found in outbox: {app["cover_letter_file_ref"]}')

        ready = not issues
        if ready:
            self._update_app(app['id'], 'ready', {})
            with self.store.transaction():
                JobAppAudit(self.store).record(app['id'], 'preflight', {'ready': True, 'warnings': warnings})

        return {
            'ok': ready,
            'outcome': 'READY' if ready else 'NOT_READY',
            'application_id': app['id'],
            'job_title': app['job_title'],
            'company': app['company'],
            'board_type': app['board_type'],
            'issues': issues,
            'warnings': warnings,
            'next_action': (
                'Propose job_app_submit with acknowledge_gaps=true' if ready
                else 'Resolve issues before submission'
            ),
        }

    def _status(self, p):
        app = self._get_app(p['application_id'])
        return {
            'ok': True,
            'outcome': 'SUCCESS',
            'application': dict(app),
        }

    # ── PROTECTED handler ─────────────────────────────────────────────────────

    def _submit(self, p, task_id, action_id, baseline=None):
        app = self._get_app(p['application_id'])
        if app['status'] not in ('planned', 'ready'):
            raise Conflict(f'Application is already {app["status"]}; cannot re-submit')
        if app['board_type']=='local_fixture':
            return self._fixture_submit(app,baseline,task_id,action_id)

        # Fail safely for unsupported boards
        if app['board_type'] not in SUPPORTED_SUBMIT_BOARDS:
            blocker = f'Automated submit not supported for board_type={app["board_type"]!r}; only {sorted(SUPPORTED_SUBMIT_BOARDS)}'
            self._update_app(app['id'], 'blocked', {'blocker': blocker, 'submit_action_id': action_id})
            with self.store.transaction():
                JobAppAudit(self.store).record(app['id'], 'submit_blocked', {'reason': blocker, 'action_id': action_id})
            return failure('BLOCKED', blocker, application_id=app['id'], board_type=app['board_type'])

        # Parse Greenhouse board slug and job ID from URL (fail-closed, structured)
        job_url = app.get('job_url') or ''
        try:
            board, gh_job_id = parse_greenhouse_url(job_url)
        except GreenhouseURLError as exc:
            blocker = str(exc)
            self._update_app(app['id'], 'blocked', {'blocker': blocker, 'submit_action_id': action_id})
            with self.store.transaction():
                JobAppAudit(self.store).record(app['id'], 'submit_blocked', {'reason': blocker})
            return failure('BLOCKED', blocker, application_id=app['id'])

        # Read candidate contact info (not from payload — never logged)
        candidate_path = self._config / 'candidate.json'
        if not candidate_path.is_file():
            raise Denied('config/candidate.json not found; candidate contact info required')
        try:
            contact = json.loads(candidate_path.read_text())
        except (ValueError, OSError) as e:
            raise Denied(f'config/candidate.json unreadable: {type(e).__name__}')

        required_contact = ('first_name', 'last_name', 'email')
        missing = [f for f in required_contact if not contact.get(f, '').strip()]
        if missing:
            raise Denied(f'config/candidate.json missing required fields: {missing}')

        # Resolve and verify CV path (outbox only)
        cv_path = self._resolve_outbox(app['cv_file_ref'])
        cl_path = self._resolve_outbox(app['cover_letter_file_ref']) if app['cover_letter_file_ref'] else None

        # HTTP submission
        candidate_fields = {
            'first_name': contact['first_name'].strip(),
            'last_name': contact['last_name'].strip(),
            'email': contact['email'].strip(),
        }
        if contact.get('phone', '').strip():
            candidate_fields['phone'] = contact['phone'].strip()

        try:
            http_status, resp_hash, success, parsed = self._gh.submit(
                board, gh_job_id, candidate_fields, cv_path, cl_path
            )
        except Denied:
            raise
        except Exception as e:
            blocker = f'HTTP submission failed: {type(e).__name__}'
            self._update_app(app['id'], 'failed', {
                'blocker': blocker, 'submit_action_id': action_id,
                'http_status': None, 'response_hash': '',
            })
            with self.store.transaction():
                JobAppAudit(self.store).record(app['id'], 'submit_error', {'error_type': type(e).__name__, 'action_id': action_id})
            raise Denied(blocker)

        outcome_status = 'submitted' if success else 'failed'
        evidence = {
            'action_id': action_id,
            'http_status': http_status,
            'response_hash': resp_hash,
            'board': board,
            'gh_job_id': gh_job_id,
            'cv_file_ref': app['cv_file_ref'],
            'cover_letter_file_ref': app['cover_letter_file_ref'],
            'submitted_at': time.time(),
            'external_app_id': parsed.get('id') if success and isinstance(parsed, dict) else None,
        }
        self._update_app(app['id'], outcome_status, {
            'submit_action_id': action_id,
            'http_status': http_status,
            'response_hash': resp_hash,
            'evidence': encode(evidence),
        })
        with self.store.transaction():
            JobAppAudit(self.store).record(app['id'], 'submit_' + outcome_status, {
                'action_id': action_id,
                'http_status': http_status,
                'response_hash': resp_hash,
            })

        return {
            'ok': success,
            'outcome': 'SUBMITTED' if success else 'SUBMISSION_FAILED',
            'application_id': app['id'],
            'job_title': app['job_title'],
            'company': app['company'],
            'http_status': http_status,
            'response_hash': resp_hash,
            'external_app_id': evidence.get('external_app_id'),
            'evidence_logged': True,
            'note': (
                'Application submitted to Greenhouse API; verify confirmation email'
                if success else
                f'Submission failed with HTTP {http_status}; response hash recorded; no retry'
            ),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_app(self, application_id):
        row = self.store.db.execute(
            'SELECT * FROM job_applications WHERE id=?', (application_id,)
        ).fetchone()
        if row is None:
            raise Conflict(f'Job application {application_id!r} not found')
        return dict(row)

    def _resolve_outbox(self, file_ref):
        """Resolve file_ref within outbox; enforce containment."""
        if not file_ref:
            raise Denied('Empty file reference')
        candidate = (self._outbox / file_ref).resolve()
        try:
            candidate.relative_to(self._outbox.resolve())
        except ValueError:
            raise Denied('File reference escapes the outbox boundary')
        if candidate.is_symlink():
            raise Denied('File must not be a symlink')
        return candidate

    def _update_app(self, app_id, status, fields):
        sets = ['status=?', 'updated=?']
        vals = [status, time.time()]
        for col in ('submit_action_id', 'http_status', 'response_hash', 'blocker', 'evidence'):
            if col in fields:
                sets.append(f'{col}=?')
                vals.append(fields[col])
        vals.append(app_id)
        with self.store.transaction():
            self.store.db.execute(
                f'UPDATE job_applications SET {", ".join(sets)} WHERE id=?', vals
            )


# ── Capability wrapper ─────────────────────────────────────────────────────────

class JobAppCapability(Capability):
    """All job_app kinds go through run_with_action for canonical task/action binding."""

    def run_with_controls(self, *args):
        raise Denied('Canonical action context required for job application operations')

    def run_with_action(self, p, baseline, action_id, task_id):
        action = self.worker.store.action(action_id)
        if (action['status'] != 'EXECUTING'
                or action['task_id'] != task_id
                or action['kind'] != self.kind
                or action['payload'] != p):
            raise Denied('Canonical execution claim required')
        Controls(self.worker.store).check(self.kind, baseline)
        return self.worker.run(self.kind, p, baseline, task_id, action_id)
