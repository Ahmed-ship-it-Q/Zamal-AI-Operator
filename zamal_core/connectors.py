"""n8n-owned OAuth read transport; no credential copying or refresh writes."""
import json,subprocess
from pathlib import Path
from .gemini_transport import GeminiTextTransport,NODE
from .providers import Availability,ProviderUncertain
from .integration_security import validate_data
from .store import Denied

class OAuthReadTransport(GeminiTextTransport):
    def __init__(self,connector,base=None):
        super().__init__(base)
        if connector not in ('gmail','calendar','github'):raise Denied('Unknown OAuth connector')
        self.connector=connector
    @property
    def bridge(self):return Path(__file__).with_name('connector_bridge.cjs')
    def probe(self):
        if self.base!=self.bridge.parent.parent.resolve():return None
        try:
            r=subprocess.run([str(NODE),str(self.bridge)],input=json.dumps({'connector':self.connector,'probe':True}),capture_output=True,text=True,timeout=8,env=self._env(),cwd='/')
            d=json.loads(r.stdout)
            if r.returncode or set(d)!={'ready','reference','identity'} or d['ready'] is not True:return None
            return d
        except (OSError,ValueError,subprocess.TimeoutExpired):return None
    def availability(self):
        d=self.probe()
        if not d:return Availability(False,'n8n_oauth_unavailable')
        if self.binding is None:self.binding=d
        if self.binding!=d:return Availability(False,'n8n_oauth_changed')
        return Availability(True,'oauth_bound_read_unverified')
    def read(self,arguments,check):
        validate_data(arguments)
        if not self.availability().available:return None
        try:r=self.invoke({'connector':self.connector,'arguments':arguments,'expected_identity':self.binding['identity']},check)
        except ProviderUncertain:raise Denied('OAuth read failed; reconnect or review provider before retry') from None
        validate_data(r);return r
