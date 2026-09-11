"""Bounded, trusted-local provisioning for the explicitly approved Phase 10 action.

Not a general credential editor. Invoke only through the existing Gate. Plaintext
is confined to a private owner file and the n8n importer stdin, never argv/logs.
"""
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
from .store import Denied, digest
from .safety import Controls

ROOT = Path(__file__).resolve().parents[1]
NODE = Path('/Users/book/.nvm/versions/node/v22.23.2/bin/node')
NAME = 'Zamal Operator Gateway'


def cli_env(root=ROOT):
    return {**os.environ,'PATH':str(NODE.parent)+':/usr/bin:/bin',
            'N8N_USER_FOLDER':str(root/'.n8n-data'),
            'N8N_DIAGNOSTICS_ENABLED':'false','N8N_VERSION_NOTIFICATIONS_ENABLED':'false',
            'N8N_PERSONALIZATION_ENABLED':'false'}


def n8n_cli(args, data=None, root=ROOT, timeout=90):
    # Output can contain credential-bearing execution errors. Never expose it.
    r=subprocess.run([str(NODE),str(root/'node_modules/n8n/bin/n8n'),*args],
        input=data,capture_output=True,env=cli_env(root),cwd=root,timeout=timeout,shell=False)
    if r.returncode: raise Denied('n8n operation failed; inspect metadata before recovery')
    return r.stdout


class GatewayAuthProvisioner:
    def __init__(self, store, approved_payload, root=ROOT):
        self.store,self.root,self.expected=store,Path(root),approved_payload
        self.workflow=self.root/'n8n/phase10/intake.json'
        self.workflow_hash=digest(json.loads(self.workflow.read_text()))

    def connection(self):
        return sqlite3.connect((self.root/'.n8n-data/.n8n/database.sqlite').as_uri()+'?mode=ro',uri=True)

    def validate(self, payload, project):
        if payload!=self.expected or project!='operator' or payload.get('operation')!='provision_and_bind_local_gateway_auth':
            raise Denied('Exact approved gateway configuration required')
        if payload.get('operator_secret_reference')!='state/gateway/auth.env' or payload.get('credential_name')!=NAME:
            raise Denied('Unapproved credential destination')
        if (self.root/'state/gateway/auth.env').exists():raise Denied('Gateway secret already exists; no replacement')
        for p in (self.root/'state',self.root/'state/gateway',self.workflow):
            if p.is_symlink():raise Denied('Symlink in gateway setup paths')
        workflow=json.loads(self.workflow.read_text())
        if digest(workflow)!=self.workflow_hash:raise Denied('Prepared intake changed')
        http=[n for n in workflow['nodes'] if n['name']=='Operator gateway']
        if len(http)!=1 or http[0]['parameters']['url']!='http://127.0.0.1:8788/events' or http[0]['parameters']['method']!='POST':
            raise Denied('Unapproved intake endpoint')
        with self.connection() as c:
            if c.execute('SELECT 1 FROM credentials_entity WHERE name=?',(NAME,)).fetchone():
                raise Denied('Existing gateway credential cannot be replaced')
            if c.execute('SELECT 1 FROM workflow_entity WHERE name=?',(workflow['name'],)).fetchone():
                raise Denied('Existing intake workflow cannot be replaced')

    def __call__(self,payload):raise Denied('Use the approval gate')

    def run_with_controls(self,payload,baseline):
        check=lambda:Controls(self.store).check('credential_change',baseline)
        check();self.validate(payload,'operator')
        target=self.root/'state/gateway';target.mkdir(mode=0o700,exist_ok=False)
        secret=secrets.token_urlsafe(32)
        fd=os.open(target/'auth.env',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:
            f.write('ZAMAL_N8N_GATEWAY_SECRET='+secret+'\n');f.flush();os.fsync(f.fileno())
        check()
        cid=secrets.token_hex(8)
        credential=[{'id':cid,'name':NAME,'type':'httpHeaderAuth',
                     'data':{'name':'Authorization','value':'Bearer '+secret}}]
        n8n_cli(['import:credentials','--input','/dev/stdin'],json.dumps(credential).encode(),self.root)
        with self.connection() as c:
            row=c.execute('SELECT name,type,data FROM credentials_entity WHERE id=?',(cid,)).fetchone()
            if not row or row[:2]!=(NAME,'httpHeaderAuth') or not isinstance(row[2],str) or secret in row[2]:
                raise Denied('Credential import/encryption not verified')
        check()
        workflow=json.loads(self.workflow.read_text());workflow['id']='ZamalP10Intake01'
        for node in workflow['nodes']:
            if node['name']=='Operator gateway':node['credentials']={'httpHeaderAuth':{'id':cid,'name':NAME}}
        n8n_cli(['import:workflow','--input','/dev/stdin'],json.dumps([workflow]).encode(),self.root)
        with self.connection() as c:
            row=c.execute('SELECT nodes FROM workflow_entity WHERE id=?',(workflow['id'],)).fetchone()
            if not row or not any(n.get('credentials',{}).get('httpHeaderAuth',{}).get('id')==cid for n in json.loads(row[0])):
                raise Denied('Intake credential binding not verified')
        # Persist only the non-secret credential reference in the prepared module.
        self.workflow.write_text(json.dumps(workflow,indent=2)+'\n')
        return {'credential_id':cid,'credential_name':NAME,'workflow_id':workflow['id'],
                'secret_reference':'state/gateway/auth.env','private_file':True,
                'credential_encrypted':True,'binding_verified':True}
