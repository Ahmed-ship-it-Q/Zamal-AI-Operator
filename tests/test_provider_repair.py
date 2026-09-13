"""Provider repair security contracts, using no production auth or inference."""
import json,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from zamal_core.claude_transport import ClaudeTextTransport
from zamal_core.providers import CodexTextTransport,Availability,existing_registry
from zamal_core.gemini_transport import NODE

class ProviderRepairTests(unittest.TestCase):
    def node(self,code):
        r=subprocess.run([str(NODE),'-e',"const b=require('./zamal_core/claude_bridge.cjs');"+code],capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0);return json.loads(r.stdout)
    def test_subscription_requires_valid_expiry_scope_and_plan(self):
        r=self.node("const valid={accessToken:'fixture-only',scopes:['user:inference'],subscriptionType:'pro',expiresAt:Date.now()+3600000};let denied=0;for(const change of [{accessToken:''},{accessToken:'x\\ny'},{scopes:[]},{subscriptionType:'api'},{expiresAt:0},{expiresAt:Date.now()+1000}]){try{b.validateSubscription({...valid,...change});}catch{denied++;}}console.log(JSON.stringify({denied,valid:b.validateSubscription(valid)===valid}));")
        self.assertEqual(r,{'denied':6,'valid':True})
    def test_token_only_in_child_environment_not_arguments(self):
        r=self.node("const e=b.env('fixture-only');console.log(JSON.stringify({arguments:b.args(),oauth:e.CLAUDE_CODE_OAUTH_TOKEN==='fixture-only',paid:e.ANTHROPIC_API_KEY!==undefined,proxy:e.HTTPS_PROXY!==undefined,config:e.CLAUDE_CONFIG_DIR,retries:e.CLAUDE_CODE_MAX_RETRIES}));")
        self.assertTrue(r['oauth']);self.assertFalse(r['paid']);self.assertFalse(r['proxy']);self.assertNotIn('fixture-only',json.dumps(r['arguments']));self.assertEqual(r['retries'],'0');self.assertTrue(r['config'].endswith('/state/claude-runtime'))
    def test_profile_does_not_grant_keychain_or_home_access(self):
        r=self.node("console.log(JSON.stringify(b.profile));")
        self.assertIn('(deny default)',r);self.assertNotIn('(subpath "/Users/book")',r);self.assertNotIn('/Library/Keychains',r);self.assertNotIn('/Users/book/.claude.json',r)
    def test_no_tools_hooks_mcp_or_persistence(self):
        r=self.node("console.log(JSON.stringify(b.args()));")
        self.assertEqual(r[r.index('--tools')+1],'');self.assertEqual(r[r.index('--setting-sources')+1],'');self.assertIn('--strict-mcp-config',r);self.assertIn('--no-session-persistence',r);self.assertIn('--disable-slash-commands',r)
        self.assertTrue(r[r.index('--settings')+1].endswith('/state/claude-runtime/operator-settings.json'))
        self.assertEqual(r[r.index('--effort')+1],'low')
        self.assertTrue(self.node('console.log(JSON.stringify(b.SETTINGS));')['disableAllHooks'])
    def test_resolver_access_is_literal_not_directory_wide(self):
        profile=self.node('console.log(JSON.stringify(b.profile));')
        self.assertIn('(literal "/private/var/run/mDNSResponder")',profile)
        self.assertIn('(literal "/private/var/run/resolv.conf")',profile)
        for directory in ('/private/etc','/private/var/run','/private/var','/etc','/var'):
            self.assertNotIn('(subpath "'+directory+'")',profile)
    def test_response_requires_empty_tools_and_success(self):
        r=self.node("const init={type:'system',subtype:'init',tools:[],mcp_servers:[]};const end={type:'result',subtype:'success',num_turns:1,result:'{\"summary\":\"fixture\"}'};const pack=x=>x.map(JSON.stringify).join('\\n');let denied=0;for(const seq of [[end],[{...init,tools:['Read']},end],[{...init,mcp_servers:['remote']},end],[init,{type:'assistant',message:{content:[{type:'tool_use'}]}},end],[init,{...end,subtype:'error'}],[init,{...end,num_turns:2}],[init,end,end]]){try{b.responseText(pack(seq));}catch{denied++;}}console.log(JSON.stringify({denied,valid:b.responseText(pack([init,end]))}));")
        self.assertEqual(r,{'denied':7,'valid':{'summary':'fixture'}})
    def test_alternative_root_never_reads_real_auth(self):
        with tempfile.TemporaryDirectory() as d,patch('subprocess.run') as run:
            self.assertFalse(ClaudeTextTransport(d).availability().available);run.assert_not_called()
    def test_discovery_binds_only_preflight_validated_claude(self):
        with tempfile.TemporaryDirectory() as d,patch('zamal_core.claude_transport.ClaudeTextTransport') as factory:
            transport=factory.return_value
            transport.availability.return_value=Availability(True,'fixture')
            transport.binding={'reference':'fixture:subscription','identity':'a'*64}
            registry=existing_registry(d)
            self.assertIs(registry.adapters['claude'].transport,transport)
            self.assertEqual(registry.adapters['claude'].spec.connection_ref,'fixture:subscription:binding:'+'a'*64)
            self.assertIsNone(registry.adapters['openai'].transport)
    def test_discovery_failed_claude_preflight_leaves_it_dormant(self):
        with tempfile.TemporaryDirectory() as d,patch('zamal_core.claude_transport.ClaudeTextTransport') as factory:
            factory.return_value.availability.return_value=Availability(False,'fixture_denied')
            self.assertIsNone(existing_registry(d).adapters['claude'].transport)
    def test_codex_unsafe_features_still_denied(self):
        version=subprocess.CompletedProcess([],0,'codex-cli 0.149.0\n','')
        features='\n'.join(f'{f} stable '+('true' if f=='unified_exec' else 'false') for f in CodexTextTransport.DISABLED)
        with patch('zamal_core.providers.subprocess.run',side_effect=[version,subprocess.CompletedProcess([],0,features,'')]):
            self.assertFalse(CodexTextTransport('/fixture/codex').availability().available)
    def test_codex_safe_contract_satisfied_by_0149(self):
        version=subprocess.CompletedProcess([],0,'codex-cli 0.149.0\n','')
        features='\n'.join(f'{f} stable false' for f in CodexTextTransport.DISABLED)
        with patch('zamal_core.providers.subprocess.run',side_effect=[version,subprocess.CompletedProcess([],0,features,'')]):
            result=CodexTextTransport('/fixture/codex').availability()
            self.assertTrue(result.available)
            self.assertEqual(result.reason,'existing_cli_auth_checked_on_request')
    def test_codex_wrong_version_denied(self):
        for bad in ('codex-cli 0.150.0','codex-cli 0.153.4','codex-cli 0.1.0'):
            with patch('zamal_core.providers.subprocess.run',return_value=subprocess.CompletedProcess([],0,bad+'\n','')):
                self.assertFalse(CodexTextTransport('/fixture/codex').availability().available,'version '+bad+' should be denied')

    # ── L-2: Bounded availability cache ──────────────────────────────────────

    def test_codex_cache_expires_and_reprobes(self):
        """After TTL, availability() re-probes instead of returning the cached result."""
        import time
        version = subprocess.CompletedProcess([], 0, 'codex-cli 0.149.0\n', '')
        features_ok = '\n'.join(f'{f} stable false' for f in CodexTextTransport.DISABLED)
        features_ok_proc = subprocess.CompletedProcess([], 0, features_ok, '')

        transport = CodexTextTransport('/fixture/codex')
        # First probe: returns True (available)
        with patch('zamal_core.providers.subprocess.run',
                   side_effect=[version, features_ok_proc]) as mock_run:
            a1 = transport.availability()
            self.assertTrue(a1.available)
            self.assertEqual(mock_run.call_count, 2)  # version + features probe

        # Second call within TTL: should return cached result, NO new subprocess call
        with patch('zamal_core.providers.subprocess.run') as mock_run2:
            a2 = transport.availability()
            self.assertTrue(a2.available)
            mock_run2.assert_not_called()  # cache hit, no re-probe

        # Expire the cache by moving _available_at back past TTL
        transport._available_at -= (CodexTextTransport._CACHE_TTL + 1)

        # Third call after TTL: should re-probe (subprocess called again)
        version2 = subprocess.CompletedProcess([], 0, 'codex-cli 0.149.0\n', '')
        features2 = subprocess.CompletedProcess([], 0, features_ok, '')
        with patch('zamal_core.providers.subprocess.run',
                   side_effect=[version2, features2]) as mock_run3:
            a3 = transport.availability()
            self.assertTrue(a3.available)
            self.assertGreater(mock_run3.call_count, 0)

    def test_codex_cache_ttl_default_is_300(self):
        """Cache TTL must be 300 seconds (5 minutes)."""
        self.assertEqual(CodexTextTransport._CACHE_TTL, 300)

    def test_codex_transient_failure_does_not_permanently_cache(self):
        """A transient OSError during probe must not permanently disable Codex."""
        import time
        transport = CodexTextTransport('/fixture/codex')

        # First probe: OSError (transient)
        with patch('zamal_core.providers.subprocess.run',
                   side_effect=OSError('no such file')) as _:
            a1 = transport.availability()
            self.assertFalse(a1.available)

        # Expire cache
        transport._available_at -= (CodexTextTransport._CACHE_TTL + 1)

        # Second probe: succeeds (transient failure resolved)
        version = subprocess.CompletedProcess([], 0, 'codex-cli 0.149.0\n', '')
        features = '\n'.join(f'{f} stable false' for f in CodexTextTransport.DISABLED)
        with patch('zamal_core.providers.subprocess.run',
                   side_effect=[version,
                                 subprocess.CompletedProcess([], 0, features, '')]):
            a2 = transport.availability()
        self.assertTrue(a2.available,
                        'After cache expiry, a recovered Codex must be re-detected as available')
