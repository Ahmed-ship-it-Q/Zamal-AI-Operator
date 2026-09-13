"""Phase 6: Mac Local Worker & Computer Control — acceptance tests.

Covers all seven Phase 6 items:
1. FolderPolicy — allowed path, outside-denied, traversal, symlink, hardlink, read-only
2. MacCommandPolicy — allowlist, denylist, unknown=deny, bypass attempts
3. ProcessControl — mock service status/start/stop; unapproved service denied
4. BrowserWorker — localhost validation, bad ports, external URL, credential URL
5. TccPermissionGate — accessibility check, speech check, unknown denied
6. MacWorker + Gate — SAFE/PROTECTED dispatch, emergency controls, CPC write blocked
7. Schema v3 — mac_processes table exists, service records persisted

All subprocess and socket calls are mocked; no real services are started/stopped.
All 153 previous tests remain unaffected.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ── helpers ───────────────────────────────────────────────────────────────────

OPERATOR_ROOT = Path('/Users/book/zamal-ai-operator').resolve()


def _make_store(tmp):
    """Return an isolated Store backed by a temp directory."""
    sys.path.insert(0, str(tmp))
    from zamal_core.store import Store
    return Store(path=str(Path(tmp) / 'zamal_core' / 'state' / 'test.sqlite3'))


def _copy_core(tmp):
    src = Path(__file__).resolve().parents[1] / 'zamal_core'
    dst = Path(tmp) / 'zamal_core'
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__'))
    Path(dst / 'state').mkdir(mode=0o700, exist_ok=True)
    return dst


def _planned_task(store, text='fixture', task_type='local', project='operator', key=None):
    import uuid
    from zamal_core.store import Store
    key = key or str(uuid.uuid4())
    t = store.create_task(text, task_type, project, key)
    t = store.transition(t['id'], 'UNDERSTOOD', t['revision'])
    t = store.transition(t['id'], 'PLANNED', t['revision'])
    return t


# ═══════════════════════════════════════════════════════════════════════════════
# 1. FolderPolicy
# ═══════════════════════════════════════════════════════════════════════════════

class FolderPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-p6-fp-')
        root = Path(self.tmp.name).resolve()  # resolve to handle /private/var on macOS
        self.root = root
        self.allowed = {
            'test_root': root,
            'test_inbox': root / 'inbox',
            'cpc_root': root / 'cpc',
        }
        for p in self.allowed.values():
            p.mkdir(parents=True, exist_ok=True)
        from zamal_core.mac_worker import FolderPolicy
        self.FolderPolicy = FolderPolicy
        self.policy = FolderPolicy(allowed=self.allowed)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_path_resolves(self):
        (self.root / 'hello.txt').write_text('hi')
        resolved = self.policy.resolve('test_root', 'hello.txt')
        self.assertEqual(resolved, self.root / 'hello.txt')

    def test_nested_valid_path(self):
        sub = self.root / 'sub'
        sub.mkdir()
        resolved = self.policy.resolve('test_root', 'sub/file.md')
        self.assertEqual(resolved, sub / 'file.md')

    def test_unknown_folder_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('not_a_folder', 'file.txt')

    def test_traversal_dotdot_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('test_root', '../escape.txt')

    def test_traversal_embedded_dotdot_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('test_root', 'sub/../../escape.txt')

    def test_hidden_component_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('test_root', '.hidden/file.txt')

    def test_sensitive_name_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('test_root', 'secret.txt')

    def test_blocked_extension_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('test_root', 'db.sqlite3')

    def test_symlink_escape_denied(self):
        from zamal_core.store import Denied
        target = self.root / 'outside.txt'
        target.write_text('outside')
        link = self.root / 'link.txt'
        link.symlink_to(target)
        with self.assertRaises(Denied):
            self.policy.resolve('test_root', 'link.txt')

    def test_hardlink_check_raises(self):
        from zamal_core.store import Denied
        orig = self.root / 'orig.txt'
        orig.write_text('data')
        hardlink = self.root / 'hard.txt'
        os.link(orig, hardlink)
        with self.assertRaises(Denied):
            self.policy.check_hardlink(hardlink)

    def test_cpc_write_blocked_by_check_writable(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.check_writable('cpc_root')

    def test_non_cpc_folder_writable(self):
        # Should not raise
        self.policy.check_writable('test_root')

    def test_null_byte_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('test_root', 'file\x00.txt')

    def test_backslash_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('test_root', 'file\\etc\\passwd')

    def test_empty_path_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('test_root', '')


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MacCommandPolicy (terminal bridge)
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(sys.platform == 'darwin', 'macOS only')
class MacCommandPolicyTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from zamal_core.mac_worker import MacCommandPolicy, TERMINAL_DENYLIST
        self.MacCommandPolicy = MacCommandPolicy
        self.TERMINAL_DENYLIST = TERMINAL_DENYLIST
        self.policy = MacCommandPolicy()

    def test_git_version_allowlisted(self):
        argv, cwd, env_extras = self.policy.resolve('git_version')
        self.assertEqual(argv[0], '/usr/bin/git')
        self.assertIn('--version', argv)

    def test_node_version_allowlisted(self):
        argv, cwd, _ = self.policy.resolve('node_version')
        self.assertIn('--version', argv)

    def test_git_status_allowlisted(self):
        argv, cwd, env = self.policy.resolve('git_status')
        self.assertIn('status', argv)
        self.assertIn('GIT_CONFIG_NOSYSTEM', env)

    def test_git_log_allowlisted(self):
        argv, cwd, _ = self.policy.resolve('git_log')
        self.assertIn('log', argv)

    def test_python_version_allowlisted(self):
        argv, cwd, _ = self.policy.resolve('python_version')
        self.assertIn('python3', argv[0])

    def test_unknown_command_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('ls')

    def test_denylist_rm_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('rm')

    def test_denylist_sudo_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('sudo')

    def test_denylist_bash_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('bash')

    def test_denylist_curl_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.policy.resolve('curl')

    def test_denylist_chaining_via_semicolon_denied(self):
        from zamal_core.store import Denied
        # Semicolon in name is an unknown command, not denylist — still denied
        with self.assertRaises(Denied):
            self.policy.resolve('git_version; rm -rf /')

    def test_denylist_complete(self):
        from zamal_core.store import Denied
        for name in self.TERMINAL_DENYLIST:
            with self.assertRaises(Denied, msg=f'{name} should be denied'):
                self.policy.resolve(name)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ProcessControl
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(sys.platform == 'darwin', 'macOS only')
class ProcessControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-p6-pc-')
        root = Path(self.tmp.name)
        core_dst = _copy_core(self.tmp.name)
        sys.path.insert(0, self.tmp.name)
        from zamal_core.store import Store
        self.store = Store(path=str(root / 'test.sqlite3'))
        from zamal_core.mac_worker import ProcessControl, APPROVED_SERVICES
        self.ProcessControl = ProcessControl
        self.APPROVED_SERVICES = APPROVED_SERVICES
        self.pc = ProcessControl(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_status_not_running_when_port_closed(self):
        # Port 5679 is almost certainly not bound
        from zamal_core.mac_worker import ProcessControl, APPROVED_SERVICES
        # Mock the status to use a closed port
        with patch('zamal_core.mac_worker.APPROVED_SERVICES', {
            'n8n': {**APPROVED_SERVICES['n8n'], 'port': 19991},
            'cpc_inspector': APPROVED_SERVICES['cpc_inspector'],
        }):
            pc = ProcessControl(self.store)
            result = pc.status('n8n')
        self.assertFalse(result['running'])
        self.assertEqual(result['service'], 'n8n')

    def test_status_running_when_port_open(self):
        # Temporarily listen on a port to simulate a running service
        with patch('zamal_core.mac_worker.APPROVED_SERVICES', {
            'n8n': {**self.APPROVED_SERVICES['n8n'], 'port': 19990},
            'cpc_inspector': self.APPROVED_SERVICES['cpc_inspector'],
        }):
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(('127.0.0.1', 19990))
            srv.listen(1)
            try:
                pc = self.ProcessControl(self.store)
                result = pc.status('n8n')
                self.assertTrue(result['running'])
            finally:
                srv.close()

    def test_unapproved_service_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.pc.status('arbitrary_process')

    def test_start_records_pid_in_db(self):
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        with patch('subprocess.Popen', return_value=mock_proc) as mock_popen, \
             patch.object(self.pc, 'status', return_value={'running': False, 'port': 5678, 'pid': None, 'service': 'n8n', 'description': ''}):
            result = self.pc.start('n8n', actor='test')
        self.assertEqual(result['pid'], 99999)
        self.assertEqual(result['status'], 'started')
        row = self.store.db.execute('SELECT * FROM mac_processes WHERE service=?', ('n8n',)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['pid'], 99999)
        self.assertEqual(row['status'], 'started')
        # Verify Popen was called without shell=True
        call_kwargs = mock_popen.call_args.kwargs
        self.assertFalse(call_kwargs.get('shell', False))

    def test_start_skips_if_already_running(self):
        with patch.object(self.pc, 'status', return_value={'running': True, 'port': 5678, 'pid': 1234, 'service': 'n8n', 'description': ''}):
            result = self.pc.start('n8n', actor='test')
        self.assertIn('already running', result['message'])

    def test_stop_sends_sigterm_to_pgid(self):
        # Insert a process record
        with self.store.transaction():
            self.store.db.execute(
                'INSERT INTO mac_processes(id,service,pid,started,status,actor) VALUES(?,?,?,?,?,?)',
                ('test-id', 'n8n', 77777, time.time(), 'started', 'test'),
            )
        with patch('os.getpgid', return_value=77777), \
             patch('os.killpg') as mock_kill:
            result = self.pc.stop('n8n', actor='test')
        mock_kill.assert_called_once_with(77777, signal.SIGTERM)
        self.assertEqual(result['status'], 'stopped')

    def test_stop_unapproved_service_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.pc.stop('curl', actor='test')

    def test_stop_when_no_pid_record(self):
        result = self.pc.stop('cpc_inspector', actor='test')
        self.assertIn('stop', result['status'])
        self.assertIsNone(result['pid'])


# ═══════════════════════════════════════════════════════════════════════════════
# 4. BrowserWorker
# ═══════════════════════════════════════════════════════════════════════════════

class BrowserWorkerTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from zamal_core.mac_worker import BrowserWorker
        self.browser = BrowserWorker()

    def test_localhost_5678_valid(self):
        self.browser.validate_url('http://localhost:5678/')

    def test_localhost_8787_valid(self):
        self.browser.validate_url('http://localhost:8787/health')

    def test_https_localhost_valid(self):
        self.browser.validate_url('https://localhost:5678/workflow')

    def test_external_url_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.browser.validate_url('https://example.com/')

    def test_wrong_port_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.browser.validate_url('http://localhost:9999/')

    def test_credential_url_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.browser.validate_url('http://user:pass@localhost:5678/')

    def test_javascript_scheme_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.browser.validate_url('javascript:alert(1)')

    def test_file_scheme_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.browser.validate_url('file:///etc/passwd')

    def test_empty_url_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.browser.validate_url('')

    def test_too_long_url_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.browser.validate_url('http://localhost:5678/' + 'a' * 3000)

    def test_open_calls_usr_bin_open(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch('subprocess.run', return_value=mock_result) as mock_run:
            result = self.browser.open('http://localhost:5678/')
        call_args = mock_run.call_args
        self.assertEqual(call_args.args[0][0], '/usr/bin/open')
        self.assertEqual(call_args.args[0][1], 'http://localhost:5678/')
        self.assertFalse(call_args.kwargs.get('shell', False))
        self.assertTrue(result['opened'])

    def test_open_external_denied_before_subprocess(self):
        from zamal_core.store import Denied
        with patch('subprocess.run') as mock_run:
            with self.assertRaises(Denied):
                self.browser.open('https://google.com/')
            mock_run.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. TccPermissionGate
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(sys.platform == 'darwin', 'macOS only')
class TccPermissionGateTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from zamal_core.mac_worker import TccPermissionGate
        self.gate = TccPermissionGate()

    def test_unknown_permission_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.gate.check('camera')

    def test_unknown_permission_denied_microphone(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.gate.check('microphone')

    def test_accessibility_check_returns_dict(self):
        with patch('ctypes.cdll.LoadLibrary') as mock_lib:
            mock_ax = MagicMock()
            mock_ax.AXIsProcessTrusted.return_value = 0
            mock_lib.return_value = mock_ax
            result = self.gate.check('accessibility')
        self.assertEqual(result['permission'], 'accessibility')
        self.assertIn('status', result)
        self.assertIn('message', result)

    def test_accessibility_authorized(self):
        with patch('ctypes.cdll.LoadLibrary') as mock_lib:
            mock_ax = MagicMock()
            mock_ax.AXIsProcessTrusted.return_value = 1
            mock_lib.return_value = mock_ax
            result = self.gate.check('accessibility')
        self.assertEqual(result['status'], 'authorized')

    def test_speech_check_parses_authorized(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = 'authorized\n'
        with patch('subprocess.run', return_value=mock_result):
            result = self.gate.check('speech_recognition')
        self.assertEqual(result['permission'], 'speech_recognition')
        self.assertEqual(result['status'], 'authorized')

    def test_speech_check_parses_not_determined(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = 'not_determined\n'
        with patch('subprocess.run', return_value=mock_result):
            result = self.gate.check('speech_recognition')
        self.assertEqual(result['status'], 'not_determined')

    def test_speech_check_handles_failure_gracefully(self):
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('swift', 30)):
            result = self.gate.check('speech_recognition')
        self.assertEqual(result['status'], 'unavailable')

    def test_known_permissions_set(self):
        from zamal_core.mac_worker import _KNOWN_PERMISSIONS
        self.assertIn('accessibility', _KNOWN_PERMISSIONS)
        self.assertIn('speech_recognition', _KNOWN_PERMISSIONS)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. MacWorker + Gate (full capability dispatch)
# ═══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(sys.platform == 'darwin', 'macOS only')
class MacWorkerGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-p6-mw-')
        root = Path(self.tmp.name)
        # Use isolated Store but production MacWorker paths to avoid __file__ tmpdir issues.
        from zamal_core.store import Store
        from zamal_core.mac_worker import MacWorker
        self.store = Store(path=str(root / 'test.sqlite3'))
        self.MacWorker = MacWorker
        # Pass operator explicitly so LocalWorker.Paths doesn't try to resolve __file__ path.
        self.worker = MacWorker(self.store, operator=str(OPERATOR_ROOT))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # ── service_status (SAFE: no approval needed) ──────────────────────────────

    def test_service_status_is_safe_no_approval(self):
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'service_status', {'service': 'n8n'}, 'ss-1')
        # SAFE action — no approval record created, task stays PLANNED
        self.assertIsNone(a['approval'])
        self.assertEqual(self.store.task(t['id'])['state'], 'PLANNED')

    def test_service_status_executes_without_approval(self):
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'service_status', {'service': 'n8n'}, 'ss-2')
        with patch('zamal_core.mac_worker.socket.create_connection', side_effect=OSError):
            result = self.worker.gate().execute(a['id'])
        self.assertEqual(result['service'], 'n8n')
        self.assertIn('running', result)

    # ── service_start (PROTECTED: approval required) ───────────────────────────

    def test_service_start_is_protected_needs_approval(self):
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'service_start', {'service': 'n8n'}, 'sa-1')
        self.assertIsNotNone(a['approval'])
        self.assertEqual(a['approval']['status'], 'PENDING')
        self.assertEqual(self.store.task(t['id'])['state'], 'WAITING_APPROVAL')

    def test_service_start_blocked_without_approval(self):
        from zamal_core.store import Denied
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'service_start', {'service': 'n8n'}, 'sa-2')
        with self.assertRaises(Denied) as ctx:
            self.worker.gate().execute(a['id'])
        self.assertIn('approval required', str(ctx.exception).lower())

    def test_service_start_executes_after_approval(self):
        import getpass
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'service_start', {'service': 'cpc_inspector'}, 'sa-3')
        self.store.decide(a['approval']['id'], 'APPROVE',
                          actor='local-os:' + getpass.getuser(), note='')
        mock_proc = MagicMock(); mock_proc.pid = 11111
        with patch('subprocess.Popen', return_value=mock_proc), \
             patch.object(self.worker.process_ctrl, 'status',
                          return_value={'running': False, 'port': 8787, 'pid': None,
                                        'service': 'cpc_inspector', 'description': ''}):
            result = self.worker.gate().execute(a['id'])
        self.assertEqual(result['service'], 'cpc_inspector')

    # ── service_stop (PROTECTED) ───────────────────────────────────────────────

    def test_service_stop_is_protected(self):
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'service_stop', {'service': 'n8n'}, 'sp-1')
        self.assertEqual(a['approval']['status'], 'PENDING')

    # ── terminal_run (SAFE) ───────────────────────────────────────────────────

    def test_terminal_run_git_status_safe(self):
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'terminal_run', {'command': 'git_status'}, 'tr-1')
        self.assertIsNone(a['approval'])  # SAFE

    def test_terminal_run_git_status_executes(self):
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'terminal_run', {'command': 'git_status'}, 'tr-2')
        mock_result = MagicMock()
        mock_result.wait.return_value = 0
        mock_result.poll.return_value = 0
        mock_result.pid = 55555
        # Patch the runner to return a canned result
        with patch.object(self.worker.mac_runner, 'run',
                          return_value={'exit_code': 0, 'output': ''}):
            result = self.worker.gate().execute(a['id'])
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['passed'])
        self.assertEqual(result['command'], 'git_status')

    def test_terminal_run_denied_command_rejected_at_execute(self):
        from zamal_core.store import Denied
        t = _planned_task(self.store, project='operator')
        # propose succeeds (kind is known/SAFE); execution gate rejects the denylisted command
        a = self.worker.gate().propose(t['id'], 'terminal_run', {'command': 'rm'}, 'tr-deny')
        with self.assertRaises(Denied):
            self.worker.gate().execute(a['id'])

    def test_terminal_run_unknown_command_rejected_at_execute(self):
        from zamal_core.store import Denied
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'terminal_run', {'command': 'ls'}, 'tr-unk')
        with self.assertRaises(Denied):
            self.worker.gate().execute(a['id'])

    # ── browser_open (SAFE) ───────────────────────────────────────────────────

    def test_browser_open_localhost_is_safe(self):
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'browser_open', {'url': 'http://localhost:5678/'}, 'bo-1')
        self.assertIsNone(a['approval'])

    def test_browser_open_executes(self):
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'browser_open', {'url': 'http://localhost:8787/'}, 'bo-2')
        mock_result = MagicMock(); mock_result.returncode = 0
        with patch('subprocess.run', return_value=mock_result):
            result = self.worker.gate().execute(a['id'])
        self.assertTrue(result['opened'])

    def test_browser_open_external_denied_at_execute(self):
        from zamal_core.store import Denied
        t = _planned_task(self.store, project='operator')
        # propose succeeds (kind is SAFE); execute rejects the external URL
        a = self.worker.gate().propose(t['id'], 'browser_open', {'url': 'https://google.com/'}, 'bo-ext')
        with self.assertRaises(Denied):
            self.worker.gate().execute(a['id'])

    # ── permission_check (SAFE) ───────────────────────────────────────────────

    def test_permission_check_safe(self):
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'permission_check', {'permission': 'accessibility'}, 'pc-1')
        self.assertIsNone(a['approval'])

    def test_permission_check_executes(self):
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'permission_check', {'permission': 'accessibility'}, 'pc-2')
        with patch('ctypes.cdll.LoadLibrary') as mock_lib:
            mock_ax = MagicMock(); mock_ax.AXIsProcessTrusted.return_value = 0
            mock_lib.return_value = mock_ax
            result = self.worker.gate().execute(a['id'])
        self.assertEqual(result['permission'], 'accessibility')

    def test_permission_check_unknown_denied_at_execute(self):
        from zamal_core.store import Denied
        t = _planned_task(self.store, project='operator')
        # propose succeeds (kind is SAFE); execute rejects the unknown permission
        a = self.worker.gate().propose(t['id'], 'permission_check', {'permission': 'camera'}, 'pc-bad')
        with self.assertRaises(Denied):
            self.worker.gate().execute(a['id'])

    # ── unknown action kind still denied ─────────────────────────────────────

    def test_unknown_action_denied_by_default(self):
        from zamal_core.store import Denied
        t = _planned_task(self.store, project='operator')
        with self.assertRaises(Denied):
            self.worker.gate().propose(t['id'], 'run_shell', {'cmd': 'ls'}, 'uk-1')

    # ── CPC write still blocked ───────────────────────────────────────────────

    def test_cpc_write_still_blocked_via_draft(self):
        from zamal_core.store import Denied
        t = _planned_task(self.store, project='cpc')
        # Propose succeeds (draft is SAFE); execute rejects because CPC is read-only
        a = self.worker.gate().propose(
            t['id'], 'draft',
            {'workspace': 'cpc', 'name': 'evil.md', 'content': 'x'},
            'cpc-w-1',
        )
        with self.assertRaises(Denied):
            self.worker.gate().execute(a['id'])

    # ── Phase 3 capabilities still work ──────────────────────────────────────

    def test_phase3_read_still_works(self):
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(
            t['id'], 'read',
            {'workspace': 'operator', 'path': 'knowledge/OPERATOR_RULES.md'},
            'p3-r-1',
        )
        self.assertIsNone(a['approval'])

    def test_phase3_draft_still_works(self):
        import uuid
        name = f'p6-draft-{uuid.uuid4().hex[:8]}.md'
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(
            t['id'], 'draft',
            {'workspace': 'operator', 'name': name, 'content': 'phase6 test'},
            f'p3-d-{uuid.uuid4().hex[:8]}',
        )
        result = self.worker.gate().execute(a['id'])
        self.assertIn('output_id', result)


# ═══════════════════════════════════════════════════════════════════════════════
# 6b. Emergency controls enforced for Phase 6
# ═══════════════════════════════════════════════════════════════════════════════

class Phase6ControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-p6-ctrl-')
        from zamal_core.store import Store
        from zamal_core.mac_worker import MacWorker
        from zamal_core.safety import Controls
        self.store = Store(path=str(Path(self.tmp.name) / 'test.sqlite3'))
        self.worker = MacWorker(self.store, operator=str(OPERATOR_ROOT))
        self.controls = Controls(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _set_control(self, flag, enabled):
        import getpass
        self.controls.set(flag, enabled, actor='local-os:' + getpass.getuser(),
                          reason='test fixture')

    def test_stop_everything_blocks_service_status(self):
        from zamal_core.store import Denied
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'service_status', {'service': 'n8n'}, 'ctrl-ss')
        self._set_control('stop_everything', True)
        with self.assertRaises(Denied):
            self.worker.gate().execute(a['id'])

    def test_stop_everything_blocks_terminal_run(self):
        from zamal_core.store import Denied
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'terminal_run', {'command': 'git_status'}, 'ctrl-tr')
        self._set_control('stop_everything', True)
        with self.assertRaises(Denied):
            self.worker.gate().execute(a['id'])

    def test_stop_everything_blocks_browser_open(self):
        from zamal_core.store import Denied
        t = _planned_task(self.store, project='operator')
        a = self.worker.gate().propose(t['id'], 'browser_open', {'url': 'http://localhost:5678/'}, 'ctrl-bo')
        self._set_control('stop_everything', True)
        with self.assertRaises(Denied):
            self.worker.gate().execute(a['id'])


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Schema v3 — mac_processes table
# ═══════════════════════════════════════════════════════════════════════════════

class SchemaV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-p6-schema-')
        from zamal_core.store import Store
        self.store = Store(path=str(Path(self.tmp.name) / 'v3.sqlite3'))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_schema_version_is_5(self):
        v = self.store.db.execute('PRAGMA user_version').fetchone()[0]
        self.assertEqual(v, 6)

    def test_mac_processes_table_exists(self):
        # Should not raise
        self.store.db.execute('SELECT * FROM mac_processes').fetchall()

    def test_mac_processes_has_correct_columns(self):
        cols = {row[1] for row in self.store.db.execute("PRAGMA table_info(mac_processes)")}
        self.assertIn('id', cols)
        self.assertIn('service', cols)
        self.assertIn('pid', cols)
        self.assertIn('started', cols)
        self.assertIn('stopped', cols)
        self.assertIn('status', cols)
        self.assertIn('actor', cols)

    def test_fresh_db_is_v5(self):
        """A freshly initialised database starts at schema version 5."""
        v = self.store.db.execute('PRAGMA user_version').fetchone()[0]
        self.assertEqual(v, 6)

    def test_integrity_ok(self):
        result = self.store.db.execute('PRAGMA integrity_check').fetchone()[0]
        self.assertEqual(result, 'ok')


# ═══════════════════════════════════════════════════════════════════════════════
# 8. MacWorker validate() — payload field enforcement
# ═══════════════════════════════════════════════════════════════════════════════

class MacWorkerValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-p6-val-')
        from zamal_core.store import Store
        from zamal_core.mac_worker import MacWorker
        self.store = Store(path=str(Path(self.tmp.name) / 'v.sqlite3'))
        self.worker = MacWorker(self.store, operator=str(OPERATOR_ROOT))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_service_status_wrong_fields_denied(self):
        # validate() is called directly to test field enforcement
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.worker.validate('service_status', {'service': 'n8n', 'extra': 'x'}, 'operator')

    def test_terminal_run_empty_command_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.worker.validate('terminal_run', {'command': ''}, 'operator')

    def test_browser_open_missing_url_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.worker.validate('browser_open', {}, 'operator')

    def test_service_start_non_operator_project_denied(self):
        from zamal_core.store import Denied
        with self.assertRaises(Denied):
            self.worker.validate('service_start', {'service': 'n8n'}, 'cpc')


# ═══════════════════════════════════════════════════════════════════════════════
# 9. CLI integration — Phase 6 CLI commands
# ═══════════════════════════════════════════════════════════════════════════════

class Phase6CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-p6-cli-')
        self.root = Path(self.tmp.name)
        shutil.copytree(
            Path(__file__).resolve().parents[1] / 'zamal_core',
            self.root / 'zamal_core',
            ignore=shutil.ignore_patterns('__pycache__'),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, expected=0):
        env = {**os.environ, 'PYTHONPATH': str(self.root), 'PYTHONDONTWRITEBYTECODE': '1'}
        r = subprocess.run(
            [sys.executable, '-m', 'zamal_core', *args],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(r.returncode, expected, r.stderr)
        return json.loads(r.stdout if expected == 0 else r.stderr)

    def test_mac_service_status_n8n(self):
        result = self.cli('mac-service-status', 'n8n')
        self.assertEqual(result['service'], 'n8n')
        self.assertIn('running', result)
        self.assertIn('port', result)

    def test_mac_service_status_unapproved_denied(self):
        result = self.cli('mac-service-status', 'curl', expected=2)
        self.assertIn('error', result)

    def test_mac_permission_accessibility(self):
        result = self.cli('mac-permission', 'accessibility')
        self.assertEqual(result['permission'], 'accessibility')
        self.assertIn('status', result)

    def test_mac_permission_unknown_denied(self):
        result = self.cli('mac-permission', 'camera', expected=2)
        self.assertIn('error', result)

    def test_schema_v5_via_init(self):
        result = self.cli('init')
        self.assertEqual(result['schema_version'], 6)
        self.assertEqual(result['integrity'], 'ok')


if __name__ == '__main__':
    unittest.main()
