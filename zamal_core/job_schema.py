"""Step 4 — Normalized public-job schema and related value types.

Only public data from job listings is captured here.
No private messages, authentication tokens, or personal data is stored.
"""
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional

# ── Job listing lifecycle states ───────────────────────────────────────────────

JOB_STATES: dict = {
    'DISCOVERED':       {'NORMALIZED', 'FAILED'},
    'NORMALIZED':       {'SCORED', 'REJECTED', 'FAILED'},
    'SCORED':           {'SHORTLISTED', 'REJECTED', 'FAILED'},
    'SHORTLISTED':      {'WAITING_APPROVAL', 'REJECTED'},
    'WAITING_APPROVAL': {'APPROVED', 'REJECTED'},
    'APPROVED':         {'COMPLETED', 'FAILED'},
    'REJECTED':         set(),
    'COMPLETED':        set(),
    'FAILED':           {'DISCOVERED'},
}

# ── Constants ──────────────────────────────────────────────────────────────────

APPLICATION_METHODS = frozenset({
    'direct', 'easy_apply', 'external', 'email', 'unknown',
})

JOB_SOURCES = frozenset({'linkedin', 'qatar_living'})


# ── Core schema ───────────────────────────────────────────────────────────────

@dataclass
class NormalizedJob:
    """Sanitized public job listing.  No private data or credentials stored."""
    job_id: str           # sha256(source:url)[:16]
    source: str           # 'linkedin' | 'qatar_living'
    url: str              # canonical public URL
    title: str
    company: str
    location: str
    posted_date: Optional[str]   # ISO 8601 date or None
    requirements: list           # list[str] from public listing, max 20
    application_method: str      # from APPLICATION_METHODS
    fingerprint: str             # sha256(title:company:location)[:24] — dedup key
    discovered_at: float         # unix timestamp
    raw_metadata: dict = field(default_factory=dict)  # sanitized extras

    @staticmethod
    def make_job_id(source: str, url: str) -> str:
        return hashlib.sha256(f'{source}:{url}'.encode()).hexdigest()[:16]

    @staticmethod
    def make_fingerprint(title: str, company: str, location: str) -> str:
        key = (f'{title.lower().strip()}:'
               f'{company.lower().strip()}:'
               f'{location.lower().strip()}')
        return hashlib.sha256(key.encode()).hexdigest()[:24]

    def to_dict(self) -> dict:
        return {
            'job_id': self.job_id,
            'source': self.source,
            'url': self.url,
            'title': self.title,
            'company': self.company,
            'location': self.location,
            'posted_date': self.posted_date,
            'requirements': self.requirements[:20],
            'application_method': self.application_method,
            'fingerprint': self.fingerprint,
            'discovered_at': self.discovered_at,
        }


@dataclass
class JobQuery:
    """Search parameters — passed to adapter; discover_jobs() is disabled in foundation phase."""
    keywords: list
    location: str = 'Qatar'
    max_results: int = 20
    posted_within_days: int = 30


@dataclass
class MatchResult:
    """Output from JobMatcher.evaluate() for one job listing."""
    job_id: str
    score: int            # 0 (rejected before scoring) or 90-100 (CV-match score)
    accepted: bool        # True iff 90 <= score <= 100
    rejection_reason: str # non-empty when accepted=False
    score_breakdown: dict # explainable per-component scores
    matched_keywords: list
    recency_rank: int = 0 # ranking/tie-breaker only — never part of score
