"""Real SQLite and isolated worker/Git fixtures; no external protected effects."""
import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from zamal_core.store import Store, Gate, Denied, Conflict, uid
from zamal_core.verifier import VerifierWorker
from zamal_core.safety import Controls


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='zamal-phase9-')
        self.root=Path(self.tmp.name)
        self.op=self.root/'operator';self.op.mkdir()
        self.cpc=self.root/'cpc';self.cpc.mkdir()
        subprocess.run(['/usr/bin/git','init','-q',str(self.cpc)],check=True)
        (self.cpc/'readme.txt').write_text('fixture')
        self.path=self.op/'state/operator.sqlite3'
        self.s=Store(self.path)
        self.worker=VerifierWorker(self.s,operator=self.op,cpc=self.cpc)
        self.g=self.worker.gate()

    def tearDown(self):
        self.s.close();self.tmp.cleanup()

    def task(self):
        t=self.s.create_task('Phase 9 fixture','local','operator',uid())
        for state in ('UNDERSTOOD','PLANNED'):t=self.s.transition(t['id'],state,t['revision'])
        return t

    def deletion(self):
        a=self.g.propose(self.task()['id'],'draft',{'workspace':'operator','name':uid()+'.txt','content':'fixture'},uid())
        output=self.g.execute(a['id'])
        row=self.s.db.execute('SELECT * FROM worker_outputs WHERE path=?',(output['path'],)).fetchone()
        a=self.g.propose(self.task()['id'],'delete',{'workspace':'operator','output_id':row['id'],'sha256':row['sha256']},uid())
        return a,self.op/row['path']

    def approve(self,a):
        return self.s.decide(a['approval']['id'],'APPROVE',actor='local-fixture-human')

    def resolve(self,a,choice='APPROVE',**kwargs):
        return self.g.resolve_approval(a['approval']['id'],choice,task_id=a['task_id'],action_id=a['id'],actor='local-fixture-human',**kwargs)

    def coding(self):
        t=self.task()
        a=self.g.propose(t['id'],'cpc_coding_start',{'workspace':'cpc','task_id':t['id'],'scope_files':'["readme.txt"]','owner':'codex'},uid())
        return a

    def test_A_safe_real_read_without_approval(self):
        t=self.s.create_task('read','read','cpc',uid())
        for state in ('UNDERSTOOD','PLANNED'):t=self.s.transition(t['id'],state,t['revision'])
        a=self.g.propose(t['id'],'read',{'workspace':'cpc','path':'readme.txt'},uid())
        self.assertIsNone(a['approval']);self.assertIn('fixture',json.dumps(self.g.execute(a['id'])))

    def test_B_pending_has_zero_effect(self):
        a,p=self.deletion()
        with self.assertRaises(Denied):self.g.execute(a['id'])
        self.assertTrue(p.exists());self.assertEqual(self.s.approval(a['approval']['id'])['lifecycle'],'PENDING')

    def test_C_D_exact_once_and_replay(self):
        a,p=self.deletion();self.resolve(a);self.assertFalse(p.exists())
        self.assertEqual(self.s.approval(a['approval']['id'])['lifecycle'],'EXECUTED')
        with self.assertRaises((Denied,Conflict)):self.resolve(a)
        with self.assertRaises(Denied):self.g.execute(a['id'])

    def test_E_F_edit_preserved_new_approval_required(self):
        a,p=self.deletion();self.resolve(a,'EDIT',note='Review target again')
        self.assertTrue(p.exists())
        self.assertEqual(self.s.approval(a['approval']['id'])['lifecycle'],'SUPERSEDED')
        self.assertIn('Review target again',json.dumps(self.s.task(a['task_id'])['context']))
        b=self.g.propose(a['task_id'],'delete',a['payload'],uid())
        self.assertNotEqual(a['approval']['id'],b['approval']['id'])
        with self.assertRaises(Denied):self.g.execute(b['id'],approval_id=a['approval']['id'])
        with self.assertRaises(Denied):self.g.execute(b['id'])
        self.assertTrue(p.exists())

    def test_G_reject_is_terminal(self):
        a,p=self.deletion();self.resolve(a,'REJECT',note='Not needed')
        self.assertEqual(self.s.approval(a['approval']['id'])['lifecycle'],'REJECTED')
        self.assertTrue(p.exists())
        with self.assertRaises(Conflict):self.approve(a)

    def test_H_expiry_is_durable_with_real_clock(self):
        a,p=self.deletion()
        self.s.decide(a['approval']['id'],'EDIT',actor='fixture',note='short TTL')
        b=self.g.propose(a['task_id'],'delete',a['payload'],uid(),ttl_seconds=.01)
        time.sleep(.02)
        with self.assertRaises(Denied):self.approve(b)
        self.assertEqual(self.s.approval(b['approval']['id'])['lifecycle'],'EXPIRED')
        self.assertTrue(p.exists())

    def test_H_expiry_after_decision(self):
        a,p=self.deletion();self.approve(a)
        with patch('zamal_core.store.time.time',return_value=a['approval']['expires']+1):
            with self.assertRaises(Denied):self.g.execute(a['id'])
        self.assertEqual(self.s.approval(a['approval']['id'])['lifecycle'],'EXPIRED');self.assertTrue(p.exists())

    def test_I_payload_mutation_denied(self):
        a,p=self.deletion();self.approve(a)
        self.s.db.execute('UPDATE actions SET payload=? WHERE id=?',(json.dumps({**a['payload'],'sha256':'bad'}),a['id']))
        with self.assertRaises(Denied):self.g.execute(a['id'])
        self.assertTrue(p.exists())

    def test_I_context_revision_before_decision(self):
        a,p=self.deletion();t=self.s.task(a['task_id'])
        self.s.patch_context(t['id'],{'scope':['different']},t['revision'])
        with self.assertRaises(Denied):self.approve(a)
        self.assertTrue(p.exists())

    def test_I_context_evidence_after_decision(self):
        a,p=self.deletion();self.approve(a);t=self.s.task(a['task_id'])
        self.s.patch_context(t['id'],{'evidence':['changed']},t['revision'])
        with self.assertRaises(Denied):self.g.execute(a['id'])
        self.assertTrue(p.exists())

    def test_I_actual_git_content_before_decision(self):
        a=self.coding();(self.cpc/'readme.txt').write_text('changed')
        with self.assertRaises(Denied):self.approve(a)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM cpc_coding_sessions').fetchone()[0],0)

    def test_I_actual_git_content_after_decision(self):
        a=self.coding();self.approve(a);(self.cpc/'readme.txt').write_text('changed')
        with self.assertRaises(Denied):self.g.execute(a['id'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM cpc_coding_sessions').fetchone()[0],0)

    def test_I_git_index_change(self):
        a=self.coding();self.approve(a)
        subprocess.run(['/usr/bin/git','add','readme.txt'],cwd=self.cpc,check=True)
        with self.assertRaises(Denied):self.g.execute(a['id'])

    def test_J_wrong_task_or_action(self):
        a,p=self.deletion();b,q=self.deletion()
        for task,action in [(b['task_id'],a['id']),(a['task_id'],b['id'])]:
            with self.assertRaises(Denied):
                self.g.resolve_approval(a['approval']['id'],'APPROVE',task_id=task,action_id=action,actor='fixture')
        self.assertTrue(p.exists());self.assertTrue(q.exists())

    def test_K_concurrent_real_delete_once(self):
        a,p=self.deletion();self.approve(a);barrier=threading.Barrier(2)
        def run(_):
            s=Store(self.path)
            try:
                g=VerifierWorker(s,operator=self.op,cpc=self.cpc).gate();barrier.wait(timeout=5)
                try:g.execute(a['id']);return 'executed'
                except Denied:return 'denied'
            finally:s.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(run,range(2)))
        self.assertEqual(sorted(results),['denied','executed']);self.assertFalse(p.exists())

    def test_L_pending_survives_restart(self):
        a,p=self.deletion();before=self.s.approval(a['approval']['id']);self.s.close();self.s=Store(self.path)
        self.assertEqual(before,self.s.approval(a['approval']['id']));self.assertTrue(p.exists())

    def test_M_uncertain_never_replays_or_logs_exception_secret(self):
        calls=[]
        def fail(payload):calls.append(1);raise RuntimeError('secret-credential')
        g=Gate(self.s,{'payment':fail});a=g.propose(self.task()['id'],'payment',{'target':'fixture'},uid());self.approve(a)
        with self.assertRaises(RuntimeError):g.execute(a['id'])
        self.assertEqual(self.s.approval(a['approval']['id'])['lifecycle'],'UNCERTAIN')
        with self.assertRaises(Denied):g.execute(a['id'])
        self.assertEqual(calls,[1]);self.assertNotIn('secret-credential',json.dumps(self.s.task(a['task_id'])))

    def test_M_process_exit_claim_survives_restart(self):
        def stop(p):raise SystemExit()
        g=Gate(self.s,{'payment':stop});a=g.propose(self.task()['id'],'payment',{},uid());self.approve(a)
        with self.assertRaises(SystemExit):g.execute(a['id'])
        self.s.close();self.s=Store(self.path)
        with self.assertRaises(Denied):Gate(self.s,{'payment':lambda p:None}).execute(a['id'])
        self.assertEqual(self.s.approval(a['approval']['id'])['lifecycle'],'EXECUTING')

    def test_N_cpc_session_scope_still_enforced(self):
        a=self.coding();p={**a['payload'],'scope_files':'["../outside"]'}
        self.resolve(a,'EDIT',note='invalid fixture scope')
        b=self.g.propose(a['task_id'],'cpc_coding_start',p,uid());self.approve(b)
        with self.assertRaises(Denied):self.g.execute(b['id'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM cpc_coding_sessions').fetchone()[0],0)

    def test_N_valid_session_and_later_evidence_mutation(self):
        a=self.coding();result=self.resolve(a)
        b=self.g.propose(self.task()['id'],'cpc_coding_complete',{'workspace':'cpc','session_id':result['session_id'],'run_tests':'none'},uid())
        self.approve(b)
        self.s.db.execute("UPDATE cpc_coding_sessions SET scope_files='[]' WHERE id=?",(result['session_id'],))
        with self.assertRaises(Denied):self.g.execute(b['id'])

    def test_O_independent_verifier_cannot_be_bypassed(self):
        a=self.coding();result=self.resolve(a)
        b=self.g.propose(self.task()['id'],'git_commit',{'workspace':'cpc','session_id':result['session_id'],'message':'fixture never committed'},uid())
        self.approve(b)
        with self.assertRaises(Denied):self.g.execute(b['id'])
        self.assertFalse((self.cpc/'.git/refs/heads/master').exists())

    def test_O_protected_branch_push_denied(self):
        t=self.task();a=self.g.propose(t['id'],'git_push',{'workspace':'cpc','task_id':t['id'],'remote':'origin','branch':'main'},uid())
        self.approve(a)
        with self.assertRaises(Denied):self.g.execute(a['id'])

    def test_P_stop_and_readonly_override_approval(self):
        a,p=self.deletion();self.approve(a)
        for flag in ('stop_everything','read_only_mode'):
            Controls(self.s).set(flag,True,actor='fixture',reason='test')
            with self.assertRaises(Denied):self.g.execute(a['id'])
            self.assertTrue(p.exists());Controls(self.s).set(flag,False,actor='fixture',reason='test')

    def test_immutable_contract_and_trace(self):
        a,p=self.deletion();info=self.s.approval(a['approval']['id'])
        for k in ('approval_id','task_id','action_id','request_origin','payload','risk_tier','task_revision','evidence','created_at','expires_at'):
            self.assertIn(k,info['contract'])
        with self.assertRaises(sqlite3.IntegrityError):self.s.db.execute('DELETE FROM approval_contracts')
        self.resolve(a)
        events=self.s.task(a['task_id'])['audit']
        self.assertTrue({'APPROVAL_REQUESTED','APPROVAL_DECIDED','EXECUTION_CLAIMED','EXECUTION_RETURNED'} <= {e['event'] for e in events})

    def test_legacy_unsealed_approval_denied_and_editable(self):
        a,p=self.deletion()
        self.s.db.execute('DROP TRIGGER approval_contract_no_delete')
        self.s.db.execute('DELETE FROM approval_contracts WHERE approval_id=?',(a['approval']['id'],))
        with self.assertRaises(Denied):self.approve(a)
        self.resolve(a,'EDIT',note='new bound request required');self.assertTrue(p.exists())

    def test_extension_preserves_schema_and_backup(self):
        self.assertEqual(self.s.db.execute('pragma user_version').fetchone()[0],6)
        dest=self.root/'backup.sqlite3';self.s.backup(dest);restored=Store(dest)
        try:
            self.assertEqual(restored.db.execute('pragma integrity_check').fetchone()[0],'ok')
            self.assertEqual(restored.db.execute("SELECT version FROM core_extensions WHERE name='approval_contract'").fetchone()[0],1)
        finally:restored.close()

    def test_missing_extension_state_fails_closed(self):
        self.s.db.execute('DROP TABLE approval_contracts');self.s.close()
        with self.assertRaises(sqlite3.OperationalError):Store(self.path)
        self.s=sqlite3.connect(':memory:') # tearDown closes only

    def test_file_evidence_bytes_changed_denied(self):
        import hashlib
        inbox=self.op/'inbox';inbox.mkdir();f=inbox/'test.txt';f.write_text('evidence')
        t=self.task();t=self.s.patch_context(t['id'],{'evidence':[{'reference':'test.txt','sha256':hashlib.sha256(f.read_bytes()).hexdigest()}]},t['revision'])
        g=Gate(self.s,{'payment':lambda p:None})
        a=g.propose(t['id'],'payment',{},uid());self.approve(a);f.write_text('changed')
        with self.assertRaises(Denied):g.execute(a['id'])

    def test_verification_record_change_denied(self):
        a=self.coding();result=self.resolve(a);session=result['session_id']
        with self.s.transaction():
            record=self.worker.ver_records.create(task_id=a['task_id'],session_id=session,
                verifier='independent',test_command='fixture',test_argv=[],exit_code=0,
                stdout='',stderr='',passed=True,browser_required=False,status='PASS',evidence={'fixture':True})
        b=self.g.propose(self.task()['id'],'git_commit',{'workspace':'cpc','session_id':session,'message':'fixture'},uid())
        self.approve(b)
        self.s.db.execute("UPDATE verification_records SET evidence='{}' WHERE id=?",(record['id'],))
        with self.assertRaises(Denied):self.g.execute(b['id'])

    def test_expiry_configuration_rejects_invalid_values(self):
        for ttl in (0,-1,604801,float('inf'),float('nan')):
            with self.assertRaises(ValueError):self.g.propose(self.task()['id'],'payment',{},uid(),ttl_seconds=ttl)

    def test_approval_cannot_enable_unknown_capability(self):
        with self.assertRaises(Denied):self.g.propose(self.task()['id'],'arbitrary_shell',{'command':'echo bad'},uid())

    def test_edited_or_rejected_cannot_be_reopened(self):
        for choice in ('EDIT','REJECT'):
            a,p=self.deletion();self.resolve(a,choice,note='fixture')
            for next_choice in ('APPROVE','EDIT','REJECT'):
                with self.assertRaises(Conflict):self.s.decide(a['approval']['id'],next_choice,actor='fixture',note='fixture')
            self.assertTrue(p.exists())

    def test_concurrent_decisions_only_once(self):
        a,p=self.deletion();barrier=threading.Barrier(2)
        def decide(_):
            s=Store(self.path)
            try:
                barrier.wait(timeout=5)
                try:s.decide(a['approval']['id'],'APPROVE',actor='fixture');return 'approved'
                except Conflict:return 'denied'
            finally:s.close()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(decide,range(2)))
        self.assertEqual(sorted(results),['approved','denied']);self.assertTrue(p.exists())

    def test_approval_expiry_field_cannot_be_extended(self):
        a,p=self.deletion();self.approve(a)
        self.s.db.execute('UPDATE approvals SET expires=expires+100 WHERE id=?',(a['approval']['id'],))
        with self.assertRaises(Denied):self.g.execute(a['id'])
        self.assertTrue(p.exists())

    def test_expiry_during_slow_preflight_denied(self):
        class Slow:
            def validate(self,p,project):time.sleep(.03)
            def __call__(self,p):raise AssertionError('Must not dispatch')
        g=Gate(self.s,{'payment':Slow()});a=g.propose(self.task()['id'],'payment',{},uid(),ttl_seconds=.02)
        self.approve(a)
        with self.assertRaises(Denied):g.execute(a['id'])
        self.assertEqual(self.s.action(a['id'])['status'],'PENDING')

    def test_real_cli_review_and_approve_exact_action(self):
        import shutil,sys
        shutil.copytree(Path(__file__).resolve().parents[1]/'zamal_core',self.op/'zamal_core',ignore=shutil.ignore_patterns('__pycache__'))
        a,p=self.deletion()
        env={**os.environ,'PYTHONPATH':str(self.op),'PYTHONDONTWRITEBYTECODE':'1'}
        def cli(*args):
            r=subprocess.run([sys.executable,'-m','zamal_core',*args],cwd=self.op,env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(r.returncode,0,r.stderr);return json.loads(r.stdout)
        review=cli('approval',a['approval']['id'])
        self.assertEqual(review['contract']['payload'],a['payload'])
        cli('resolve-approval',a['approval']['id'],'APPROVE','--task-id',a['task_id'],'--action-id',a['id'])
        self.assertFalse(p.exists())
        self.assertEqual(cli('approval',a['approval']['id'])['lifecycle'],'EXECUTED')

    def test_O_unrelated_staged_file_cannot_ride_commit(self):
        a=self.coding();result=self.resolve(a);session=result['session_id']
        with self.s.transaction():
            self.worker.ver_records.create(task_id=a['task_id'],session_id=session,
                verifier='independent',test_command='fixture',test_argv=[],exit_code=0,
                stdout='',stderr='',passed=True,browser_required=False,status='PASS',evidence={'fixture':True})
        (self.cpc/'unrelated.txt').write_text('must not commit')
        subprocess.run(['/usr/bin/git','add','unrelated.txt'],cwd=self.cpc,check=True)
        b=self.g.propose(self.task()['id'],'git_commit',{'workspace':'cpc','session_id':session,'message':'must not execute'},uid())
        self.approve(b)
        with self.assertRaisesRegex(Denied,'Unrelated staged'):
            self.g.execute(b['id'])
        self.assertEqual(self.s.action(b['id'])['status'],'PENDING')

    def test_O_merge_cannot_target_a_different_current_branch(self):
        t=self.task()
        a=self.g.propose(t['id'],'git_merge',{'workspace':'cpc','task_id':t['id'],
            'source_branch':'fixture-source','target_branch':'fixture-target'},uid())
        self.approve(a)
        with self.assertRaisesRegex(Denied,'Merge target'):
            self.g.execute(a['id'])
        self.assertEqual(self.s.action(a['id'])['status'],'PENDING')
