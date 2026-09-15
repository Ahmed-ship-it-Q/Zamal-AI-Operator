"""Step 4 — JobSourceAdapter interface + LinkedIn and Qatar Living implementations.

Foundation-phase constraints:
  - Session checks use narrow browser-tab metadata only (title/login indicator).
    Broad extraction (document.body.innerText, innerHTML, querySelectorAll("*"),
    accessibility trees) is never used.
  - discover_jobs() raises NotImplementedError — disabled until live-search phase.
  - No credentials, cookies, session tokens or private data are stored or logged.
  - No interaction with currently open tabs is initiated.
"""
import time
from abc import ABC, abstractmethod
from typing import Optional

from .job_schema import NormalizedJob, JobQuery


# ── Abstract interface ─────────────────────────────────────────────────────────

class JobSourceAdapter(ABC):
    """Every job source must implement this interface."""

    SOURCE_ID: str = ''

    @abstractmethod
    def session_status(self) -> dict:
        """Return browser-tab metadata only.  Never returns page content.

        Keys:
          source (str), session_present (bool), logged_in (bool), note (str)
        """

    @abstractmethod
    def is_session_active(self) -> bool:
        """True if a logged-in browser session is detectable for this source."""

    def discover_jobs(self, query: JobQuery) -> list:
        """Public job discovery — disabled in foundation phase."""
        raise NotImplementedError(
            f'{self.SOURCE_ID}: live job discovery is disabled in the foundation '
            'phase.  Enable via explicit operator authorization in Step 4 live phase.'
        )

    @abstractmethod
    def make_fixture_job(self, **overrides) -> NormalizedJob:
        """Return a synthetic NormalizedJob for testing.  No network calls."""


# ── LinkedIn implementation ────────────────────────────────────────────────────

class LinkedInAdapter(JobSourceAdapter):
    """LinkedIn Jobs — foundation phase, session metadata only."""

    SOURCE_ID = 'linkedin'
    _DOMAIN = 'linkedin.com'
    # Narrow login check: presence of global nav element only.
    # No page content, no notifications, no message counts.
    _LOGIN_JS = (
        'document.querySelector(".global-nav") !== null '
        '|| document.querySelector("nav[aria-label]") !== null'
    )

    def session_status(self) -> dict:
        try:
            from .ai_browser_transport import _exec_js_in_domain
        except ImportError:
            return self._unavailable('import_error')
        ok, result = _exec_js_in_domain(self._DOMAIN, self._LOGIN_JS, timeout=5)
        if not ok:
            return self._unavailable('no_chrome_tab')
        logged_in = result.strip().lower() == 'true'
        return {
            'source': self.SOURCE_ID,
            'session_present': True,
            'logged_in': logged_in,
            'note': 'session_active' if logged_in else 'session_required',
        }

    def is_session_active(self) -> bool:
        return self.session_status().get('logged_in', False)

    def make_fixture_job(self, **overrides) -> NormalizedJob:
        title    = overrides.get('title', 'Senior Accountant')
        company  = overrides.get('company', 'Acme Corp Qatar WLL')
        location = overrides.get('location', 'Doha, Qatar')
        url      = overrides.get('url', 'https://www.linkedin.com/jobs/view/fixture-li-001')
        return NormalizedJob(
            job_id=NormalizedJob.make_job_id(self.SOURCE_ID, url),
            source=self.SOURCE_ID,
            url=url,
            title=title,
            company=company,
            location=location,
            posted_date=overrides.get('posted_date', _today_minus(3)),
            requirements=overrides.get('requirements', [
                'CPA or ACCA qualification required',
                '5+ years accounting experience',
                'QuickBooks or ERP systems proficiency',
                'IFRS standards knowledge',
                'VAT compliance experience',
                'Budgeting and financial reporting',
                'Reconciliation and audit experience',
            ]),
            application_method=overrides.get('application_method', 'easy_apply'),
            fingerprint=NormalizedJob.make_fingerprint(title, company, location),
            discovered_at=overrides.get('discovered_at', time.time()),
            raw_metadata={'fixture': True},
        )

    @staticmethod
    def _unavailable(note: str) -> dict:
        return {'source': 'linkedin', 'session_present': False, 'logged_in': False, 'note': note}


# ── Qatar Living implementation ────────────────────────────────────────────────

class QatarLivingAdapter(JobSourceAdapter):
    """Qatar Living Jobs — foundation phase, session metadata only."""

    SOURCE_ID = 'qatar_living'
    _DOMAIN = 'qatarliving.com'
    _LOGIN_JS = (
        'document.querySelector(".user-menu, .profile-link, [data-user]") !== null'
    )

    def session_status(self) -> dict:
        try:
            from .ai_browser_transport import _exec_js_in_domain
        except ImportError:
            return self._unavailable('import_error')
        ok, result = _exec_js_in_domain(self._DOMAIN, self._LOGIN_JS, timeout=5)
        if not ok:
            return self._unavailable('no_chrome_tab')
        logged_in = result.strip().lower() == 'true'
        return {
            'source': self.SOURCE_ID,
            'session_present': True,
            'logged_in': logged_in,
            'note': 'session_active' if logged_in else 'session_required',
        }

    def is_session_active(self) -> bool:
        return self.session_status().get('logged_in', False)

    def make_fixture_job(self, **overrides) -> NormalizedJob:
        title    = overrides.get('title', 'Finance Manager')
        company  = overrides.get('company', 'Gulf Holdings WLL')
        location = overrides.get('location', 'Qatar')
        url      = overrides.get('url', 'https://www.qatarliving.com/jobs/fixture-ql-002')
        return NormalizedJob(
            job_id=NormalizedJob.make_job_id(self.SOURCE_ID, url),
            source=self.SOURCE_ID,
            url=url,
            title=title,
            company=company,
            location=location,
            posted_date=overrides.get('posted_date', _today_minus(5)),
            requirements=overrides.get('requirements', [
                'ACCA or CPA qualification',
                '7+ years finance experience',
                'IFRS and GAAP knowledge',
                'ERP systems proficiency',
                'Budgeting and forecasting',
                'Financial reporting and reconciliation',
                'VAT compliance experience',
            ]),
            application_method=overrides.get('application_method', 'direct'),
            fingerprint=NormalizedJob.make_fingerprint(title, company, location),
            discovered_at=overrides.get('discovered_at', time.time()),
            raw_metadata={'fixture': True},
        )

    @staticmethod
    def _unavailable(note: str) -> dict:
        return {'source': 'qatar_living', 'session_present': False, 'logged_in': False, 'note': note}


# ── Registry ──────────────────────────────────────────────────────────────────

_ADAPTERS: dict[str, JobSourceAdapter] = {}


def _init_adapters() -> None:
    if not _ADAPTERS:
        _ADAPTERS['linkedin'] = LinkedInAdapter()
        _ADAPTERS['qatar_living'] = QatarLivingAdapter()


def get_adapter(source_id: str) -> JobSourceAdapter:
    _init_adapters()
    if source_id not in _ADAPTERS:
        raise KeyError(f'Unknown source {source_id!r}. Allowed: {sorted(_ADAPTERS)}')
    return _ADAPTERS[source_id]


def all_session_statuses() -> dict:
    """Return session_status for all sources.  Safe to call at any time."""
    _init_adapters()
    return {sid: a.session_status() for sid, a in _ADAPTERS.items()}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _today_minus(days: int) -> str:
    import datetime
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
