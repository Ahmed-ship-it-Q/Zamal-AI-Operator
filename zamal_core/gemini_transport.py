"""Phase 4 text-only Gemini transport reusing the existing encrypted n8n connection."""
import json
import os
from pathlib import Path
import shutil
import selectors
import signal
import subprocess
import time
from .providers import Availability, ProviderUnavailable, ProviderUncertain
from .store import encode

NODE=Path(shutil.which(os.environ.get('ZAMAL_NODE') or 'node') or '')
BRIDGE=Path(__file__).with_name('gemini_bridge.cjs')

_TIMING_LOG = Path(__file__).resolve().parent.parent / 'logs' / 'timing.log'

def _tlog(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts} [TIMING] {msg}\n'
    try:
        with open(_TIMING_LOG, 'a') as _f:
            _f.write(line)
    except OSError:
        pass

class GeminiTextTransport:
    safe_errors = frozenset()
    def __init__(self, base=None):
        self.base=Path(base or BRIDGE.parent.parent).resolve()
        self.binding=None
        self.last_reason='transport_not_connected'
    @property
    def bridge(self):
        return BRIDGE
    def _env(self):
        node_dir = str(Path(NODE).parent) if NODE.is_file() else '/usr/local/bin'
        return {'PATH':f'{node_dir}:/usr/bin:/bin','HOME':str(Path.home()),'LANG':'en_US.UTF-8','TZ':'Asia/Qatar'}
    def probe(self):
        # Fixture/alternative roots cannot accidentally access the production credential.
        if self.base!=self.bridge.parent.parent.resolve() or not NODE.is_file():
            return None
        try:
            r=subprocess.run([str(NODE),str(self.bridge),'--probe'],cwd='/',env=self._env(),capture_output=True,timeout=8)
            d=json.loads(r.stdout)
            if r.returncode or set(d)!={'ready','model','reference','identity'} or d['ready'] is not True:return None
            return d
        except (OSError,ValueError,subprocess.TimeoutExpired):return None
    def availability(self):
        current=self.probe()
        if not current:return Availability(False,'existing_gemini_connection_unavailable')
        if self.binding is None:self.binding=current
        if current!=self.binding:return Availability(False,'gemini_connection_changed')
        return Availability(True,'existing_credential_bound_auth_checked_on_request')
    def invoke(self,request,check):
        check()
        if not self.availability().available:raise ProviderUnavailable('Gemini unavailable before dispatch')
        check()
        p=subprocess.Popen([str(NODE),str(self.bridge),'--invoke',self.binding['identity']],
            cwd='/',env=self._env(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            shell=False,close_fds=True,start_new_session=True)
        selector=selectors.DefaultSelector();data=encode(request).encode();output=bytearray();offset=0
        try:
            for stream in (p.stdin,p.stdout):
                os.set_blocking(stream.fileno(),False)
                selector.register(stream,selectors.EVENT_WRITE if stream is p.stdin else selectors.EVENT_READ)
            deadline=time.monotonic()+60
            while selector.get_map() or p.poll() is None:
                check()
                if time.monotonic()>deadline:raise ProviderUncertain('Gemini timeout; no automatic retry')
                for key,event in selector.select(.05):
                    if event & selectors.EVENT_WRITE:
                        try:offset+=os.write(key.fd,data[offset:offset+8192])
                        except BrokenPipeError:offset=len(data)
                        if offset==len(data):selector.unregister(key.fileobj);key.fileobj.close()
                    else:
                        chunk=os.read(key.fd,8192)
                        if not chunk:selector.unregister(key.fileobj);key.fileobj.close()
                        else:
                            output.extend(chunk)
                            if len(output)>128000:raise ProviderUncertain('Gemini output limit')
            code=p.wait();check()
            try:result=json.loads(output)
            except ValueError:raise ProviderUncertain('Invalid Gemini response; no automatic retry') from None
            if code:
                reason=result.get('transport_error','gemini_transport_failed')
                # Only fixed error codes emitted by the trusted bridge are surfaced.
                if reason not in self.safe_errors | {'provider_http_'+str(n) for n in (400,401,403,404,429,500,502,503,504)}:reason='gemini_transport_failed'
                raise ProviderUncertain(reason+'; no automatic retry')
            return result
        finally:
            try:os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            p.wait();selector.close()
            for stream in (p.stdin,p.stdout):
                if not stream.closed:stream.close()
    def chat(self, text):
        _RETRYABLE = frozenset({'provider_http_429','provider_http_500',
                                 'provider_http_502','provider_http_503','provider_http_504'})
        _t_avail = time.monotonic()
        _tlog('gemini_availability_check_start')
        avail = self.availability()
        _tlog(f'gemini_availability_check_done {time.monotonic()-_t_avail:.2f}s')
        if not avail.available:raise ProviderUnavailable('Gemini unavailable')
        request=encode({'schema':2,'text':text}).encode()
        last_exc=ProviderUncertain('gemini_chat_failed')
        for attempt in range(3):
            if attempt:
                delay=5*(2**(attempt-1))  # 5s, 10s
                _tlog(f'gemini_chat_retry attempt={attempt} delay={delay}s')
                time.sleep(delay)
            _t_node = time.monotonic()
            _tlog(f'gemini_node_start (spawning Node for chat, attempt={attempt+1})')
            r=subprocess.run([str(NODE),str(self.bridge),'--chat',self.binding['identity']],
                cwd='/',env=self._env(),input=request,capture_output=True,timeout=60)
            _tlog(f'gemini_node_done {time.monotonic()-_t_node:.2f}s')
            try:result=json.loads(r.stdout)
            except ValueError:raise ProviderUncertain('Invalid Gemini chat response') from None
            if not r.returncode:return result.get('reply','')
            err=result.get('transport_error','gemini_chat_failed')
            last_exc=ProviderUncertain(err)
            if err not in _RETRYABLE:break
        raise last_exc
