"""Step 4 — Job listing lifecycle, store table, Telegram notifications, executor interface.

Job lifecycle states (separate from task states):
  DISCOVERED → NORMALIZED → SCORED → SHORTLISTED → WAITING_APPROVAL
  → APPROVED / REJECTED → COMPLETED / FAILED

This module:
  - migrate_job_lifecycle(): creates the job_listings table.
  - JobListingStore: CRUD + state transitions for job listings.
  - JobTelegramNotifier: APPROVAL_REQUIRED / ACTION_REQUIRED (sanitized metadata only).
  - JobApplicationExecutor: interface only — disabled until live phase.
"""
import json
import time
import uuid
from typing import Optional

from .job_schema import NormalizedJob, MatchResult, JOB_STATES

# Action kinds exposed to Policy.requires_approval()
JOB_LIFECYCLE_SAFE = frozenset({
    'job_source_status',    # read browser-tab metadata
    'job_shortlist_view',   # read current shortlist
    'job_score_explain',    # read score breakdown for a listing
    'job_search_discover',  # discover public listings (disabled in foundation)
})

JOB_LIFECYCLE_PROTECTED = frozenset({
    'job_search_activate',  # enable live search for a source
    'job_submit_application',  # submit application (requires approval)
})

JOB_LIFECYCLE_KINDS = JOB_LIFECYCLE_SAFE | JOB_LIFECYCLE_PROTECTED


# ── Schema migration ───────────────────────────────────────────────────────────

def migrate_job_lifecycle(store) -> None:
    """Idempotent — creates job_listings table if not present."""
    store.db.executescript('''
        CREATE TABLE IF NOT EXISTS job_listings (
          id TEXT PRIMARY KEY,
          source TEXT NOT NULL,
          job_id TEXT NOT NULL,
          fingerprint TEXT NOT NULL,
          url TEXT NOT NULL,
          title TEXT NOT NULL,
          company TEXT NOT NULL,
          location TEXT NOT NULL,
          posted_date TEXT,
          requirements TEXT NOT NULL DEFAULT '[]',
          application_method TEXT NOT NULL DEFAULT 'unknown',
          state TEXT NOT NULL DEFAULT 'DISCOVERED',
          score INTEGER,
          score_breakdown TEXT,
          rejection_reason TEXT,
          matched_keywords TEXT NOT NULL DEFAULT '[]',
          task_id TEXT,
          approval_id TEXT,
          discovered_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          UNIQUE(source, job_id)
        );
        CREATE INDEX IF NOT EXISTS job_listings_state ON job_listings(state);
        CREATE INDEX IF NOT EXISTS job_listings_source ON job_listings(source);
        CREATE TRIGGER IF NOT EXISTS job_listings_updated
          AFTER UPDATE ON job_listings
          BEGIN
            UPDATE job_listings SET updated_at=strftime('%s','now') WHERE id=NEW.id;
          END;
    ''')


# ── Store layer ────────────────────────────────────────────────────────────────

class JobListingStore:
    """CRUD and state machine for job_listings table."""

    def __init__(self, store):
        self._db = store.db

    def upsert(self, job: NormalizedJob) -> str:
        """Insert or ignore (dedup on source+job_id).  Returns listing id."""
        lid = str(uuid.uuid4())
        now = time.time()
        self._db.execute(
            '''INSERT OR IGNORE INTO job_listings
               (id, source, job_id, fingerprint, url, title, company, location,
                posted_date, requirements, application_method, state,
                discovered_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (lid, job.source, job.job_id, job.fingerprint, job.url,
             job.title, job.company, job.location, job.posted_date,
             json.dumps(job.requirements[:20]), job.application_method,
             'DISCOVERED', job.discovered_at, now),
        )
        # Fetch actual id (may differ if row already existed)
        row = self._db.execute(
            'SELECT id FROM job_listings WHERE source=? AND job_id=?',
            (job.source, job.job_id),
        ).fetchone()
        return row['id'] if row else lid

    def apply_match(self, listing_id: str, result: MatchResult) -> None:
        """Store score and advance state to SCORED or REJECTED."""
        next_state = 'SCORED' if result.accepted else 'REJECTED'
        self._db.execute(
            '''UPDATE job_listings
               SET score=?, score_breakdown=?, rejection_reason=?,
                   matched_keywords=?, state=?
               WHERE id=?''',
            (result.score, json.dumps(result.score_breakdown),
             result.rejection_reason or '',
             json.dumps(result.matched_keywords),
             next_state, listing_id),
        )

    def advance(self, listing_id: str, new_state: str) -> None:
        """Move listing to new_state, enforcing JOB_STATES transitions."""
        row = self._db.execute(
            'SELECT state FROM job_listings WHERE id=?', (listing_id,)
        ).fetchone()
        if not row:
            raise KeyError(f'job listing {listing_id} not found')
        current = row['state']
        allowed = JOB_STATES.get(current, set())
        if new_state not in allowed:
            raise ValueError(f'Invalid job listing transition {current} → {new_state}')
        self._db.execute(
            'UPDATE job_listings SET state=? WHERE id=?', (new_state, listing_id)
        )

    def get(self, listing_id: str) -> Optional[dict]:
        row = self._db.execute(
            'SELECT * FROM job_listings WHERE id=?', (listing_id,)
        ).fetchone()
        return dict(row) if row else None

    def shortlist(self) -> list:
        """Return listings in SHORTLISTED or WAITING_APPROVAL state."""
        rows = self._db.execute(
            "SELECT * FROM job_listings WHERE state IN ('SHORTLISTED','WAITING_APPROVAL') "
            'ORDER BY score DESC, discovered_at DESC'
        ).fetchall()
        return [dict(r) for r in rows]

    def history(self, limit: int = 50) -> list:
        """Return completed/approved/rejected listings."""
        rows = self._db.execute(
            "SELECT * FROM job_listings WHERE state IN "
            "('APPROVED','REJECTED','COMPLETED','FAILED') "
            'ORDER BY updated_at DESC LIMIT ?', (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def fingerprints(self) -> set:
        """All seen fingerprints (for dedup)."""
        rows = self._db.execute('SELECT fingerprint FROM job_listings').fetchall()
        return {r['fingerprint'] for r in rows}

    def processed_job_ids(self) -> set:
        """All job_ids that have been scored (prevent re-processing)."""
        rows = self._db.execute(
            "SELECT job_id FROM job_listings WHERE state NOT IN ('DISCOVERED','FAILED')"
        ).fetchall()
        return {r['job_id'] for r in rows}


# ── Telegram notifier ──────────────────────────────────────────────────────────

class JobTelegramNotifier:
    """Send sanitized job-approval requests and action alerts via Telegram.

    Only metadata (title, company, location, score, URL) is sent — never CV
    content, private messages, or any personal data.
    """

    def send_approval_required(self, listing: dict, task_id: str) -> bool:
        """Send APPROVAL_REQUIRED notification for a shortlisted job."""
        try:
            from .control_center import _load_auth_env
            _load_auth_env()
            from .telegram_bot import TelegramConfig, TelegramAPI
            tc = TelegramConfig()
            if not tc.configured:
                return False
            api = TelegramAPI(tc)
            msg = (
                '\U0001f4cb APPROVAL_REQUIRED — Job Application\n'
                f"Title:    {listing.get('title','?')}\n"
                f"Company:  {listing.get('company','?')}\n"
                f"Location: {listing.get('location','?')}\n"
                f"Source:   {listing.get('source','?')}\n"
                f"Score:    {listing.get('score','?')}/99\n"
                f"URL:      {listing.get('url','?')}\n"
                f"Task:     {task_id[:8]}\n\n"
                'Reply APPROVE or REJECT via Control Center.'
            )
            for cid in tc._allowed:
                api.send_message(cid, msg)
            return True
        except Exception:
            return False

    def send_action_required(self, title: str, detail: str) -> bool:
        """Send ACTION_REQUIRED alert with sanitized detail."""
        try:
            from .control_center import _load_auth_env
            _load_auth_env()
            from .telegram_bot import TelegramConfig, TelegramAPI
            tc = TelegramConfig()
            if not tc.configured:
                return False
            api = TelegramAPI(tc)
            msg = f'\u26a0\ufe0f ACTION_REQUIRED\n{title}\n{detail[:200]}'
            for cid in tc._allowed:
                api.send_message(cid, msg)
            return True
        except Exception:
            return False

    def send_session_checkpoint(self, source: str, task_id: str) -> bool:
        """Alert that a browser session is required to continue."""
        return self.send_action_required(
            f'Browser session required: {source}',
            f'task={task_id[:8]} — Log in to {source} then resume from Control Center.',
        )


# ── Application executor interface ────────────────────────────────────────────

class JobApplicationExecutor:
    """Executes approved job applications.

    DISABLED in foundation phase.  Raises NotImplementedError on any execute call.
    Enable in Step 4 live phase after explicit operator authorization.
    """

    _LIVE_ENABLED: bool = False

    @classmethod
    def enable_live(cls) -> None:
        """Enable live application execution.  Requires explicit operator call."""
        cls._LIVE_ENABLED = True

    def execute(self, listing: dict, task_id: str, approval_id: str) -> dict:
        """Submit an approved application.  Disabled in foundation phase."""
        if not self._LIVE_ENABLED:
            raise NotImplementedError(
                'JobApplicationExecutor is disabled in the foundation phase. '
                'Enable via explicit operator command in Step 4 live phase.'
            )
        # Live implementation goes here.
        raise NotImplementedError('Live execution not yet implemented.')

    def can_execute(self) -> bool:
        return self._LIVE_ENABLED
