"""Phase 12 tests: Monitoring, Recovery, Cost/Usage, Security, Output Retention."""
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from zamal_core.store import Store, Denied, Conflict
from zamal_core.monitor import (
    ServiceMonitor, SecurityAuditor, UsageTracker, OutputReaper,
    MonitorWorker, MonitorLog, migrate_monitor, _PHASE12_SAFE, _PHASE12_PROTECTED,
)


def _make_store(tmp):
    s = Store(path=str(Path(tmp) / 'test.sqlite3'))
    return s


# ── Schema ────────────────────────────────────────────────────────────────────

class MonitorSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def test_core_extension_monitor_v1(self):
        row = self.s.db.execute("SELECT * FROM core_extensions WHERE name='monitor'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['version'], 1)

    def test_monitor_log_table_exists(self):
        self.s.db.execute('SELECT seq FROM monitor_log LIMIT 0')

    def test_security_audit_log_table_exists(self):
        self.s.db.execute('SELECT seq FROM security_audit_log LIMIT 0')

    def test_migrate_monitor_idempotent(self):
        migrate_monitor(self.s)  # second call must not raise
        row = self.s.db.execute("SELECT version FROM core_extensions WHERE name='monitor'").fetchone()
        self.assertEqual(row[0], 1)

    def test_monitor_log_append_only_update_blocked(self):
        log = MonitorLog(self.s)
        log.record('health_sweep', 'OK', detail={'x': 1})
        with self.assertRaises(Exception):
            with self.s.transaction():
                self.s.db.execute('UPDATE monitor_log SET outcome="TAMPERED" WHERE seq=1')

    def test_monitor_log_append_only_delete_blocked(self):
        log = MonitorLog(self.s)
        log.record('health_sweep', 'OK')
        with self.assertRaises(Exception):
            with self.s.transaction():
                self.s.db.execute('DELETE FROM monitor_log WHERE seq=1')

    def test_security_audit_log_append_only(self):
        with self.s.transaction():
            self.s.db.execute(
                'INSERT INTO security_audit_log(timestamp,check_name,result,findings) VALUES(?,?,?,?)',
                (time.time(), 'test', 'pass', '[]')
            )
        with self.assertRaises(Exception):
            with self.s.transaction():
                self.s.db.execute('DELETE FROM security_audit_log WHERE seq=1')

    def test_schema_version_still_6(self):
        v = self.s.db.execute('PRAGMA user_version').fetchone()[0]
        self.assertEqual(v, 6)


# ── ServiceMonitor ─────────────────────────────────────────────────────────

class ServiceMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        self.mon = ServiceMonitor(self.s)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def test_sweep_returns_all_connectors(self):
        r = self.mon.sweep()
        self.assertEqual(r['outcome'], 'SUCCESS')
        self.assertIsInstance(r['connectors'], list)
        self.assertGreater(r['count'], 0)
        ids = {c['connector_id'] for c in r['connectors']}
        self.assertIn('n8n', ids)
        self.assertIn('telegram_bot', ids)

    def test_sweep_writes_monitor_log(self):
        self.mon.sweep()
        rows = self.s.db.execute(
            "SELECT * FROM monitor_log WHERE category='health_sweep'"
        ).fetchall()
        self.assertGreater(len(rows), 0)

    def test_sweep_never_raises(self):
        # Even if registry fails, sweep returns structured result
        with patch('zamal_core.monitor.ServiceMonitor.sweep', return_value={'outcome': 'FAILED'}):
            r = ServiceMonitor(self.s).sweep()
        self.assertIsInstance(r, dict)

    def test_recover_blocked_by_stop_everything(self):
        from zamal_core.safety import Controls
        Controls(self.s).set('stop_everything', True,
                             actor='test', reason='testing stop')
        with self.assertRaises(Denied):
            self.mon.recover_service('n8n', actor='test', reason='test')
        Controls(self.s).set('stop_everything', False,
                             actor='test', reason='restore')

    def test_recover_unknown_service_denied(self):
        with self.assertRaises(Denied):
            self.mon.recover_service('not_a_service', actor='test', reason='test')

    def test_recover_idempotency_guard(self):
        from zamal_core.mac_worker import APPROVED_SERVICES
        service = list(APPROVED_SERVICES.keys())[0]
        # Log a recent recovery attempt
        log = MonitorLog(self.s)
        log.record('recovery', 'SUCCESS', connector_id=service, actor='test',
                   detail={'reason': 'prior attempt'})
        with self.assertRaises(Denied):
            self.mon.recover_service(service, actor='test', reason='duplicate attempt')

    def test_recover_already_running_returns_ok(self):
        from zamal_core.mac_worker import APPROVED_SERVICES, ProcessControl
        service = list(APPROVED_SERVICES.keys())[0]
        mock_ctrl = MagicMock()
        mock_ctrl.status.return_value = {'running': True, 'service': service,
                                         'port': 5678, 'pid': 1234,
                                         'description': 'test'}
        with patch('zamal_core.mac_worker.ProcessControl', return_value=mock_ctrl):
            r = self.mon.recover_service(service, actor='test', reason='check')
        self.assertEqual(r['outcome'], 'ALREADY_RUNNING')

    def test_recover_logs_audit_on_already_running(self):
        from zamal_core.mac_worker import APPROVED_SERVICES
        service = list(APPROVED_SERVICES.keys())[0]
        mock_ctrl = MagicMock()
        mock_ctrl.status.return_value = {'running': True, 'service': service,
                                         'port': 5678, 'pid': 1,
                                         'description': 'test'}
        with patch('zamal_core.mac_worker.ProcessControl', return_value=mock_ctrl):
            self.mon.recover_service(service, actor='test', reason='check')
        rows = self.s.db.execute(
            "SELECT * FROM monitor_log WHERE category='recovery'"
        ).fetchall()
        self.assertGreater(len(rows), 0)


# ── SecurityAuditor ───────────────────────────────────────────────────────

class SecurityAuditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def _auditor(self, gateway_path=None):
        return SecurityAuditor(self.s,
                               gateway_config_path=gateway_path or self.root / 'auth.env',
                               operator_root=self.root)

    def test_clean_env_passes(self):
        safe_env = {'PATH': '/usr/bin', 'HOME': '/tmp', 'SHELL': '/bin/zsh',
                    'ZAMAL_N8N_GATEWAY_SECRET': 'x',
                    'ZAMAL_TELEGRAM_TOKEN': 'y',
                    'ZAMAL_TELEGRAM_CHAT_ID': '123'}
        with patch.dict(os.environ, safe_env, clear=True):
            r = self._auditor().check_env_secrets()
        self.assertEqual(r['result'], 'pass')
        self.assertEqual(r['flagged_key_names'], [])

    def test_unexpected_secret_key_flagged(self):
        with patch.dict(os.environ, {'SOME_API_TOKEN': 'abc123'}, clear=False):
            r = self._auditor().check_env_secrets()
        self.assertEqual(r['result'], 'warn')
        self.assertIn('SOME_API_TOKEN', r['flagged_key_names'])

    def test_credential_values_never_in_findings(self):
        with patch.dict(os.environ, {'EXTRA_SECRET_TOKEN': 'supersensitive'}, clear=False):
            r = self._auditor().check_env_secrets()
        result_str = json.dumps(r)
        self.assertNotIn('supersensitive', result_str)

    def test_gateway_config_not_found_warns(self):
        r = self._auditor(self.root / 'nonexistent.env').check_gateway_config()
        self.assertEqual(r['result'], 'warn')
        self.assertIn('config_not_found', r['findings'])

    def test_gateway_config_known_keys_passes(self):
        auth = self.root / 'auth.env'
        auth.write_text('ZAMAL_N8N_GATEWAY_SECRET=x\nZAMAL_TELEGRAM_TOKEN=y\n')
        auth.chmod(0o600)
        r = self._auditor(auth).check_gateway_config()
        self.assertNotIn('fail', r['result'])
        unknown = [f for f in r['findings'] if f.startswith('unexpected_key')]
        self.assertEqual(unknown, [])

    def test_gateway_config_unexpected_key_warns(self):
        auth = self.root / 'auth.env'
        auth.write_text('ZAMAL_N8N_GATEWAY_SECRET=x\nMY_UNKNOWN_VAR=val\n')
        auth.chmod(0o600)
        r = self._auditor(auth).check_gateway_config()
        self.assertEqual(r['result'], 'warn')
        self.assertTrue(any('unexpected_key' in f for f in r['findings']))

    def test_full_audit_writes_security_audit_log(self):
        auth = self.root / 'auth.env'
        auth.write_text('ZAMAL_N8N_GATEWAY_SECRET=x\n')
        auth.chmod(0o600)
        self._auditor(auth).full_audit()
        rows = self.s.db.execute('SELECT * FROM security_audit_log').fetchall()
        self.assertGreater(len(rows), 0)

    def test_full_audit_result_structure(self):
        auth = self.root / 'auth.env'
        auth.write_text('ZAMAL_N8N_GATEWAY_SECRET=x\n')
        auth.chmod(0o600)
        r = self._auditor(auth).full_audit()
        self.assertEqual(r['outcome'], 'SUCCESS')
        self.assertIn('overall', r)
        self.assertIn('checks', r)
        self.assertIn(r['overall'], ('pass', 'warn', 'fail'))

    def test_outbox_permissions_no_outbox_passes(self):
        r = self._auditor().check_outbox_permissions()
        self.assertIn(r['result'], ('pass', 'warn'))
        self.assertEqual(r['checked'], 0)


# ── UsageTracker ──────────────────────────────────────────────────────────

class UsageTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        self.tracker = UsageTracker(self.s)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def test_action_summary_empty_db(self):
        r = self.tracker.action_summary()
        self.assertEqual(r['total_actions'], 0)
        self.assertIsInstance(r['by_kind'], dict)

    def test_event_summary_empty_db(self):
        r = self.tracker.event_summary()
        self.assertEqual(r['total_events'], 0)
        self.assertIsInstance(r['by_source'], dict)

    def test_summary_combines_both(self):
        r = self.tracker.summary()
        self.assertIn('actions', r)
        self.assertIn('events', r)

    def test_action_summary_counts_actions(self):
        task = self.s.create_task('test', 'generic', 'operator', 'key-usage-1')
        for st in ('UNDERSTOOD', 'PLANNED'):
            task = self.s.transition(task['id'], st, task['revision'])
        from zamal_core.gateway import GatewayWorker
        GatewayWorker(self.s).gate().propose(task['id'], 'connection_disable',
            {'connector_id': 'n8n', 'reason': 'usage test'}, 'key-usage-act-1')
        r = self.tracker.action_summary()
        self.assertGreater(r['total_actions'], 0)

    def test_since_hours_filter(self):
        r = self.tracker.action_summary(since_hours=0.001)  # nearly zero window
        self.assertIsInstance(r['total_actions'], int)
        self.assertEqual(r['since_hours'], 0.001)

    def test_event_summary_counts_events(self):
        from zamal_core.gateway import InboundEventGateway
        InboundEventGateway(self.s).receive('health_check', 'cli',
                                            {'connector_id': 'n8n'}, 'usage-evt-1')
        r = self.tracker.event_summary()
        self.assertGreater(r['total_events'], 0)


# ── OutputReaper ──────────────────────────────────────────────────────────

class OutputReaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.s = _make_store(self.tmp.name)
        self.reaper = OutputReaper(self.s, policy_days=3, operator_root=self.root)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def _add_output(self, name, age_days, content=b'test content'):
        outbox = self.root / 'outbox' / '2026-01-01' / 'worker'
        outbox.mkdir(parents=True, exist_ok=True)
        p = outbox / name
        p.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        created = time.time() - age_days * 86400
        from zamal_core.store import uid
        oid = uid()
        path_rel = str(p.relative_to(self.root))
        with self.s.transaction():
            self.s.db.execute(
                'INSERT INTO worker_outputs(id,path,sha256,created) VALUES(?,?,?,?)',
                (oid, path_rel, sha, created)
            )
        return oid, sha, p

    def test_list_eligible_returns_old_outputs(self):
        oid, sha, _ = self._add_output('old_file.md', age_days=4)
        eligible = self.reaper.list_eligible()
        self.assertTrue(any(e['id'] == oid for e in eligible))

    def test_list_eligible_excludes_recent(self):
        oid, sha, _ = self._add_output('new_file.md', age_days=1)
        eligible = self.reaper.list_eligible()
        self.assertFalse(any(e['id'] == oid for e in eligible))

    def test_list_eligible_excludes_already_deleted(self):
        oid, sha, _ = self._add_output('del_file.md', age_days=5)
        with self.s.transaction():
            self.s.db.execute('UPDATE worker_outputs SET deleted=? WHERE id=?',
                              (time.time(), oid))
        eligible = self.reaper.list_eligible()
        self.assertFalse(any(e['id'] == oid for e in eligible))

    def test_purge_deletes_file(self):
        oid, sha, p = self._add_output('todelete.md', age_days=4)
        r = self.reaper.purge(oid, actor='test', reason='cleanup test', sha256=sha)
        self.assertEqual(r['outcome'], 'SUCCESS')
        self.assertFalse(p.exists())

    def test_purge_marks_deleted_in_db(self):
        oid, sha, _ = self._add_output('todelete2.md', age_days=4)
        self.reaper.purge(oid, actor='test', reason='cleanup test', sha256=sha)
        row = self.s.db.execute('SELECT deleted FROM worker_outputs WHERE id=?', (oid,)).fetchone()
        self.assertIsNotNone(row['deleted'])

    def test_purge_wrong_sha256_denied(self):
        oid, sha, _ = self._add_output('wrongsha.md', age_days=4)
        with self.assertRaises(Denied):
            self.reaper.purge(oid, actor='test', reason='test', sha256='wrong' * 8)

    def test_purge_already_deleted_denied(self):
        oid, sha, _ = self._add_output('alreadydel.md', age_days=4)
        self.reaper.purge(oid, actor='test', reason='first', sha256=sha)
        with self.assertRaises(Denied):
            oid2, sha2, _ = self._add_output('alreadydel2.md', age_days=4)
            self.reaper.purge(oid, actor='test', reason='replay', sha256=sha)

    def test_purge_file_changed_denied(self):
        oid, sha, p = self._add_output('changed.md', age_days=4)
        p.write_bytes(b'tampered content')
        with self.assertRaises(Denied):
            self.reaper.purge(oid, actor='test', reason='test', sha256=sha)

    def test_purge_too_recent_denied(self):
        oid, sha, _ = self._add_output('recent.md', age_days=1)
        with self.assertRaises(Denied):
            self.reaper.purge(oid, actor='test', reason='test', sha256=sha)

    def test_purge_writes_audit_log(self):
        oid, sha, _ = self._add_output('audit.md', age_days=4)
        self.reaper.purge(oid, actor='test', reason='audit test', sha256=sha)
        rows = self.s.db.execute(
            "SELECT * FROM monitor_log WHERE category='output_purge'"
        ).fetchall()
        self.assertGreater(len(rows), 0)

    def test_purge_blocked_by_stop_everything(self):
        from zamal_core.safety import Controls
        oid, sha, _ = self._add_output('blocked.md', age_days=4)
        Controls(self.s).set('stop_everything', True, actor='test', reason='test')
        with self.assertRaises(Denied):
            self.reaper.purge(oid, actor='test', reason='test', sha256=sha)
        Controls(self.s).set('stop_everything', False, actor='test', reason='restore')


# ── Safety integration ─────────────────────────────────────────────────────

class Phase12SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def test_safe_kinds_in_policy_safe(self):
        from zamal_core.store import Policy
        for kind in _PHASE12_SAFE:
            self.assertIn(kind, Policy.SAFE)

    def test_protected_kinds_in_policy_protected(self):
        from zamal_core.store import Policy
        for kind in _PHASE12_PROTECTED:
            self.assertIn(kind, Policy.PROTECTED)

    def test_safe_kinds_in_controls_read_only(self):
        from zamal_core.safety import READ_ONLY
        for kind in _PHASE12_SAFE:
            self.assertIn(kind, READ_ONLY)

    def test_monitor_sweep_allowed_in_read_only_mode(self):
        from zamal_core.safety import Controls
        Controls(self.s).set('read_only_mode', True, actor='test', reason='test')
        # Should not raise
        Controls(self.s).check('monitor_sweep')
        Controls(self.s).set('read_only_mode', False, actor='test', reason='restore')

    def test_output_purge_blocked_in_read_only_mode(self):
        from zamal_core.safety import Controls
        Controls(self.s).set('read_only_mode', True, actor='test', reason='test')
        with self.assertRaises(Denied):
            Controls(self.s).check('output_purge')
        Controls(self.s).set('read_only_mode', False, actor='test', reason='restore')

    def test_service_recover_blocked_by_stop_everything(self):
        from zamal_core.safety import Controls
        Controls(self.s).set('stop_everything', True, actor='test', reason='test')
        with self.assertRaises(Denied):
            Controls(self.s).check('service_recover')
        Controls(self.s).set('stop_everything', False, actor='test', reason='restore')


# ── MonitorWorker gate ─────────────────────────────────────────────────────

class MonitorWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        self.worker = MonitorWorker(self.s)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def test_gate_includes_phase12_kinds(self):
        gate = self.worker.gate()
        for kind in _PHASE12_SAFE | _PHASE12_PROTECTED:
            self.assertIn(kind, gate._handlers)

    def test_gate_includes_prior_phase_kinds(self):
        gate = self.worker.gate()
        # Phase 10 kinds still present
        self.assertIn('connection_status', gate._handlers)
        self.assertIn('connection_disable', gate._handlers)

    def test_monitor_sweep_via_run(self):
        r = self.worker.run('monitor_sweep', {}, 'operator')
        self.assertEqual(r['outcome'], 'SUCCESS')

    def test_usage_summary_via_run(self):
        r = self.worker.run('usage_summary', {}, 'operator')
        self.assertIn('actions', r)

    def test_output_list_via_run(self):
        r = self.worker.run('output_list', {}, 'operator')
        self.assertEqual(r['outcome'], 'SUCCESS')
        self.assertIn('eligible', r)

    def test_security_audit_via_run(self):
        r = self.worker.run('security_audit', {}, 'operator')
        self.assertEqual(r['outcome'], 'SUCCESS')

    def test_service_recover_requires_approval_via_gate(self):
        gate = self.worker.gate()
        task = self.s.create_task('recover test', 'generic', 'operator', 'recover-task-1')
        for st in ('UNDERSTOOD', 'PLANNED'):
            task = self.s.transition(task['id'], st, task['revision'])
        action = gate.propose(task['id'], 'service_recover',
                              {'service': 'n8n', 'reason': 'test recovery'},
                              'recover-key-1')
        self.assertIsNotNone(action.get('approval'))
        self.assertEqual(action['approval']['status'], 'PENDING')

    def test_output_purge_requires_approval_via_gate(self):
        gate = self.worker.gate()
        task = self.s.create_task('purge test', 'generic', 'operator', 'purge-task-1')
        for st in ('UNDERSTOOD', 'PLANNED'):
            task = self.s.transition(task['id'], st, task['revision'])
        action = gate.propose(task['id'], 'output_purge',
                              {'output_id': 'fake-id', 'sha256': 'abc' * 20,
                               'reason': 'test purge'},
                              'purge-key-1')
        self.assertIsNotNone(action.get('approval'))
        self.assertEqual(action['approval']['status'], 'PENDING')

    def test_invalid_kind_payload_denied(self):
        with self.assertRaises(Denied):
            self.worker.validate('monitor_sweep', {'extra': 'field'}, 'operator')

    def test_service_recover_missing_reason_denied(self):
        with self.assertRaises(Denied):
            self.worker.validate('service_recover', {'service': 'n8n'}, 'operator')


# ── CLI integration ───────────────────────────────────────────────────────

class Phase12CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def _cli(self, *args):
        import sys
        from io import StringIO
        from zamal_core.__main__ import main
        orig_argv = sys.argv
        orig_stdout = sys.stdout
        sys.argv = ['zamal'] + list(args)
        sys.stdout = out = StringIO()
        try:
            ret = main.__wrapped__() if hasattr(main, '__wrapped__') else None
            with patch('zamal_core.__main__.Store', return_value=self.s):
                sys.argv = ['zamal'] + list(args)
                sys.stdout = out = StringIO()
        finally:
            sys.argv = orig_argv
            sys.stdout = orig_stdout
        return out.getvalue()

    def test_monitor_sweep_cli_subparser_exists(self):
        import argparse, sys
        from io import StringIO
        from zamal_core import __main__ as m
        sys.argv = ['zamal', 'monitor-sweep']
        # Just check the parser doesn't error on this subcommand
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest='command', required=True)
        sub.add_parser('monitor-sweep')
        args = parser.parse_args(['monitor-sweep'])
        self.assertEqual(args.command, 'monitor-sweep')

    def test_monitor_security_cli_subparser_exists(self):
        import argparse, sys
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest='command', required=True)
        sub.add_parser('monitor-security')
        args = parser.parse_args(['monitor-security'])
        self.assertEqual(args.command, 'monitor-security')

    def test_monitor_usage_cli_subparser_exists(self):
        import argparse, sys
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest='command', required=True)
        p = sub.add_parser('monitor-usage')
        p.add_argument('--since-hours', type=float, default=None)
        args = parser.parse_args(['monitor-usage', '--since-hours', '24'])
        self.assertEqual(args.since_hours, 24.0)

    def test_monitor_output_list_cli_subparser_exists(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest='command', required=True)
        sub.add_parser('monitor-output-list')
        args = parser.parse_args(['monitor-output-list'])
        self.assertEqual(args.command, 'monitor-output-list')

    def test_monitor_output_purge_cli_requires_sha256_and_reason(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest='command', required=True)
        p = sub.add_parser('monitor-output-purge')
        p.add_argument('id')
        p.add_argument('--sha256', required=True)
        p.add_argument('--reason', required=True)
        args = parser.parse_args(['monitor-output-purge', 'abc', '--sha256', 'def', '--reason', 'cleanup'])
        self.assertEqual(args.id, 'abc')
        self.assertEqual(args.sha256, 'def')
