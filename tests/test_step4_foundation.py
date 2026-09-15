"""Step 4 Foundation Tests.

Tests cover:
  - Both job source adapters (fixture jobs, session_status, discover disabled)
  - Qatar filter: accepts Qatar/Doha, rejects non-Qatar
  - Finance role filter: accepts accounting roles, rejects software roles
  - Scoring 90-99 range; no score above 99
  - Score inflation prevention
  - Rejection rules: duplicate, expired, non-Qatar, non-finance
  - Deduplication via fingerprint and job_id
  - Job lifecycle state transitions
  - CVProfileProvider disabled by default, fixture profile works
  - JobListingStore: upsert, score, advance, approve, reject, idempotency
  - Approval/reject cycle through CC endpoints
  - Checkpoint save/resume for job-related task (uses existing mechanism)
  - Emergency Stop blocks job_submit_application
  - n8n workflow structure is valid JSON with all 4 workflows inactive
  - CC job endpoints return valid JSON
  - Zero real browser search, CV read, or external submission
  - Policy.requires_approval() for job_ kinds
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path

# Make zamal_core importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── 1. Schema ─────────────────────────────────────────────────────────────────

class TestNormalizedJob(unittest.TestCase):

    def setUp(self):
        from zamal_core.job_schema import NormalizedJob
        self.NJ = NormalizedJob

    def test_make_job_id_deterministic(self):
        id1 = self.NJ.make_job_id('linkedin', 'https://example.com/jobs/1')
        id2 = self.NJ.make_job_id('linkedin', 'https://example.com/jobs/1')
        self.assertEqual(id1, id2)
        self.assertEqual(len(id1), 16)

    def test_make_job_id_different_sources(self):
        id1 = self.NJ.make_job_id('linkedin', 'https://example.com/jobs/1')
        id2 = self.NJ.make_job_id('qatar_living', 'https://example.com/jobs/1')
        self.assertNotEqual(id1, id2)

    def test_make_fingerprint_case_insensitive(self):
        f1 = self.NJ.make_fingerprint('Senior Accountant', 'Acme Corp', 'Doha, Qatar')
        f2 = self.NJ.make_fingerprint('senior accountant', 'acme corp', 'doha, qatar')
        self.assertEqual(f1, f2)
        self.assertEqual(len(f1), 24)

    def test_to_dict_caps_requirements(self):
        from zamal_core.job_schema import NormalizedJob
        job = NormalizedJob(
            job_id='x', source='linkedin', url='https://example.com',
            title='Accountant', company='Corp', location='Qatar',
            posted_date='2026-09-01', requirements=['req' + str(i) for i in range(30)],
            application_method='direct', fingerprint='fp', discovered_at=time.time(),
        )
        d = job.to_dict()
        self.assertLessEqual(len(d['requirements']), 20)

    def test_job_states_terminal(self):
        from zamal_core.job_schema import JOB_STATES
        self.assertEqual(JOB_STATES['REJECTED'], set())
        self.assertEqual(JOB_STATES['COMPLETED'], set())

    def test_job_states_shortlisted_can_be_approved_or_rejected(self):
        from zamal_core.job_schema import JOB_STATES
        self.assertIn('WAITING_APPROVAL', JOB_STATES['SHORTLISTED'])
        self.assertIn('REJECTED', JOB_STATES['SHORTLISTED'])


# ── 2. Adapters ───────────────────────────────────────────────────────────────

class TestLinkedInAdapter(unittest.TestCase):

    def setUp(self):
        from zamal_core.job_source_adapter import LinkedInAdapter
        self.adapter = LinkedInAdapter()

    def test_source_id(self):
        self.assertEqual(self.adapter.SOURCE_ID, 'linkedin')

    def test_discover_jobs_disabled(self):
        from zamal_core.job_schema import JobQuery
        q = JobQuery(keywords=['accountant'], location='Qatar')
        with self.assertRaises(NotImplementedError):
            self.adapter.discover_jobs(q)

    def test_fixture_job_structure(self):
        job = self.adapter.make_fixture_job()
        self.assertEqual(job.source, 'linkedin')
        self.assertIn('linkedin.com', job.url)
        self.assertIsInstance(job.job_id, str)
        self.assertEqual(len(job.job_id), 16)
        self.assertIsInstance(job.requirements, list)
        self.assertIsInstance(job.fingerprint, str)

    def test_fixture_job_overrides(self):
        job = self.adapter.make_fixture_job(
            title='Finance Director', company='TestCo', location='Doha, Qatar'
        )
        self.assertEqual(job.title, 'Finance Director')
        self.assertEqual(job.company, 'TestCo')

    def test_session_status_returns_dict_without_network(self):
        # Chrome is running so this may succeed or return no_chrome_tab — either is valid
        st = self.adapter.session_status()
        self.assertIn('source', st)
        self.assertIn('session_present', st)
        self.assertIn('logged_in', st)
        self.assertIn('note', st)
        self.assertEqual(st['source'], 'linkedin')
        # Verify no private data returned
        self.assertNotIn('inbox', st)
        self.assertNotIn('messages', st)
        self.assertNotIn('notifications', st)


class TestQatarLivingAdapter(unittest.TestCase):

    def setUp(self):
        from zamal_core.job_source_adapter import QatarLivingAdapter
        self.adapter = QatarLivingAdapter()

    def test_source_id(self):
        self.assertEqual(self.adapter.SOURCE_ID, 'qatar_living')

    def test_discover_jobs_disabled(self):
        from zamal_core.job_schema import JobQuery
        q = JobQuery(keywords=['accountant'], location='Qatar')
        with self.assertRaises(NotImplementedError):
            self.adapter.discover_jobs(q)

    def test_fixture_job_structure(self):
        job = self.adapter.make_fixture_job()
        self.assertEqual(job.source, 'qatar_living')
        self.assertIn('qatarliving.com', job.url)
        self.assertIsInstance(job.requirements, list)

    def test_fixture_jobs_different_fingerprints(self):
        from zamal_core.job_source_adapter import LinkedInAdapter, QatarLivingAdapter
        li = LinkedInAdapter().make_fixture_job(
            title='Accountant', company='CompA', location='Doha, Qatar'
        )
        ql = QatarLivingAdapter().make_fixture_job(
            title='Finance Manager', company='CompB', location='Qatar'
        )
        self.assertNotEqual(li.fingerprint, ql.fingerprint)


class TestAdapterRegistry(unittest.TestCase):

    def test_get_adapter_linkedin(self):
        from zamal_core.job_source_adapter import get_adapter
        a = get_adapter('linkedin')
        self.assertEqual(a.SOURCE_ID, 'linkedin')

    def test_get_adapter_qatar_living(self):
        from zamal_core.job_source_adapter import get_adapter
        a = get_adapter('qatar_living')
        self.assertEqual(a.SOURCE_ID, 'qatar_living')

    def test_get_adapter_unknown_raises(self):
        from zamal_core.job_source_adapter import get_adapter
        with self.assertRaises(KeyError):
            get_adapter('aiapply')

    def test_all_session_statuses_returns_both(self):
        from zamal_core.job_source_adapter import all_session_statuses
        st = all_session_statuses()
        self.assertIn('linkedin', st)
        self.assertIn('qatar_living', st)
        for s in st.values():
            self.assertIn('source', s)
            self.assertIn('logged_in', s)

    def test_no_aiapply_adapter(self):
        from zamal_core.job_source_adapter import _ADAPTERS, _init_adapters
        _init_adapters()
        self.assertNotIn('aiapply', _ADAPTERS)


# ── 3. CV Profile ─────────────────────────────────────────────────────────────

class TestCVProfileProvider(unittest.TestCase):

    def setUp(self):
        from zamal_core.cv_profile import CVProfileProvider
        CVProfileProvider.revoke()  # ensure clean state

    def tearDown(self):
        from zamal_core.cv_profile import CVProfileProvider
        CVProfileProvider.revoke()

    def test_disabled_by_default(self):
        from zamal_core.cv_profile import CVProfileProvider
        self.assertFalse(CVProfileProvider.is_enabled())

    def test_get_profile_raises_when_disabled(self):
        from zamal_core.cv_profile import CVProfileProvider
        with self.assertRaises(RuntimeError):
            CVProfileProvider.get_profile()

    def test_fixture_profile_no_auth_required(self):
        from zamal_core.cv_profile import CVProfileProvider
        profile = CVProfileProvider.get_fixture_profile()
        self.assertTrue(profile.is_fixture)
        self.assertGreater(len(profile.skills), 0)
        self.assertGreater(len(profile.qualifications), 0)
        self.assertIn('Qatar', profile.target_locations)

    def test_fixture_profile_sanitized(self):
        from zamal_core.cv_profile import CVProfileProvider
        profile = CVProfileProvider.get_fixture_profile()
        d = profile.to_sanitized_dict()
        self.assertIn('skills_count', d)
        self.assertNotIn('email_masked', d)  # not exposed in sanitized dict

    def test_authorize_enables_access(self):
        from zamal_core.cv_profile import CVProfileProvider
        CVProfileProvider.authorize()
        self.assertTrue(CVProfileProvider.is_enabled())

    def test_get_profile_requires_env_var(self):
        from zamal_core.cv_profile import CVProfileProvider
        CVProfileProvider.authorize()
        # ZAMAL_CV_PATH not set → RuntimeError
        os.environ.pop('ZAMAL_CV_PATH', None)
        with self.assertRaises(RuntimeError):
            CVProfileProvider.get_profile()

    def test_revoke_clears_state(self):
        from zamal_core.cv_profile import CVProfileProvider
        CVProfileProvider.authorize()
        CVProfileProvider.revoke()
        self.assertFalse(CVProfileProvider.is_enabled())


# ── 4. Matcher — filters ──────────────────────────────────────────────────────

class TestQatarFilter(unittest.TestCase):

    def _chk(self, loc, expected):
        from zamal_core.job_matcher import is_qatar_location
        self.assertEqual(is_qatar_location(loc), expected, loc)

    def test_accepts_qatar(self):   self._chk('Qatar', True)
    def test_accepts_doha(self):    self._chk('Doha', True)
    def test_accepts_doha_qatar(self): self._chk('Doha, Qatar', True)
    def test_rejects_dubai(self):   self._chk('Dubai, UAE', False)
    def test_rejects_london(self):  self._chk('London, UK', False)
    def test_rejects_riyadh(self):  self._chk('Riyadh, Saudi Arabia', False)
    def test_rejects_remote(self):  self._chk('Remote', False)
    def test_rejects_empty(self):   self._chk('', False)


class TestFinanceFilter(unittest.TestCase):

    def _chk(self, title, expected):
        from zamal_core.job_matcher import is_finance_role
        self.assertEqual(is_finance_role(title), expected, title)

    def test_accepts_senior_accountant(self): self._chk('Senior Accountant', True)
    def test_accepts_finance_manager(self):   self._chk('Finance Manager', True)
    def test_accepts_cfo(self):               self._chk('Chief Financial Officer', True)
    def test_accepts_audit(self):             self._chk('Internal Audit Manager', True)
    def test_accepts_controller(self):        self._chk('Financial Controller', True)
    def test_rejects_software_engineer(self): self._chk('Software Engineer', False)
    def test_rejects_hr_manager(self):        self._chk('HR Manager', False)
    def test_rejects_marketing(self):         self._chk('Marketing Director', False)
    def test_rejects_cto(self):               self._chk('Chief Technology Officer', False)


# ── 5. Matcher — scoring ──────────────────────────────────────────────────────

class TestJobMatcher(unittest.TestCase):

    def _fixture_li(self, **kw):
        from zamal_core.job_source_adapter import LinkedInAdapter
        return LinkedInAdapter().make_fixture_job(**kw)

    def _fixture_ql(self, **kw):
        from zamal_core.job_source_adapter import QatarLivingAdapter
        return QatarLivingAdapter().make_fixture_job(**kw)

    def setUp(self):
        from zamal_core.job_matcher import JobMatcher
        from zamal_core.cv_profile import CVProfileProvider
        self.profile = CVProfileProvider.get_fixture_profile()
        self.matcher = JobMatcher(profile=self.profile)

    def test_fixture_linkedin_job_accepted(self):
        job = self._fixture_li()
        r = self.matcher.evaluate(job)
        self.assertTrue(r.accepted, f'expected accepted, got score={r.score} reason={r.rejection_reason}')
        self.assertGreaterEqual(r.score, 90)
        self.assertLessEqual(r.score, 100)

    def test_fixture_qatar_living_job_accepted(self):
        job = self._fixture_ql()
        r = self.matcher.evaluate(job)
        self.assertTrue(r.accepted, f'expected accepted, got score={r.score} reason={r.rejection_reason}')
        self.assertGreaterEqual(r.score, 90)
        self.assertLessEqual(r.score, 100)

    def test_score_never_above_100(self):
        for _ in range(10):
            job = self._fixture_li()
            r = self.matcher.evaluate(job)
            self.assertLessEqual(r.score, 100, 'score inflation: exceeded 100')

    def test_non_qatar_rejected(self):
        job = self._fixture_li(location='Dubai, UAE')
        r = self.matcher.evaluate(job)
        self.assertFalse(r.accepted)
        self.assertEqual(r.score, 0)
        self.assertIn('location_not_qatar', r.rejection_reason)

    def test_non_finance_rejected(self):
        job = self._fixture_li(title='Software Engineer')
        r = self.matcher.evaluate(job)
        self.assertFalse(r.accepted)
        self.assertIn('role_not_finance', r.rejection_reason)

    def test_expired_job_rejected(self):
        job = self._fixture_li(posted_date='2020-01-01')
        r = self.matcher.evaluate(job)
        self.assertFalse(r.accepted)
        self.assertIn('60_days', r.rejection_reason)

    def test_score_breakdown_all_components_present(self):
        from zamal_core.job_matcher import SCORE_COMPONENTS
        job = self._fixture_li()
        r = self.matcher.evaluate(job)
        for key in SCORE_COMPONENTS:
            self.assertIn(key, r.score_breakdown, f'missing component {key}')

    def test_score_breakdown_values_non_negative(self):
        job = self._fixture_li()
        r = self.matcher.evaluate(job)
        for k, v in r.score_breakdown.items():
            self.assertGreaterEqual(v, 0, f'{k} is negative')

    def test_recency_rank_not_in_score(self):
        """recency_rank must exist on result but must not appear in score_breakdown."""
        job = self._fixture_li()
        r = self.matcher.evaluate(job)
        self.assertNotIn('recency', r.score_breakdown)
        self.assertNotIn('recency_rank', r.score_breakdown)
        self.assertIsInstance(r.recency_rank, int)

    def test_no_profile_does_not_raise(self):
        from zamal_core.job_matcher import JobMatcher
        m = JobMatcher(profile=None)
        job = self._fixture_li()
        r = m.evaluate(job)
        self.assertIsInstance(r.score, int)
        self.assertGreaterEqual(r.score, 0)
        self.assertLessEqual(r.score, 100)


# ── 6. Deduplication ──────────────────────────────────────────────────────────

class TestDeduplication(unittest.TestCase):

    def setUp(self):
        from zamal_core.job_matcher import JobMatcher
        self.matcher = JobMatcher()

    def test_duplicate_job_id_rejected(self):
        from zamal_core.job_source_adapter import LinkedInAdapter
        job = LinkedInAdapter().make_fixture_job()
        self.matcher.mark_processed(job)
        r = self.matcher.evaluate(job)
        self.assertFalse(r.accepted)
        self.assertIn('already_processed', r.rejection_reason)

    def test_duplicate_fingerprint_rejected(self):
        from zamal_core.job_source_adapter import LinkedInAdapter, QatarLivingAdapter
        li = LinkedInAdapter().make_fixture_job(
            title='Senior Accountant', company='Corp X', location='Doha, Qatar'
        )
        # Different source, different URL → different job_id, but same fingerprint
        ql = QatarLivingAdapter().make_fixture_job(
            title='Senior Accountant', company='Corp X', location='Doha, Qatar'
        )
        self.assertEqual(li.fingerprint, ql.fingerprint)
        self.matcher.mark_processed(li)
        r = self.matcher.evaluate(ql)
        self.assertFalse(r.accepted)
        self.assertIn('duplicate_listing', r.rejection_reason)

    def test_different_jobs_not_duplicate(self):
        from zamal_core.job_source_adapter import LinkedInAdapter
        j1 = LinkedInAdapter().make_fixture_job(url='https://linkedin.com/jobs/1')
        j2 = LinkedInAdapter().make_fixture_job(
            url='https://linkedin.com/jobs/2', company='Other Corp'
        )
        self.matcher.mark_processed(j1)
        r = self.matcher.evaluate(j2)
        # j2 should not be blocked as duplicate
        self.assertNotIn('duplicate', r.rejection_reason)

    def test_is_duplicate_flag(self):
        from zamal_core.job_source_adapter import LinkedInAdapter
        job = LinkedInAdapter().make_fixture_job()
        self.assertFalse(self.matcher.is_duplicate(job))
        self.matcher.mark_processed(job)
        self.assertTrue(self.matcher.is_duplicate(job))


# ── 7. Job listing store ──────────────────────────────────────────────────────

class TestJobListingStore(unittest.TestCase):

    def setUp(self):
        from zamal_core.store import Store
        from zamal_core.job_lifecycle import JobListingStore
        self.store = Store(':memory:')
        self.jls = JobListingStore(self.store)

    def tearDown(self):
        self.store.close()

    def _fixture_li(self, **kw):
        from zamal_core.job_source_adapter import LinkedInAdapter
        return LinkedInAdapter().make_fixture_job(**kw)

    def test_upsert_creates_listing(self):
        job = self._fixture_li()
        lid = self.jls.upsert(job)
        self.assertIsNotNone(lid)
        row = self.jls.get(lid)
        self.assertIsNotNone(row)
        self.assertEqual(row['title'], job.title)
        self.assertEqual(row['state'], 'DISCOVERED')

    def test_upsert_is_idempotent(self):
        job = self._fixture_li()
        lid1 = self.jls.upsert(job)
        lid2 = self.jls.upsert(job)  # same source+job_id
        self.assertEqual(lid1, lid2)

    def test_apply_match_advances_to_scored(self):
        from zamal_core.job_schema import MatchResult
        job = self._fixture_li()
        lid = self.jls.upsert(job)
        r = MatchResult(job_id=job.job_id, score=94, accepted=True,
                        rejection_reason='', score_breakdown={'test': 94},
                        matched_keywords=['ifrs'])
        self.jls.apply_match(lid, r)
        row = self.jls.get(lid)
        self.assertEqual(row['state'], 'SCORED')
        self.assertEqual(row['score'], 94)

    def test_apply_match_rejected_advances_to_rejected(self):
        from zamal_core.job_schema import MatchResult
        job = self._fixture_li()
        lid = self.jls.upsert(job)
        r = MatchResult(job_id=job.job_id, score=0, accepted=False,
                        rejection_reason='non_qatar',
                        score_breakdown={'rejected_before_scoring': 'non_qatar'},
                        matched_keywords=[])
        self.jls.apply_match(lid, r)
        row = self.jls.get(lid)
        self.assertEqual(row['state'], 'REJECTED')

    def test_advance_state_machine(self):
        from zamal_core.job_schema import MatchResult
        job = self._fixture_li()
        lid = self.jls.upsert(job)
        r = MatchResult(job_id=job.job_id, score=95, accepted=True,
                        rejection_reason='', score_breakdown={}, matched_keywords=[])
        self.jls.apply_match(lid, r)                 # → SCORED
        self.jls.advance(lid, 'SHORTLISTED')         # SCORED → SHORTLISTED
        self.jls.advance(lid, 'WAITING_APPROVAL')    # → WAITING_APPROVAL
        self.jls.advance(lid, 'APPROVED')            # → APPROVED
        row = self.jls.get(lid)
        self.assertEqual(row['state'], 'APPROVED')

    def test_invalid_transition_raises(self):
        job = self._fixture_li()
        lid = self.jls.upsert(job)
        # DISCOVERED → APPROVED is not a valid transition
        with self.assertRaises(ValueError):
            self.jls.advance(lid, 'APPROVED')

    def test_shortlist_returns_shortlisted_rows(self):
        from zamal_core.job_schema import MatchResult
        j1 = self._fixture_li(url='https://linkedin.com/jobs/sl1')
        j2 = self._fixture_li(url='https://linkedin.com/jobs/sl2', company='OtherCo')
        lid1 = self.jls.upsert(j1)
        lid2 = self.jls.upsert(j2)
        for lid, job in [(lid1, j1), (lid2, j2)]:
            r = MatchResult(job_id=job.job_id, score=92, accepted=True,
                            rejection_reason='', score_breakdown={}, matched_keywords=[])
            self.jls.apply_match(lid, r)
            self.jls.advance(lid, 'SHORTLISTED')
        shortlist = self.jls.shortlist()
        self.assertGreaterEqual(len(shortlist), 2)

    def test_fingerprints_set(self):
        job = self._fixture_li()
        self.jls.upsert(job)
        fps = self.jls.fingerprints()
        self.assertIn(job.fingerprint, fps)

    def test_processed_job_ids(self):
        from zamal_core.job_schema import MatchResult
        job = self._fixture_li()
        lid = self.jls.upsert(job)
        r = MatchResult(job_id=job.job_id, score=93, accepted=True,
                        rejection_reason='', score_breakdown={}, matched_keywords=[])
        self.jls.apply_match(lid, r)  # moves past DISCOVERED
        ids = self.jls.processed_job_ids()
        self.assertIn(job.job_id, ids)


# ── 8. Approval / reject via CC endpoints ────────────────────────────────────

class TestJobCCEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import socket, time
        from zamal_core.control_center import ControlCenterServer

        def _free_port():
            with socket.socket() as s:
                s.bind(('127.0.0.1', 0))
                return s.getsockname()[1]

        cls.port = _free_port()
        cls.server = ControlCenterServer(port=cls.port)
        cls.server.serve(block=False)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=0.3):
                    break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.stop()
        except Exception:
            pass

    def _get(self, path):
        import urllib.request
        url = f'http://127.0.0.1:{self.port}{path}'
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.getcode(), json.loads(r.read())

    def _post(self, path, data):
        import urllib.request, urllib.error
        url = f'http://127.0.0.1:{self.port}{path}'
        body = json.dumps(data).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={'Content-Type': 'application/json'},
                                     method='POST')
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.getcode(), json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_job_sources_returns_dict(self):
        code, data = self._get('/api/jobs/sources')
        self.assertEqual(code, 200)
        self.assertIn('sources', data)
        self.assertIsInstance(data['sources'], dict)

    def test_job_sources_has_both_adapters(self):
        code, data = self._get('/api/jobs/sources')
        self.assertEqual(code, 200)
        src = data['sources']
        self.assertIn('linkedin', src)
        self.assertIn('qatar_living', src)

    def test_job_shortlist_returns_list(self):
        code, data = self._get('/api/jobs/shortlist')
        self.assertEqual(code, 200)
        self.assertIn('shortlist', data)
        self.assertIsInstance(data['shortlist'], list)

    def test_job_history_returns_list(self):
        code, data = self._get('/api/jobs/history')
        self.assertEqual(code, 200)
        self.assertIn('history', data)
        self.assertIsInstance(data['history'], list)

    def test_job_approve_missing_listing_id_400(self):
        code, data = self._post('/api/jobs/approve', {})
        self.assertEqual(code, 400)

    def test_job_reject_missing_listing_id_400(self):
        code, data = self._post('/api/jobs/reject', {})
        self.assertEqual(code, 400)

    def test_job_approve_reject_nonexistent_404(self):
        code, _ = self._post('/api/jobs/approve', {'listing_id': 'nonexistent'})
        self.assertEqual(code, 404)
        code2, _ = self._post('/api/jobs/reject', {'listing_id': 'nonexistent'})
        self.assertEqual(code2, 404)

    def test_job_full_cycle_approve(self):
        """Insert a fixture job, shortlist it, approve via CC, verify state."""
        from zamal_core.store import Store
        from zamal_core.job_lifecycle import JobListingStore
        from zamal_core.job_source_adapter import LinkedInAdapter
        from zamal_core.job_schema import MatchResult

        store = Store()
        jls = JobListingStore(store)
        job = LinkedInAdapter().make_fixture_job(url='https://linkedin.com/jobs/cc-cycle-test')
        lid = jls.upsert(job)
        r = MatchResult(job_id=job.job_id, score=95, accepted=True,
                        rejection_reason='', score_breakdown={}, matched_keywords=[])
        jls.apply_match(lid, r)
        jls.advance(lid, 'SHORTLISTED')
        store.close()

        code, data = self._post('/api/jobs/approve', {'listing_id': lid})
        self.assertEqual(code, 200)
        self.assertTrue(data.get('ok'))
        self.assertEqual(data.get('state'), 'APPROVED')

    def test_job_full_cycle_reject(self):
        """Insert a fixture job, shortlist it, reject via CC."""
        from zamal_core.store import Store
        from zamal_core.job_lifecycle import JobListingStore
        from zamal_core.job_source_adapter import QatarLivingAdapter
        from zamal_core.job_schema import MatchResult

        store = Store()
        jls = JobListingStore(store)
        job = QatarLivingAdapter().make_fixture_job(url='https://qatarliving.com/jobs/cc-reject-test')
        lid = jls.upsert(job)
        r = MatchResult(job_id=job.job_id, score=91, accepted=True,
                        rejection_reason='', score_breakdown={}, matched_keywords=[])
        jls.apply_match(lid, r)
        jls.advance(lid, 'SHORTLISTED')
        store.close()

        code, data = self._post('/api/jobs/reject',
                                {'listing_id': lid, 'reason': 'test_rejection'})
        self.assertEqual(code, 200)
        self.assertTrue(data.get('ok'))
        self.assertEqual(data.get('state'), 'REJECTED')


# ── 9. Emergency Stop blocks job submission ───────────────────────────────────

class TestEmergencyStopBlocksJobSubmit(unittest.TestCase):

    def test_job_submit_application_is_protected(self):
        from zamal_core.store import Policy
        p = Policy()
        self.assertTrue(p.requires_approval('job_submit_application'))

    def test_job_source_status_is_safe(self):
        from zamal_core.store import Policy
        p = Policy()
        self.assertFalse(p.requires_approval('job_source_status'))

    def test_job_shortlist_view_is_safe(self):
        from zamal_core.store import Policy
        p = Policy()
        self.assertFalse(p.requires_approval('job_shortlist_view'))

    def test_job_search_discover_is_safe(self):
        from zamal_core.store import Policy
        p = Policy()
        self.assertFalse(p.requires_approval('job_search_discover'))

    def test_stop_everything_blocks_job_submit(self):
        from zamal_core.store import Store
        from zamal_core.safety import Controls, Denied
        s = Store()
        c = Controls(s)
        try:
            c.set('stop_everything', True, actor='test', reason='test')
            with self.assertRaises(Denied):
                c.check('job_submit_application')
        finally:
            try:
                c.set('stop_everything', False, actor='test', reason='cleanup')
            except Exception:
                pass
            s.close()

    def test_pause_external_blocks_job_submit(self):
        from zamal_core.store import Store
        from zamal_core.safety import Controls, Denied
        s = Store()
        c = Controls(s)
        try:
            c.set('pause_external_actions', True, actor='test', reason='test')
            with self.assertRaises(Denied):
                c.check('job_submit_application')
        finally:
            try:
                c.set('pause_external_actions', False, actor='test', reason='cleanup')
            except Exception:
                pass
            s.close()


# ── 10. Checkpoint / resume (job task) ───────────────────────────────────────

class TestJobCheckpointResume(unittest.TestCase):

    def test_checkpoint_save_and_resume_same_task(self):
        import socket, time, json
        import urllib.request, urllib.error
        from zamal_core.control_center import ControlCenterServer

        def _free_port():
            with socket.socket() as s:
                s.bind(('127.0.0.1', 0))
                return s.getsockname()[1]

        port = _free_port()
        server = ControlCenterServer(port=port)
        server.serve(block=False)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                    break
            except Exception:
                time.sleep(0.1)

        def post(path, data):
            url = f'http://127.0.0.1:{port}{path}'
            body = json.dumps(data).encode()
            req = urllib.request.Request(url, data=body,
                                         headers={'Content-Type': 'application/json'},
                                         method='POST')
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.getcode(), json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        # Create a job task
        code, created = post('/api/tasks', {'text': 'step4_job_checkpoint_test'})
        self.assertIn(code, (200, 201))
        task_id = created['task_id']

        # Save checkpoint
        code2, saved = post('/api/checkpoint-save', {'task_id': task_id, 'note': 'session_required'})
        self.assertEqual(code2, 200)
        self.assertTrue(saved.get('ok'))

        # Resume same task_id
        code3, resumed = post('/api/checkpoint-resume', {'task_id': task_id})
        self.assertEqual(code3, 200)
        self.assertTrue(resumed.get('ok'))
        self.assertEqual(resumed.get('task_id'), task_id)
        self.assertFalse(resumed.get('duplicate_action'), 'resume must not create duplicate')

        try:
            server.stop()
        except Exception:
            pass


# ── 11. Application executor disabled ─────────────────────────────────────────

class TestJobApplicationExecutorDisabled(unittest.TestCase):

    def test_executor_disabled_by_default(self):
        from zamal_core.job_lifecycle import JobApplicationExecutor
        ex = JobApplicationExecutor()
        self.assertFalse(ex.can_execute())

    def test_execute_raises_not_implemented(self):
        from zamal_core.job_lifecycle import JobApplicationExecutor
        ex = JobApplicationExecutor()
        with self.assertRaises(NotImplementedError):
            ex.execute({}, 'task-1', 'approval-1')

    def test_enable_live_sets_flag(self):
        from zamal_core.job_lifecycle import JobApplicationExecutor
        try:
            JobApplicationExecutor.enable_live()
            self.assertTrue(JobApplicationExecutor()._LIVE_ENABLED)
        finally:
            JobApplicationExecutor._LIVE_ENABLED = False


# ── 12. n8n workflow structure ────────────────────────────────────────────────

class TestN8nJobWorkflowStructure(unittest.TestCase):

    def _load(self):
        p = Path(__file__).parent.parent / 'config' / 'n8n_job_workflow_structure.json'
        return json.loads(p.read_text())

    def test_file_is_valid_json(self):
        d = self._load()
        self.assertIsInstance(d, dict)

    def test_all_workflows_inactive(self):
        d = self._load()
        for w in d['workflows']:
            self.assertFalse(w.get('active'), f"workflow {w['id']} must be inactive")
            for node in w.get('nodes', []):
                self.assertFalse(node.get('active'), f"node in {w['id']} must be inactive")

    def test_four_workflows_present(self):
        d = self._load()
        self.assertEqual(len(d['workflows']), 4)

    def test_workflow_ids(self):
        d = self._load()
        ids = {w['id'] for w in d['workflows']}
        self.assertIn('job-intake', ids)
        self.assertIn('job-scoring', ids)
        self.assertIn('job-approval', ids)
        self.assertIn('job-application', ids)

    def test_status_is_inactive(self):
        d = self._load()
        self.assertEqual(d['meta']['status'], 'INACTIVE')

    def test_phase_is_foundation(self):
        d = self._load()
        self.assertEqual(d['meta']['phase'], 'foundation')


# ── 13. No AIApply references ────────────────────────────────────────────────

class TestNoAIApply(unittest.TestCase):

    def test_no_aiapply_in_job_modules(self):
        root = Path(__file__).parent.parent / 'zamal_core'
        for f in root.glob('job_*.py'):
            content = f.read_text().lower()
            self.assertNotIn('aiapply', content, f'{f.name} contains AIApply reference')

    def test_no_aiapply_in_config(self):
        config = Path(__file__).parent.parent / 'config'
        for f in config.glob('*.json'):
            content = f.read_text().lower()
            self.assertNotIn('aiapply', content, f'{f.name} contains AIApply reference')


# ── 14. Zero real network / browser / CV access ──────────────────────────────

class TestNoRealExternalAccess(unittest.TestCase):

    def test_discover_jobs_raises_not_implemented(self):
        from zamal_core.job_source_adapter import LinkedInAdapter, QatarLivingAdapter
        from zamal_core.job_schema import JobQuery
        q = JobQuery(keywords=['accountant'])
        for Cls in (LinkedInAdapter, QatarLivingAdapter):
            with self.assertRaises(NotImplementedError):
                Cls().discover_jobs(q)

    def test_cv_profile_provider_disabled(self):
        from zamal_core.cv_profile import CVProfileProvider
        CVProfileProvider.revoke()
        self.assertFalse(CVProfileProvider.is_enabled())
        with self.assertRaises(RuntimeError):
            CVProfileProvider.get_profile()

    def test_job_executor_disabled(self):
        from zamal_core.job_lifecycle import JobApplicationExecutor
        ex = JobApplicationExecutor()
        ex._LIVE_ENABLED = False
        with self.assertRaises(NotImplementedError):
            ex.execute({}, 't', 'a')

    def test_fixture_jobs_have_fixture_metadata(self):
        from zamal_core.job_source_adapter import LinkedInAdapter, QatarLivingAdapter
        for Cls in (LinkedInAdapter, QatarLivingAdapter):
            job = Cls().make_fixture_job()
            self.assertTrue(job.raw_metadata.get('fixture'),
                            f'{Cls.SOURCE_ID} fixture job missing fixture=True marker')


# ── 15. CC HTML contains all Step 4 sections ─────────────────────────────────

class TestCCHTMLJobSections(unittest.TestCase):

    def test_cc_html_has_job_sections(self):
        from zamal_core.control_center import ControlCenterServer
        import socket, time, urllib.request

        def _free_port():
            with socket.socket() as s:
                s.bind(('127.0.0.1', 0))
                return s.getsockname()[1]

        port = _free_port()
        server = ControlCenterServer(port=port)
        server.serve(block=False)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                    break
            except Exception:
                time.sleep(0.1)

        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=5) as r:
                html = r.read().decode('utf-8')
            for section in (
                'Job Sources', 'Job Shortlist', 'Application History',
                '/api/jobs/sources', '/api/jobs/shortlist', '/api/jobs/history',
            ):
                self.assertIn(section, html, f'CC HTML missing Step 4 section: {section!r}')
        finally:
            try:
                server.stop()
            except Exception:
                pass


# ── 16. CV-match scoring component tests ─────────────────────────────────────

class TestCVMatchScoring(unittest.TestCase):
    """Focused tests for the rebuilt CV-match scoring engine."""

    def _make_job(self, title, location='Doha, Qatar', requirements=None, posted_date='2026-09-10'):
        import hashlib, time as _time
        from zamal_core.job_schema import NormalizedJob
        reqs = requirements or ['Financial reporting', 'General ledger', '5+ years experience']
        url = f'https://www.linkedin.com/jobs/view/test-{hashlib.md5(title.encode()).hexdigest()[:6]}/'
        return NormalizedJob(
            job_id=hashlib.sha256(url.encode()).hexdigest()[:16],
            source='linkedin', url=url,
            title=title, company='Test Co', location=location,
            posted_date=posted_date, requirements=reqs,
            application_method='direct',
            fingerprint=hashlib.sha256(f'{title}{location}'.encode()).hexdigest()[:24],
            discovered_at=_time.time(),
        )

    def setUp(self):
        from zamal_core.job_matcher import JobMatcher
        from zamal_core.cv_profile import CVProfileProvider
        self.profile = CVProfileProvider.get_fixture_profile()
        self.matcher = JobMatcher(profile=self.profile)

    # -- Component architecture --

    def test_score_components_sum_to_100(self):
        from zamal_core.job_matcher import SCORE_COMPONENTS
        self.assertEqual(sum(SCORE_COMPONENTS.values()), 100)

    def test_location_not_a_score_component(self):
        from zamal_core.job_matcher import SCORE_COMPONENTS
        self.assertNotIn('location_match', SCORE_COMPONENTS)
        self.assertNotIn('location', SCORE_COMPONENTS)

    def test_recency_not_a_score_component(self):
        from zamal_core.job_matcher import SCORE_COMPONENTS
        self.assertNotIn('recency', SCORE_COMPONENTS)
        self.assertNotIn('recency_rank', SCORE_COMPONENTS)

    def test_recency_rank_is_separate_field(self):
        job = self._make_job('Senior Accountant')
        r = self.matcher.evaluate(job)
        self.assertIsInstance(r.recency_rank, int)
        self.assertNotIn('recency', r.score_breakdown)

    # -- Seniority caps --

    def test_director_finance_below_90(self):
        job = self._make_job(
            'Director of Finance',
            requirements=['ACCA required', '15+ years experience',
                          'Financial reporting', 'IFRS', 'General ledger',
                          'ERP systems', 'Budgeting', 'Reconciliation']
        )
        r = self.matcher.evaluate(job)
        self.assertFalse(r.accepted, f'Director should score <90, got {r.score}')
        self.assertLess(r.score, 90)
        self.assertLessEqual(r.score_breakdown['role_seniority'], 2)

    def test_assistant_director_finance_below_90(self):
        job = self._make_job(
            'Assistant Director of Finance',
            requirements=['10+ years', 'ACCA', 'IFRS', 'Budgeting',
                          'Financial reporting', 'General ledger', 'ERP systems',
                          'Reconciliation', 'Payroll', 'Excel']
        )
        r = self.matcher.evaluate(job)
        self.assertFalse(r.accepted, f'Assistant Director should score <90, got {r.score}')
        self.assertLess(r.score, 90)
        self.assertLessEqual(r.score_breakdown['role_seniority'], 5)

    def test_intern_below_90(self):
        job = self._make_job(
            'Tax Intern',
            requirements=['Currently enrolled', '0-1 years', 'Excel', 'Finance']
        )
        r = self.matcher.evaluate(job)
        self.assertFalse(r.accepted, f'Intern should score <90, got {r.score}')
        self.assertLess(r.score, 90)
        self.assertLessEqual(r.score_breakdown['role_seniority'], 5)

    def test_senior_accountant_seniority_at_least_15(self):
        job = self._make_job('Senior Accountant')
        r = self.matcher.evaluate(job)
        self.assertGreaterEqual(r.score_breakdown['role_seniority'], 15)

    def test_chief_accountant_seniority_at_least_15(self):
        job = self._make_job('Chief Accountant')
        r = self.matcher.evaluate(job)
        self.assertGreaterEqual(r.score_breakdown['role_seniority'], 15)

    def test_accountant_seniority_is_20(self):
        job = self._make_job('Accountant')
        r = self.matcher.evaluate(job)
        self.assertEqual(r.score_breakdown['role_seniority'], 20)

    # -- Education / certification --

    def test_required_cert_absent_caps_education_to_zero(self):
        from zamal_core.cv_profile import CVProfileProvider
        from zamal_core.job_matcher import JobMatcher
        real_profile = CVProfileProvider.load_cached_profile()  # B.Com only, no ACCA/CPA
        matcher = JobMatcher(profile=real_profile)
        job = self._make_job(
            'Senior Accountant',
            requirements=['CPA required', 'Must hold CPA', '5+ years experience',
                          'Financial reporting', 'General ledger', 'Excel',
                          'Accounts payable', 'Bank reconciliation', 'Journal entries']
        )
        r = matcher.evaluate(job)
        self.assertEqual(r.score_breakdown['education_certification'], 0,
                         'Required cert absent must zero out education component')
        self.assertFalse(r.accepted, f'Missing required cert must cap below 90, got {r.score}')
        self.assertLess(r.score, 90)

    def test_preferred_cert_reduces_but_does_not_reject(self):
        """Preferred cert not in profile reduces score but job may still qualify."""
        from zamal_core.cv_profile import CVProfileProvider
        from zamal_core.job_matcher import JobMatcher
        real_profile = CVProfileProvider.load_cached_profile()
        matcher = JobMatcher(profile=real_profile)
        job = self._make_job(
            'Senior Accountant',
            requirements=['CPA preferred', '5+ years experience',
                          'Financial reporting', 'General ledger', 'Excel',
                          'Accounts payable', 'Bank reconciliation']
        )
        r = matcher.evaluate(job)
        # Education score should be > 0 (not hard-capped)
        self.assertGreater(r.score_breakdown['education_certification'], 0)

    def test_required_cert_present_in_fixture_profile_full_edu(self):
        """Fixture profile has ACCA; job requiring ACCA should get full education pts."""
        job = self._make_job(
            'Senior Accountant',
            requirements=['ACCA required', 'Must hold ACCA', '5+ years', 'IFRS',
                          'Financial reporting', 'General ledger']
        )
        r = self.matcher.evaluate(job)
        self.assertEqual(r.score_breakdown['education_certification'], 15)

    # -- Experience years --

    def test_experience_below_required_reduces_score(self):
        """Candidate with 8 years vs 12-year requirement should score lower."""
        from zamal_core.job_matcher import _score_experience
        pts, _ = _score_experience('12+ years of relevant experience', self.profile)
        self.assertLess(pts, 15)  # deficit of 4 → 0 pts

    def test_experience_meets_requirement_scores_well(self):
        from zamal_core.job_matcher import _score_experience
        pts, _ = _score_experience('5+ years of accounting experience', self.profile)
        self.assertGreaterEqual(pts, 22)

    # -- Skills --

    def test_zero_skill_matches_zero_skills_pts(self):
        from zamal_core.job_matcher import _score_skills
        pts, matched = _score_skills('sql python docker kubernetes aws react', self.profile)
        self.assertEqual(pts, 0)
        self.assertEqual(matched, [])

    def test_five_skill_matches_full_skills_pts(self):
        from zamal_core.job_matcher import _score_skills
        # Fixture profile skills: IFRS, QuickBooks, VAT Compliance, Budgeting, Reconciliation
        desc = 'ifrs quickbooks vat compliance budgeting reconciliation financial reporting'
        pts, matched = _score_skills(desc, self.profile)
        self.assertEqual(pts, 35)
        self.assertGreaterEqual(len(matched), 5)


if __name__ == '__main__':
    unittest.main()
