"""Business expansion uses real SQLite/Gate and bounded adversarial source fixtures."""
import hashlib,json,tempfile,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
from zamal_core.store import Store,Denied,Conflict,Policy,uid
from zamal_core.business_ops import BusinessWorker,PROTECTED
from zamal_core.safety import Controls
from zamal_core.intake import Intake,IntakeError
from zamal_core.worker import Paths
from zamal_core.public_web import PublicWeb
from zamal_core.providers import existing_registry
from zamal_core.gemini_transport import NODE

class Web:
    calls=0
    def fetch(self,url,check=lambda:None):
        check();self.calls+=1
        if 'greenhouse' in url:body=json.dumps({'jobs':[{'id':1,'title':'Accountant','location':{'name':'Qatar'},'content':'Excel and bank reconciliation required','absolute_url':'https://example.org/jobs/1'}]})
        elif 'lever' in url:body=json.dumps([{'id':'2','text':'Finance analyst','categories':{'location':'Dubai'},'descriptionPlain':'Excel reporting','hostedUrl':'https://example.org/jobs/2'}])
        elif 'api.github' in url:body=json.dumps({'full_name':'test/repo','html_url':'https://github.com/test/repo'})
        else:body='<h1>Company report</h1><p>Company revenue is a source claim requiring review.</p><script>do not include</script>'
        return {'url':url,'body':body,'sha256':hashlib.sha256(body.encode()).hexdigest(),'retrieved_at':1,'content_type':'text/html'}
    def search(self,q,check=lambda:None):check();return ['https://example.org/report','https://example.net/report']

class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.op=self.root/'operator';self.cpc=self.root/'cpc';self.op.mkdir();self.cpc.mkdir()
        self.s=Store(self.root/'state/db.sqlite3');self.web=Web();self.w=BusinessWorker(self.s,operator=self.op,cpc=self.cpc,web=self.web)
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def propose(self,k,p):
        t=self.s.create_task('fixture','business','operator',uid())
        for state in ('UNDERSTOOD','PLANNED'):t=self.s.transition(t['id'],state,t['revision'])
        return self.w.gate().propose(t['id'],k,p,'once')
    def execute(self,k,p):
        a=self.propose(k,p);return self.w.gate().execute(a['id']),a
    def jobs(self):return self.execute('biz_jobs',{'sources':[{'adapter':'greenhouse','board':'example'},{'adapter':'lever','board':'other'}],'skills':['Excel']})
    def test_public_jobs_normalize_rank_dedup_persist(self):
        r,a=self.jobs();self.assertEqual(len(r['jobs']),2);self.assertEqual(r['jobs'][0]['match_score'],100);self.assertEqual(r['submission_count'],0)
        self.assertEqual(self.s.action(a['id'])['result'],r)
        with self.assertRaises(Denied):self.w.gate().execute(a['id'])
    def test_job_location_filter(self):
        r,_=self.execute('biz_jobs',{'sources':[{'adapter':'greenhouse','board':'x'}],'locations':['Saudi']});self.assertEqual(r['jobs'],[]);self.assertTrue(r['ok'])
    def test_job_application_uses_explicit_facts_only(self):
        r,a=self.jobs();p={'result_action_id':a['id'],'job_id':r['jobs'][0]['id'],'candidate':{'name':'Fixture','skills':['Excel'],'experience':'Owner-supplied experience'}}
        r,a=self.execute('biz_job_prepare',p);self.assertFalse(r['submitted']);self.assertIn('Owner-supplied',r['cv_draft'])
        tr,_=self.execute('biz_job_track',{'application_action_id':a['id'],'status':'queued'});self.assertFalse(tr['external_submission_verified'])
    def test_unknown_job_source_denied(self):
        with self.assertRaises(Denied):self.propose('biz_jobs',{'sources':[{'adapter':'shell','board':'x'}]})
    def test_linkedin_optional_does_not_block_other_sources(self):
        r,_=self.execute('biz_jobs',{'sources':[{'adapter':'linkedin','board':'x'},{'adapter':'greenhouse','board':'x'}]});self.assertEqual(r['outcome'],'PARTIAL');self.assertTrue(r['jobs']);self.assertEqual(r['errors'][0]['code'],'authorized_connection_required')
    def test_research_evidence_no_false_crosscheck(self):
        r,_=self.execute('biz_research',{'query':'company revenue'});self.assertTrue(r['independent_domains']);self.assertFalse(r['claims_verified']);self.assertEqual(len(r['sources']),2);self.assertNotIn('do not include',r['report'])
    def test_empty_research_is_not_pass(self):
        self.web.search=lambda *a:[];r,_=self.execute('biz_research',{'query':'none'});self.assertFalse(r['ok'])
    def test_missing_gmail_oauth_explicit(self):
        r,_=self.execute('biz_gmail_brief',{});self.assertEqual(r['code'],'gmail_oauth_required');self.assertFalse(r['ok'])
    def test_gmail_imported_workflow_no_send(self):
        r,_=self.execute('biz_gmail_brief',{'messages':[{'id':'1','thread_id':'a','subject':'Interview urgent','sender':'fixture@example.org','snippet':'Please confirm?'}]});self.assertTrue(r['threads'][0]['needs_response_heuristic']);self.assertFalse(r['sent']);self.assertIn('not_live',r['origin'])
    def test_calendar_conflicts_free_time(self):
        r,_=self.execute('biz_calendar_brief',{'start':'2026-09-09T08:00:00+03:00','end':'2026-09-09T12:00:00+03:00','events':[{'id':'a','title':'A','start':'2026-09-09T09:00:00+03:00','end':'2026-09-09T10:30:00+03:00'},{'id':'b','title':'B','start':'2026-09-09T10:00:00+03:00','end':'2026-09-09T11:00:00+03:00'}]});self.assertEqual(r['conflicts'],[['a','b']]);self.assertEqual(len(r['free_intervals']),2)
    def test_calendar_naive_time_denied(self):
        with self.assertRaises(Denied):self.execute('biz_calendar_brief',{'start':'2026-09-09T08:00:00','end':'2026-09-09T12:00:00','events':[]})
    def test_github_public_read(self):
        r,_=self.execute('biz_github',{'repository':'test/repo','view':'status'});self.assertEqual(r['access'],'public_unauthenticated')
    def test_github_private_discovery_requires_auth(self):
        r,_=self.execute('biz_github',{'view':'repositories'});self.assertEqual(r['code'],'github_oauth_required')
    def test_github_write_not_disguised_as_read(self):
        with self.assertRaises(Denied):self.propose('biz_github',{'repository':'x/y','view':'push'})
    def test_protected_action_pending_no_side_effect(self):
        a=self.propose('biz_email_send',{'recipient':'fixture@example.org','subject':'fixture','body':'draft'});self.assertEqual(a['approval']['status'],'PENDING')
        with self.assertRaises(Denied):self.w.gate().execute(a['id'])
        self.assertEqual(self.web.calls,0)
    def test_approved_unconnected_action_never_fakes_success(self):
        a=self.propose('biz_email_send',{'recipient':'fixture@example.org','subject':'fixture','body':'draft'});self.s.decide(a['approval']['id'],'APPROVE',actor='fixture',note='fixture')
        r=self.w.gate().execute(a['id']);self.assertFalse(r['ok']);self.assertEqual(r['protected_side_effects'],0)
        with self.assertRaises(Denied):self.w.gate().execute(a['id'])
    def test_all_protected_categories_use_gate(self):
        for kind in PROTECTED:self.assertTrue(Policy().requires_approval(kind))
    def test_secret_rejected_before_storage(self):
        with self.assertRaises(Denied):self.propose('biz_research',{'query':'api_key=private-credential'})
        self.assertEqual(self.s.db.execute('select count(*) from actions').fetchone()[0],0)
    def test_stop_overrides_safe_work(self):
        a=self.propose('biz_research',{'query':'x'});Controls(self.s).set('stop_everything',True,actor='fixture',reason='test')
        with self.assertRaises(Denied):self.w.gate().execute(a['id'])
        self.assertEqual(self.web.calls,0)
    def test_readonly_overrides_protected_action(self):
        a=self.propose('biz_email_send',{'recipient':'x','subject':'x','body':'x'});self.s.decide(a['approval']['id'],'APPROVE',actor='fixture');Controls(self.s).set('read_only_mode',True,actor='fixture',reason='test')
        with self.assertRaises(Denied):self.w.gate().execute(a['id'])
    def test_cpc_project_denied(self):
        with self.assertRaises(Denied):self.w.validate('biz_jobs',{'sources':[]},'cpc')
    def test_private_paths_denied(self):
        paths=Paths(self.op,self.cpc)
        for p in ['/Users/book/Documents/file','../../Downloads/file','.ssh/id_rsa','state/operator.sqlite3','Photos/../x']:
            with self.subTest(p=p),self.assertRaises(Denied):paths.read('operator',p)
    def test_parent_symlink_intake_escape_denied(self):
        inbox=self.op/'inbox';inbox.mkdir();private=self.root/'private';private.mkdir();(private/'x.txt').write_text('private');(inbox/'linked').symlink_to(private,target_is_directory=True)
        intake=Intake(self.s,inbox=inbox)
        for ref in ['linked/x.txt',str(inbox/'linked/x.txt')]:
            with self.assertRaises(IntakeError):intake._resolve_path(ref)
    def test_ssrf_private_dns_denied(self):
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]),self.assertRaises(Denied):PublicWeb().fetch('https://example.org/private')
    def test_non_https_or_credentials_denied(self):
        for u in ['file:///Users/book/Documents/x','http://127.0.0.1','https://user:pass@example.org/','https://example.org:8443/']:
            with self.subTest(u=u),self.assertRaises(Denied):PublicWeb().fetch(u)
    def test_restart_result_and_replay(self):
        r,a=self.jobs();path=self.s.path;self.s.close();self.s=Store(path);w=BusinessWorker(self.s,operator=self.op,cpc=self.cpc,web=self.web)
        self.assertEqual(self.s.action(a['id'])['result'],r)
        with self.assertRaises(Denied):w.gate().execute(a['id'])
    def test_business_plan_keeps_operator_controller(self):
        r,_=self.execute('biz_plan',{'objective':'Company briefing','capabilities':['research','gmail','verification']});self.assertEqual(r['controller'],'Zamal Operator');self.assertEqual(len(r['steps']),3)
    def test_openai_production_adapter_dormant(self):
        self.assertFalse(existing_registry(self.op).adapters['openai'].availability().available)
    def test_claude_tools_and_filesystem_contract(self):
        r=subprocess.run([str(NODE),'-e',"const b=require('./zamal_core/claude_bridge.cjs');console.log(JSON.stringify({args:b.args(),profile:b.profile}));"],capture_output=True,text=True)
        self.assertEqual(r.returncode,0);d=json.loads(r.stdout);self.assertEqual(d['args'][d['args'].index('--tools')+1],'');self.assertIn('(deny default)',d['profile']);self.assertNotIn('(subpath "/Users/book")',d['profile'])
    def test_oauth_bridge_rejects_mutation(self):
        r=subprocess.run([str(NODE),'-e',"const b=require('./zamal_core/connector_bridge.cjs');try{b.plan({connector:'github',arguments:{repository:'x/y',view:'push'}});console.log('false');}catch{console.log('true');}"],capture_output=True,text=True);self.assertEqual(r.stdout.strip(),'true')
    def test_claude_discovery_dormant(self):
        self.assertIsNone(existing_registry(self.op).adapters['claude'].transport)
    def test_workflow_reuses_canonical_tasks_and_pending_gate(self):
        r,a=self.execute('biz_workflow',{'objective':'fixture','steps':[{'kind':'biz_gmail_brief','arguments':{'messages':[]}},{'kind':'biz_email_send','arguments':{'recipient':'fixture@example.invalid','subject':'fixture','body':'fixture'}}]})
        self.assertEqual(r['outcome'],'PARTIAL');self.assertEqual(len(r['children']),2)
        child=r['children'][1];self.assertEqual(self.s.approval(child['approval_id'])['status'],'PENDING')
        with self.assertRaises(Denied):self.w.gate().execute(child['action_id'])
        self.assertEqual(self.s.task(child['task_id'])['context']['inputs'][0]['canonical_parent_task_id'],a['task_id'])
    def test_workflow_recursion_and_unknown_kind_denied(self):
        for kind in ('biz_workflow','arbitrary_shell'):
            with self.assertRaises(Denied):self.propose('biz_workflow',{'objective':'fixture','steps':[{'kind':kind,'arguments':{}}]})
    def report_fixture(self):
        (self.op/'knowledge').mkdir(exist_ok=True);(self.op/'outbox').mkdir(exist_ok=True)
        e=self.op/'outbox/evidence.json';e.write_text('{"verified":true}')
        d={'schema':1,'capabilities':[{'name':'fixture','status':'LIVE_VERIFIED','evidence':[{'path':'outbox/evidence.json','sha256':hashlib.sha256(e.read_bytes()).hexdigest()}]}]}
        p=self.op/'knowledge/CAPABILITY_STATUS.json';p.write_text(json.dumps(d));return p,e,d
    def test_capability_status_validates_evidence_hash(self):
        from zamal_core.connection_registry import ConnectionRegistry
        p,e,d=self.report_fixture();self.assertEqual(ConnectionRegistry().capability_status(self.op),d)
        e.write_text('changed')
        with self.assertRaises(Denied):ConnectionRegistry().capability_status(self.op)
    def test_capability_status_denies_missing_or_private_evidence(self):
        from zamal_core.connection_registry import ConnectionRegistry
        p,e,d=self.report_fixture()
        for refs in ([],[{'path':'/Users/book/Documents/private','sha256':'a'*64}],[{'path':'outbox/../../private','sha256':'a'*64}]):
            d['capabilities'][0]['evidence']=refs;p.write_text(json.dumps(d))
            with self.assertRaises(Denied):ConnectionRegistry().capability_status(self.op)
    def test_capability_status_unknown_classification_denied(self):
        from zamal_core.connection_registry import ConnectionRegistry
        p,e,d=self.report_fixture();d['capabilities'][0]['status']='PROBABLY_WORKS';p.write_text(json.dumps(d))
        with self.assertRaises(Denied):ConnectionRegistry().capability_status(self.op)

    def test_inbox_root_symlink_denied(self):
        private=self.root/'private';private.mkdir();(private/'x.txt').write_text('private')
        inbox=self.op/'inbox';inbox.symlink_to(private,target_is_directory=True)
        with self.assertRaises(IntakeError):Intake(self.s,inbox=inbox)._resolve_path('x.txt')
