"""Real local browser plus existing task/Gate/Telegram path; no external service."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch,MagicMock

from zamal_core.store import Store,Denied,uid
from zamal_core.job_app import JobApplicationWorker,validate_job_app
from zamal_core.safety import Controls
from zamal_core.telegram_bot import TelegramBot,_approval_keyboard
from zamal_core.job_browser_fixture import identity


@unittest.skipUnless(sys.platform == 'darwin', 'macOS only')
class JobBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.op=Path(self.tmp.name)
        self.db=self.op/'state/operator.sqlite3';self.s=Store(self.db)
        self.worker=JobApplicationWorker(self.s,operator=self.op)
        self.gate=self.worker.gate()
        self.env=patch.dict(os.environ,{'ZAMAL_TELEGRAM_TOKEN':'fixture-token-never-a-real-credential','ZAMAL_TELEGRAM_CHAT_ID':'12345'})
        self.env.start();self.bot=TelegramBot(self.s,tmp_dir=self.op/'inbox');self.bot._api=MagicMock()

    def tearDown(self):
        self.env.stop();self.s.close();self.tmp.cleanup()

    def task(self):
        t=self.s.create_task('Synthetic browser form only','verification','operator',uid())
        for state in ('UNDERSTOOD','PLANNED'):t=self.s.transition(t['id'],state,t['revision'])
        return t

    def prepare(self,real=False):
        task=self.task();a=self.gate.propose(task['id'],'job_app_fixture_prepare',{'fixture':'local-browser-v1'},uid())
        if real:r=self.gate.execute(a['id'])
        else:
            with patch('zamal_core.job_browser_fixture.run',return_value={'contract':'fixture','fields':[],'upload_sha256':'fixture','bridge_identity':identity(),'submissions':0}):r=self.gate.execute(a['id'])
        task=self.task()
        a=self.gate.propose(task['id'],'job_app_submit',{'application_id':r['application_id'],'acknowledge_gaps':True},uid())
        return a,r

    def callback(self,a,choice,update=1,chat=12345):
        data={'update_id':update,'callback_query':{'id':str(update),'message':{'chat':{'id':chat}},'data':'approval:'+a['approval']['id']+':'+choice}}
        with patch('zamal_core.job_app.JobApplicationWorker',return_value=self.worker):
            return self.bot.process_update(data)

    def test_real_browser_telegram_boundary_resume_and_evidence(self):
        a,prepared=self.prepare(real=True)
        self.assertEqual(prepared['evidence']['submissions'],0)
        with self.assertRaises(Denied):self.gate.execute(a['id'])
        self.bot._reply(12345,{'outcome':'APPROVAL_REQUIRED','approval_id':a['approval']['id']})
        keyboard=self.bot._api.send_message.call_args.kwargs['reply_markup']
        self.assertEqual(keyboard,_approval_keyboard(a['approval']['id']))
        self.assertEqual([b['callback_data'].rsplit(':',1)[1] for b in keyboard['inline_keyboard'][0]],['APPROVE','EDIT','REJECT'])
        self.assertFalse(self.callback(a,'APPROVE',chat=99999)['ok'])
        self.assertTrue(self.callback(a,'APPROVE',update=2)['ok'])
        result=self.s.action(a['id'])['result']
        self.assertEqual(result['evidence']['submissions'],1)
        self.assertEqual(result['evidence']['confirmation'],'Local fixture accepted')
        self.assertFalse(self.callback(a,'APPROVE',update=3)['ok'])
        self.assertFalse(self.callback(a,'APPROVE',update=2)['ok'])
        app_id=prepared['application_id']
        audit=[dict(r) for r in self.s.db.execute('SELECT operation,detail FROM job_app_audit WHERE application_id=?',(app_id,))]
        self.assertEqual([x['operation'] for x in audit],['browser_prepared','browser_submit_verified'])
        self.s.close();self.s=Store(self.db)
        self.assertEqual(self.s.action(a['id'])['status'],'SUCCEEDED')
        output=os.environ.get('ZAMAL_BROWSER_EVIDENCE_DIR')
        if output:
            target=Path(output);target.mkdir(parents=True,exist_ok=True)
            (target/('live-'+uid()+'.json')).write_text(json.dumps({'scope':'Real sandboxed Chrome; synthetic local form; Telegram wire/API is a fixture, actual authorized-update handler and Gate are exercised','prepared':prepared,'approval_request':keyboard,'result':result,'audit':audit,'restart_verified':True,'replay_denied':True,'unauthorized_chat_denied':True,'no_external_submission':True},indent=2)+'\n')

    def test_edit_and_reject_never_dispatch(self):
        for choice in ('EDIT','REJECT'):
            a,_=self.prepare()
            with patch('zamal_core.job_browser_fixture.run') as run:
                self.assertTrue(self.callback(a,choice,update=int(a['id'].replace('-','')[:8],16))['ok'])
                with self.assertRaises(Denied):self.gate.execute(a['id'])
                run.assert_not_called()

    def test_changed_evidence_cannot_authorize(self):
        a,p=self.prepare()
        self.s.db.execute('UPDATE job_applications SET evidence=? WHERE id=?',('{}',p['application_id']))
        with patch('zamal_core.job_browser_fixture.run') as run:
            self.assertFalse(self.callback(a,'APPROVE')['ok']);run.assert_not_called()

    def test_emergency_controls_override_approval(self):
        for flag in ('stop_everything','pause_external_actions','read_only_mode'):
            a,_=self.prepare();Controls(self.s).set(flag,True,actor='fixture',reason='test')
            with patch('zamal_core.job_browser_fixture.run') as run:
                self.assertFalse(self.callback(a,'APPROVE',update=int(a['id'].replace('-','')[:8],16))['ok']);run.assert_not_called()
            Controls(self.s).set(flag,False,actor='fixture',reason='test complete')

    def test_no_url_file_profile_script_or_private_path_input(self):
        for key,value in [('url','https://example.com'),('file','/Users/book/Documents/private'),('script','submit()'),('profile','/Users/book/Library'),('delete',True)]:
            with self.assertRaises(Denied):validate_job_app('job_app_fixture_prepare',{'fixture':'local-browser-v1',key:value},'operator')
        with self.assertRaises(Denied):validate_job_app('job_app_fixture_prepare',{'fixture':'../private'},'operator')
        with self.assertRaises(Denied):validate_job_app('job_app_fixture_prepare',{'fixture':'local-browser-v1'},'cpc')

    def test_wrong_approval_binding_denied(self):
        a,_=self.prepare();b,_=self.prepare()
        with patch('zamal_core.job_browser_fixture.run') as run:
            with self.assertRaises(Denied):self.gate.execute(a['id'],approval_id=b['approval']['id'])
            run.assert_not_called()
