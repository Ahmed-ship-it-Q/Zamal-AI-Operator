import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from zamal_core import Store, Gate, Denied, Conflict
from zamal_core.safety import Controls, FLAGS
from zamal_core.worker import LocalWorker, Paths, CommandPolicy, Runner, MAX_BYTES


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-worker-test-')
        self.root = Path(self.tmp.name).resolve()
        self.operator = self.root / 'operator'
        self.cpc = self.root / 'cpc'
        self.operator.mkdir()
        self.cpc.mkdir()
        self.s = Store(self.operator / 'state/operator.sqlite3')
        self.w = LocalWorker(self.s, operator=self.operator, cpc=self.cpc)
        self.gate = self.w.gate()
        self.seq = 0

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def action(self, kind, payload, project=None):
        self.seq += 1
        t = self.s.create_task('fixture', 'local', project or payload['workspace'], str(self.seq))
        for state in ('UNDERSTOOD', 'PLANNED'):
            t = self.s.transition(t['id'], state, t['revision'])
        return self.gate.propose(t['id'], kind, payload, 'a')

    def execute(self, kind, payload, project=None):
        return self.gate.execute(self.action(kind, payload, project)['id'])

    def control(self, flag, enabled=True):
        return Controls(self.s).set(flag, enabled, actor='test-human', reason='fixture')

    def draft(self, name='draft.md', content='fixture'):
        return self.execute('draft', {'workspace': 'operator', 'name': name, 'content': content})

    def test_read_and_inspect_both_workspaces(self):
        for label, folder in [('operator', self.operator), ('cpc', self.cpc)]:
            (folder / 'README.md').write_text('hello')
            result = self.execute('read', {'workspace': label, 'path': 'README.md'})
            self.assertEqual(result['text'], 'hello')
            result = self.execute('inspect', {'workspace': label, 'path': '.'})
            self.assertEqual(result['entries'], [{'name': 'README.md', 'directory': False}])

    def test_absolute_traversal_protected_and_case_alias_paths_denied(self):
        for path in ['/etc/passwd', '../outside', 'a/../../outside', 'a//b', './README.md',
                     'state/operator.sqlite3', 'STATE/operator.sqlite3', '.env', '.env.example',
                     'config/secret.json', 'Config/other.json', 'keys.pem', 'credentials.json',
                     'node_modules/file.js', 'database/file', 'a\\..\\b', 'a\x00b']:
            with self.subTest(path=path), self.assertRaises(Denied):
                self.execute('read', {'workspace': 'operator', 'path': path})

    def test_prefix_sibling_is_not_workspace(self):
        sibling = self.root / 'operator-other'
        sibling.mkdir()
        (sibling / 'README.md').write_text('outside')
        with self.assertRaises(Denied):
            self.execute('read', {'workspace': str(sibling), 'path': 'README.md'})

    def test_file_and_directory_symlink_and_hardlink_denied(self):
        target = self.root / 'outside.txt'
        target.write_text('secret outside')
        (self.operator / 'link.txt').symlink_to(target)
        (self.operator / 'linked').symlink_to(self.root, target_is_directory=True)
        os.link(target, self.operator / 'hard.txt')
        for path in ('link.txt', 'linked/outside.txt', 'hard.txt'):
            with self.assertRaises(Denied):
                self.execute('read', {'workspace': 'operator', 'path': path})

    def test_parent_swap_after_validation_cannot_escape(self):
        folder = self.operator / 'docs'
        folder.mkdir()
        (folder / 'a.txt').write_text('inside')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'a.txt').write_text('outside')
        a = self.action('read', {'workspace': 'operator', 'path': 'docs/a.txt'})
        original = self.w.handlers['read'].run_with_controls
        def swap(payload, baseline):
            folder.rename(self.operator / 'old-docs')
            folder.symlink_to(outside, target_is_directory=True)
            return original(payload, baseline)
        with patch.object(self.w.handlers['read'], 'run_with_controls', side_effect=swap):
            with self.assertRaises(Denied):
                self.gate.execute(a['id'])
        self.assertEqual((outside / 'a.txt').read_text(), 'outside')

    def test_fifo_and_oversize_files_denied(self):
        os.mkfifo(self.operator / 'pipe')
        (self.operator / 'large.txt').write_bytes(b'x' * (MAX_BYTES + 1))
        for path in ('pipe', 'large.txt'):
            with self.assertRaises(Denied):
                self.execute('read', {'workspace': 'operator', 'path': path})

    def test_secrets_filtered_from_text_results(self):
        (self.operator / 'README.md').write_text('API_KEY=should-not-appear https://user:password@example.test')
        result = self.execute('read', {'workspace': 'operator', 'path': 'README.md'})
        self.assertNotIn('should-not-appear', result['text'])
        self.assertNotIn('user:password', result['text'])

    def test_task_project_binding_denied(self):
        (self.operator / 'a.txt').write_text('hello')
        a = self.action('read', {'workspace': 'operator', 'path': 'a.txt'}, project='cpc')
        with self.assertRaises(Denied):
            self.gate.execute(a['id'])
        self.assertEqual(self.s.action(a['id'])['status'], 'PENDING')

    def test_draft_date_directory_new_file_only_and_manifest(self):
        result = self.draft()
        path = self.operator / result['path']
        self.assertRegex(result['path'], r'^outbox/\d{4}-\d{2}-\d{2}/worker/draft.md$')
        self.assertEqual(path.read_text(), 'fixture')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['sha256'], hashlib.sha256(b'fixture').hexdigest())
        with self.assertRaises(Denied):
            self.draft(content='replacement')
        self.assertEqual(path.read_text(), 'fixture')

    def test_output_redirection_and_source_names_denied(self):
        for name in ('../source.py', '/tmp/file.txt', '.env', 'a.sh', 'auth.json'):
            with self.assertRaises(Denied):
                self.draft(name)
        (self.operator / 'outbox').symlink_to(self.cpc, target_is_directory=True)
        with self.assertRaises(Denied):
            self.draft()
        self.assertEqual(list(self.cpc.iterdir()), [])

    def test_cpc_write_and_delete_denied_even_with_approval(self):
        for kind, p in [('draft', {'workspace': 'cpc', 'name': 'x.md', 'content': 'x'}),
                        ('prepare_code', {'workspace': 'cpc', 'name': 'x.py', 'content': 'x'}),
                        ('delete', {'workspace': 'cpc', 'output_id': 'unknown', 'sha256': 'unknown'})]:
            a = self.action(kind, p)
            if a['approval']:
                self.s.decide(a['approval']['id'], 'APPROVE', actor='fixture-human')
            with self.assertRaises(Denied):
                self.gate.execute(a['id'])
        self.assertEqual(list(self.cpc.iterdir()), [])

    def test_owned_output_delete_requires_approval_and_cannot_replay(self):
        result = self.draft()
        a = self.action('delete', {'workspace': 'operator', 'output_id': result['output_id'], 'sha256': result['sha256']})
        with self.assertRaises(Denied):
            self.gate.execute(a['id'])
        self.assertTrue((self.operator / result['path']).exists())
        self.s.decide(a['approval']['id'], 'APPROVE', actor='fixture-human')
        deleted = self.gate.execute(a['id'])
        self.assertEqual(deleted['deleted_output_id'], result['output_id'])
        self.assertFalse((self.operator / result['path']).exists())
        with self.assertRaises(Denied):
            self.gate.execute(a['id'])

    def test_changed_or_unowned_output_cannot_be_deleted(self):
        result = self.draft()
        (self.operator / result['path']).write_text('changed by someone else')
        a = self.action('delete', {'workspace': 'operator', 'output_id': result['output_id'], 'sha256': result['sha256']})
        self.s.decide(a['approval']['id'], 'APPROVE', actor='fixture-human')
        with self.assertRaises(Denied):
            self.gate.execute(a['id'])
        self.assertEqual(self.s.action(a['id'])['approval']['status'], 'APPROVED')
        self.assertEqual((self.operator / result['path']).read_text(), 'changed by someone else')
        a = self.action('delete', {'workspace': 'operator', 'output_id': 'not-owned', 'sha256': 'unknown'})
        self.s.decide(a['approval']['id'], 'APPROVE', actor='fixture-human')
        with self.assertRaises(Denied):
            self.gate.execute(a['id'])

    def test_unknown_command_and_injected_parameters_denied(self):
        for command in ('sh', 'rm -rf /', 'git push --force', 'git reset --hard', 'npm test', 'python -c', 'node -e', 'node_version; touch /tmp/pwn'):
            with self.assertRaises(Denied):
                self.execute('verify', {'workspace': 'operator', 'command': command})
        for extra in ('argv', 'cwd', 'env', 'executable', 'shell'):
            with self.assertRaises(Denied):
                self.execute('verify', {'workspace': 'operator', 'command': 'node_version', extra: 'injection'})

    def test_python_syntax_never_executes_source(self):
        (self.operator / 'sample.py').write_text('raise RuntimeError("would execute")\n')
        self.assertTrue(self.execute('verify', {'workspace': 'operator', 'command': 'python_syntax', 'path': 'sample.py'})['passed'])
        (self.operator / 'bad.py').write_text('def broken(:')
        self.assertFalse(self.execute('verify', {'workspace': 'operator', 'command': 'python_syntax', 'path': 'bad.py'})['passed'])

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS only')
    def test_real_fixed_version_and_node_syntax_commands(self):
        for command in ('git_version', 'node_version'):
            r = self.execute('verify', {'workspace': 'operator', 'command': command})
            self.assertTrue(r['passed'], r)
        (self.operator / 'sample.js').write_text('throw new Error("must not execute");')
        r = self.execute('verify', {'workspace': 'operator', 'command': 'node_syntax', 'path': 'sample.js'})
        self.assertTrue(r['passed'], r)
        (self.operator / 'bad.js').write_text('const = ;')
        self.assertFalse(self.execute('verify', {'workspace': 'operator', 'command': 'node_syntax', 'path': 'bad.js'})['passed'])

    def test_controls_persist_audit_and_stop_all_capabilities(self):
        self.control('stop_everything')
        self.s.close()
        self.s = Store(self.operator / 'state/operator.sqlite3')
        self.w = LocalWorker(self.s, operator=self.operator, cpc=self.cpc)
        self.gate = self.w.gate()
        self.assertEqual(Controls(self.s).status()['stop_everything'], 1)
        self.assertEqual(Controls(self.s).history()[0]['actor'], 'test-human')
        for kind, p in [('inspect', {'workspace': 'operator', 'path': '.'}),
                        ('draft', {'workspace': 'operator', 'name': 'x.md', 'content': 'x'}),
                        ('verify', {'workspace': 'operator', 'command': 'git_version'})]:
            with self.assertRaises(Denied):
                self.execute(kind, p)
        self.assertFalse((self.operator / 'outbox').exists())

    def test_read_only_allows_reads_blocks_writes(self):
        self.control('read_only_mode')
        self.execute('inspect', {'workspace': 'operator', 'path': '.'})
        with self.assertRaises(Denied):
            self.draft()

    def test_external_and_specific_controls_override_valid_approval(self):
        for flag, kind in [('pause_external_actions', 'deploy'), ('disable_github_push', 'git_push'), ('disable_email_send', 'email_send')]:
            self.control(flag)
            a = self.action(kind, {'workspace': 'operator'})
            self.s.decide(a['approval']['id'], 'APPROVE', actor='fixture-human')
            calls = []
            with self.assertRaises(Denied):
                Gate(self.s, {kind: lambda p: calls.append(p)}).execute(a['id'])
            self.assertEqual(calls, [])
            self.assertEqual(self.s.action(a['id'])['approval']['status'], 'APPROVED')
            self.control(flag, False)
        # External pause must not disable safe local reading.
        self.control('pause_external_actions')
        self.execute('inspect', {'workspace': 'operator', 'path': '.'})

    def test_stop_between_claim_and_dispatch_denies_side_effect(self):
        a = self.action('draft', {'workspace': 'operator', 'name': 'x.md', 'content': 'x'})
        original = self.w.handlers['draft'].run_with_controls
        def stop(payload, baseline):
            self.control('stop_everything')
            return original(payload, baseline)
        with patch.object(self.w.handlers['draft'], 'run_with_controls', side_effect=stop):
            with self.assertRaises(Denied):
                self.gate.execute(a['id'])
        self.assertFalse((self.operator / 'outbox').exists())
        self.assertEqual(self.s.action(a['id'])['status'], 'UNCERTAIN')

    def test_fast_on_off_latches_cancellation_for_inflight_work(self):
        controls = Controls(self.s)
        baseline = controls.check('draft')
        self.control('read_only_mode')
        self.control('read_only_mode', False)
        with self.assertRaises(Denied):
            controls.check('draft', baseline)
        controls.check('inspect', baseline)
        controls.check('draft')

    def test_control_validation_and_audit_immutability(self):
        with self.assertRaises(ValueError):
            Controls(self.s).set('unknown', True, actor='human', reason='test')
        with self.assertRaises(ValueError):
            Controls(self.s).set('stop_everything', 'false', actor='human', reason='test')
        self.control('stop_everything')
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.db.execute('DELETE FROM control_audit')

    def test_unconnected_protected_capabilities_stay_blocked(self):
        for kind in ('git_commit', 'git_push', 'git_reset', 'email_send', 'payment'):
            a = self.action(kind, {'workspace': 'operator'})
            self.s.decide(a['approval']['id'], 'APPROVE', actor='fixture-human')
            with self.assertRaises(Denied):
                self.gate.execute(a['id'])

    def test_failed_command_cannot_complete_without_verification_evidence(self):
        (self.operator / 'bad.py').write_text('invalid syntax !')
        a = self.action('verify', {'workspace': 'operator', 'command': 'python_syntax', 'path': 'bad.py'})
        self.assertFalse(self.gate.execute(a['id'])['passed'])
        t = self.s.task(a['task_id'])
        with self.assertRaises(Denied):
            self.s.transition(t['id'], 'COMPLETED', t['revision'])

    def test_missing_controls_fail_closed_across_restart(self):
        self.s.db.execute('DELETE FROM controls')
        self.s.close()
        self.s = Store(self.operator / 'state/operator.sqlite3')
        with self.assertRaises(Denied):
            Controls(self.s).check('inspect')

    def test_control_resume_never_resets_approved_or_consumed_actions(self):
        result = self.draft()
        a = self.action('delete', {'workspace': 'operator', 'output_id': result['output_id'], 'sha256': result['sha256']})
        self.s.decide(a['approval']['id'], 'APPROVE', actor='fixture-human')
        self.control('read_only_mode')
        with self.assertRaises(Denied):
            self.gate.execute(a['id'])
        self.assertTrue((self.operator / result['path']).exists())
        self.control('read_only_mode', False)
        self.gate.execute(a['id'])
        self.control('stop_everything')
        self.control('stop_everything', False)
        with self.assertRaises(Denied):
            self.gate.execute(a['id'])
        self.assertIn('EXECUTION_DENIED', [e['event'] for e in self.s.task(a['task_id'])['audit']])

    def test_additive_migration_preserves_v1_task_and_approval(self):
        a = self.action('email_send', {'workspace': 'operator'})
        before = self.s.task(a['task_id'])
        for table in ('controls', 'control_audit', 'worker_outputs'):
            self.s.db.execute('DROP TABLE ' + table)
        self.s.db.execute('PRAGMA user_version=1')
        self.s.close()
        self.s = Store(self.operator / 'state/operator.sqlite3')
        self.assertEqual(self.s.task(a['task_id']), before)
        self.assertEqual(self.s.db.execute('PRAGMA user_version').fetchone()[0], 6)


class RunnerTests(unittest.TestCase):
    # Direct trusted fixture argv tests runner mechanics. These commands are NOT
    # registered/available through CommandPolicy or the production worker.
    def test_timeout_and_output_bound(self):
        r = Runner()
        with self.assertRaisesRegex(Denied, 'timeout'):
            r.run([sys.executable, '-c', 'import time; time.sleep(10)'], b'', lambda: None, timeout=0.15)
        with self.assertRaisesRegex(Denied, 'output limit'):
            r.run([sys.executable, '-c', 'print("x"*10000)'], b'', lambda: None, output_limit=100)

    def test_environment_is_clean_and_cwd_fixed(self):
        with patch.dict(os.environ, {'SECRET_FIXTURE': 'must-not-leak', 'NODE_OPTIONS': '--eval=bad'}):
            r = Runner().run([sys.executable, '-c', 'import os,json;print(json.dumps({"secret":os.getenv("SECRET_FIXTURE"),"node":os.getenv("NODE_OPTIONS"),"cwd":os.getcwd()}))'], b'', lambda: None)
        self.assertEqual(json.loads(r['output']), {'secret': None, 'node': None, 'cwd': '/'})

    def test_stop_interrupts_running_process_and_descendants(self):
        with tempfile.TemporaryDirectory(prefix='zamal-stop-test-') as tmp:
            path = Path(tmp) / 'state.sqlite3'
            s = Store(path)
            started = threading.Event()
            def run():
                local = Store(path)
                controls = Controls(local)
                baseline = controls.check('verify')
                started.set()
                try:
                    Runner().run([sys.executable, '-c', 'import subprocess,time; subprocess.Popen(["/bin/sleep","10"]); time.sleep(10)'], b'', lambda: controls.check('verify', baseline))
                finally:
                    local.close()
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(run)
                    self.assertTrue(started.wait(5))
                    time.sleep(0.15)
                    before = time.monotonic()
                    Controls(s).set('stop_everything', True, actor='test-human', reason='fixture')
                    with self.assertRaises(Denied):
                        future.result(timeout=3)
                    self.assertLess(time.monotonic() - before, 2)
            finally:
                s.close()
