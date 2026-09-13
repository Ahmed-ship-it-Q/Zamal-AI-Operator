"""Bounded local browser fixture transport, reused by JobApplicationWorker's gate."""
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time

from .store import Denied, digest

from .paths import NODE_BIN as NODE
BRIDGE=Path(__file__).with_suffix('.cjs')


def identity():
    return hashlib.sha256(BRIDGE.read_bytes()+Path(__file__).read_bytes()).hexdigest()


def run(operator, operation, check, expected=None):
    if operation not in ('prepare','submit'):
        raise Denied('Unknown browser fixture operation')
    if operation=='submit' and not isinstance(expected,dict):
        raise Denied('Prepared browser contract required')
    check()
    initial_identity=identity()
    runtime=Path(operator)/'state'/'browser-fixtures'
    if runtime.is_symlink():raise Denied('Invalid browser fixture runtime')
    runtime.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(dir=runtime) as private:
        process=subprocess.Popen([str(NODE),str(BRIDGE)],cwd=private,
            env={'HOME':private,'TMPDIR':private,'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8'},
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            start_new_session=True)
        selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
        buffer=b'';events=[];deadline=time.monotonic()+60;authorized=False
        try:
            process.stdin.write(json.dumps({'operation':operation}).encode()+b'\n');process.stdin.flush()
            while True:
                check()
                if identity()!=initial_identity:raise Denied('Browser fixture implementation changed')
                if time.monotonic()>deadline:raise Denied('Browser fixture timed out; do not replay')
                for key,_ in selector.select(.05):
                    chunk=os.read(key.fd,4096)
                    if not chunk:
                        selector.unregister(process.stdout)
                        continue
                    buffer+=chunk
                    if len(buffer)>65536:raise Denied('Browser fixture output exceeds limit')
                    while b'\n' in buffer:
                        line,buffer=buffer.split(b'\n',1)
                        event=json.loads(line)
                        if event.get('phase')=='FAILED':raise Denied('Browser fixture failed; no replay')
                        events.append(event)
                        if len(events)>2:raise Denied('Unexpected browser fixture response')
                        if event.get('phase')=='PREPARED':
                            if len(events)!=1 or event.get('submissions')!=0 or not event.get('filled') or not event.get('navigated'):
                                raise Denied('Invalid pre-submit boundary')
                            if operation=='submit':
                                if expected.get('bridge_identity')!=initial_identity or any(event.get(k)!=expected.get(k) for k in ('contract','fields','upload_sha256')):
                                    raise Denied('Browser form/upload changed since approval')
                                check()  # Last trusted gate check before the only final-action signal.
                                process.stdin.write(json.dumps({'execute':True,'contract':event['contract']}).encode()+b'\n');process.stdin.flush();authorized=True
                        elif event.get('phase')!='VERIFIED':raise Denied('Unknown browser fixture response')
                if not selector.get_map() and process.poll() is not None:break
            check()
            if process.returncode or buffer:raise Denied('Incomplete browser fixture response')
            if operation=='prepare':
                if len(events)!=1:raise Denied('Unexpected prepare side effect')
                return {**events[0],'bridge_identity':initial_identity}
            if len(events)!=2 or not authorized or events[-1].get('phase')!='VERIFIED' or events[-1].get('submissions')!=1 or events[-1].get('contract')!=expected['contract']:
                raise Denied('Browser final action not confirmed')
            return {**events[-1],'bridge_identity':initial_identity,'prepared':events[0]}
        finally:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait();selector.close();process.stdin.close();process.stdout.close()
