"""Step 4 — Explainable CV-match scoring engine (v2).

Score = sum of five CV-match components (max 100):
  skills_responsibilities   0–35   Skill keyword overlap vs job description
  experience_years          0–25   Years-of-experience match
  role_seniority            0–20   Title/level alignment with profile seniority
  education_certification   0–15   Degree and professional certification match
  software_industry          0–5   Specific tools, ERP, language match

Location is a mandatory pre-filter only — never a score component.
Recency is stored in recency_rank and used for tie-breaking only — never
added to the match score.

Acceptance band: 90 ≤ score ≤ 100.

Mandatory caps (score forced below 90):
  Director / VP / CFO / executive title  → role_seniority = 0-2  → max ≤ 82
  Assistant-director title               → role_seniority = 5    → max ≤ 85
  Required professional cert absent from
    profile (e.g. CPA required, profile
    has B.Com only)                      → education = 0         → max ≤ 85

Pre-score rejection rules (unchanged):
  1. duplicate_job_id
  2. duplicate_fingerprint
  3. non_qatar            — location not Qatar/Doha
  4. non_finance          — title lacks accounting/finance keyword
  5. expired              — posted_date > 60 days ago
"""
import datetime
import re
from dataclasses import dataclass
from typing import Optional

from .job_schema import NormalizedJob, MatchResult

# ── Public constants ───────────────────────────────────────────────────────────

SCORE_COMPONENTS: dict = {
    'skills_responsibilities': 35,
    'experience_years': 25,
    'role_seniority': 20,
    'education_certification': 15,
    'software_industry': 5,
}

SCORE_MIN = 90
SCORE_MAX = 100
MAX_LISTING_AGE_DAYS = 60

# ── Pre-filter configuration ───────────────────────────────────────────────────

QATAR_LOCATIONS = frozenset({
    'qatar', 'doha', 'qa', 'state of qatar',
    'doha qatar', 'doha, qatar', 'qatar, middle east',
    'lusail', 'al khor', 'al wakrah', 'dukhan', 'mesaieed',
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

# Recency tiers used for ranking only — (max_age_days, rank_points)
_RECENCY_TIERS = [(7, 15), (14, 10), (30, 5)]

# Professional certifications to detect in job descriptions
_CERT_PATTERNS = [
    ('acca', r'\bacca\b'),
    ('cpa',  r'\bcpa\b'),
    ('cma',  r'\bcma\b'),
    ('cfa',  r'\bcfa\b'),
    ('ca',   r'\bca\b'),
    ('aca',  r'\baca\b'),
    ('icaew', r'\bicaew\b'),
    ('chartered accountant', r'chartered\s+accountant'),
]

# Requirement-strength markers
_REQUIRED_WORDS = frozenset({
    'required', 'must', 'mandatory', 'essential', 'must have',
    'must hold', 'must possess', 'is required',
})
_PREFERRED_WORDS = frozenset({
    'preferred', 'preference', 'desirable', 'advantage', 'ideally',
    'plus', 'bonus', 'beneficial', 'would be an asset',
})

# Software / tool keywords that may appear in job descriptions
_SOFTWARE_KW = frozenset({'oracle', 'quickbooks', 'erp', 'sap', 'excel', 'pos', 'xero', 'sage'})


@dataclass
class _RejectionRule:
    name: str
    reason: str


# ── Matcher ────────────────────────────────────────────────────────────────────

class JobMatcher:
    """Configurable, explainable CV-match scorer. One instance per batch run."""

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
        """Run pre-filters then CV-match score. Returns MatchResult."""
        rule = self._apply_rejection_rules(job)
        if rule:
            return MatchResult(
                job_id=job.job_id,
                score=0,
                accepted=False,
                rejection_reason=rule.reason,
                score_breakdown={'rejected_before_scoring': rule.name},
                matched_keywords=[],
                recency_rank=0,
            )
        breakdown, matched_kw = self._compute_score(job)
        total = sum(breakdown.values())
        accepted = SCORE_MIN <= total <= SCORE_MAX
        return MatchResult(
            job_id=job.job_id,
            score=total,
            accepted=accepted,
            rejection_reason=('' if accepted
                              else f'score_{total}_below_threshold_{SCORE_MIN}'),
            score_breakdown=breakdown,
            matched_keywords=matched_kw,
            recency_rank=_recency_rank(job),
        )

    def mark_processed(self, job: NormalizedJob) -> None:
        self._seen_fingerprints.add(job.fingerprint)
        self._processed_job_ids.add(job.job_id)

    def is_duplicate(self, job: NormalizedJob) -> bool:
        return (job.job_id in self._processed_job_ids
                or job.fingerprint in self._seen_fingerprints)

    # ── Pre-filters ──────────────────────────────────────────────────────────

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

    # ── CV-match scoring ─────────────────────────────────────────────────────

    def _compute_score(self, job: NormalizedJob) -> tuple:
        desc = ' '.join(job.requirements).lower()
        p = self._profile

        skills_pts, matched_kw = _score_skills(desc, p)
        exp_pts, _exp_note   = _score_experience(desc, p)
        sen_pts, _sen_note   = _score_seniority(job.title)
        edu_pts, _edu_note   = _score_education(desc, p)
        sw_pts,  _sw_note    = _score_software(desc, p)

        breakdown = {
            'skills_responsibilities': skills_pts,
            'experience_years': exp_pts,
            'role_seniority': sen_pts,
            'education_certification': edu_pts,
            'software_industry': sw_pts,
        }
        return breakdown, matched_kw


# ── Scoring sub-functions ─────────────────────────────────────────────────────

def _score_skills(desc: str, profile) -> tuple:
    """(points 0-35, matched_skill_list).  7 pts per match, cap at 5 matches."""
    if not profile:
        return 20, []  # neutral when no profile configured

    skills = {s.lower() for s in getattr(profile, 'skills', [])}
    matched = sorted(s for s in skills if s in desc)
    pts = min(len(matched) * 7, 35)
    return pts, matched


def _score_experience(desc: str, profile) -> tuple:
    """(points 0-25, note).  Compares required years vs profile years."""
    profile_years = getattr(profile, 'years_experience', 0) if profile else 0

    # Try multiple phrasings in priority order
    patterns = [
        r'(\d+)\s*\+\s*years?\s*(?:of\s*)?(?:relevant\s*)?(?:work\s*)?(?:professional\s*)?experience',
        r'minimum\s*(?:of\s*)?(\d+)\s*years?\s*',
        r'at\s*least\s*(\d+)\s*years?\s*',
        r'(\d+)\s*years?\s*(?:of\s*)?(?:relevant|related|direct|proven|solid|strong)?\s*experience',
    ]
    required = None
    for pat in patterns:
        m = re.search(pat, desc)
        if m:
            yr = int(m.group(1))
            if 1 <= yr <= 25:
                required = yr
                break

    if required is None:
        # Broad catch-all: "N+? years ... experience" (up to 60 chars between)
        m = re.search(r'(\d+)\+?\s*years?\b.{0,60}experience\b', desc)
        if m:
            yr = int(m.group(1))
            if 1 <= yr <= 25:
                required = yr
    if required is None:
        return 20, 'years_not_specified'

    diff = profile_years - required
    if diff >= 3:
        return 25, f'{profile_years}yr_exceeds_{required}yr_req'
    if diff >= 0:
        return 22, f'{profile_years}yr_meets_{required}yr_req'
    if diff >= -1:
        return 15, f'{profile_years}yr_1yr_short_of_{required}yr_req'
    if diff >= -2:
        return 5,  f'{profile_years}yr_2yr_short_of_{required}yr_req'
    return 0, f'{profile_years}yr_insufficient_for_{required}yr_req'


def _score_seniority(title: str) -> tuple:
    """(points 0-20, tier_name).

    Seniority tiers (must be checked in order — specific before general):
      executive/CFO    0   → max total ≤ 80 → always rejected
      director         2   → max total ≤ 82 → always rejected
      assistant-dir    5   → max total ≤ 85 → always rejected
      senior-manager   8   → max total ≤ 88 → always rejected
      finance-manager 10   → max total ≤ 90 → borderline (requires near-perfect elsewhere)
      chief-accountant 18  → can reach 90+
      senior-IC        17  → can reach 90+
      mid-level-IC     20  → ideal level for 8-yr profile
      junior           8   → max total ≤ 88 → always rejected
      intern/trainee   3   → max total ≤ 83 → always rejected
      unclassified    15
    """
    t = title.lower()

    # Executive / C-suite (→ 0)
    if any(kw in t for kw in [
        'chief financial officer', 'cfo', 'vice president', 'vp finance',
        'vp of finance', 'group cfo', 'group chief financial',
    ]):
        return 0, 'executive'

    # Director-level (→ 2)
    if any(kw in t for kw in [
        'director of finance', 'finance director', 'financial director',
        'director of accounts', 'director of accounting',
        'head of finance', 'head of accounts', 'head of accounting',
    ]):
        return 2, 'director'

    # Assistant/Deputy Director (→ 5)
    if any(kw in t for kw in ['assistant director', 'deputy director', 'associate director']):
        return 5, 'assistant_director'

    # Bare "director" without qualifier (→ 2)
    if 'director' in t:
        return 2, 'director'

    # Senior manager / controller tier (→ 8)
    if any(kw in t for kw in [
        'senior finance manager', 'senior financial manager',
        'strategic finance', 'financial controller', 'finance controller',
        'group finance manager',
    ]):
        return 8, 'senior_manager'

    # Finance / Accounting Manager (→ 10)
    if any(kw in t for kw in [
        'finance manager', 'financial manager', 'accounting manager',
        'accounts manager', 'manager finance', 'manager accounting',
    ]):
        return 10, 'finance_manager'

    # Chief Accountant / Head Accountant — senior IC role, common in ME (→ 18)
    if any(kw in t for kw in ['chief accountant', 'head accountant', 'group accountant']):
        return 18, 'chief_accountant'

    # Senior IC specialist (→ 17)
    if 'senior' in t:
        return 17, 'senior_ic'

    # Lead / Principal / Complex (→ 17)
    if any(kw in t for kw in ['lead ', 'principal ', 'complex ']):
        return 17, 'senior_ic'

    # Mid-level IC — perfect match for 8-year accountant (→ 20)
    if any(kw in t for kw in [
        'accountant', 'financial analyst', 'finance specialist',
        'finance officer', 'finance executive', 'payroll',
        'fund accountant', 'tax specialist', 'accounts officer',
    ]):
        return 20, 'mid_level_ic'

    # Junior / Assistant-accountant (→ 8)
    if any(kw in t for kw in ['junior ', 'assistant accountant', 'accounts assistant',
                               'finance assistant', 'accounting assistant']):
        return 8, 'junior'

    # Intern / Trainee / Graduate (→ 3)
    if any(kw in t for kw in ['intern', 'trainee', 'graduate', 'entry level', 'entry-level']):
        return 3, 'intern_trainee'

    return 15, 'unclassified'


def _score_education(desc: str, profile) -> tuple:
    """(points 0-15, note).

    15: required cert present in profile, OR degree required and matched
    12: cert preferred but profile has it; OR degree present, no cert req
    10: cert preferred, profile lacks it; OR degree required/matched, no cert req
     0: required cert absent from profile  → hard cap (total max ≤ 85)
    """
    if not profile:
        return 12, 'no_profile'

    profile_quals = frozenset(q.lower() for q in getattr(profile, 'qualifications', []))

    # Detect professional certs mentioned and classify as required/preferred
    cert_hits = []
    for cert_name, pattern in _CERT_PATTERNS:
        for m in re.finditer(pattern, desc):
            idx = m.start()
            ctx = desc[max(0, idx - 150):idx + 100]
            is_req = any(w in ctx for w in _REQUIRED_WORDS)
            is_pref = any(w in ctx for w in _PREFERRED_WORDS)
            cert_hits.append({
                'name': cert_name.split()[0],  # short form
                'required': is_req,
                'preferred': is_pref or not is_req,
            })
            break  # one hit per cert type

    required_certs = [c for c in cert_hits if c['required']]
    preferred_certs = [c for c in cert_hits if not c['required']]

    # Does profile hold any professional cert?
    _PROF_CERT_KEYS = {'acca', 'cpa', 'cma', 'cfa', 'ca', 'aca', 'icaew', 'chartered'}
    profile_has_cert = any(
        any(ck in q for ck in _PROF_CERT_KEYS) for q in profile_quals
    )

    # Does profile satisfy a specific required cert?
    def _profile_satisfies(cert_name: str) -> bool:
        return any(cert_name in q or q in cert_name for q in profile_quals)

    # Degree requirement
    has_degree_req = any(kw in desc for kw in [
        'bachelor', 'degree', 'b.com', 'b.sc', 'bsc', 'bba', 'graduation',
        'graduate degree', 'university degree',
    ])
    profile_has_degree = any(kw in profile_quals for kw in [
        'bachelor', 'b.com', 'bachelor of commerce', 'b.com accounting',
    ])

    # --- Decision tree ---

    if required_certs:
        satisfied = [c for c in required_certs if _profile_satisfies(c['name'])]
        if satisfied:
            return 15, f'required_cert_present:{satisfied[0]["name"]}'
        # Required cert missing from profile → hard cap
        missing = ','.join(c['name'] for c in required_certs)
        return 0, f'missing_required_cert:{missing}'

    if preferred_certs:
        cert_names = ','.join(c['name'] for c in preferred_certs)
        if profile_has_cert:
            return 12, f'cert_preferred_profile_has_cert:{cert_names}'
        if has_degree_req and profile_has_degree:
            return 10, f'cert_preferred_degree_match_no_profile_cert:{cert_names}'
        return 8, f'cert_preferred_no_profile_cert:{cert_names}'

    # No cert mentioned
    if has_degree_req:
        if profile_has_degree:
            return 15, 'degree_required_and_matched'
        return 10, 'degree_required_not_matched'

    # No specific education requirement
    if profile_has_degree:
        return 12, 'no_edu_req_profile_has_degree'
    return 10, 'no_edu_req_no_degree'


def _score_software(desc: str, profile) -> tuple:
    """(points 0-5, note).  Tool and language matches from profile."""
    if not profile:
        return 2, 'no_profile'

    # Software tools from profile skills
    profile_skills_lower = {s.lower() for s in getattr(profile, 'skills', [])}
    matched_sw = sorted(_SOFTWARE_KW & profile_skills_lower & set(desc.split()))
    # Also check multi-word tool names
    for sw in list(_SOFTWARE_KW - set(matched_sw)):
        if sw in desc:
            matched_sw.append(sw)

    # Language match
    lang_match = any(
        lang.lower() in desc
        for lang in getattr(profile, 'languages', [])
    )

    hits = len(matched_sw) + (1 if lang_match else 0)
    detail = ','.join(matched_sw[:2]) or ('lang' if lang_match else '')
    if hits >= 2:
        return 5, f'tools:{detail}'
    if hits == 1:
        return 3, f'tools:{detail}'
    return 2, 'no_specific_tools_mentioned'


# ── Standalone filter helpers (importable by tests) ───────────────────────────

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


def _recency_rank(job: NormalizedJob) -> int:
    """Ranking/tie-breaker only — never added to match score."""
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
