"""Phase 9 bindings on the existing approvals/actions queue, never a second queue.

Trusted-local control plane. Contracts are immutable; lifecycle is persisted in
existing approvals/actions and derived for display. Legacy unsealed approvals
remain readable/cancellable but cannot authorize newly protected dispatch.
"""
import json
import os
import stat
import subprocess
from pathlib import Path
from .store import Denied, digest, encode


def migrate(store):
    with store.transaction():
        store.db.execute('CREATE TABLE IF NOT EXISTS core_extensions (name TEXT PRIMARY KEY, version INTEGER NOT NULL)')
        row = store.db.execute("SELECT version FROM core_extensions WHERE name='approval_contract'").fetchone()
        if row:
            if row[0] != 1:
                raise Denied('Unknown approval contract version')
            # Do not silently repair missing safety state.
            store.db.execute('SELECT approval_id,material,hash FROM approval_contracts LIMIT 0')
            return
        store.db.execute('''CREATE TABLE approval_contracts (
            approval_id TEXT PRIMARY KEY REFERENCES approvals(id),
            material TEXT NOT NULL, hash TEXT NOT NULL)''')
        store.db.execute('''CREATE TRIGGER approval_contract_no_update BEFORE UPDATE ON approval_contracts
            BEGIN SELECT RAISE(ABORT, 'approval contract is immutable'); END''')
        store.db.execute('''CREATE TRIGGER approval_contract_no_delete BEFORE DELETE ON approval_contracts
            BEGIN SELECT RAISE(ABORT, 'approval contract is immutable'); END''')
        store.db.execute("INSERT INTO core_extensions VALUES('approval_contract',1)")


def records(store, task_id, payload):
    # Existing workflow carrier tasks may reference a coding task/session. Bind
    # both identities explicitly instead of pretending they are the same task.
    ids = {task_id}
    if payload.get('task_id'):
        ids.add(payload['task_id'])
    if payload.get('session_id'):
        row = store.db.execute('SELECT task_id FROM cpc_coding_sessions WHERE id=?', (payload['session_id'],)).fetchone()
        if row:
            ids.add(row[0])
    result = {}
    for key, table in (('event_id','personal_calendar'), ('subscription_id','personal_subscriptions'), ('record_id','personal_entries')):
        if payload.get(key) and store.db.execute("SELECT 1 FROM core_extensions WHERE name='personal_ops'").fetchone():
            row=store.db.execute('SELECT * FROM '+table+' WHERE id=?',(payload[key],)).fetchone()
            result['personal_'+key]=dict(row) if row else None
    if payload.get('connector_id'):
        row = store.db.execute('SELECT * FROM connection_controls WHERE connector_id=?',(payload['connector_id'],)).fetchone()
        result['connection_control'] = dict(row) if row else None
    for table in ('cpc_coding_sessions', 'git_snapshots', 'verification_records', 'task_worktrees', 'rollback_log'):
        result[table] = [dict(r) for tid in sorted(ids) for r in store.db.execute(
            f'SELECT * FROM {table} WHERE task_id=? ORDER BY id', (tid,))]
    result['referenced_tasks'] = [dict(store._row('tasks', tid)) for tid in sorted(ids) if tid != task_id]
    if payload.get('output_id'):
        row = store.db.execute('SELECT * FROM worker_outputs WHERE id=?', (payload['output_id'],)).fetchone()
        result['output'] = dict(row) if row else None
    return result


def task_identity(task):
    return {k: task[k] for k in ('id','request_key','fingerprint','command','task_type','project','context')}


def git_identity(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise Denied('Invalid approval repository root')
    env = {'PATH':'/usr/bin:/bin', 'HOME':'/var/empty', 'GIT_OPTIONAL_LOCKS':'0',
           'GIT_CONFIG_NOSYSTEM':'1', 'GIT_CONFIG_GLOBAL':'/dev/null', 'LC_ALL':'C'}
    def run(args, allow_unborn=False):
        r = subprocess.run(['/usr/bin/git','--no-optional-locks',*args],cwd=root,env=env,
                           capture_output=True,timeout=20,shell=False)
        if r.returncode and not allow_unborn:
            raise Denied('Cannot capture approval Git identity')
        if len(r.stdout)>8*1024*1024:
            raise Denied('Approval Git evidence exceeds bound')
        return r.stdout
    run(['rev-parse','--show-toplevel'])
    files = run(['ls-files','--others','--exclude-standard','-z']).split(b'\0')
    untracked = []
    for raw in files:
        if not raw: continue
        rel = os.fsdecode(raw)
        p = root / rel
        if p.is_symlink() or any(x.is_symlink() for x in p.parents if x != root.parent):
            raise Denied('Symlink in approval evidence')
        st = p.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 8*1024*1024:
            raise Denied('Unsupported approval evidence file')
        import hashlib
        untracked.append([rel,hashlib.sha256(p.read_bytes()).hexdigest()])
    import hashlib
    result = {'root':str(root.resolve()),'root_identity':[root.stat().st_dev,root.stat().st_ino],
              'untracked':untracked}
    for key,args in {
        'head':['rev-parse','--verify','HEAD'],
        'branch':['symbolic-ref','--quiet','HEAD'],
        'refs':['show-ref'],
        'status':['status','--porcelain=v1','-z','--untracked-files=all'],
        'diff':['diff','--binary','--no-ext-diff','--no-textconv'],
        'index':['diff','--cached','--binary','--no-ext-diff','--no-textconv'],
        'config':['config','--local','--list'],
    }.items():
        data=run(args,allow_unborn=key in ('head','branch','refs'))
        result[key]=hashlib.sha256(data).hexdigest()
    return result


def worker_identity(handler, payload):
    worker = getattr(handler, 'worker', None)
    if worker is None or payload.get('workspace') != 'cpc' or getattr(handler, 'kind', '') not in (
        'cpc_coding_start','cpc_coding_complete','verify_tests','git_worktree_create',
        'git_rollback','git_commit','git_push','git_merge'):
        return None
    return git_identity(worker.paths.roots['cpc'])


def file_evidence(task, inbox):
    import hashlib
    items = json.loads(task['context']).get('evidence', [])
    result = []
    for item in items:
        if not isinstance(item, dict) or 'reference' not in item or 'sha256' not in item:
            continue
        ref = Path(item['reference'])
        root = Path(inbox).absolute()
        path = ref if ref.is_absolute() else root / ref
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            raise Denied('Evidence outside approved inbox') from None
        if not parts or any(p in ('.','..') or p.startswith('.') for p in parts):
            raise Denied('Unsafe approval evidence path')
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = child
            f = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(f, 'rb') as stream:
                st = os.fstat(stream.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 32*1024*1024:
                    raise Denied('Unsupported approval evidence file')
                data = stream.read(32*1024*1024+1)
                sha = hashlib.sha256(data).hexdigest()
                if len(data)>32*1024*1024 or sha != item['sha256']:
                    raise Denied('Approval evidence file changed')
                result.append({'reference':str(ref),'sha256':sha})
        finally:
            os.close(fd)
    return result


def seal(store, action, handler):
    approval = action['approval']
    task = store._row('tasks',action['task_id'])
    worker = getattr(handler, 'worker', None)
    inbox = (worker.paths.roots['operator'] if worker is not None else store.path.parent.parent) / 'inbox'
    try:
        files = file_evidence(task, inbox)
    except (OSError, Denied):
        files = None  # Reviewable proposal; missing evidence can never authorize dispatch.
    material = {'inbox':str(inbox), 'files':files, 'approval_id':approval['id'],'action_id':action['id'],'task_id':task['id'],
                'request_origin':task['request_key'],'action_request_key':action['request_key'],
                'kind':action['kind'],'payload':action['payload'],'binding':action['binding'],
                'risk_tier':'PROTECTED','task_revision':task['revision'],
                'task':task_identity(task),'evidence':records(store,task['id'],action['payload']),
                'worker_evidence':worker_identity(handler,action['payload']),
                'created_at':approval['created'],'expires_at':approval['expires']}
    store.db.execute('INSERT INTO approval_contracts VALUES(?,?,?)',
                     (approval['id'],encode(material),digest(material)))


def validate(store, action, stage, handler=None):
    a=action['approval']
    if not a: raise Denied('Missing approval')
    row=store.db.execute('SELECT * FROM approval_contracts WHERE approval_id=?',(a['id'],)).fetchone()
    if row is None: raise Denied('Legacy or missing approval contract; EDIT and request a new approval')
    m=json.loads(row['material'])
    if digest(m)!=row['hash']: raise Denied('Corrupt approval contract')
    task=store._row('tasks',action['task_id'])
    offset={'decision':0,'execution':1,'dispatch':2}[stage]
    if (m['approval_id']!=a['id'] or m['action_id']!=action['id'] or m['task_id']!=task['id']
        or m['kind']!=action['kind'] or m['payload']!=action['payload']
        or m['binding']!=action['binding'] or a['binding']!=action['binding']
        or m['expires_at']!=a['expires'] or m['created_at']!=a['created']
        or task['revision']!=m['task_revision']+offset or task_identity(task)!=m['task']
        or records(store,task['id'],action['payload'])!=m['evidence']):
        raise Denied('Approval task, payload, revision or evidence changed; new approval required')
    if m['files'] is None or file_evidence(task, m['inbox']) != m['files']:
        raise Denied('Approval file evidence changed')
    if stage == 'decision' and m['worker_evidence'] is not None:
        if git_identity(m['worker_evidence']['root']) != m['worker_evidence']:
            raise Denied('Approval repository/diff changed; new approval required')
    if stage != 'decision':
        current=worker_identity(handler,action['payload'])
        if current!=m['worker_evidence']:
            raise Denied('Approval repository/diff changed or was not captured; new approval required')
    return m


def lifecycle(action):
    a=action['approval']
    if not a: return None
    if a['status']=='CONSUMED':
        return {'SUCCEEDED':'EXECUTED','UNCERTAIN':'UNCERTAIN','EXECUTING':'EXECUTING'}.get(action['status'],'INVALID')
    return {'EDIT_REQUESTED':'SUPERSEDED'}.get(a['status'],a['status'])


def worker_safety(store, handler, payload):
    """Additional approval-bound checks around existing Phase 7/8 capabilities."""
    worker = getattr(handler, 'worker', None)
    kind = getattr(handler, 'kind', '')
    if worker is None: return
    if kind == 'git_commit':
        session = worker.cpc_sessions.session(payload['session_id'])
        scope = json.loads(session['scope_files'])
        before = json.loads(session['git_before'])
        snap = worker.ver_git.snapshot_detailed()
        # Phase 8 commits the index: unrelated pre-staged files must never ride along.
        if any(not worker.cpc_scope._in_scope(f, scope) for f in snap['staged']):
            raise Denied('Unrelated staged files; commit approval cannot include them')
        if set(snap['dirty_files']) & set(before['dirty_files']):
            raise Denied('Pre-existing dirty files cannot be committed by this approval')
    if kind == 'git_merge':
        if worker.ver_git.branch() != payload['target_branch']:
            raise Denied('Merge target is not the current branch')
