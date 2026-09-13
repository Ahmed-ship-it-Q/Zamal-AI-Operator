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
        return {'PATH':'/usr/bin:/bin','HOME':str(Path.home()),'LANG':'en_US.UTF-8','TZ':'Asia/Qatar'}
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
