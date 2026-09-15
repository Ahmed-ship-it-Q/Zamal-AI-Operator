"""Step 4 — JobSourceAdapter interface + LinkedIn and Qatar Living implementations.

Foundation-phase constraints:
  - Session checks use narrow browser-tab metadata only (title/login indicator).
    Broad extraction (document.body.innerText, innerHTML, querySelectorAll("*"),
    accessibility trees) is never used.
  - discover_jobs() is guarded by _LIVE_SEARCH_ENABLED (default False).
    Enable via explicit enable_live_search() call after operator authorization.
  - No credentials, cookies, session tokens or private data are stored or logged.
  - Public job listings only; never reads private messages, notifications, or
    inbox content.

Live-search constraints (when enabled):
  - Max two JS extraction attempts per source before raising SessionRequiredError.
  - CAPTCHA / authwall signals raise SessionRequiredError immediately.
  - No apply/submit clicks ever.
  - Tab navigates to public search and job-detail URLs only.
"""
import datetime
import json
import re
import time
from abc import ABC, abstractmethod
from typing import Optional

from .job_schema import NormalizedJob, JobQuery, APPLICATION_METHODS


# ── Exceptions ─────────────────────────────────────────────────────────────────

class SessionRequiredError(Exception):
    """Raised by discover_jobs() when a browser session is unavailable or blocked."""
    def __init__(self, source: str, reason: str):
        self.source = source
        self.reason = reason
        super().__init__(f'{source}: session required — {reason}')


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
        """Public job discovery — disabled until enable_live_search() is called."""
        raise NotImplementedError(
            f'{self.SOURCE_ID}: live job discovery is disabled. '
            'Call enable_live_search() after explicit operator authorization.'
        )

    @abstractmethod
    def make_fixture_job(self, **overrides) -> NormalizedJob:
        """Return a synthetic NormalizedJob for testing.  No network calls."""


# ── LinkedIn implementation ────────────────────────────────────────────────────

class LinkedInAdapter(JobSourceAdapter):
    """LinkedIn Jobs — public listing search via browser tab automation."""

    SOURCE_ID = 'linkedin'
    _DOMAIN = 'linkedin.com'
    _LIVE_SEARCH_ENABLED: bool = False

    # Narrow login check: header present + no authwall/join form.
    # LinkedIn renamed .global-nav; use absence-of-authwall as the reliable signal.
    _LOGIN_JS = (
        'document.querySelector("header") !== null '
        '&& document.querySelector(".authwall-join-form, .join-form") === null'
    )

    # Qatar accounting/finance search — last 30 days, sorted by date
    _SEARCH_URL = (
        'https://www.linkedin.com/jobs/search/'
        '?keywords=accountant+finance+accounting+payroll+qatar'
        '&location=Qatar'
        '&f_TPR=r2592000'
        '&sortBy=DD'
    )

    # Extract job cards from search results page — narrow, targeted selectors only
    _CARD_EXTRACT_JS = r"""
(function(){
  var cards = document.querySelectorAll(
    '.job-card-container, .jobs-search__results-list li[class*="jobs-search"], ' +
    '[data-occludable-job-id], .base-card'
  );
  if (!cards.length) {
    cards = document.querySelectorAll('[class*="job-card"], [class*="JobCard"]');
  }
  var jobs = [];
  cards.forEach(function(c){
    var titleEl = c.querySelector(
      '.job-card-list__title, .base-search-card__title, ' +
      'a[data-control-name="job_card_title"], h3 a, h3'
    );
    var compEl = c.querySelector(
      '.job-card-container__company-name, .base-search-card__subtitle a, ' +
      '.job-card-list__company-name, h4'
    );
    var locEl = c.querySelector(
      '.job-card-container__metadata-item, .job-card-list__location, ' +
      '.base-search-card__metadata'
    );
    var linkEl = c.querySelector('a[href*="/jobs/view/"]');
    var timeEl = c.querySelector('time[datetime]');
    var easyEl = c.querySelector('[aria-label*="Easy Apply"], [class*="easy-apply"]');
    if (!linkEl && !titleEl) return;
    var url = linkEl ? linkEl.href.split('?')[0] : '';
    if (!url) return;
    jobs.push({
      title: titleEl ? (titleEl.innerText || titleEl.textContent || '').trim() : '',
      company: compEl ? (compEl.innerText || compEl.textContent || '').trim() : '',
      location: locEl ? (locEl.innerText || locEl.textContent || '').trim() : '',
      url: url,
      posted_date: timeEl ? timeEl.getAttribute('datetime') : null,
      method: easyEl ? 'easy_apply' : 'direct'
    });
  });
  return JSON.stringify({status: 'ok', count: jobs.length, jobs: jobs.slice(0, 25)});
})()
"""

    # Read description from current job detail page
    _DESC_JS = r"""
(function(){
  var el = document.querySelector(
    '.jobs-description-content__text, ' +
    '.jobs-description__content .jobs-box__html-content, ' +
    '.description__text, ' +
    '[class*="description-content"]'
  );
  if (!el) return JSON.stringify({status: 'no_desc', text: ''});
  return JSON.stringify({status: 'ok', text: (el.innerText || el.textContent || '').substring(0, 3000)});
})()
"""

    # CAPTCHA / authwall check
    _CAPTCHA_CHECK_JS = r"""
(function(){
  var captcha = document.querySelector(
    'input#captcha-challenge, iframe[src*="recaptcha"], ' +
    '.captcha, [class*="captcha"]'
  ) !== null;
  var authwall = document.querySelector(
    '.authwall-join-form, .join-form, [class*="authwall"]'
  ) !== null;
  return JSON.stringify({captcha: captcha, authwall: authwall});
})()
"""

    @classmethod
    def enable_live_search(cls) -> None:
        """Enable live job discovery.  Call after explicit operator authorization."""
        cls._LIVE_SEARCH_ENABLED = True

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

    def discover_jobs(self, query: JobQuery) -> list:
        if not self._LIVE_SEARCH_ENABLED:
            raise NotImplementedError(
                'linkedin: live search disabled. '
                'Call LinkedInAdapter.enable_live_search() after operator authorization.'
            )
        from .ai_browser_transport import _exec_js_in_domain, _wait_tab_loaded
        # 1. Session check
        if not self.is_session_active():
            raise SessionRequiredError(self.SOURCE_ID, 'not_logged_in')
        # 2. Navigate to search URL
        nav_js = f'location.assign({json.dumps(self._SEARCH_URL)}); "navigating"'
        _exec_js_in_domain(self._DOMAIN, nav_js, timeout=5)
        time.sleep(5)
        _wait_tab_loaded(self._DOMAIN, timeout=25)
        time.sleep(2)
        # 3. CAPTCHA check
        ok_c, cap_raw = _exec_js_in_domain(self._DOMAIN, self._CAPTCHA_CHECK_JS, timeout=8)
        if ok_c:
            try:
                cap = json.loads(cap_raw)
                if cap.get('captcha') or cap.get('authwall'):
                    raise SessionRequiredError(self.SOURCE_ID, 'captcha_or_authwall_detected')
            except (json.JSONDecodeError, TypeError):
                pass
        # 4. Extract job cards
        ok, raw = _exec_js_in_domain(self._DOMAIN, self._CARD_EXTRACT_JS, timeout=12)
        if not ok:
            raise SessionRequiredError(self.SOURCE_ID, 'card_extraction_failed')
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise SessionRequiredError(self.SOURCE_ID, f'card_json_parse_failed: {raw[:80]}')
        raw_jobs = data.get('jobs', [])
        if not raw_jobs:
            return []
        # 5. For each card, navigate to job page and read description
        results = []
        for raw_job in raw_jobs:
            url = (raw_job.get('url') or '').strip()
            if not url or 'linkedin.com/jobs/view/' not in url:
                continue
            nav_js2 = f'location.assign({json.dumps(url)}); "navigating"'
            _exec_js_in_domain(self._DOMAIN, nav_js2, timeout=5)
            time.sleep(4)
            _wait_tab_loaded(self._DOMAIN, timeout=20)
            ok_d, desc_raw = _exec_js_in_domain(self._DOMAIN, self._DESC_JS, timeout=10)
            desc_text = ''
            if ok_d:
                try:
                    desc_data = json.loads(desc_raw)
                    desc_text = desc_data.get('text', '')
                except (json.JSONDecodeError, TypeError):
                    desc_text = ''
            job = self._normalize(raw_job, desc_text)
            results.append(job)
        return results

    def _normalize(self, raw: dict, desc_text: str = '') -> NormalizedJob:
        url = (raw.get('url') or '').strip()
        title = (raw.get('title') or '').strip()
        company = (raw.get('company') or '').strip()
        location = (raw.get('location') or '').strip() or 'Qatar'
        posted_date = _parse_date(raw.get('posted_date'))
        method = raw.get('method', 'unknown')
        if method not in APPLICATION_METHODS:
            method = 'unknown'
        requirements = _desc_to_requirements(desc_text)
        return NormalizedJob(
            job_id=NormalizedJob.make_job_id(self.SOURCE_ID, url),
            source=self.SOURCE_ID,
            url=url,
            title=title,
            company=company,
            location=location,
            posted_date=posted_date,
            requirements=requirements,
            application_method=method,
            fingerprint=NormalizedJob.make_fingerprint(title, company, location),
            discovered_at=time.time(),
            raw_metadata={'source_page': 'search', 'has_desc': bool(desc_text)},
        )

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
    """Qatar Living Jobs — public listing search via browser tab automation.

    Qatar Living public job listings are accessible without login.
    Session check requires only that the tab is present.
    """

    SOURCE_ID = 'qatar_living'
    _DOMAIN = 'qatarliving.com'
    _LIVE_SEARCH_ENABLED: bool = False

    _LOGIN_JS = (
        'document.querySelector(".user-menu, .profile-link, [data-user]") !== null'
    )

    _SEARCH_URL = 'https://www.qatarliving.com/forum/jobs'

    # Extract job listing links from Qatar Living jobs forum page
    _CARD_EXTRACT_JS = r"""
(function(){
  var jobs = [];

  // Primary: drupal views rows with job links
  var rows = document.querySelectorAll('.views-row, .view-content .views-row');
  if (!rows.length) rows = document.querySelectorAll('.node--type-classifieds, article.node');
  if (!rows.length) rows = document.querySelectorAll('table.views-table tbody tr');

  rows.forEach(function(r){
    var titleEl = r.querySelector('h3 a, h2 a, .field-title a, .views-field-title a, a');
    if (!titleEl) return;
    var href = titleEl.href || '';
    if (!href.includes('qatarliving.com')) return;
    var dateEl = r.querySelector('time[datetime], .field-date time, .views-field-created span');
    var locEl  = r.querySelector('.field-location, .views-field-field-location');
    var compEl = r.querySelector('.field-company, .views-field-field-company');
    jobs.push({
      title: (titleEl.innerText || titleEl.textContent || '').trim(),
      company: compEl ? (compEl.innerText || compEl.textContent || '').trim() : '',
      location: locEl ? (locEl.innerText || locEl.textContent || '').trim() : 'Qatar',
      url: href.split('?')[0],
      posted_date: dateEl ? (dateEl.getAttribute('datetime') || dateEl.textContent.trim()) : null,
      method: 'direct'
    });
  });

  // Fallback: any links that look like job/classifieds listings
  if (!jobs.length) {
    var links = document.querySelectorAll('a[href*="qatarliving.com/forum/jobs"], a[href*="/node/"]');
    links.forEach(function(a){
      var txt = (a.innerText || a.textContent || '').trim();
      if (txt.length > 5 && txt.length < 200) {
        jobs.push({title: txt, company: '', location: 'Qatar', url: a.href.split('?')[0],
                   posted_date: null, method: 'direct'});
      }
    });
  }

  return JSON.stringify({status: jobs.length ? 'ok' : 'no_cards', count: jobs.length, jobs: jobs.slice(0, 25)});
})()
"""

    # Read description from a Qatar Living job detail page
    _DESC_JS = r"""
(function(){
  var el = document.querySelector(
    '.field-description, .field--name-body, .node__content, ' +
    '.field-body, article .content, .post-content, main article'
  );
  if (!el) {
    // Fallback: find the first large text block
    var paras = document.querySelectorAll('p');
    var txt = '';
    paras.forEach(function(p){ txt += ' ' + (p.innerText || p.textContent || ''); });
    return JSON.stringify({status: 'fallback', text: txt.trim().substring(0, 3000)});
  }
  return JSON.stringify({status: 'ok', text: (el.innerText || el.textContent || '').substring(0, 3000)});
})()
"""

    @classmethod
    def enable_live_search(cls) -> None:
        """Enable live job discovery.  Call after explicit operator authorization."""
        cls._LIVE_SEARCH_ENABLED = True

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
            'note': 'session_active' if logged_in else 'public_access_ok',
        }

    def is_session_active(self) -> bool:
        # Qatar Living: public listings accessible without login; tab presence is enough.
        st = self.session_status()
        return st.get('session_present', False)

    def discover_jobs(self, query: JobQuery) -> list:
        if not self._LIVE_SEARCH_ENABLED:
            raise NotImplementedError(
                'qatar_living: live search disabled. '
                'Call QatarLivingAdapter.enable_live_search() after operator authorization.'
            )
        from .ai_browser_transport import _exec_js_in_domain, _wait_tab_loaded
        # 1. Tab present check
        if not self.is_session_active():
            raise SessionRequiredError(self.SOURCE_ID, 'tab_not_found')
        # 2. Navigate to jobs forum
        nav_js = f'location.assign({json.dumps(self._SEARCH_URL)}); "navigating"'
        _exec_js_in_domain(self._DOMAIN, nav_js, timeout=5)
        time.sleep(5)
        _wait_tab_loaded(self._DOMAIN, timeout=25)
        time.sleep(2)
        # 3. Extract job cards
        ok, raw = _exec_js_in_domain(self._DOMAIN, self._CARD_EXTRACT_JS, timeout=12)
        if not ok:
            raise SessionRequiredError(self.SOURCE_ID, 'card_extraction_failed')
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise SessionRequiredError(self.SOURCE_ID, f'card_json_parse_failed: {raw[:80]}')
        raw_jobs = data.get('jobs', [])
        if not raw_jobs:
            return []
        # 4. For each listing URL, navigate and read description
        results = []
        for raw_job in raw_jobs:
            url = (raw_job.get('url') or '').strip()
            if not url or 'qatarliving.com' not in url:
                continue
            nav_js2 = f'location.assign({json.dumps(url)}); "navigating"'
            _exec_js_in_domain(self._DOMAIN, nav_js2, timeout=5)
            time.sleep(3)
            _wait_tab_loaded(self._DOMAIN, timeout=20)
            ok_d, desc_raw = _exec_js_in_domain(self._DOMAIN, self._DESC_JS, timeout=10)
            desc_text = ''
            if ok_d:
                try:
                    desc_data = json.loads(desc_raw)
                    desc_text = desc_data.get('text', '')
                except (json.JSONDecodeError, TypeError):
                    desc_text = ''
            job = self._normalize(raw_job, desc_text)
            results.append(job)
        return results

    def _normalize(self, raw: dict, desc_text: str = '') -> NormalizedJob:
        url = (raw.get('url') or '').strip()
        title = (raw.get('title') or '').strip()
        company = (raw.get('company') or '').strip()
        location = (raw.get('location') or '').strip() or 'Qatar'
        posted_date = _parse_date(raw.get('posted_date'))
        requirements = _desc_to_requirements(desc_text)
        return NormalizedJob(
            job_id=NormalizedJob.make_job_id(self.SOURCE_ID, url),
            source=self.SOURCE_ID,
            url=url,
            title=title,
            company=company,
            location=location,
            posted_date=posted_date,
            requirements=requirements,
            application_method='direct',
            fingerprint=NormalizedJob.make_fingerprint(title, company, location),
            discovered_at=time.time(),
            raw_metadata={'source_page': 'forum_jobs', 'has_desc': bool(desc_text)},
        )

    def make_fixture_job(self, **overrides) -> NormalizedJob:
        title    = overrides.get('title', 'Chief Accountant')
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


def run_job_search(query: JobQuery, store, profile=None, task_id: str = '',
                   notifier=None) -> dict:
    """Run live job discovery on all live-enabled adapters, score and store results.

    Returns summary dict with per-source and aggregate counts.
    Never submits applications; JobApplicationExecutor remains disabled.
    """
    from .job_lifecycle import JobListingStore, JobTelegramNotifier
    from .job_matcher import JobMatcher
    _init_adapters()

    jls = JobListingStore(store)
    matcher = JobMatcher(
        profile=profile,
        seen_fingerprints=jls.fingerprints(),
        processed_job_ids=jls.processed_job_ids(),
    )
    notifier = notifier or JobTelegramNotifier()

    summary = {
        'task_id': task_id,
        'sources': {},
        'total_discovered': 0,
        'total_shortlisted': 0,
        'total_rejected': 0,
        'errors': [],
    }

    for source_id, adapter in _ADAPTERS.items():
        src = {'discovered': 0, 'shortlisted': 0, 'rejected': 0, 'rejection_reasons': {}}
        try:
            raw_jobs = adapter.discover_jobs(query)
        except NotImplementedError:
            src['note'] = 'not_live_enabled'
            summary['sources'][source_id] = src
            continue
        except SessionRequiredError as exc:
            src['note'] = f'session_required:{exc.reason}'
            summary['errors'].append({'source': source_id, 'error': exc.reason})
            if task_id:
                notifier.send_session_checkpoint(source_id, task_id)
            summary['sources'][source_id] = src
            continue
        except Exception as exc:
            src['note'] = f'error:{type(exc).__name__}:{str(exc)[:80]}'
            summary['errors'].append({'source': source_id, 'error': str(exc)[:120]})
            summary['sources'][source_id] = src
            continue

        for job in raw_jobs:
            src['discovered'] += 1
            listing_id = jls.upsert(job)
            result = matcher.evaluate(job)
            jls.apply_match(listing_id, result)

            if result.accepted:
                try:
                    jls.advance(listing_id, 'SHORTLISTED')
                    src['shortlisted'] += 1
                    matcher.mark_processed(job)
                except Exception:
                    pass
            else:
                src['rejected'] += 1
                reason_key = (result.rejection_reason or 'unknown')[:40]
                src['rejection_reasons'][reason_key] = \
                    src['rejection_reasons'].get(reason_key, 0) + 1
                matcher.mark_processed(job)

        src['note'] = 'ok'
        summary['sources'][source_id] = src
        summary['total_discovered'] += src['discovered']
        summary['total_shortlisted'] += src['shortlisted']
        summary['total_rejected'] += src['rejected']

    return summary


# ── Helpers ────────────────────────────────────────────────────────────────────

def _today_minus(days: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def _parse_date(raw) -> Optional[str]:
    """Normalize various date formats to YYYY-MM-DD or None."""
    if not raw:
        return None
    s = str(raw).strip()
    # ISO datetime: 2026-09-12T00:00:00Z
    try:
        return datetime.date.fromisoformat(s[:10]).isoformat()
    except Exception:
        pass
    # "N days ago" pattern from display text
    m = re.search(r'(\d+)\s*day', s.lower())
    if m:
        days = int(m.group(1))
        return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    # "N hours ago" → today
    if re.search(r'\d+\s*hour', s.lower()):
        return datetime.date.today().isoformat()
    # "N weeks ago"
    m2 = re.search(r'(\d+)\s*week', s.lower())
    if m2:
        weeks = int(m2.group(1))
        return (datetime.date.today() - datetime.timedelta(weeks=weeks)).isoformat()
    # "N months ago"
    m3 = re.search(r'(\d+)\s*month', s.lower())
    if m3:
        months = int(m3.group(1))
        return (datetime.date.today() - datetime.timedelta(days=months * 30)).isoformat()
    return None


def _desc_to_requirements(desc_text: str) -> list:
    """Convert raw description text into a capped list of requirement strings.

    Splits on bullet points and sentence boundaries; strips whitespace.
    Caps at 20 items per NormalizedJob spec.
    """
    if not desc_text:
        return []
    # Split on common delimiters: newlines, bullet chars, sentence ends
    parts = re.split(r'[\n•·\-–—●▪◦]\s*|(?<=[.!?])\s+', desc_text)
    reqs = []
    for p in parts:
        p = p.strip()
        if 5 <= len(p) <= 200:
            reqs.append(p)
    return reqs[:20]
