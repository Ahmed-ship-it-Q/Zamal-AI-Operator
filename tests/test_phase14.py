"""Phase 14 real SQLite/Gate integration and adversarial fixtures."""
import concurrent.futures
import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zamal_core.store import Store, Gate, Policy, Denied, Conflict, uid
from zamal_core.personal_ops import (PersonalOpsWorker, _PHASE14_SAFE, _PHASE14_PROTECTED,
    _PHASE14_READ_ONLY, migrate_personal_ops, OpsAudit)
from zamal_core.safety import Controls
from zamal_core.gateway import InboundEventGateway

SAMPLES={
'ops_calendar_add':dict(title='fixture',start_date='2026-09-09',end_date='2026-09-10'),
'ops_calendar_list':{},'ops_calendar_update':None,
'ops_finance_record':dict(entry_type='expense',amount_cents=1234,description='fixture',entry_date='2026-09-09',currency='QAR'),
'ops_finance_summary':{},
'ops_subscription_add':dict(name='fixture',amount_cents=100,billing_cycle='monthly',next_due=datetime.date.today().isoformat()),
'ops_subscriptions_list':{},'ops_alerts_check':{},'ops_alerts_list':{},'ops_alerts_acknowledge':None,
'ops_research_note':dict(topic='fixture',notes='local research',source='https://example.org',tags=['fixture']),
'ops_research_list':{},'ops_document_register':dict(title='fixture',file_ref='evidence/fixture.txt'),
'ops_documents_list':{},'ops_contact_add':dict(name='fixture',notes='local record'),'ops_contacts_list':{},
'ops_learning_log':dict(topic='fixture',notes='learning record',completed=True),'ops_learning_list':{},
'ops_portfolio_update':dict(project_name='fixture',description='local portfolio metadata'),'ops_portfolio_list':{},
'ops_summary':{},'ops_system_status':{},
'ops_content_draft':dict(title='fixture',notes='local content draft'),'ops_content_list':{},
'ops_news_record':dict(title='fixture',notes='news reference only',reference='https://example.org'),'ops_news_list':{},
'ops_travel_plan':dict(title='fixture',notes='local itinerary draft'),'ops_travel_list':{},
'ops_admin_record':dict(title='fixture',notes='local checklist'),'ops_admin_list':{},
}
PROTECTED={
'ops_payment_submit':dict(description='fixture',amount_cents=123,currency='QAR',recipient='fixture',reason='test'),
'ops_subscription_cancel':dict(subscription_id='fixture',reason='test'),
'ops_calendar_external_send':dict(event_id='fixture',recipient='fixture',reason='test'),
'ops_content_publish':dict(record_id='fixture',recipient='fixture',reason='test'),
'ops_travel_submit':dict(record_id='fixture',recipient='fixture',reason='test'),
'ops_admin_submit':dict(record_id='fixture',recipient='fixture',reason='test'),
}
class Phase14Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='zamal-phase14-');self.root=Path(self.tmp.name)
        self.op=self.root/'operator';self.cpc=self.root/'cpc';self.op.mkdir();self.cpc.mkdir()
        (self.cpc/'untouched.txt').write_text('CPC fixture must remain unchanged')
        self.s=Store(self.op/'state/operator.sqlite3');self.w=PersonalOpsWorker(self.s,operator=self.op,cpc=self.cpc);self.g=self.w.gate()
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def task(self,project='operator'):
        t=self.s.create_task('Phase 14 fixture','personal_ops',project,uid())
        for state in ('UNDERSTOOD','PLANNED'):t=self.s.transition(t['id'],state,t['revision'])
        return t
    def propose(self,kind,p,project='operator',ttl_seconds=86400):return self.g.propose(self.task(project)['id'],kind,p,uid(),ttl_seconds)
    def execute(self,kind,p):return self.g.execute(self.propose(kind,p)['id'])
    def sample(self,kind):
        if kind=='ops_calendar_update':return {'event_id':self.execute('ops_calendar_add',SAMPLES['ops_calendar_add'])['id'],'status':'completed'}
        if kind=='ops_alerts_acknowledge':
            self.execute('ops_subscription_add',SAMPLES['ops_subscription_add']);return {'alert_id':self.execute('ops_alerts_check',{})['alerts_generated'][0]['id']}
        return dict(SAMPLES[kind])
    def test_schema_restart_and_backup(self):
        r=self.execute('ops_content_draft',SAMPLES['ops_content_draft'])
        self.assertEqual(self.s.db.execute("select version from core_extensions where name='personal_ops'").fetchone()[0],2)
        migrate_personal_ops(self.s)
        self.s.backup(self.root/'backup.sqlite3')
        with sqlite3.connect(self.root/'backup.sqlite3') as db:self.assertEqual(db.execute('select id from personal_entries').fetchone()[0],r['id'])
        self.assertEqual(self.s.db.execute('pragma user_version').fetchone()[0],6)
    def test_upgrade_preserves_v1_records(self):
        r=self.execute('ops_calendar_add',SAMPLES['ops_calendar_add'])
        self.s.db.execute('drop table personal_entries');self.s.db.execute("update core_extensions set version=1 where name='personal_ops'")
        migrate_personal_ops(self.s)
        self.assertEqual(self.s.db.execute('select id from personal_calendar').fetchone()[0],r['id'])
    def test_missing_schema_fails_closed(self):
        self.s.db.execute('drop table personal_contacts')
        with self.assertRaises(sqlite3.Error):migrate_personal_ops(self.s)
    def test_audit_atomic_and_private(self):
        with patch.object(OpsAudit,'record',side_effect=RuntimeError('fixture')):
            with self.assertRaises(RuntimeError):self.execute('ops_contact_add',{'name':'PRIVATE_NAME'})
        self.assertEqual(self.s.db.execute('select count(*) from personal_contacts').fetchone()[0],0)
        self.execute('ops_contact_add',{'name':'PRIVATE_NAME'})
        self.assertNotIn('PRIVATE_NAME',str([tuple(r) for r in self.s.db.execute('select * from personal_ops_audit')]))
        with self.assertRaises(sqlite3.Error):self.s.db.execute('delete from personal_ops_audit')
    def test_payload_secrets_denied_before_persistence(self):
        for payload in ({'topic':'x','notes':'Bearer fixture-secret-value'}, {'topic':'x','notes':'x','authorization':'x'}):
            with self.assertRaises(Denied):self.propose('ops_research_note',payload)
        self.assertEqual(self.s.db.execute('select count(*) from actions').fetchone()[0],0)
    def test_invalid_values_denied(self):
        for kind,p in [('ops_calendar_add',{**SAMPLES['ops_calendar_add'],'start_date':'garbage'}),
            ('ops_finance_record',{**SAMPLES['ops_finance_record'],'amount_cents':True}),
            ('ops_finance_record',{**SAMPLES['ops_finance_record'],'currency':'QAR;DROP'}),
            ('ops_alerts_check',{'days_ahead':100000}),('ops_document_register',{'title':'x','file_ref':'../../.ssh/id_rsa'}),
            ('ops_research_note',{'topic':'x','notes':'x','task_id':'wrong'}),('ops_contact_add',{'name':' '*3})]:
            with self.subTest(kind=kind),self.assertRaises(Denied):self.propose(kind,p)
    def test_unknown_consequential_action_denied(self):
        for k in ('ops_finance_transfer','ops_delete','ops_deploy','ops_security_change'):
            with self.assertRaises(Denied):self.propose(k,{})
    def test_currency_never_combined(self):
        self.execute('ops_finance_record',SAMPLES['ops_finance_record'])
        self.execute('ops_finance_record',{**SAMPLES['ops_finance_record'],'currency':'USD','amount_cents':9})
        result=self.execute('ops_finance_summary',{})
        self.assertEqual(result['by_currency']['QAR']['expense_cents'],1234)
        self.assertEqual(result['by_currency']['USD']['expense_cents'],9)
        self.assertNotIn('net_cents',result)
    def test_missing_calendar_update_not_false_success(self):
        with self.assertRaises(Conflict):self.execute('ops_calendar_update',{'event_id':'missing','status':'completed'})
    def test_alert_acknowledgement_not_regenerated(self):
        self.execute('ops_subscription_add',SAMPLES['ops_subscription_add'])
        a=self.execute('ops_alerts_check',{})['alerts_generated'][0]
        self.execute('ops_alerts_acknowledge',{'alert_id':a['id']})
        self.assertEqual(self.execute('ops_alerts_check',{})['alerts_generated'][0]['id'],a['id'])
        self.assertEqual(self.s.db.execute('select count(*) from personal_alerts').fetchone()[0],1)
    def test_task_binding_and_replay(self):
        a=self.propose('ops_content_draft',SAMPLES['ops_content_draft']);result=self.g.execute(a['id'])
        self.assertEqual(result['task_id'],a['task_id'])
        self.assertEqual(self.s.db.execute('select task_id from personal_entries').fetchone()[0],a['task_id'])
        self.assertEqual(self.g.propose(a['task_id'],a['kind'],a['payload'],a['request_key'])['id'],a['id'])
        with self.assertRaises(Denied):self.g.execute(a['id'])
    def test_concurrent_duplicate_execution(self):
        a=self.propose('ops_content_draft',SAMPLES['ops_content_draft'])
        def attempt(_):
            s=Store(self.s.path)
            try:
                PersonalOpsWorker(s,operator=self.op,cpc=self.cpc).gate().execute(a['id']);return True
            except Denied:return False
            finally:s.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:self.assertEqual(sum(pool.map(attempt,range(2))),1)
    def test_restart_pending_and_uncertain_no_replay(self):
        a=self.propose('ops_payment_submit',PROTECTED['ops_payment_submit'])
        s=Store(self.s.path)
        try:self.assertEqual(s.approval(a['approval']['id'])['status'],'PENDING')
        finally:s.close()
        self.s.decide(a['approval']['id'],'APPROVE',actor='fixture')
        with self.assertRaises(Denied):self.g.execute(a['id'])
        self.assertEqual(self.s.action(a['id'])['status'],'UNCERTAIN')
        with self.assertRaises(Denied):self.g.execute(a['id'])
    def test_edit_reject_expiry_and_mutation(self):
        for decision in ('EDIT','REJECT'):
            a=self.propose('ops_payment_submit',PROTECTED['ops_payment_submit'])
            self.s.decide(a['approval']['id'],decision,actor='fixture',note='fixture')
            with self.assertRaises(Denied):self.g.execute(a['id'])
        a=self.propose('ops_payment_submit',PROTECTED['ops_payment_submit'])
        self.s.db.execute('update approvals set expires=0 where id=?',(a['approval']['id'],))
        with self.assertRaises(Denied):self.s.decide(a['approval']['id'],'APPROVE',actor='fixture')
        a=self.propose('ops_payment_submit',PROTECTED['ops_payment_submit'])
        self.s.db.execute('update actions set payload=? where id=?',('{}',a['id']))
        with self.assertRaises(Denied):self.s.decide(a['approval']['id'],'APPROVE',actor='fixture')
    def test_referenced_calendar_change_invalidates_approval(self):
        event=self.execute('ops_calendar_add',SAMPLES['ops_calendar_add'])
        a=self.propose('ops_calendar_external_send',{**PROTECTED['ops_calendar_external_send'],'event_id':event['id']})
        self.execute('ops_calendar_update',{'event_id':event['id'],'status':'cancelled'})
        with self.assertRaises(Denied):self.s.decide(a['approval']['id'],'APPROVE',actor='fixture')
    def test_wrong_task_and_emergency_override(self):
        a=self.propose('ops_payment_submit',PROTECTED['ops_payment_submit'])
        with self.assertRaises(Denied):self.g.resolve_approval(a['approval']['id'],'APPROVE',task_id=uid(),action_id=a['id'],actor='fixture')
        self.s.decide(a['approval']['id'],'APPROVE',actor='fixture')
        Controls(self.s).set('pause_external_actions',True,actor='fixture',reason='test')
        with self.assertRaises(Denied):self.g.execute(a['id'])
        self.assertEqual(self.s.approval(a['approval']['id'])['status'],'APPROVED')
    def test_read_only_allows_listing_blocks_recording(self):
        read=self.propose('ops_summary',{});write=self.propose('ops_contact_add',{'name':'fixture'})
        Controls(self.s).set('read_only_mode',True,actor='fixture',reason='test')
        self.assertTrue(self.g.execute(read['id'])['ok'])
        with self.assertRaises(Denied):self.g.execute(write['id'])
    def test_stop_and_direct_worker_denial(self):
        with self.assertRaises(Denied):self.w.run('ops_contact_add',{'name':'fixture'},'operator')
        a=self.propose('ops_summary',{})
        Controls(self.s).set('stop_everything',True,actor='fixture',reason='test')
        with self.assertRaises(Denied):self.g.execute(a['id'])
    def test_cpc_isolation_and_parent_workers_preserved(self):
        before=(self.cpc/'untouched.txt').read_bytes()
        for k in _PHASE14_SAFE:
            with self.assertRaises(Denied):self.propose(k,{},'cpc')
        self.assertEqual((self.cpc/'untouched.txt').read_bytes(),before)
        for k in ('cpc_coding_start','git_commit','monitor_sweep','security_audit','automation_observe','usage_summary','service_recover'):
            self.assertIn(k,self.g._handlers)
    def test_gateway_uses_canonical_pending_gate(self):
        e=InboundEventGateway(self.s);p={'connector_id':'n8n','kind':'ops_payment_submit','arguments':PROTECTED['ops_payment_submit'],'text':'fixture','project':'operator'}
        with patch('zamal_core.connection_registry.ToolHealthGate.check',return_value={'status':'OK'}):
            result=e.receive('workflow_trigger','n8n',p,'fixture');duplicate=e.receive('workflow_trigger','n8n',p,'fixture')
        self.assertEqual(result['outcome'],'APPROVAL_REQUIRED');self.assertEqual(duplicate['outcome'],'DUPLICATE')
        self.assertEqual(self.s.approval(result['approval_id'])['status'],'PENDING')
        self.assertEqual(e.receive('workflow_trigger','n8n',{**p,'approve':True},'malicious')['outcome'],'DENIED')

    def test_shared_context_contains_canonical_result(self):
        from zamal_core.context_bus import ContextBus
        a=self.propose('ops_content_draft',SAMPLES['ops_content_draft']);r=self.g.execute(a['id'])
        ctx=ContextBus(self.s).source(a['task_id'])
        self.assertEqual(ctx['original_task']['id'],a['task_id'])
        self.assertEqual(ctx['actions'][0]['result']['id'],r['id'])

    def test_cli_uses_existing_propose_execute_and_approval(self):
        import io,contextlib
        from zamal_core.__main__ import main
        def cli(argv):
            output=io.StringIO()
            with patch('sys.argv',['zamal',*argv]),patch('zamal_core.__main__.Store',side_effect=lambda:Store(self.s.path)),contextlib.redirect_stdout(output):
                self.assertEqual(main(),0)
            return json.loads(output.getvalue())
        t=self.task()
        a=cli(['propose',t['id'],'ops_contact_add','--payload',json.dumps({'name':'fixture'}),'--key',uid()])
        self.assertTrue(cli(['execute',a['id']])['ok'])
        a=self.propose('ops_payment_submit',PROTECTED['ops_payment_submit'])
        result=cli(['resolve-approval',a['approval']['id'],'REJECT','--task-id',a['task_id'],'--action-id',a['id']])
        self.assertEqual(result['status'],'REJECTED')

    def test_latched_emergency_after_claim_blocks_record(self):
        a=self.propose('ops_contact_add',{'name':'fixture'})
        original=self.w.run
        def run(kind,p,baseline):
            c=Controls(self.s)
            c.set('read_only_mode',True,actor='fixture',reason='test')
            c.set('read_only_mode',False,actor='fixture',reason='test')
            return original(kind,p,baseline)
        with patch.object(self.w,'run',side_effect=run),self.assertRaises(Denied):self.g.execute(a['id'])
        self.assertEqual(self.s.db.execute('select count(*) from personal_contacts').fetchone()[0],0)

    def test_concurrent_alert_checks_deduplicate(self):
        self.execute('ops_subscription_add',SAMPLES['ops_subscription_add'])
        actions=[self.propose('ops_alerts_check',{}) for _ in range(2)]
        def execute(a):
            s=Store(self.s.path)
            try:return PersonalOpsWorker(s,operator=self.op,cpc=self.cpc).gate().execute(a['id'])
            finally:s.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(execute,actions))
        self.assertTrue(all(r['ok'] for r in results))
        self.assertEqual(self.s.db.execute('select count(*) from personal_alerts').fetchone()[0],1)

def safe_test(kind):
    def test(self):
        p=self.sample(kind);a=self.propose(kind,p)
        self.assertIsNone(a['approval']);r=self.g.execute(a['id']);self.assertTrue(r['ok'])
        self.assertEqual(self.s.action(a['id'])['status'],'SUCCEEDED')
    return test
for kind in sorted(_PHASE14_SAFE):setattr(Phase14Tests,'test_safe_'+kind,safe_test(kind))
def protected_test(kind):
    def test(self):
        a=self.propose(kind,PROTECTED[kind]);self.assertEqual(a['approval']['status'],'PENDING')
        with self.assertRaises(Denied):self.g.execute(a['id'])
        self.assertEqual(self.s.action(a['id'])['status'],'PENDING')
        self.assertIsNone(self.s.action(a['id'])['result'])
    return test
for kind in sorted(_PHASE14_PROTECTED):setattr(Phase14Tests,'test_protected_'+kind,protected_test(kind))
