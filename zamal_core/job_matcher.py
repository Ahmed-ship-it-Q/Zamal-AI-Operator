"""Step 4 — Explainable job matching engine.

Scoring policy:
  - Minimum accepted score: 90 (SCORE_MIN).  Maximum: 99 (SCORE_MAX).
  - Score 0 means pre-score rejection (a filter failed before scoring).
  - No score component inflates results beyond earned value.
  - All components are individually capped and named for auditability.

Score components (max total = 99):
  location_match   0 | 25   Qatar or Doha → 25; otherwise 0
  role_match       0 | 25   Accounting/finance title → 25; otherwise 0
  keyword_match    0–25     Skills-in-requirements overlap (capped at 25)
  recency          0–15     ≤7 days = 15, ≤14 = 10, ≤30 = 5, older or unknown = 0
  requirements_fit 0–9      Profile qualifications found in requirements

Pre-score rejection rules (in priority order):
  1. duplicate_job_id     — job_id already in processed set
  2. duplicate_fingerprint — same role+company+location already seen
  3. non_qatar            — location is not Qatar or Doha
  4. non_finance          — title has no accounting/finance keyword
  5. expired              — posted_date more than 60 days ago
"""
import datetime
from dataclasses import dataclass
from typing import Optional

from .job_schema import NormalizedJob, MatchResult

# ── Filter configuration ───────────────────────────────────────────────────────

QATAR_LOCATIONS = frozenset({
    'qatar', 'doha', 'qa', 'state of qatar',
    'doha qatar', 'doha, qatar', 'qatar, middle east',
})

FINANCE_ROLE_KEYWORDS = frozenset({
    'accountant', 'accounting', 'finance', 'financial', 'audit', 'auditor',
    'controller', 'cfo', 'chief financial', 'bookkeeper', 'bookkeeping',
    'tax', 'treasury', 'payroll', 'cost accountant', 'management accountant',
    'ifrs', 'gaap', 'vat', 'budget', 'budgeting', 'forecasting',
    'accounts payable', 'accounts receivable', 'reconciliation',
    'financial reporting', 'financial analyst', 'chief accountant',
    'senior accountant', 'junior accountant', 'finance manager',
    'finance director', 'head of finance', 'internal audit',
    'external audit', 'statutory audit', 'compliance',
})

# (max_age_days, points)
_RECENCY_TIERS = [(7, 15), (14, 10), (30, 5)]

SCORE_MIN = 90
SCORE_MAX = 99
MAX_LISTING_AGE_DAYS = 60


@dataclass
class _RejectionRule:
    name: str
    reason: str


# ── Matcher ────────────────────────────────────────────────────────────────────

class JobMatcher:
    """Configurable, explainable matcher.  One instance per batch run."""

    def __init__(
        self,
        profile=None,
        seen_fingerprints: Optional[set] = None,
        processed_job_ids: Optional[set] = None,
    ):
        self._profile = profile
        self._seen_fingerprints: set = set(seen_fingerprints or [])
        self._processed_job_ids: set = set(processed_job_ids or [])

    # ── Public API ───────────────────────────────────────────────────────────

    def evaluate(self, job: NormalizedJob) -> MatchResult:
        """Run rejection rules then score.  Returns MatchResult."""
        rule = self._apply_rejection_rules(job)
        if rule:
            return MatchResult(
                job_id=job.job_id,
                score=0,
                accepted=False,
                rejection_reason=rule.reason,
                score_breakdown={'rejected_before_scoring': rule.name},
                matched_keywords=[],
            )
        breakdown, matched_kw = self._compute_score(job)
        total = min(sum(breakdown.values()), SCORE_MAX)
        accepted = SCORE_MIN <= total <= SCORE_MAX
        return MatchResult(
            job_id=job.job_id,
            score=total,
            accepted=accepted,
            rejection_reason=('' if accepted
                              else f'score_{total}_below_threshold_{SCORE_MIN}'),
            score_breakdown=breakdown,
            matched_keywords=matched_kw,
        )

    def mark_processed(self, job: NormalizedJob) -> None:
        """Record job as processed to prevent re-queuing."""
        self._seen_fingerprints.add(job.fingerprint)
        self._processed_job_ids.add(job.job_id)

    def is_duplicate(self, job: NormalizedJob) -> bool:
        return (job.job_id in self._processed_job_ids
                or job.fingerprint in self._seen_fingerprints)

    # ── Rejection rules ──────────────────────────────────────────────────────

    def _apply_rejection_rules(self, job: NormalizedJob) -> Optional[_RejectionRule]:
        if job.job_id in self._processed_job_ids:
            return _RejectionRule('duplicate_job_id', 'already_processed')
        if job.fingerprint in self._seen_fingerprints:
            return _RejectionRule('duplicate_fingerprint',
                                  'duplicate_listing_same_role_company_location')
        if not is_qatar_location(job.location):
            return _RejectionRule('non_qatar', f'location_not_qatar:{job.location!r}')
        if not is_finance_role(job.title):
            return _RejectionRule('non_finance', f'role_not_finance:{job.title!r}')
        if is_expired(job):
            return _RejectionRule('expired', 'posted_more_than_60_days_ago')
        return None

    # ── Scoring ──────────────────────────────────────────────────────────────

    def _compute_score(self, job: NormalizedJob) -> tuple:
        profile_skills: set = set()
        profile_quals: frozenset = frozenset()
        if self._profile:
            profile_skills = {s.lower() for s in getattr(self._profile, 'skills', [])}
            profile_quals = frozenset(
                q.lower() for q in getattr(self._profile, 'qualifications', [])
            )

        req_text = ' '.join(job.requirements).lower()

        # Location: 0 | 25
        loc_pts = 25 if is_qatar_location(job.location) else 0

        # Role: 0 | 25
        role_pts = 25 if is_finance_role(job.title) else 0

        # Keyword overlap: 0–25
        # Score based on absolute number of skill matches (5+ = full credit),
        # not fraction of profile size — a large profile shouldn't penalise fit.
        if profile_skills:
            matched_kw = [s for s in profile_skills if s in req_text]
            kw_pts = min(len(matched_kw) * 5, 25)
        else:
            # No profile configured → no penalty; fixture/unprofiled jobs score neutral
            matched_kw = []
            kw_pts = 25

        # Recency: 0–15
        rec_pts = _recency_pts(job)

        # Requirements fit: 0–9
        if profile_quals:
            hits = sum(1 for q in profile_quals if q in req_text)
            req_pts = min(hits * 3, 9)
        else:
            req_pts = 9  # no profile → no penalty

        breakdown = {
            'location_match': loc_pts,
            'role_match': role_pts,
            'keyword_match': kw_pts,
            'recency': rec_pts,
            'requirements_fit': req_pts,
        }
        return breakdown, matched_kw


# ── Standalone filter helpers (importable) ────────────────────────────────────

def is_qatar_location(location: str) -> bool:
    loc = location.lower().strip()
    return any(q in loc for q in QATAR_LOCATIONS)


def is_finance_role(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in FINANCE_ROLE_KEYWORDS)


def is_expired(job: NormalizedJob) -> bool:
    if not job.posted_date:
        return False
    try:
        pd = datetime.date.fromisoformat(job.posted_date)
        return (datetime.date.today() - pd).days > MAX_LISTING_AGE_DAYS
    except Exception:
        return False


def _recency_pts(job: NormalizedJob) -> int:
    if not job.posted_date:
        return 5
    try:
        pd = datetime.date.fromisoformat(job.posted_date)
        age = (datetime.date.today() - pd).days
        for max_days, pts in _RECENCY_TIERS:
            if age <= max_days:
                return pts
        return 0
    except Exception:
        return 5
