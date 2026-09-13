"""Phase 6: Mac Local Worker & Computer Control.

Seven capabilities:
1. FolderPolicy       — explicit allowed folders; traversal/symlink/hardlink blocked
2. MacCommandPolicy   — expanded allowlist; TERMINAL_DENYLIST; unknown = deny
3. Safe file ops      — Phase 3 extended with folder-policy enforcement
4. ProcessControl     — start/stop/status of APPROVED_SERVICES only; PID tracked
5. BrowserWorker      — /usr/bin/open with localhost-only URL policy in Phase 6
6. TccPermissionGate  — detect/report macOS permissions; never bypass TCC
7. MacWorker          — extends LocalWorker; gate/emergency controls enforced

No shell=True, no sudo, no arbitrary process control, CPC read-only throughout.
Approval required for service_start, service_stop (PROTECTED).
"""
import ctypes
import os
import re
import selectors
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from types import MappingProxyType

from .store import Denied, Gate, uid
from .safety import Controls
from .worker import LocalWorker, Capability, redact, MAX_OUTPUT

# ── Root paths (trusted startup, never from payload) ──────────────────────────
# Imported from centralized paths module; never hardcode user-specific strings here.
from .paths import OPERATOR_ROOT, CPC_ROOT, NODE_BIN

# ── Folder policy ─────────────────────────────────────────────────────────────

ALLOWED_FOLDERS = MappingProxyType({
    'operator_root':   OPERATOR_ROOT,
    'operator_outbox': OPERATOR_ROOT / 'outbox',
    'operator_inbox':  OPERATOR_ROOT / 'inbox',
    'cpc_root':        CPC_ROOT,
})
READ_ONLY_FOLDERS = frozenset({'cpc_root'})

_BLOCKED_COMPONENTS = frozenset({
    '..', '.', '',
    '.git', '.ssh', '.aws', '.codex', '.claude', '.env',
    'state', 'node_modules', '.n8n-data', 'backups', '.venv', 'database',
})
_BLOCKED_EXTENSIONS = frozenset({'.pem', '.key', '.p12', '.pfx', '.sqlite', '.sqlite3', '.db'})
_SENSITIVE_NAME = re.compile(r'(?i)(secret|credential|password|token|auth|id_rsa|keychain)')


class FolderPolicy:
    """Validates paths against the explicit ALLOWED_FOLDERS list.

    Blocks traversal (..), hidden components, symlinks, hardlinks, sensitive
    names, and all paths outside a named allowed folder. CPC is always read-only.
    """
    def __init__(self, allowed=None):
        self._folders = {k: Path(v).resolve() for k, v in (allowed or ALLOWED_FOLDERS).items()}

    def resolve(self, folder_name, relative_path):
        """Validate relative_path inside the named folder. Returns resolved Path."""
        if folder_name not in self._folders:
            raise Denied(f'Folder {folder_name!r} is not in the allowed folder list')
        if not isinstance(relative_path, str) or not relative_path:
            raise Denied('Path must be a non-empty string')
        if '\x00' in relative_path or '\\' in relative_path:
            raise Denied('Path contains unsafe characters')
        base = self._folders[folder_name]
        parts = [p for p in relative_path.split('/') if p]
        current = base
        for part in parts:
            if part in _BLOCKED_COMPONENTS or part.startswith('.'):
                raise Denied(f'Path component {part!r} is not permitted')
            if _SENSITIVE_NAME.search(part):
                raise Denied('Path component matches blocked sensitive pattern')
            if Path(part).suffix.lower() in _BLOCKED_EXTENSIONS:
                raise Denied(f'File extension not permitted in this context')
            candidate = current / part
            if candidate.is_symlink():
                raise Denied('Path component is a symlink; escape denied')
            current = candidate
        resolved = current.resolve()
        try:
            resolved.relative_to(base)
        except ValueError:
            raise Denied('Resolved path escapes the allowed folder boundary')
        return resolved

    def check_writable(self, folder_name):
        """Raise Denied if the named folder is read-only by policy."""
        if folder_name in READ_ONLY_FOLDERS:
            raise Denied(f'Folder {folder_name!r} is read-only by policy; no writes permitted')

    def check_hardlink(self, path):
        """Raise Denied if path has multiple hardlinks."""
        if path.exists() and path.is_file() and path.stat().st_nlink != 1:
            raise Denied('File has multiple hardlinks; access denied')

    def allowed_folder_names(self):
        return list(self._folders.keys())


# ── Terminal bridge ────────────────────────────────────────────────────────────

# Explicitly denied command names — blocked regardless of context or arguments.
TERMINAL_DENYLIST = frozenset({
    'rm', 'sudo', 'su', 'curl', 'wget', 'ssh', 'scp', 'rsync',
    'chmod', 'chown', 'chgrp', 'dd', 'mkfs', 'fdisk', 'diskutil',
    'osascript', 'applescript', 'exec', 'eval', 'source',
    'bash', 'sh', 'zsh', 'fish', 'csh', 'tcsh',
    'ruby', 'perl', 'php', 'lua',
    'nc', 'netcat', 'nmap', 'tcpdump',
    'kill', 'killall', 'pkill',
    'launchctl', 'crontab', 'at',
    'defaults', 'nvram', 'csrutil', 'spctl',
    'codesign', 'security',
    'ditto', 'tee',
})


class MacCommandPolicy:
    """Extended command allowlist for the Phase 6 terminal bridge.

    All commands have fixed executables and arguments. Caller supplies only the
    command NAME; argv, cwd and env_extras are hardcoded. Unknown names and
    all names in TERMINAL_DENYLIST are denied by default.
    """
    def __init__(self, node=None, operator_root=None):
        self._node = str(node or NODE_BIN)
        self._root = str(operator_root or OPERATOR_ROOT)
        self._git = '/usr/bin/git'
        self._python = '/usr/bin/python3'

    def resolve(self, name):
        """Return (argv, cwd, env_extras) for an allowlisted command name.

        Raises Denied if the name is denylisted or not in the allowlist.
        """
        if name in TERMINAL_DENYLIST:
            raise Denied(f'Command {name!r} is explicitly denied by denylist')
        _GIT_ENV = {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
        commands = {
            # Phase 3 (preserved)
            'node_version':   ([self._node, '--version'], '/', {}),
            'git_version':    ([self._git, '--version'], '/', {}),
            'node_syntax':    ([self._node, '--check', '--input-type=commonjs'], '/', {}),
            # Phase 6 additions
            'git_status':     ([self._git, 'status', '--porcelain'], self._root, _GIT_ENV),
            'git_log':        ([self._git, 'log', '--oneline', '-20'], self._root, _GIT_ENV),
            'python_version': ([self._python, '--version'], '/', {}),
        }
        if name not in commands:
            raise Denied(f'Command {name!r} is not allowlisted; unknown commands denied by default')
        argv, cwd, env_extras = commands[name]
        exe = Path(argv[0])
        if not exe.is_file() or not os.access(exe, os.X_OK):
            raise Denied(f'Trusted executable not available: {argv[0]}')
        return argv, cwd, env_extras


class MacRunner:
    """subprocess runner with per-command cwd and env_extras support.

    Mirrors worker.Runner exactly but allows the cwd and env to vary
    by command. All other safety properties (shell=False, scrubbed env,
    start_new_session, timeout, output limit, SIGKILL on exit) are preserved.
    """

    def run(self, argv, stdin_bytes, cwd, env_extras, check,
            timeout=10, output_limit=MAX_OUTPUT):
        check()
        env = {
            'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent',
            'LANG': 'C', 'LC_ALL': 'C', 'TZ': 'Asia/Qatar',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            **env_extras,
        }
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=cwd, env=env,
            shell=False, start_new_session=True, close_fds=True,
        )
        sel = selectors.DefaultSelector()
        output = bytearray()
        offset = 0
        try:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                sel.register(stream, selectors.EVENT_READ)
            if stdin_bytes:
                os.set_blocking(process.stdin.fileno(), False)
                sel.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
            deadline = time.monotonic() + timeout
            while sel.get_map() or process.poll() is None:
                check()
                if time.monotonic() >= deadline:
                    raise Denied('Mac terminal bridge timeout; process group terminated')
                for key, event in sel.select(0.05):
                    if event & selectors.EVENT_WRITE:
                        try:
                            offset += os.write(key.fd, stdin_bytes[offset:offset + 8192])
                        except BrokenPipeError:
                            offset = len(stdin_bytes)
                        if offset >= len(stdin_bytes):
                            sel.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            sel.unregister(key.fileobj)
                            key.fileobj.close()
                        else:
                            output.extend(chunk)
                            if len(output) > output_limit:
                                raise Denied('Mac terminal bridge output limit exceeded')
            check()
            return {
                'exit_code': process.wait(),
                'output': redact(output.decode('utf-8', errors='replace')),
            }
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            sel.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()


# ── Process control ────────────────────────────────────────────────────────────

APPROVED_SERVICES = MappingProxyType({
    'n8n': {
        'port': 5678,
        'start_argv': ['/bin/bash', str(OPERATOR_ROOT / 'scripts/start_n8n.sh')],
        'start_env': {
            'HOME': str(Path.home()),
            'N8N_USER_FOLDER': str(OPERATOR_ROOT / '.n8n-data'),
            'N8N_HOST': '127.0.0.1',
            'N8N_PORT': '5678',
            'N8N_PROTOCOL': 'http',
            'GENERIC_TIMEZONE': 'Asia/Qatar',
            'TZ': 'Asia/Qatar',
            'PATH': f"{NODE_BIN.parent}:/usr/bin:/bin:/usr/local/bin",
        },
        'description': 'n8n workflow automation',
    },
    'cpc_inspector': {
        'port': 8787,
        'start_argv': [str(NODE_BIN), str(OPERATOR_ROOT / 'scripts/cpc_readonly_server.js')],
        'start_env': {
            'HOME': str(Path.home()),
            'TZ': 'Asia/Qatar',
            'LANG': 'en_US.UTF-8',
            'PATH': f"{NODE_BIN.parent}:/usr/bin:/bin",
        },
        'description': 'CPC read-only inspector',
    },
})


class ProcessControl:
    """Manages the lifecycle of APPROVED_SERVICES only.

    Service names are constrained to the APPROVED_SERVICES allowlist.
    Payload supplies only the service name; start args and env are hardcoded.
    Unrelated processes cannot be started, stopped or inspected.
    PID is recorded in the mac_processes DB table for stop tracking.
    """
    def __init__(self, store, operator_root=None):
        self.store = store

    def _cfg(self, service):
        if service not in APPROVED_SERVICES:
            raise Denied(f'Service {service!r} is not in the approved service list')
        return APPROVED_SERVICES[service]

    def status(self, service):
        """Check if an approved service is running. SAFE — no side effects."""
        cfg = self._cfg(service)
        port = cfg['port']
        running = False
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                running = True
        except OSError:
            pass
        row = self.store.db.execute(
            'SELECT pid, status FROM mac_processes WHERE service=?', (service,)
        ).fetchone()
        pid = None
        if row and row['pid']:
            pid = row['pid']
            if pid:
                try:
                    os.kill(pid, 0)  # existence check only — signal 0 never kills
                except ProcessLookupError:
                    pid = None  # process is gone
        return {'service': service, 'running': running, 'port': port, 'pid': pid,
                'description': cfg['description']}

    def start(self, service, actor):
        """Start an approved service as a background process. PROTECTED."""
        cfg = self._cfg(service)
        st = self.status(service)
        if st['running']:
            return {**st, 'message': f'Service {service!r} is already running'}
        argv = cfg['start_argv']
        for entry in argv:
            if not Path(entry).exists():
                raise Denied(f'Service start file not found: {entry}')
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=cfg['start_env'], cwd='/',
            shell=False, start_new_session=True, close_fds=True,
        )
        pid = process.pid
        now = time.time()
        with self.store.transaction():
            self.store.db.execute(
                'INSERT INTO mac_processes(id,service,pid,started,stopped,status,actor) '
                'VALUES(?,?,?,?,NULL,?,?) '
                'ON CONFLICT(service) DO UPDATE SET '
                'pid=excluded.pid, started=excluded.started, stopped=NULL, '
                'status=excluded.status, actor=excluded.actor',
                (uid(), service, pid, now, 'started', actor),
            )
        return {'service': service, 'pid': pid, 'status': 'started',
                'port': cfg['port'], 'description': cfg['description']}

    def stop(self, service, actor):
        """Send SIGTERM to the tracked service process group. PROTECTED."""
        cfg = self._cfg(service)
        row = self.store.db.execute(
            'SELECT pid FROM mac_processes WHERE service=?', (service,)
        ).fetchone()
        pid = row['pid'] if row and row['pid'] else None
        killed = False
        if pid:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                killed = True
            except (ProcessLookupError, PermissionError, OSError):
                pass
        now = time.time()
        with self.store.transaction():
            self.store.db.execute(
                'INSERT INTO mac_processes(id,service,pid,started,stopped,status,actor) '
                'VALUES(?,?,?,NULL,?,?,?) '
                'ON CONFLICT(service) DO UPDATE SET '
                'stopped=excluded.stopped, status=excluded.status, actor=excluded.actor',
                (uid(), service, pid, now, 'stopped', actor),
            )
        return {'service': service, 'pid': pid,
                'status': 'stopped' if killed else 'stop_attempted',
                'port': cfg['port']}


# ── Browser worker ─────────────────────────────────────────────────────────────

ALLOWED_LOCAL_PORTS = frozenset({5678, 8787})
_LOCALHOST_URL = re.compile(r'^https?://localhost:(\d{1,5})(/.*)?$', re.I)
_CREDENTIAL_URL = re.compile(r'(https?://)[^\s/@:]+:[^\s/@]+@', re.I)
_OPEN_BIN = '/usr/bin/open'


class BrowserWorker:
    """Controlled browser open via /usr/bin/open.

    Phase 6 policy: localhost URLs on ALLOWED_LOCAL_PORTS only.
    No credentials, no javascript:, no file:, no external URLs.
    External URL access requires a future phase with explicit allowlist.
    """

    def validate_url(self, url):
        """Validate URL against browser policy. Raises Denied if not allowed."""
        if not isinstance(url, str) or not url.strip():
            raise Denied('URL must be a non-empty string')
        if len(url) > 2048:
            raise Denied('URL exceeds maximum length')
        if _CREDENTIAL_URL.search(url):
            raise Denied('URL must not contain embedded credentials')
        lower = url.lower()
        if not lower.startswith(('http://', 'https://')):
            raise Denied('Only http:// and https:// URLs are permitted')
        m = _LOCALHOST_URL.match(url)
        if not m:
            raise Denied(
                'Phase 6 browser worker permits localhost URLs only; '
                'external URLs require a later phase with explicit allowlist'
            )
        port = int(m.group(1))
        if port not in ALLOWED_LOCAL_PORTS:
            raise Denied(
                f'Port {port} is not in the approved local port list '
                f'{sorted(ALLOWED_LOCAL_PORTS)}'
            )

    def open(self, url):
        """Open a validated localhost URL in the default browser."""
        self.validate_url(url)
        open_bin = Path(_OPEN_BIN)
        if not open_bin.is_file() or not os.access(open_bin, os.X_OK):
            raise Denied('/usr/bin/open is unavailable')
        result = subprocess.run(
            [_OPEN_BIN, url],
            capture_output=True, text=True, timeout=10, shell=False,
            env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home())},
        )
        return {'url': url, 'exit_code': result.returncode, 'opened': result.returncode == 0}


# ── macOS TCC permission gate ─────────────────────────────────────────────────

_KNOWN_PERMISSIONS = frozenset({'accessibility', 'speech_recognition'})


class TccPermissionGate:
    """Detects macOS TCC permission status. Reports; never bypasses TCC.

    Checking a permission never triggers a prompt — it only reads current state.
    Only known, implemented permission services are accepted; unknown = Denied.
    """

    def check(self, permission_name):
        """Return {'permission', 'status', 'message'} dict.

        status: 'authorized' | 'denied' | 'not_determined' | 'restricted' | 'unavailable'
        """
        if permission_name not in _KNOWN_PERMISSIONS:
            raise Denied(
                f'Permission {permission_name!r} is not a known checkable service; '
                f'known: {sorted(_KNOWN_PERMISSIONS)}'
            )
        if permission_name == 'accessibility':
            return self._check_accessibility()
        if permission_name == 'speech_recognition':
            return self._check_speech()
        raise Denied('Unimplemented permission check')  # unreachable guard

    def _check_accessibility(self):
        try:
            ax = ctypes.cdll.LoadLibrary(
                '/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices'
            )
            trusted = bool(ax.AXIsProcessTrusted())
            status = 'authorized' if trusted else 'not_determined'
            return {
                'permission': 'accessibility',
                'status': status,
                'message': (
                    'Accessibility enabled'
                    if trusted else
                    'Enable in System Preferences → Security & Privacy → Privacy → Accessibility'
                ),
            }
        except OSError:
            return {'permission': 'accessibility', 'status': 'unavailable',
                    'message': 'ApplicationServices framework unavailable'}

    def _check_speech(self):
        """Check SFSpeechRecognizer authorization status via brief Swift probe."""
        swift = '/usr/bin/swift'
        if not Path(swift).is_file():
            return {'permission': 'speech_recognition', 'status': 'unavailable',
                    'message': 'swift not found'}
        script = (
            'import Foundation; import Speech; '
            'let s = SFSpeechRecognizer.authorizationStatus(); '
            'switch s { '
            'case .authorized: print("authorized"); '
            'case .denied: print("denied"); '
            'case .restricted: print("restricted"); '
            'default: print("not_determined") }'
        )
        try:
            with tempfile.NamedTemporaryFile(suffix='.swift', mode='w', delete=False,
                                            prefix='zamal_perm_') as f:
                f.write(script)
                fn = f.name
            r = subprocess.run(
                [swift, fn], capture_output=True, text=True, timeout=30, shell=False,
                env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'LANG': 'en_US.UTF-8'},
            )
            try:
                os.unlink(fn)
            except OSError:
                pass
            raw = r.stdout.strip()
            status = raw if raw in ('authorized', 'denied', 'restricted', 'not_determined') else 'unavailable'
            _msgs = {
                'authorized': 'Speech Recognition permission granted to Terminal',
                'denied': 'Denied — enable in System Preferences → Security & Privacy → Privacy → Speech Recognition',
                'restricted': 'Restricted by policy',
                'not_determined': 'Not yet requested; run a voice intake to trigger permission prompt',
                'unavailable': 'Swift permission probe returned unexpected output',
            }
            return {'permission': 'speech_recognition', 'status': status, 'message': _msgs[status]}
        except Exception:
            return {'permission': 'speech_recognition', 'status': 'unavailable',
                    'message': 'Swift permission probe failed'}


# ── Mac Worker ─────────────────────────────────────────────────────────────────

_PHASE6_KINDS = frozenset({
    'service_status', 'service_start', 'service_stop',
    'terminal_run', 'browser_open', 'permission_check',
})

_PHASE6_FIELDS = {
    'service_status':   frozenset({'service'}),
    'service_start':    frozenset({'service'}),
    'service_stop':     frozenset({'service'}),
    'terminal_run':     frozenset({'command'}),
    'browser_open':     frozenset({'url'}),
    'permission_check': frozenset({'permission'}),
}


class MacWorker(LocalWorker):
    """Phase 6 Mac worker: extends LocalWorker with computer control.

    All Phase 3 capabilities (read, inspect, draft, prepare_code, verify, delete)
    are fully preserved. Phase 6 adds service control, terminal bridge, browser
    open and permission checking. Emergency controls are enforced at every point.
    """

    def __init__(self, store, *, operator=None, cpc=None, node=None):
        super().__init__(store, operator=operator, cpc=cpc, node=node)
        op_root = Path(operator or OPERATOR_ROOT)
        self.folder_policy = FolderPolicy()
        self.mac_commands = MacCommandPolicy(node=node, operator_root=op_root)
        self.mac_runner = MacRunner()
        self.process_ctrl = ProcessControl(store, op_root)
        self.browser = BrowserWorker()
        self.tcc = TccPermissionGate()
        # Extend the immutable Phase 3 handler registry with Phase 6 capabilities.
        all_handlers = dict(self.handlers)
        all_handlers.update({k: Capability(self, k) for k in _PHASE6_KINDS})
        self.handlers = MappingProxyType(all_handlers)

    def gate(self):
        return Gate(self.store, self.handlers)

    def validate(self, kind, p, project):
        if kind not in _PHASE6_KINDS:
            return super().validate(kind, p, project)
        if not isinstance(p, dict):
            raise Denied('Structured payload required')
        expected = _PHASE6_FIELDS[kind]
        if set(p) != expected:
            raise Denied(f'Payload must have exactly these fields: {sorted(expected)}')
        if not all(isinstance(v, str) and v for v in p.values()):
            raise Denied('All payload values must be non-empty strings')
        # Per-kind validation
        if kind in ('service_status', 'service_start', 'service_stop'):
            if p['service'] not in APPROVED_SERVICES:
                raise Denied(f'Service {p["service"]!r} not in approved service list')
            if kind in ('service_start', 'service_stop'):
                if project not in ('operator', str(OPERATOR_ROOT)):
                    raise Denied('Service start/stop requires operator project')
        elif kind == 'terminal_run':
            self.mac_commands.resolve(p['command'])
        elif kind == 'browser_open':
            self.browser.validate_url(p['url'])
        elif kind == 'permission_check':
            if p['permission'] not in _KNOWN_PERMISSIONS:
                raise Denied(f'Unknown permission: {p["permission"]}')

    def run(self, kind, p, baseline):
        if kind not in _PHASE6_KINDS:
            return super().run(kind, p, baseline)
        check = lambda: self.controls.check(kind, baseline)
        check()
        # Revalidate at dispatch (after gate claim, after any concurrent control changes).
        # Phase 6 actions are operator-bound; there is no workspace field in the payload.
        self.validate(kind, p, 'operator')
        if kind == 'service_status':
            result = self.process_ctrl.status(p['service'])
            check()
            return result
        if kind == 'service_start':
            result = self.process_ctrl.start(p['service'], actor='worker')
            check()
            return result
        if kind == 'service_stop':
            result = self.process_ctrl.stop(p['service'], actor='worker')
            check()
            return result
        if kind == 'terminal_run':
            argv, cwd, env_extras = self.mac_commands.resolve(p['command'])
            result = self.mac_runner.run(argv, b'', cwd, env_extras, check)
            check()
            return {**result, 'passed': result['exit_code'] == 0, 'command': p['command']}
        if kind == 'browser_open':
            result = self.browser.open(p['url'])
            check()
            return result
        if kind == 'permission_check':
            result = self.tcc.check(p['permission'])
            check()
            return result
        raise Denied('Unimplemented Phase 6 capability')
