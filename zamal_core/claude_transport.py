"""Claude subscription text adapter; strict OS sandbox and no tools/hooks/MCP."""
from pathlib import Path
from .gemini_transport import GeminiTextTransport
from .providers import Availability
class ClaudeTextTransport(GeminiTextTransport):
    safe_errors = frozenset({'subscription_unavailable_or_expired','isolated_auth_failed','isolated_auth_runtime_failed','unsupported_cli_version','claude_runtime_access_denied','claude_runtime_dns_failed','claude_runtime_timeout','claude_cli_option_error','claude_turn_limit','claude_cli_execution_failed','claude_response_contract','claude_response_not_json','claude_settings_changed','claude_access_system_configuration','claude_access_terminal_device','claude_access_runtime_configuration','claude_access_user_configuration','claude_access_system_temp'})
    @property
    def bridge(self):return Path(__file__).with_name('claude_bridge.cjs')
    def availability(self):
        current=self.probe()
        if not current:return Availability(False,'claude_sandbox_or_subscription_unavailable')
        if self.binding is None:self.binding=current
        if current!=self.binding:return Availability(False,'claude_runtime_changed')
        return Availability(True,'subscription_sandbox_bound')
