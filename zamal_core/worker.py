"""Bounded local capabilities. No shell, project-script execution or CPC writes.

Policy and control-plane code is trusted, payloads are not. All filesystem access
uses pinned directory descriptors and O_NOFOLLOW; only new owned outputs can be
written/deleted. Commands are fixed parser/version invocations, never user argv.
"""
import ast
from contextlib import contextmanager
import datetime
import hashlib
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time
from types import MappingProxyType
from zoneinfo import ZoneInfo

from .store import Gate, Denied, uid
from .safety import Controls

MAX_BYTES = 256 * 1024
MAX_OUTPUT = 64 * 1024
DENIED_PARTS = frozenset({'state', 'approvals', '.n8n-data', '.git', '.ssh', '.aws',
                         '.codex', '.agents', '.claude', 'node_modules', '.venv',
                         'database', 'backups', 'logs'})
SENSITIVE = re.compile(r'(secret|credential|password|token|auth|keychain|id_rsa|id_ed25519)', re.I)


def safe_name(name):
    return (name.lower() not in DENIED_PARTS and not name.startswith('.')
            and not SENSITIVE.search(name)
            and not name.lower().endswith(('.pem', '.key', '.p12', '.pfx', '.sqlite', '.sqlite3', '.db')))


def redact(text):
    text = re.sub(r'(?i)((?:api[_-]?key|password|secret|token|authorization)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', text)
    text = re.sub(r'\b(?:sk-|AIza)[A-Za-z0-9_-]{16,}', '[REDACTED]', text)
    return re.sub(r'(https?://)[^\s/@:]+:[^\s/@]+@', r'\1[REDACTED]@', text)


class Paths:
    def __init__(self, operator, cpc):
        # Roots supplied ONLY by trusted application startup, never action payloads.
        self.roots = MappingProxyType({'operator': Path(operator).resolve(strict=True),
                                      'cpc': Path(cpc).resolve(strict=True)})
        if self.roots['operator'] == self.roots['cpc']:
            raise Denied('Workspace roots must differ')

    def parts(self, workspace, relative):
        if workspace not in self.roots or not isinstance(relative, str) or len(relative) > 2048:
            raise Denied('Unknown workspace or invalid relative path')
        if relative in ('', '.'):
            return []
        parts = relative.split('/')
        if relative.startswith('/') or any(p in ('', '.', '..') or '\\' in p or '\x00' in p or not safe_name(p) for p in parts):
            raise Denied('Path is outside permitted content or contains a protected component')
        # Only this existing public config file is needed by the local worker.
        if any(part.lower() == 'config' for part in parts) and relative != 'config/cpc_project_path.txt':
            raise Denied('Configuration is protected')
        return parts

    @contextmanager
    def directory(self, workspace, parts, create=False):
        root = self.roots[workspace]
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in root.parts[1:]:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = new
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = new
            yield fd
        except OSError as exc:
            raise Denied('Workspace path is unavailable, linked or not permitted') from exc
        finally:
            os.close(fd)

    def read(self, workspace, relative):
        parts = self.parts(workspace, relative)
        if not parts:
            raise Denied('A regular file is required')
        with self.directory(workspace, parts[:-1]) as parent:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except OSError as exc:
                raise Denied('File is unavailable or linked') from exc
            with os.fdopen(fd, 'rb') as f:
                info = os.fstat(f.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
                    raise Denied('Only bounded, unlinked regular files may be read')
                data = f.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise Denied('File exceeds size limit')
                return data

    def listing(self, workspace, relative):
        parts = self.parts(workspace, relative)
        with self.directory(workspace, parts) as fd:
            # scandir is incremental so a large directory cannot allocate an unbounded list.
            result = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    if len(result) >= 200:
                        break
                    if not safe_name(entry.name) or entry.is_symlink():
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                        continue
                    if stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
                        result.append({'name': entry.name, 'directory': stat.S_ISDIR(info.st_mode)})
            return sorted(result, key=lambda x: x['name'])


class CommandPolicy:
    """No caller-supplied command, options, environment, executable or cwd."""
    def __init__(self, node=None):
        self.node = str(node or Path.home() / '.nvm/versions/node/v22.23.2/bin/node')

    def resolve(self, name):
        commands = {'node_version': [self.node, '--version'],
                    'git_version': ['/usr/bin/git', '--version'],
                    'node_syntax': [self.node, '--check', '--input-type=commonjs']}
        if name not in commands:
            raise Denied('Command is not allowlisted; shell, project scripts and mutation commands are blocked')
        executable = Path(commands[name][0])
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise Denied('Trusted executable unavailable')
        return commands[name]


class Runner:
    def run(self, argv, stdin, check, timeout=10, output_limit=MAX_OUTPUT):
        # Only CommandPolicy supplies argv. No inherited secrets, startup hooks or user cwd.
        check()
        env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C', 'LC_ALL': 'C',
               'TZ': 'Asia/Qatar', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd='/', env=env,
                                   shell=False, start_new_session=True, close_fds=True)
        selector = selectors.DefaultSelector()
        output = bytearray()
        offset = 0
        try:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            if stdin:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
            deadline = time.monotonic() + timeout
            while selector.get_map() or process.poll() is None:
                check()
                if time.monotonic() >= deadline:
                    raise Denied('Worker timeout; process group terminated')
                for key, event in selector.select(0.05):
                    if event & selectors.EVENT_WRITE:
                        try:
                            offset += os.write(key.fd, stdin[offset:offset + 8192])
                        except BrokenPipeError:
                            offset = len(stdin)
                        if offset >= len(stdin):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        else:
                            output.extend(chunk)
                            if len(output) > output_limit:
                                raise Denied('Worker output limit; process group terminated')
            check()
            return {'exit_code': process.wait(), 'output': redact(output.decode('utf-8', errors='replace'))}
        finally:
            # Also terminate surviving descendants after the leader exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            selector.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()


class Capability:
    def __init__(self, worker, kind):
        self.worker, self.kind = worker, kind

    def __call__(self, payload):
        raise Denied('Worker capabilities must be invoked through Gate.execute')

    def validate(self, payload, project):
        self.worker.validate(self.kind, payload, project)

    def run_with_controls(self, payload, baseline):
        return self.worker.run(self.kind, payload, baseline)


class LocalWorker:
    def __init__(self, store, *, operator=None, cpc=None, node=None):
        self.store = store
        self.paths = Paths(operator or Path(__file__).resolve().parent.parent,
                           cpc or '/Users/book/Desktop/AI-GROCERY-DECISION-PLATFORM')
        self.controls = Controls(store)
        self.commands = CommandPolicy(node)
        self.runner = Runner()
        self.handlers = MappingProxyType({kind: Capability(self, kind)
                                         for kind in ('read', 'inspect', 'draft', 'prepare_code', 'verify', 'delete')})

    def gate(self):
        return Gate(self.store, self.handlers)

    def output(self, payload):
        row = self.store.db.execute('SELECT * FROM worker_outputs WHERE id=?', (payload['output_id'],)).fetchone()
        if row is None or row['deleted'] is not None or row['sha256'] != payload['sha256']:
            raise Denied('Deletion requires an existing worker-owned output with matching digest')
        if not re.fullmatch(r'outbox/\d{4}-\d{2}-\d{2}/worker/[A-Za-z0-9_-]+\.(?:md|txt|json|py|js)', row['path']):
            raise Denied('Only generated worker output is eligible')
        data = self.paths.read('operator', row['path'])
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise Denied('Output changed since creation; deletion denied')
        return dict(row)

    def validate(self, kind, p, project):
        if not isinstance(p, dict):
            raise Denied('Structured worker payload required')
        fields = {'read': {'workspace', 'path'}, 'inspect': {'workspace', 'path'},
                  'draft': {'workspace', 'name', 'content'}, 'prepare_code': {'workspace', 'name', 'content'},
                  'verify': {'workspace', 'command', 'path'}, 'delete': {'workspace', 'output_id', 'sha256'}}[kind]
        required = fields - ({'path'} if kind == 'verify' else set())
        if set(p) - fields or required - set(p) or not all(isinstance(v, str) for v in p.values()):
            raise Denied('Invalid worker payload fields')
        workspace = p['workspace']
        if workspace not in self.paths.roots or project not in (workspace, str(self.paths.roots[workspace])):
            raise Denied('Action workspace must match the task project')
        if kind in ('draft', 'prepare_code', 'delete') and workspace != 'operator':
            raise Denied('CPC workspace is read-only')
        if kind in ('read', 'inspect'):
            self.paths.parts(workspace, p['path'])
            if kind == 'read':
                self.paths.read(workspace, p['path'])
            else:
                self.paths.listing(workspace, p['path'])
        elif kind in ('draft', 'prepare_code'):
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}\.(?:md|txt|json|py|js)', p['name']) or not safe_name(p['name']):
                raise Denied('Output name must be a plain permitted filename')
            if len(p['content'].encode()) > MAX_BYTES:
                raise Denied('Output exceeds size limit')
            # Ensure the write path is not redirected before the gate claim.
            self.paths.parts('operator', self.output_path(p['name']))
        elif kind == 'delete':
            self.output(p)
        elif kind == 'verify':
            command = p['command']
            if command == 'python_syntax' or command == 'node_syntax':
                if 'path' not in p:
                    raise Denied('Syntax check requires a file')
                self.paths.read(workspace, p['path'])
            elif 'path' in p:
                raise Denied('Version commands accept no path')
            if command != 'python_syntax':
                self.commands.resolve(command)

    @staticmethod
    def output_path(name):
        today = datetime.datetime.now(ZoneInfo('Asia/Qatar')).date().isoformat()
        return 'outbox/' + today + '/worker/' + name

    def run(self, kind, p, baseline):
        check = lambda: self.controls.check(kind, baseline)
        check()
        # Revalidate at dispatch, after gate approval claim and any concurrent controls.
        self.validate(kind, p, p['workspace'])
        if kind == 'read':
            data = self.paths.read(p['workspace'], p['path'])
            check()
            return {'text': redact(data.decode('utf-8', errors='replace')), 'sha256': hashlib.sha256(data).hexdigest()}
        if kind == 'inspect':
            result = self.paths.listing(p['workspace'], p['path'])
            check()
            return {'entries': result, 'limit': 200}
        if kind in ('draft', 'prepare_code'):
            relative = self.output_path(p['name'])
            parts = self.paths.parts('operator', relative)
            data = p['content'].encode()
            # Serialize directory creation and final mutation with emergency changes.
            with self.store.transaction():
                check()
                with self.paths.directory('operator', parts[:-1], create=True) as parent:
                    check()
                    try:
                        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    except OSError as exc:
                        raise Denied('Output exists or is unavailable; overwrite forbidden') from exc
                    with os.fdopen(fd, 'wb') as f:
                        f.write(data)
                        f.flush()
                        os.fsync(f.fileno())
                    os.fsync(parent)
                    output_id, sha = uid(), hashlib.sha256(data).hexdigest()
                    self.store.db.execute('INSERT INTO worker_outputs VALUES(?,?,?,?,NULL)', (output_id, relative, sha, time.time()))
            return {'output_id': output_id, 'path': relative, 'sha256': sha, 'bytes': len(data)}
        if kind == 'delete':
            with self.store.transaction():
                check()
                output = self.output(p)
                parts = self.paths.parts('operator', output['path'])
                with self.paths.directory('operator', parts[:-1]) as parent:
                    # unlink never follows a substituted symlink; it cannot delete its target.
                    os.unlink(parts[-1], dir_fd=parent)
                    os.fsync(parent)
                self.store.db.execute('UPDATE worker_outputs SET deleted=? WHERE id=?', (time.time(), output['id']))
            return {'deleted_output_id': output['id'], 'path': output['path']}
        if kind == 'verify':
            command = p['command']
            data = self.paths.read(p['workspace'], p['path']) if 'path' in p else b''
            if command == 'python_syntax':
                check()
                try:
                    ast.parse(data)
                    check()
                    return {'passed': True, 'command': command, 'sha256': hashlib.sha256(data).hexdigest()}
                except (SyntaxError, ValueError, RecursionError):
                    return {'passed': False, 'command': command, 'error': 'Syntax validation failed'}
            result = self.runner.run(self.commands.resolve(command), data, check)
            return {**result, 'passed': result['exit_code'] == 0, 'command': command}
        raise Denied('Unimplemented capability')
