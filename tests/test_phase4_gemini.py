"""Phase 4 Gemini bridge safety contract; no real inference in tests."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from zamal_core.gemini_transport import GeminiTextTransport,NODE,BRIDGE
from zamal_core.providers import ProviderUnavailable,ProviderUncertain,existing_registry
from zamal_core.store import Denied

class GeminiTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.script=self.root/'child.py'
        self.script.write_text('import sys,json\nr=json.load(sys.stdin)\nprint(json.dumps({"reply":r}))\n')
        self.t=GeminiTextTransport(self.root)
        self.binding={'ready':True,'model':'models/fixture','reference':'n8n:fixture','identity':'a'*64}
        self.t.probe=Mock(return_value=self.binding)
    def tearDown(self):self.temp.cleanup()
    def invoke(self,check=lambda:None):
        with patch('zamal_core.gemini_transport.NODE',Path(sys.executable)),patch('zamal_core.gemini_transport.BRIDGE',self.script):
            return self.t.invoke({'safe':'fixture'},check)
    def node(self,source):
        r=subprocess.run([str(NODE),'-e',source],cwd=BRIDGE.parent.parent,capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr);return json.loads(r.stdout)
    def test_fixture_root_never_uses_production_credential(self):
        self.assertFalse(GeminiTextTransport(self.root).availability().available)
    def test_configuration_mutation_denied(self):
        self.assertTrue(self.t.availability().available)
        self.t.probe.return_value={**self.binding,'identity':'b'*64}
        with self.assertRaises(ProviderUnavailable):self.invoke()
    def test_missing_connection_never_spawns(self):
        self.t.probe.return_value=None
        with patch('subprocess.Popen') as p,self.assertRaises(ProviderUnavailable):self.invoke()
        p.assert_not_called()
    def test_check_denies_before_dispatch(self):
        with patch('subprocess.Popen') as p,self.assertRaises(Denied):self.invoke(lambda:(_ for _ in ()).throw(Denied('stopped')))
        p.assert_not_called()
    def test_bounded_child_and_clean_environment(self):
        self.assertEqual(self.invoke(),{'reply':{'safe':'fixture'}})
        self.assertNotIn('NODE_OPTIONS',self.t._env());self.assertNotIn('HTTPS_PROXY',self.t._env())
    def test_error_does_not_leak_or_retry(self):
        self.script.write_text('import sys,json\njson.load(sys.stdin)\nprint(json.dumps({"transport_error":"Bearer PRIVATE"}))\nsys.exit(1)\n')
        with self.assertRaises(ProviderUncertain) as caught:self.invoke()
        self.assertNotIn('PRIVATE',str(caught.exception))
        self.assertIn('no automatic retry',str(caught.exception))
    def test_safe_http_status_preserved(self):
        self.script.write_text('import sys,json\njson.load(sys.stdin)\nprint(json.dumps({"transport_error":"provider_http_403"}))\nsys.exit(1)\n')
        with self.assertRaisesRegex(ProviderUncertain,'provider_http_403'):self.invoke()
    def test_mid_request_stop_terminates(self):
        self.script.write_text('import time\ntime.sleep(30)\n')
        count=0
        def check():
            nonlocal count
            count+=1
            if count>3:raise Denied('stop')
        with self.assertRaises(Denied):self.invoke(check)
    def test_json_invalid_is_uncertain(self):
        self.script.write_text('print("not json")\n')
        with self.assertRaises(ProviderUncertain):self.invoke()
    def test_request_contract_no_tools_or_security_override(self):
        result=self.node("const b=require('./zamal_core/gemini_bridge.cjs'); const r=b.generationBody({schema:1,stage:'planner',tools:[],context:{context_id:'task:fixture'},context_hash:'a'.repeat(64)}); console.log(JSON.stringify(r));")
        self.assertEqual(result['tools'],[]);self.assertNotIn('safetySettings',result)
        self.assertEqual(result['generationConfig']['maxOutputTokens'],2048)
        self.assertNotIn('apiKey',json.dumps(result))
    def test_tool_request_rejected(self):
        r=self.node("const b=require('./zamal_core/gemini_bridge.cjs'); try{ b.generationBody({schema:1,stage:'coder',tools:[{name:'shell'}],context:{context_id:'x'},context_hash:'a'.repeat(64)}); console.log('false'); }catch{console.log('true');}")
        self.assertTrue(r)
    def test_tool_response_and_truncation_rejected(self):
        r=self.node("const b=require('./zamal_core/gemini_bridge.cjs'); let denied=0; for(const c of [{finishReason:'STOP',content:{parts:[{functionCall:{name:'shell'}}]}},{finishReason:'MAX_TOKENS',content:{parts:[{text:'{}'}]}}]){try{b.responseText({candidates:[c]});}catch{denied++;}} console.log(JSON.stringify(denied));")
        self.assertEqual(r,2)
    def test_plain_json_response_preserved(self):
        r=self.node("const b=require('./zamal_core/gemini_bridge.cjs'); console.log(JSON.stringify(b.responseText({candidates:[{finishReason:'STOP',content:{parts:[{text:'{\"summary\":\"draft only\"}'}]}}]})));")
        self.assertEqual(r,{'summary':'draft only'})
