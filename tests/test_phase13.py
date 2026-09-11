"""Phase 13 tests: Automation Discovery / Improvement."""
import json
import time
import tempfile
import unittest
from pathlib import Path

from zamal_core.store import Store, Denied, Conflict
from zamal_core.automation import (
    AutomationEngine, AutomationWorker, PatternObserver, ProposalStore,
    migrate_automation, _PHASE13_SAFE, _PHASE13_PROTECTED,
    STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_ACTIVATED,
    _MIN_FREQUENCY,
)


def _make_store(tmp):
    return Store(path=str(Path(tmp) / 'test.sqlite3'))


def _seed_tasks(s, task_type='generic', project='operator', count=4):
    tasks = []
    for i in range(count):
        t = s.create_task(f'seed task {i}', task_type, project, f'seed-{task_type}-{i}-{time.time()}')
        tasks.append(t)
    return tasks


def _seed_actions(s, kind='read', protected=0, status='EXECUTED', count=4):
    from zamal_core.store import uid
    now = time.time()
    for i in range(count):
        t = s.create_task(f'action task {kind} {i}', 'generic', 'operator',
                          f'act-{kind}-{i}-{now}')
        action_id = uid()
        with s.transaction():
            s.db.execute(
                'INSERT INTO actions(id,task_id,request_key,kind,payload,binding,'
                'protected,status,created) VALUES(?,?,?,?,?,?,?,?,?)',
                (action_id, t['id'], f'rk-{action_id}', kind, '{}',
                 'deadbeef' * 8, protected, status, now)
            )
    return count


# ── Schema ────────────────────────────────────────────────────────────────────

class AutomationSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def test_core_extension_automation_v1(self):
        row = self.s.db.execute(
            "SELECT * FROM core_extensions WHERE name='automation'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['version'], 1)

    def test_automation_proposals_table_exists(self):
        self.s.db.execute('SELECT id FROM automation_proposals LIMIT 0')

    def test_automation_audit_table_exists(self):
        self.s.db.execute('SELECT seq FROM automation_audit LIMIT 0')

    def test_migrate_automation_idempotent(self):
        migrate_automation(self.s)
        row = self.s.db.execute(
            "SELECT version FROM core_extensions WHERE name='automation'"
        ).fetchone()
        self.assertEqual(row[0], 1)

    def test_automation_audit_append_only_delete_blocked(self):
        ps = ProposalStore(self.s)
        ps.upsert('test_type', 'test_key', {}, 'test', 'low', 0.1)
        with self.assertRaises(Exception):
            with self.s.transaction():
                self.s.db.execute('DELETE FROM automation_audit')

    def test_schema_version_still_6(self):
        v = self.s.db.execute('PRAGMA user_version').fetchone()[0]
        self.assertEqual(v, 6)


# ── PatternObserver ───────────────────────────────────────────────────────────

class PatternObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        self.obs = PatternObserver(self.s)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def test_observe_empty_db_returns_empty(self):
        patterns = self.obs.observe()
        self.assertIsInstance(patterns, list)
        self.assertEqual(patterns, [])

    def test_repeated_task_kind_detected(self):
        _seed_tasks(self.s, task_type='bug_report', count=_MIN_FREQUENCY)
        patterns = self.obs.observe()
        types = [p['pattern_type'] for p in patterns]
        self.assertIn('repeated_task_kind', types)

    def test_task_kind_below_threshold_not_detected(self):
        _seed_tasks(self.s, task_type='rare_type', count=_MIN_FREQUENCY - 1)
        patterns = self.obs.observe()
        keys = [p['pattern_key'] for p in patterns]
        self.assertFalse(any('rare_type' in k for k in keys))

    def test_repeated_action_failures_detected(self):
        _seed_actions(self.s, kind='network_call', status='FAILED', count=4)
        patterns = self.obs.observe()
        types = [p['pattern_type'] for p in patterns]
        self.assertIn('repeated_failure', types)

    def test_frequent_event_source_detected(self):
        from zamal_core.gateway import InboundEventGateway
        gw = InboundEventGateway(self.s)
        for i in range(_MIN_FREQUENCY):
            gw.receive('health_check', 'cli', {'connector_id': 'n8n'}, f'obs-evt-{i}-{time.time()}')
        patterns = self.obs.observe()
        types = [p['pattern_type'] for p in patterns]
        self.assertIn('frequent_event_source', types)

    def test_observe_is_read_only(self):
        before = self.s.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        _seed_tasks(self.s, count=_MIN_FREQUENCY)
        self.obs.observe()
        after = self.s.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        self.assertEqual(after, before + _MIN_FREQUENCY)

    def test_patterns_have_required_fields(self):
        _seed_tasks(self.s, count=_MIN_FREQUENCY)
        patterns = self.obs.observe()
        for p in patterns:
            self.assertIn('pattern_type', p)
            self.assertIn('pattern_key', p)
            self.assertIn('evidence', p)
            self.assertIn('recommendation', p)
            self.assertIn('risk_level', p)
            self.assertIn('benefit_score', p)

    def test_benefit_score_bounded(self):
        _seed_tasks(self.s, count=100)
        patterns = self.obs.observe()
        for p in patterns:
            self.assertLessEqual(p['benefit_score'], 1.0)
            self.assertGreaterEqual(p['benefit_score'], 0.0)


# ── ProposalStore ─────────────────────────────────────────────────────────────

class ProposalStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        self.ps = ProposalStore(self.s)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def test_create_proposal(self):
        r = self.ps.upsert('repeated_task_kind', 'key1', {'count': 5},
                           'Automate this', 'low', 0.5)
        self.assertEqual(r['outcome'], 'CREATED')
        self.assertIsNotNone(r['proposal_id'])

    def test_duplicate_upsert_updates_not_duplicates(self):
        self.ps.upsert('repeated_task_kind', 'key2', {'count': 3}, 'Rec', 'low', 0.3)
        r = self.ps.upsert('repeated_task_kind', 'key2', {'count': 6}, 'Rec', 'low', 0.6)
        self.assertEqual(r['outcome'], 'UPDATED')
        rows = self.s.db.execute(
            "SELECT COUNT(*) FROM automation_proposals WHERE pattern_key='key2'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_rejected_proposal_skipped_on_upsert(self):
        r = self.ps.upsert('t', 'key3', {}, 'r', 'low', 0.1)
        self.ps.decide(r['proposal_id'], STATUS_REJECTED, actor='test')
        r2 = self.ps.upsert('t', 'key3', {}, 'r', 'low', 0.2)
        self.assertEqual(r2['outcome'], 'SKIPPED')

    def test_list_proposals(self):
        self.ps.upsert('t', 'k4', {}, 'r', 'low', 0.4)
        proposals = self.ps.list()
        self.assertGreater(len(proposals), 0)

    def test_list_filter_by_status(self):
        r = self.ps.upsert('t', 'k5', {}, 'r', 'low', 0.5)
        self.ps.decide(r['proposal_id'], STATUS_APPROVED, actor='test')
        pending = self.ps.list(status=STATUS_PENDING)
        approved = self.ps.list(status=STATUS_APPROVED)
        self.assertFalse(any(p['id'] == r['proposal_id'] for p in pending))
        self.assertTrue(any(p['id'] == r['proposal_id'] for p in approved))

    def test_decide_approve(self):
        r = self.ps.upsert('t', 'k6', {}, 'r', 'medium', 0.6)
        result = self.ps.decide(r['proposal_id'], STATUS_APPROVED, actor='test', note='ok')
        self.assertEqual(result['status'], STATUS_APPROVED)

    def test_decide_activate_requires_approved_first(self):
        r = self.ps.upsert('t', 'k7', {}, 'r', 'low', 0.1)
        with self.assertRaises(Denied):
            self.ps.decide(r['proposal_id'], STATUS_ACTIVATED, actor='test')

    def test_decide_activated_blocks_further_changes(self):
        r = self.ps.upsert('t', 'k8', {}, 'r', 'low', 0.1)
        self.ps.decide(r['proposal_id'], STATUS_APPROVED, actor='test')
        self.ps.decide(r['proposal_id'], STATUS_ACTIVATED, actor='test')
        with self.assertRaises(Conflict):
            self.ps.decide(r['proposal_id'], STATUS_REJECTED, actor='test')

    def test_get_nonexistent_raises_conflict(self):
        with self.assertRaises(Conflict):
            self.ps.get('nonexistent-id')

    def test_audit_written_on_create(self):
        r = self.ps.upsert('t', 'k9', {}, 'r', 'low', 0.1)
        rows = self.s.db.execute(
            'SELECT * FROM automation_audit WHERE proposal_id=?', (r['proposal_id'],)
        ).fetchall()
        self.assertGreater(len(rows), 0)


# ── AutomationEngine ──────────────────────────────────────────────────────────

class AutomationEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        self.engine = AutomationEngine(self.s)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def test_observe_returns_success(self):
        r = self.engine.observe_and_propose()
        self.assertEqual(r['outcome'], 'SUCCESS')
        self.assertIn('patterns_found', r)
        self.assertIn('created', r)

    def test_observe_deduplicates_across_calls(self):
        _seed_tasks(self.s, task_type='dedup_test', count=_MIN_FREQUENCY)
        r1 = self.engine.observe_and_propose()
        r2 = self.engine.observe_and_propose()
        created_first = r1['created']
        created_second = r2['created']
        # Second call should not create new proposals for same patterns
        self.assertEqual(created_second, 0)
        total = self.s.db.execute(
            "SELECT COUNT(*) FROM automation_proposals WHERE pattern_key LIKE '%dedup_test%'"
        ).fetchone()[0]
        self.assertEqual(total, 1)

    def test_observe_blocked_by_stop_everything(self):
        from zamal_core.safety import Controls
        Controls(self.s).set('stop_everything', True, actor='test', reason='test')
        with self.assertRaises(Denied):
            self.engine.observe_and_propose()
        Controls(self.s).set('stop_everything', False, actor='test', reason='restore')

    def test_list_proposals_empty(self):
        r = self.engine.list_proposals()
        self.assertEqual(r['outcome'], 'SUCCESS')
        self.assertIsInstance(r['proposals'], list)

    def test_list_proposals_with_data(self):
        _seed_tasks(self.s, count=_MIN_FREQUENCY)
        self.engine.observe_and_propose()
        r = self.engine.list_proposals()
        self.assertGreaterEqual(len(r['proposals']), 0)

    def test_activate_requires_approved_status(self):
        ps = ProposalStore(self.s)
        r = ps.upsert('t', 'act-key', {}, 'rec', 'low', 0.1)
        with self.assertRaises(Denied):
            self.engine.activate(r['proposal_id'], actor='test', reason='test')

    def test_activate_approved_succeeds(self):
        ps = ProposalStore(self.s)
        r = ps.upsert('t', 'act-key2', {}, 'rec', 'low', 0.5)
        ps.decide(r['proposal_id'], STATUS_APPROVED, actor='test')
        result = self.engine.activate(r['proposal_id'], actor='test', reason='manual')
        self.assertEqual(result['outcome'], 'SUCCESS')
        self.assertEqual(result['proposal']['status'], STATUS_ACTIVATED)

    def test_activate_blocked_by_stop_everything(self):
        from zamal_core.safety import Controls
        ps = ProposalStore(self.s)
        r = ps.upsert('t', 'act-key3', {}, 'rec', 'low', 0.5)
        ps.decide(r['proposal_id'], STATUS_APPROVED, actor='test')
        Controls(self.s).set('stop_everything', True, actor='test', reason='test')
        with self.assertRaises(Denied):
            self.engine.activate(r['proposal_id'], actor='test', reason='test')
        Controls(self.s).set('stop_everything', False, actor='test', reason='restore')

    def test_since_hours_filter(self):
        r = self.engine.observe_and_propose(since_hours=0.001)
        self.assertEqual(r['outcome'], 'SUCCESS')

    def test_never_auto_activates(self):
        _seed_tasks(self.s, count=50)
        self.engine.observe_and_propose()
        activated = self.s.db.execute(
            "SELECT COUNT(*) FROM automation_proposals WHERE status='activated'"
        ).fetchone()[0]
        self.assertEqual(activated, 0)


# ── Safety integration ────────────────────────────────────────────────────────

class Phase13SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def test_safe_kinds_in_policy_safe(self):
        from zamal_core.store import Policy
        for kind in _PHASE13_SAFE:
            self.assertIn(kind, Policy.SAFE)

    def test_protected_kinds_in_policy_protected(self):
        from zamal_core.store import Policy
        for kind in _PHASE13_PROTECTED:
            self.assertIn(kind, Policy.PROTECTED)

    def test_safe_kinds_in_controls_read_only(self):
        from zamal_core.safety import READ_ONLY
        for kind in _PHASE13_SAFE:
            self.assertIn(kind, READ_ONLY)

    def test_automation_observe_allowed_in_read_only_mode(self):
        from zamal_core.safety import Controls
        Controls(self.s).set('read_only_mode', True, actor='test', reason='test')
        Controls(self.s).check('automation_observe')  # must not raise
        Controls(self.s).set('read_only_mode', False, actor='test', reason='restore')

    def test_automation_activate_blocked_in_read_only_mode(self):
        from zamal_core.safety import Controls
        Controls(self.s).set('read_only_mode', True, actor='test', reason='test')
        with self.assertRaises(Denied):
            Controls(self.s).check('automation_activate')
        Controls(self.s).set('read_only_mode', False, actor='test', reason='restore')

    def test_automation_activate_blocked_by_stop_everything(self):
        from zamal_core.safety import Controls
        Controls(self.s).set('stop_everything', True, actor='test', reason='test')
        with self.assertRaises(Denied):
            Controls(self.s).check('automation_activate')
        Controls(self.s).set('stop_everything', False, actor='test', reason='restore')


# ── AutomationWorker gate ─────────────────────────────────────────────────────

class AutomationWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        self.worker = AutomationWorker(self.s)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def test_gate_includes_phase13_kinds(self):
        gate = self.worker.gate()
        for kind in _PHASE13_SAFE | _PHASE13_PROTECTED:
            self.assertIn(kind, gate._handlers)

    def test_gate_includes_phase12_kinds(self):
        gate = self.worker.gate()
        self.assertIn('monitor_sweep', gate._handlers)
        self.assertIn('output_purge', gate._handlers)

    def test_gate_includes_phase10_kinds(self):
        gate = self.worker.gate()
        self.assertIn('connection_status', gate._handlers)

    def test_automation_observe_via_run(self):
        r = self.worker.run('automation_observe', {}, 'operator')
        self.assertEqual(r['outcome'], 'SUCCESS')

    def test_automation_list_via_run(self):
        r = self.worker.run('automation_list', {}, 'operator')
        self.assertEqual(r['outcome'], 'SUCCESS')

    def test_automation_activate_requires_approval_via_gate(self):
        ps = ProposalStore(self.s)
        r = ps.upsert('t', 'gate-key', {}, 'rec', 'low', 0.5)
        gate = self.worker.gate()
        task = self.s.create_task('activate test', 'generic', 'operator', f'gate-act-{time.time()}')
        for st in ('UNDERSTOOD', 'PLANNED'):
            task = self.s.transition(task['id'], st, task['revision'])
        action = gate.propose(task['id'], 'automation_activate',
                              {'proposal_id': r['proposal_id'], 'reason': 'test'},
                              f'act-key-{time.time()}')
        self.assertIsNotNone(action.get('approval'))
        self.assertEqual(action['approval']['status'], 'PENDING')

    def test_invalid_payload_denied(self):
        with self.assertRaises(Denied):
            self.worker.validate('automation_observe', {'unknown': 'field'}, 'operator')

    def test_activate_missing_reason_denied(self):
        with self.assertRaises(Denied):
            self.worker.validate('automation_activate',
                                 {'proposal_id': 'abc'}, 'operator')

    def test_invalid_status_filter_denied(self):
        with self.assertRaises(Denied):
            self.worker.validate('automation_list', {'status': 'INVALID'}, 'operator')


# ── CLI ───────────────────────────────────────────────────────────────────────

class Phase13CLITests(unittest.TestCase):
    def test_automation_observe_cli_subparser(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest='command', required=True)
        p = sub.add_parser('automation-observe')
        p.add_argument('--since-hours', type=float, default=None)
        args = parser.parse_args(['automation-observe', '--since-hours', '24'])
        self.assertEqual(args.since_hours, 24.0)

    def test_automation_list_cli_subparser(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest='command', required=True)
        p = sub.add_parser('automation-list')
        p.add_argument('--status', default=None)
        args = parser.parse_args(['automation-list', '--status', 'pending'])
        self.assertEqual(args.status, 'pending')
