import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zamal_core.store import Store,Gate,Denied,uid
from zamal_core.gateway_setup import GatewayAuthProvisioner,NAME

class GatewaySetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        path=self.root/'n8n/phase10';path.mkdir(parents=True)
        source=Path(__file__).resolve().parents[1]/'n8n/phase10/intake.json'
        workflow=json.loads(source.read_text())
        for n in workflow['nodes']:n.pop('credentials',None)
        (path/'intake.json').write_text(json.dumps(workflow))
        db=self.root/'.n8n-data/.n8n';db.mkdir(parents=True)
        with sqlite3.connect(db/'database.sqlite') as c:
            c.execute('create table credentials_entity(id,name,type,data)')
            c.execute('create table workflow_entity(id,name,nodes)')
        self.s=Store(self.root/'state/operator.sqlite3')
        self.payload={'operation':'provision_and_bind_local_gateway_auth','operator_secret_reference':'state/gateway/auth.env','credential_name':NAME}
        self.handler=GatewayAuthProvisioner(self.s,self.payload,self.root)
        self.g=Gate(self.s,{'credential_change':self.handler})
        t=self.s.create_task('fixture','security','operator',uid())
        for state in ('UNDERSTOOD','PLANNED'):t=self.s.transition(t['id'],state,t['revision'])
        self.a=self.g.propose(t['id'],'credential_change',self.payload,uid())
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def test_pending_cannot_create_secret(self):
        with self.assertRaises(Denied):self.g.execute(self.a['id'])
        self.assertFalse((self.root/'state/gateway').exists())
    def test_existing_secret_or_credentials_not_replaced(self):
        d=self.root/'state/gateway';d.mkdir();f=d/'auth.env';f.write_text('existing')
        with self.assertRaises(Denied):self.handler.validate(self.payload,'operator')
        self.assertEqual(f.read_text(),'existing')
    def test_changed_prepared_workflow_denied(self):
        self.handler.workflow.write_text('{}')
        with self.assertRaises(Denied):self.handler.validate(self.payload,'operator')
    def test_fixture_provision_is_private_bound_and_nonreplayable(self):
        def fake_cli(args,data,root):
            records=json.loads(data)
            with self.handler.connection() as reader:pass
            with sqlite3.connect(self.root/'.n8n-data/.n8n/database.sqlite') as c:
                for r in records:
                    if args[0]=='import:credentials':
                        self.assertTrue(r['data']['value'].startswith('Bearer '))
                        c.execute('insert into credentials_entity values(?,?,?,?)',(r['id'],r['name'],r['type'],'fixture-ciphertext'))
                    else:c.execute('insert into workflow_entity values(?,?,?)',(r['id'],r['name'],json.dumps(r['nodes'])))
        self.s.decide(self.a['approval']['id'],'APPROVE',actor='fixture')
        with patch('zamal_core.gateway_setup.n8n_cli',side_effect=fake_cli):result=self.g.execute(self.a['id'])
        self.assertTrue(result['binding_verified']);f=self.root/'state/gateway/auth.env'
        self.assertEqual(f.stat().st_mode & 0o777,0o600)
        secret=f.read_text().split('=',1)[1].strip()
        self.assertNotIn(secret,json.dumps(self.s.task(self.a['task_id'])))
        with self.assertRaises(Denied):self.g.execute(self.a['id'])
    def test_loader_rejects_permissive_file_and_symlink(self):
        spec=importlib.util.spec_from_file_location('run_gateway',Path(__file__).resolve().parents[1]/'scripts/run_gateway.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        f=self.root/'auth.env';f.write_text('ZAMAL_N8N_GATEWAY_SECRET='+'x'*43+'\n');f.chmod(0o644)
        with self.assertRaises(Denied):module.load_secret(f)
        f.chmod(0o600);self.assertEqual(len(module.load_secret(f)),43)
        link=self.root/'link';link.symlink_to(f)
        with self.assertRaises(OSError):module.load_secret(link)
