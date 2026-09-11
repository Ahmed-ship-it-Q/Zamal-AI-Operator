"""Approval-gated planner → coder → verifier handoff; draft data only.

A lightweight carrier task owns the single-use inference authorization. It points
to the ORIGINAL canonical task; it does not duplicate that task/context. The exact
source snapshot and allowed connection set bind the bounded three-stage chain.
"""
import json
import time

from .context_bus import ContextBus
from .providers import ProviderUnavailable, STAGES
from .routing import Router, RoutingPolicy, manifest
from .safety import Controls
from .store import Gate, Conflict, Denied, digest, encode, uid


class HandoffCapability:
    def __init__(self, engine):
        self.engine = engine

    def __call__(self, payload):
        raise Denied('Inference must go through the persistent gate')

    def validate(self, payload, project):
        self.engine.validate(payload, project)

    def run_with_controls(self, payload, baseline):
        return self.engine.run(payload, baseline)


class HandoffEngine:
    def __init__(self, store, registry, rules_root=None):
        self.store, self.registry = store, registry
        self.bus = ContextBus(store, rules_root)
        self.controls = Controls(store)

    def gate(self):
        return Gate(self.store, {'ai_inference': HandoffCapability(self)})

    def get(self, run_id):
        row = self.store.db.execute('SELECT * FROM ai_runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise Conflict('Unknown AI run')
        data = dict(row)
        data['policy'] = json.loads(data['policy'])
        return data

    @staticmethod
    def payload(run):
        return {'run_id': run['id'], 'source_task_id': run['task_id'],
                'source_hash': run['source_hash'], 'policy': run['policy']}

    def preview(self, task_id, policy=None):
        policy = policy or RoutingPolicy()
        snapshot = self.bus.snapshot(task_id, max_bytes=policy.max_context_bytes)
        report = {stage: Router(self.registry, policy).evaluate(snapshot, stage) for stage in STAGES}
        with self.store.transaction():
            self.bus._event(task_id, None, 'ROUTING_PREVIEW', report)
        return report

    def prepare(self, task_id, request_key, policy=None):
        policy = policy or RoutingPolicy()
        if not isinstance(request_key, str) or not request_key.strip():
            raise ValueError('AI run idempotency key required')
        self.controls.check('ai_inference')
        self.bus.ensure(task_id)
        spec = {'routing': policy.data(), 'connections': manifest(self.registry, policy)}
        # Reserve once before preparing the carrier. A crash during preparation
        # leaves a visible PREPARING record, never an executable untracked request.
        with self.store.transaction():
            source = self.bus._source(task_id)
            if source['original_task']['state'] in ('COMPLETED', 'REJECTED', 'FAILED', 'RECOVERING'):
                raise Denied('Terminal/recovering source task cannot start a new AI run')
            source_hash = digest(source)
            previous = self.store.db.execute('SELECT id FROM ai_runs WHERE task_id=? AND request_key=?', (task_id, request_key)).fetchone()
            if previous:
                run = self.get(previous[0])
                if run['source_hash'] != source_hash or run['policy'] != spec:
                    raise Conflict('AI run key reused for different context/policy')
                if run['status'] == 'PREPARING':
                    raise Conflict('Run preparation interrupted or in progress; inspect before retry')
                return run
            state = self.bus._state(task_id)
            if state['active_run']:
                raise Conflict('Canonical task already has an active AI owner')
            run_id = uid()
            self.store.db.execute('INSERT INTO ai_runs VALUES(?,?,?,?,?,?,NULL,NULL,?,?)',
                                  (run_id, task_id, request_key, source_hash, encode(source), encode(spec), 'PREPARING', time.time()))
            self.store.db.execute("UPDATE bus_state SET active_run=?,status='WAITING_APPROVAL',revision=revision+1,next_action='Review inference request' WHERE task_id=?", (run_id, task_id))
            self.bus._event(task_id, run_id, 'RUN_RESERVED', {'source_hash': source_hash, 'policy': spec})
        try:
            carrier = self.store.create_task('Authorize text-only AI handoff for canonical task ' + task_id,
                                             'ai_dispatch', source['original_task']['project'], 'ai-run:' + run_id,
                                             {'inputs': [{'canonical_task_id': task_id}],
                                              'plan': list(STAGES), 'rules': ['Draft-only inference; no tools or project execution']})
            for state in ('UNDERSTOOD', 'PLANNED'):
                carrier = self.store.transition(carrier['id'], state, carrier['revision'])
            run = self.get(run_id)
            action = self.gate().propose(carrier['id'], 'ai_inference', self.payload(run), 'inference')
            with self.store.transaction():
                self.store.db.execute("UPDATE ai_runs SET carrier_id=?,action_id=?,status='PREPARED' WHERE id=?", (carrier['id'], action['id'], run_id))
                self.bus._event(task_id, run_id, 'APPROVAL_LINKED', {'action_id': action['id'], 'approval_id': action['approval']['id']})
        except Exception:
            with self.store.transaction():
                self.store.db.execute("UPDATE ai_runs SET status='PREPARATION_FAILED' WHERE id=?", (run_id,))
                self.bus._event(task_id, run_id, 'PREPARATION_FAILED', {})
            raise
        return self.get(run_id)

    def validate(self, payload, project):
        if not isinstance(payload, dict) or set(payload) != {'run_id', 'source_task_id', 'source_hash', 'policy'}:
            raise Denied('Invalid inference action payload')
        run = self.get(payload['run_id'])
        if payload != self.payload(run) or run['status'] != 'PREPARED':
            raise Denied('Inference request not prepared or already claimed')
        if not run['action_id'] or not run['carrier_id']:
            raise Denied('Inference authorization linkage missing')
        linked = self.store.action(run['action_id'])
        if linked['task_id'] != run['carrier_id'] or linked['payload'] != payload:
            raise Denied('Inference authorization does not match carrier')
        if self.store._row('tasks', run['carrier_id'])['project'] != project:
            raise Denied('Inference carrier project mismatch')
        self.bus._assert_source(run)
        policy = RoutingPolicy.from_data(run['policy']['routing'])
        if manifest(self.registry, policy) != run['policy']['connections']:
            raise Denied('Provider connection/policy changed since approval request')
        state = self.bus._state(run['task_id'])
        if state['active_run'] != run['id']:
            raise Denied('Canonical context owner mismatch')

    def run(self, payload, baseline):
        run = self.get(payload['run_id'])
        linked = self.store.action(run['action_id'])
        if linked['status'] != 'EXECUTING' or not linked['approval'] or linked['approval']['status'] != 'CONSUMED':
            raise Denied('A claimed, approved inference action is required')
        policy = RoutingPolicy.from_data(run['policy']['routing'])
        router = Router(self.registry, policy)
        def check():
            self.controls.check('ai_inference', baseline)
            self.bus._assert_source(run)
            if manifest(self.registry, policy) != run['policy']['connections']:
                raise Denied('Provider configuration changed during handoff')
        try:
            with self.store.transaction():
                check()
                current = self.get(run['id'])
                if current['status'] != 'PREPARED':
                    raise Denied('AI run already claimed')
                self.store.db.execute("UPDATE ai_runs SET status='RUNNING' WHERE id=?", (run['id'],))
            for index, stage in enumerate(STAGES):
                with self.store.transaction():
                    check()
                    self.store.db.execute("UPDATE bus_state SET owner=?,status='RUNNING',revision=revision+1,next_action=? WHERE task_id=?",
                                          (stage, 'Receive ' + stage + ' draft', run['task_id']))
                    self.bus._event(run['task_id'], run['id'], 'HANDOFF',
                                    {'from': STAGES[index-1] if index else 'user', 'to': stage,
                                     'context_id': 'task:' + run['task_id'], 'source_hash': run['source_hash']})
                    snapshot = self.bus._snapshot(run['task_id'], max_bytes=policy.max_context_bytes)
                excluded = []
                while True:
                    check()
                    route = router.evaluate(snapshot, stage, excluded)
                    with self.store.transaction():
                        self.bus._event(run['task_id'], run['id'], 'ROUTE_SELECTED', route)
                    name = route['selected']
                    if name is None:
                        raise ProviderUnavailable('No policy-eligible provider remains')
                    try:
                        result = self.registry.adapters[name].invoke(snapshot, stage, check)
                        break
                    except ProviderUnavailable:
                        check()
                        excluded.append(name)
                        with self.store.transaction():
                            self.bus._event(run['task_id'], run['id'], 'PROVIDER_UNAVAILABLE', {'provider': name, 'stage': stage})
                with self.store.transaction():
                    check()
                    self.store.db.execute('INSERT INTO ai_results VALUES(?,?,?,?,?)',
                                          (run['id'], stage, name, snapshot.fingerprint, encode(result)))
                    self.bus._event(run['task_id'], run['id'], 'RESULT_PUBLISHED',
                                    {'provider': name, 'stage': stage, 'context_hash': snapshot.fingerprint})
            with self.store.transaction():
                check()
                self.store.db.execute("UPDATE ai_runs SET status='SUCCEEDED' WHERE id=?", (run['id'],))
                self.store.db.execute("UPDATE bus_state SET owner='user',status='READY_FOR_REVIEW',active_run=NULL,revision=revision+1,next_action='Review proposals; execution requires its own gate' WHERE task_id=?", (run['task_id'],))
                self.bus._event(run['task_id'], run['id'], 'HANDOFF_COMPLETE', {'stages': list(STAGES)})
            return {'run_id': run['id'], 'context_id': 'task:' + run['task_id'], 'stages': list(STAGES),
                    'status': 'DRAFTS_READY', 'executed_project_changes': False}
        except Exception as exc:
            with self.store.transaction():
                self.store.db.execute("UPDATE ai_runs SET status='BLOCKED' WHERE id=?", (run['id'],))
                self.store.db.execute("UPDATE bus_state SET status='RECOVERING',revision=revision+1,next_action='Review blocked run; never replay automatically' WHERE task_id=?", (run['task_id'],))
                self.bus._event(run['task_id'], run['id'], 'HANDOFF_BLOCKED', {'error_type': type(exc).__name__})
            raise
