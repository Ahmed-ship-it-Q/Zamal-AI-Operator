"""OpenAI bridge contract plus shared bounded transport adversarial tests."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch,Mock
from tests import test_phase4_gemini as shared
from zamal_core.openai_transport import OpenAITextTransport
from zamal_core.gemini_transport import NODE

class OpenAITransportTests(shared.GeminiTransportTests):
    def setUp(self):
        super().setUp()
        self.t=OpenAITextTransport(self.root)
        self.t.probe=Mock(return_value=self.binding)
    def invoke(self,check=lambda:None):
        with patch('zamal_core.gemini_transport.NODE',Path(sys.executable)),patch.object(OpenAITextTransport,'bridge',self.script):
            return self.t.invoke({'safe':'fixture'},check)
    def node(self,source):
        r=subprocess.run([str(NODE),'-e',source.replace('gemini_bridge.cjs','openai_bridge.cjs')],capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr);return json.loads(r.stdout)
    def test_fixture_root_never_uses_production_credential(self):
        self.assertFalse(OpenAITextTransport(self.root).availability().available)
    def test_request_contract_no_tools_or_security_override(self):
        r=self.node("const b=require('./zamal_core/openai_bridge.cjs'); console.log(JSON.stringify(b.generationBody({schema:1,stage:'planner',tools:[],context:{context_id:'task:fixture'},context_hash:'a'.repeat(64)})));")
        self.assertEqual(r['tools'],[]);self.assertEqual(r['tool_choice'],'none');self.assertFalse(r['store'])
        self.assertEqual(r['max_output_tokens'],2048);self.assertTrue(r['text']['format']['strict'])
        self.assertFalse(r['text']['format']['schema']['additionalProperties']);self.assertNotIn('apiKey',json.dumps(r))
    def test_tool_response_and_truncation_rejected(self):
        r=self.node("const b=require('./zamal_core/openai_bridge.cjs'); let denied=0; for(const r of [{status:'completed',output:[{type:'function_call'}]},{status:'incomplete',output:[]},{status:'completed',output:[{type:'message',role:'assistant',status:'completed',content:[{type:'refusal',refusal:'no'}]}]}]){try{b.responseText(r);}catch{denied++;}} console.log(JSON.stringify(denied));")
        self.assertEqual(r,3)
    def test_plain_json_response_preserved(self):
        r=self.node("const b=require('./zamal_core/openai_bridge.cjs'); console.log(JSON.stringify(b.responseText({status:'completed',output:[{type:'message',role:'assistant',status:'completed',content:[{type:'output_text',text:'{\"summary\":\"draft only\"}'}]}]})));")
        self.assertEqual(r,{'summary':'draft only'})
    def test_endpoint_header_and_organization_override_rejected(self):
        r=self.node("const b=require('./zamal_core/openai_bridge.cjs');let denied=0;for(const c of [{apiKey:'fixture',url:'https://attacker.invalid/v1'},{apiKey:'fixture',header:true},{apiKey:'fixture',organizationId:'bad\\r\\nheader'},{apiKey:''}]){try{b.credentialFields(c);}catch{denied++;}}console.log(JSON.stringify(denied));")
        self.assertEqual(r,4)
    def test_metadata_probe_has_no_secret_material(self):
        # Probe uses a fixture root here; production metadata is checked separately live.
        self.assertEqual(set(self.binding),{'ready','model','reference','identity'})
        self.assertNotIn('OPENAI_API_KEY',self.t._env())
