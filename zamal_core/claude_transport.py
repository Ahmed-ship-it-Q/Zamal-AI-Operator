"""Claude subscription text adapter; strict OS sandbox and no tools/hooks/MCP."""
from pathlib import Path
from .gemini_transport import GeminiTextTransport
from .providers import Availability
class ClaudeTextTransport(GeminiTextTransport):
    @property
    def bridge(self):return Path(__file__).with_name('claude_bridge.cjs')
    def availability(self):
        current=self.probe()
        if not current:return Availability(False,'claude_sandbox_or_subscription_unavailable')
        if self.binding is None:self.binding=current
        if current!=self.binding:return Availability(False,'claude_runtime_changed')
        return Availability(True,'subscription_sandbox_bound')
