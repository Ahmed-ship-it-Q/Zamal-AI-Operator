"""OpenAI uses the existing bounded, interruptible credential bridge contract."""
from pathlib import Path
from .gemini_transport import GeminiTextTransport
from .providers import Availability

class OpenAITextTransport(GeminiTextTransport):
    @property
    def bridge(self):
        return Path(__file__).with_name('openai_bridge.cjs')
    def availability(self):
        current=self.probe()
        if not current:return Availability(False,'existing_openai_connection_unavailable')
        if self.binding is None:self.binding=current
        if current!=self.binding:return Availability(False,'openai_connection_changed')
        return Availability(True,'existing_credential_bound_auth_checked_on_request')
