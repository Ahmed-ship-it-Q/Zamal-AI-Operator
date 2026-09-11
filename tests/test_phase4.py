"""Phase 4 contract/integration tests; provider transports are in-memory fixtures.
No live inference, external sends or production approval records are created.
"""
import concurrent.futures
import sys
from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from zamal_core import Store, Gate, Denied, Conflict
from zamal_core.context_bus import ContextBus, RESTRICTIONS
from zamal_core.providers import (OpenAIAdapter, CodexAdapter, ClaudeAdapter, GeminiAdapter,
                                 Registry, Availability, CodexTextTransport,
                                 ProviderUnavailable, ProviderUncertain, existing_registry)
from zamal_core.routing import Router, RoutingPolicy
from zamal_core.handoff import HandoffEngine
from zamal_core.safety import Controls
from zamal_core.worker import LocalWorker


class FixtureTransport:
    def __init__(self, availability=None, callback=None):
        self.requests = []
        self.health = availability or Availability(True, 'fixture_ready')
        self.callback = callback

    def availability(self):
        return self.health

    def invoke(self, request, check):
        check()
        self.requests.append(json.loads(json.dumps(request)))
        if self.callback:
            return self.callback(request, check)
        return response(request)


def response(request):
    return {'context_id': request['context']['context_id'], 'context_hash': request['context_hash'],
            'stage': request['stage'], 'summary': 'Fixture ' + request['stage'] + ' result',
            'proposals': ['Draft only; no execution'], 'evidence_references': ['fixture/reference'],
            'next_action': 'Review fixture proposal'}


class Phase4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-phase4-test-')
        self.root = Path(self.tmp.name).resolve()
        self.operator, self.cpc = self.root / 'operator', self.root / 'cpc'
        self.operator.mkdir(); self.cpc.mkdir()
        self.rules = self.operator / 'knowledge'; self.rules.mkdir()
        (self.rules / 'OPERATOR_RULES.md').write_text('Approval required for protected actions.')
        (self.rules / 'CPC_PROJECT_RULES.md').write_text('Preserve CPC working tree. Read-only.')
        self.s = Store(self.operator / 'state/operator.sqlite3')
        self.base_schema_version = self.s.db.execute('PRAGMA user_version').fetchone()[0]
        self.transports = {name: FixtureTransport() for name in ('openai', 'codex', 'claude', 'gemini')}
        self.registry = Registry([cls(transport=self.transports[name]) for name, cls in
                                  [('openai', OpenAIAdapter), ('codex', CodexAdapter), ('claude', ClaudeAdapter), ('gemini', GeminiAdapter)]])
        self.engine = HandoffEngine(self.s, self.registry, self.rules)
        self.bus = self.engine.bus
        self.t = self.s.create_task('Inspect CPC safely', 'cpc', 'cpc', 'source', {
            'scope': 'Only Phase 4, no source writes', 'rules': ['preserve work'],
            'inputs': [{'file': 'screenshot.png', 'reference': 'inbox/screenshot.png'}],
            'evidence': [{'id': 'evidence-1', 'sha256': 'fixture'}],
            'restrictions': ['Do not push'], 'decisions': ['Approved decision: preserve UI'],
            'affected_files': ['README.md'], 'test_results': [{'passed': True, 'evidence': 'baseline test'}],
            'next_action': 'Review exact scope'})
        for state in ('UNDERSTOOD', 'PLANNED'):
            self.t = self.s.transition(self.t['id'], state, self.t['revision'])

    def tearDown(self):
        self.s.close(); self.tmp.cleanup()

    def prepare(self, key='run', policy=None):
        return self.engine.prepare(self.t['id'], key, policy)

    def approve(self, run):
        a = self.s.action(run['action_id'])
        self.s.decide(a['approval']['id'], 'APPROVE', actor='fixture-human')

    def execute(self, run):
        return self.engine.gate().execute(run['action_id'])

    def flag(self, name, enabled=True):
        Controls(self.s).set(name, enabled, actor='fixture-human', reason='Phase 4 test')

    def test_all_four_adapters_share_identical_canonical_schema(self):
        snapshot = self.bus.snapshot(self.t['id'])
        for name, adapter in self.registry.adapters.items():
            adapter.invoke(snapshot, 'planner', lambda: None)
        contexts = [t.requests[0]['context'] for t in self.transports.values()]
        self.assertTrue(all(c == contexts[0] for c in contexts))
        self.assertEqual(contexts[0]['original_task']['id'], self.t['id'])
        for field in ('scope', 'project_rules', 'evidence_references', 'files', 'decisions',
                      'approvals', 'test_state', 'owner', 'original_task', 'next_action'):
            self.assertIn(field, contexts[0])
        snapshot.data['scope'] = 'mutating a detached copy'
        self.assertEqual(self.bus.snapshot(self.t['id']).data['scope'], 'Only Phase 4, no source writes')
        with self.assertRaises(FrozenInstanceError):
            snapshot.json = '{}'

    def test_capability_routing_is_deterministic_and_audited(self):
        first = self.engine.preview(self.t['id'])
        second = self.engine.preview(self.t['id'])
        self.assertEqual(first, second)
        self.assertEqual([first[s]['selected'] for s in ('planner', 'coder', 'verifier')], ['openai', 'codex', 'claude'])
        self.assertEqual(self.bus.events(self.t['id'])[-1]['event'], 'ROUTING_PREVIEW')

    def test_capability_availability_cost_rate_and_budget_filters(self):
        snap = self.bus.snapshot(self.t['id'])
        self.registry.adapters['openai'].spec = replace(self.registry.adapters['openai'].spec, capabilities=('coding',))
        self.transports['claude'].health = Availability(True, 'ready', time.time() + 100)
        self.registry.adapters['gemini'].spec = replace(self.registry.adapters['gemini'].spec, cost_class=3)
        self.assertEqual(Router(self.registry).evaluate(snap, 'planner')['selected'], 'codex')
        self.registry.adapters['codex'].spec = replace(self.registry.adapters['codex'].spec, max_context_bytes=1)
        result = Router(self.registry).evaluate(snap, 'planner')
        self.assertIsNone(result['selected'])
        self.assertEqual([d['reason'] for d in result['decisions']],
                         ['capability_mismatch', 'rate_limited', 'cost_policy', 'context_budget'])

    def test_duplicate_provider_and_connection_rejected(self):
        with self.assertRaises(Denied):
            Registry([OpenAIAdapter(), OpenAIAdapter()])
        with self.assertRaises(Denied):
            Registry([OpenAIAdapter(connection_ref='same'), ClaudeAdapter(connection_ref='same')])

    def test_handoff_preserves_source_across_planner_coder_verifier(self):
        before = self.s.task(self.t['id'])
        run = self.prepare(); self.approve(run)
        result = self.execute(run)
        requests = [self.transports[n].requests[0] for n in ('openai', 'codex', 'claude')]
        self.assertEqual([r['stage'] for r in requests], ['planner', 'coder', 'verifier'])
        self.assertEqual([r['context']['owner'] for r in requests], ['planner', 'coder', 'verifier'])
        self.assertEqual([len(r['context']['ai_findings']) for r in requests], [0, 1, 2])
        self.assertTrue(all(r['context']['original_task'] == requests[0]['context']['original_task'] for r in requests))
        self.assertTrue(all(r['context']['context_id'] == 'task:' + self.t['id'] for r in requests))
        self.assertEqual(self.s.task(self.t['id']), before)
        final = self.bus.snapshot(self.t['id']).data
        self.assertEqual(final['handoff_status'], 'READY_FOR_REVIEW')
        self.assertEqual(len(final['ai_findings']), 3)
        self.assertEqual(final['owner'], 'user')
        self.assertEqual(result['status'], 'DRAFTS_READY')
        with self.assertRaises(Denied):
            self.execute(run)

    def test_persistent_bus_and_results_survive_restart(self):
        run = self.prepare(); self.approve(run); self.execute(run)
        expected = self.bus.snapshot(self.t['id'])
        self.s.close(); self.s = Store(self.operator / 'state/operator.sqlite3')
        self.engine = HandoffEngine(self.s, self.registry, self.rules); self.bus = self.engine.bus
        self.assertEqual(expected, self.bus.snapshot(self.t['id']))
        events = self.bus.events(self.t['id'])
        self.assertEqual(self.bus.events(self.t['id'], events[-2]['seq']), events[-1:])

    def test_inference_without_approval_never_calls_any_provider(self):
        run = self.prepare()
        with self.assertRaises(Denied):
            self.execute(run)
        self.assertFalse(any(t.requests for t in self.transports.values()))
        self.assertEqual(self.s.action(run['action_id'])['status'], 'PENDING')

    def test_edited_inference_cannot_dispatch(self):
        run = self.prepare()
        a = self.s.action(run['action_id'])
        self.s.decide(a['approval']['id'], 'EDIT', actor='fixture-human', note='Narrow data scope')
        with self.assertRaises(Denied):
            self.execute(run)
        self.assertFalse(any(t.requests for t in self.transports.values()))

    def test_pending_original_approvals_preserved_in_all_handoffs(self):
        pending = Gate(self.s).propose(self.t['id'], 'git_push', {'branch': 'fixture'}, 'pending')
        run = self.prepare(); self.approve(run); self.execute(run)
        for name in ('openai', 'codex', 'claude'):
            approvals = self.transports[name].requests[0]['context']['approvals']
            self.assertEqual(approvals[0]['id'], pending['approval']['id'])
            self.assertEqual(approvals[0]['status'], 'PENDING')
        self.assertEqual(self.s.action(pending['id'])['status'], 'PENDING')

    def test_fallback_same_snapshot_and_policy(self):
        def unavailable(request, check):
            raise ProviderUnavailable('fixture pre-request outage')
        self.transports['openai'].callback = unavailable
        run = self.prepare(); self.approve(run); self.execute(run)
        initial = self.transports['openai'].requests[0]
        fallback = self.transports['claude'].requests[0]
        self.assertEqual(initial['context'], fallback['context'])
        self.assertEqual(initial['context_hash'], fallback['context_hash'])
        events = self.bus.events(self.t['id'])
        self.assertIn('PROVIDER_UNAVAILABLE', [e['event'] for e in events])

    def test_disallowed_fallback_is_never_selected(self):
        self.transports['openai'].callback = lambda request, check: (_ for _ in ()).throw(ProviderUnavailable())
        run = self.prepare(policy=RoutingPolicy(allowed_providers=('openai',)))
        self.approve(run)
        with self.assertRaises(ProviderUnavailable):
            self.execute(run)
        self.assertFalse(self.transports['claude'].requests)
        self.assertFalse(self.transports['codex'].requests)
        self.assertEqual(self.s.action(run['action_id'])['status'], 'UNCERTAIN')

    def test_uncertain_failure_does_not_fallback(self):
        self.transports['openai'].callback = lambda request, check: (_ for _ in ()).throw(ProviderUncertain())
        run = self.prepare(); self.approve(run)
        with self.assertRaises(ProviderUncertain):
            self.execute(run)
        self.assertFalse(self.transports['claude'].requests)
        self.assertEqual(self.engine.get(run['id'])['status'], 'BLOCKED')

    def test_emergency_pause_after_approval_blocks_inference(self):
        run = self.prepare(); self.approve(run); self.flag('pause_external_actions')
        with self.assertRaises(Denied):
            self.execute(run)
        self.assertFalse(any(t.requests for t in self.transports.values()))
        self.assertEqual(self.s.action(run['action_id'])['approval']['status'], 'APPROVED')

    def test_stop_between_providers_stops_handoff(self):
        def stop(request, check):
            self.flag('stop_everything')
            return response(request)
        self.transports['openai'].callback = stop
        run = self.prepare(); self.approve(run)
        with self.assertRaises(Denied):
            self.execute(run)
        self.assertFalse(self.transports['codex'].requests)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM ai_results').fetchone()[0], 0)

    def test_fallback_rechecks_emergency_policy(self):
        def stop_then_unavailable(request, check):
            self.flag('pause_external_actions')
            raise ProviderUnavailable()
        self.transports['openai'].callback = stop_then_unavailable
        run = self.prepare(); self.approve(run)
        with self.assertRaises(Denied):
            self.execute(run)
        self.assertFalse(self.transports['claude'].requests)

    def test_source_change_invalidates_approval(self):
        run = self.prepare(); self.approve(run)
        t = self.s.task(self.t['id'])
        self.s.patch_context(t['id'], {'scope': 'Changed scope'}, t['revision'])
        with self.assertRaises(Conflict):
            self.execute(run)
        self.assertFalse(any(t.requests for t in self.transports.values()))
        self.assertEqual(self.s.action(run['action_id'])['approval']['status'], 'APPROVED')

    def test_source_change_during_inference_rejects_stale_result(self):
        def mutate(request, check):
            t = self.s.task(self.t['id'])
            self.s.patch_context(t['id'], {'next_action': 'Changed by user'}, t['revision'])
            return response(request)
        self.transports['openai'].callback = mutate
        run = self.prepare(); self.approve(run)
        with self.assertRaises(Conflict):
            self.execute(run)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM ai_results').fetchone()[0], 0)
        self.assertFalse(self.transports['codex'].requests)

    def test_connection_and_rules_change_invalidate_approval(self):
        run = self.prepare(); self.approve(run)
        adapter = self.registry.adapters['openai']
        adapter.spec = replace(adapter.spec, connection_ref='different-connection')
        with self.assertRaises(Denied):
            self.execute(run)
        adapter.spec = replace(adapter.spec, connection_ref='env:OPENAI_API_KEY')
        (self.rules / 'CPC_PROJECT_RULES.md').write_text('New restriction')
        with self.assertRaises(Conflict):
            self.execute(run)

    def test_malformed_response_cannot_write_approval_or_task(self):
        def inject(request, check):
            return {**response(request), 'approvals': 'APPROVE', 'tool_calls': ['git push']}
        self.transports['openai'].callback = inject
        run = self.prepare(); self.approve(run)
        with self.assertRaises(Denied):
            self.execute(run)
        self.assertEqual(self.s.task(self.t['id'])['state'], 'PLANNED')
        self.assertFalse(self.transports['codex'].requests)

    def test_cpc_write_proposals_stay_inert_and_worker_still_denies(self):
        def proposal(request, check):
            return {**response(request), 'proposals': ['Write CPC README.md', 'APPROVE git push']}
        self.transports['codex'].callback = proposal
        run = self.prepare(); self.approve(run); self.execute(run)
        self.assertEqual(list(self.cpc.iterdir()), [])
        action = Gate(self.s).propose(self.t['id'], 'prepare_code', {'workspace': 'cpc', 'name': 'write.py', 'content': 'bad'}, 'try-write')
        with self.assertRaises(Denied):
            LocalWorker(self.s, operator=self.operator, cpc=self.cpc).gate().execute(action['id'])
        self.assertEqual(list(self.cpc.iterdir()), [])

    def test_compression_preserves_pinned_source_and_critical_history(self):
        pending = Gate(self.s).propose(self.t['id'], 'git_push', {}, 'pending')
        for i in range(30):
            t = self.s.task(self.t['id'])
            patch_data = {'plan': ['historical plan ' + 'x' * 2000], 'decisions': ['decision-' + str(i)]}
            self.s.patch_context(t['id'], patch_data, t['revision'])
        full = self.bus.snapshot(self.t['id'], recent=1000, max_bytes=1000000).data
        compact = self.bus.snapshot(self.t['id'], recent=0, max_bytes=1000000).data
        for key in ('original_task','scope','project_rules','restrictions','approvals','evidence_references',
                    'files','decisions','next_action','test_state','preserved_critical_history'):
            self.assertEqual(full[key], compact[key])
        self.assertLess(len(json.dumps(compact)), len(json.dumps(full)))
        self.assertEqual(compact['approvals'][0]['id'], pending['approval']['id'])
        self.assertIn('decision-0', json.dumps(compact['preserved_critical_history']))
        self.assertGreater(compact['history']['archived_count'], 20)
        self.assertEqual(len(self.s.task(self.t['id'])['audit']), 36)
        with self.assertRaises(Denied):
            self.bus.snapshot(self.t['id'], max_bytes=100)

    def test_prepare_idempotency_and_single_owner(self):
        run = self.prepare()
        self.assertEqual(run['id'], self.prepare()['id'])
        with self.assertRaises(Conflict):
            self.prepare('another')
        with self.assertRaises(Conflict):
            self.prepare(policy=RoutingPolicy(allowed_providers=('codex',)))
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM ai_runs').fetchone()[0], 1)

    def test_extension_does_not_change_prior_schema_or_task(self):
        self.assertEqual(self.s.db.execute('PRAGMA user_version').fetchone()[0], self.base_schema_version)
        self.assertEqual(self.s.db.execute("SELECT version FROM core_extensions WHERE name='context_bus'").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.bus.ensure(self.t['id'])
            self.s.db.execute('DELETE FROM bus_events')

    def test_codex_transport_has_fixed_tools_disabled_arguments(self):
        transport = CodexTextTransport(executable='/trusted/codex')
        argv = transport.argv()
        self.assertIn('--ignore-user-config', argv)
        self.assertIn('read-only', argv)
        for feature in ('shell_tool','apps','plugins','multi_agent','hooks','computer_use','code_mode_host'):
            self.assertEqual(argv[argv.index(feature)-1], '--disable')
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', argv)
        self.assertEqual(argv[-1], '-')

    def test_n8n_reference_discovery_never_copies_credentials(self):
        database = self.operator / '.n8n-data/.n8n/database.sqlite'
        database.parent.mkdir(parents=True)
        c = sqlite3.connect(database)
        c.execute('CREATE TABLE workflow_entity(id TEXT,name TEXT,nodes TEXT)')
        c.execute('INSERT INTO workflow_entity VALUES(?,?,?)', ('workflow','Zamal AI Operator - Command Center', json.dumps([
            {'id':'node','type':'@n8n/n8n-nodes-langchain.lmChatGoogleGemini','credentials':{'googlePalmApi':{'id':'existing-id'}}}])))
        c.commit(); c.close()
        registry = existing_registry(self.operator)
        self.assertEqual(len(registry.adapters), 4)
        self.assertEqual(registry.adapters['gemini'].spec.connection_ref, 'n8n:workflow:node:existing-id')
        self.assertFalse(registry.adapters['gemini'].availability().available)
        self.assertFalse(registry.adapters['openai'].availability().available)

    def test_rejected_and_expired_inference_are_blocked(self):
        run = self.prepare()
        a = self.s.action(run['action_id'])
        self.s.decide(a['approval']['id'], 'REJECT', actor='fixture-human')
        with self.assertRaises(Denied):
            self.execute(run)
        self.assertFalse(any(t.requests for t in self.transports.values()))
        # Expiry check against an isolated second source task.
        t = self.s.create_task('second source', 'local', 'cpc', 'second')
        for state in ('UNDERSTOOD', 'PLANNED'):
            t = self.s.transition(t['id'], state, t['revision'])
        run = self.engine.prepare(t['id'], 'expiry')
        self.approve(run)
        a = self.s.action(run['action_id'])
        self.s.db.execute('UPDATE approvals SET expires=0 WHERE id=?', (a['approval']['id'],))
        with self.assertRaises(Denied):
            self.execute(run)
        self.assertFalse(any(t.requests for t in self.transports.values()))

    def test_false_availability_cannot_spoof_eligible_reason(self):
        self.transports['openai'].health = Availability(False, 'eligible')
        route = Router(self.registry).evaluate(self.bus.snapshot(self.t['id']), 'planner')
        self.assertEqual(route['selected'], 'claude')

    def test_connection_change_during_fallback_blocks_new_destination(self):
        def change(request, check):
            adapter = self.registry.adapters['claude']
            adapter.spec = replace(adapter.spec, connection_ref='unapproved-destination')
            raise ProviderUnavailable()
        self.transports['openai'].callback = change
        run = self.prepare(); self.approve(run)
        with self.assertRaises(Denied):
            self.execute(run)
        self.assertFalse(self.transports['claude'].requests)

    def test_concurrent_prepare_and_dispatch_are_deduplicated(self):
        def prepare(_):
            s = Store(self.operator / 'state/operator.sqlite3')
            try:
                engine = HandoffEngine(s, self.registry, self.rules)
                try:
                    return engine.prepare(self.t['id'], 'race')['id']
                except Conflict:
                    return None
            finally:
                s.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(prepare, range(2)))
        unique = {v for v in ids if v is not None}
        self.assertEqual(len(unique), 1)
        run = self.engine.get(unique.pop()); self.approve(run)
        def execute(_):
            s = Store(self.operator / 'state/operator.sqlite3')
            try:
                engine = HandoffEngine(s, self.registry, self.rules)
                try:
                    engine.gate().execute(run['action_id'])
                    return 'executed'
                except Denied:
                    return 'denied'
            finally:
                s.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(execute, range(2)))
        self.assertEqual(sorted(results), ['denied', 'executed'])
        self.assertEqual(sum(len(t.requests) for t in self.transports.values()), 3)

    def test_codex_json_transport_subprocess_contract(self):
        # Real subprocess, fake provider executable. No remote model call.
        executable = self.root / 'fixture-codex'
        executable.write_text('#!' + sys.executable + '\n' +
            'import json,sys\n' +
            'if "--version" in sys.argv: print("codex-cli 0.153.4"); sys.exit()\n' +
            'raw=sys.stdin.read(); request=json.loads(raw[raw.index("{ "):] if "{ " in raw else raw[raw.index("{"):])\n' +
            'result={"context_id":request["context"]["context_id"],"context_hash":request["context_hash"],"stage":request["stage"],"summary":"fixture","proposals":[],"evidence_references":[],"next_action":"review"}\n' +
            'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":json.dumps(result)}}))\n')
        executable.chmod(0o700)
        adapter = CodexAdapter(transport=CodexTextTransport(str(executable), str(self.root)))
        snap = self.bus.snapshot(self.t['id'])
        # This test exercises the JSON pipe/transport, not the installed CLI's
        # version or managed feature discovery (tested independently below).
        with patch.object(adapter.transport, 'availability', return_value=Availability(True, 'fixture_ready')):
            result = adapter.invoke(snap, 'planner', lambda: None)
        self.assertEqual(result['context_hash'], snap.fingerprint)
        self.assertEqual(result['summary'], 'fixture')

    def test_missing_rules_fail_closed(self):
        (self.rules / 'CPC_PROJECT_RULES.md').unlink()
        with self.assertRaises(Denied):
            self.bus.snapshot(self.t['id'])

    def test_provider_output_cannot_claim_a_different_context(self):
        self.transports['openai'].callback = lambda request, check: {**response(request), 'context_id': 'task:other'}
        run = self.prepare(); self.approve(run)
        with self.assertRaises(Denied):
            self.execute(run)
        self.assertFalse(self.transports['codex'].requests)

    def test_codex_availability_requires_version_and_effective_safety_features(self):
        from subprocess import CompletedProcess
        version = CompletedProcess([], 0, 'codex-cli 0.153.4\n', '')
        feature_text = '\n'.join(f'{name} stable false' for name in CodexTextTransport.DISABLED)
        safe = CompletedProcess([], 0, feature_text, '')
        transport = CodexTextTransport('/fixture/codex')
        with patch('zamal_core.providers.subprocess.run', side_effect=[version, safe]) as probe:
            self.assertTrue(transport.availability().available)
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(probe.call_args.args[0][-2:], ['features', 'list'])
        unsafe = CompletedProcess([], 0, feature_text.replace('unified_exec stable false', 'unified_exec stable true'), '')
        transport = CodexTextTransport('/fixture/codex')
        with patch('zamal_core.providers.subprocess.run', side_effect=[version, unsafe]):
            self.assertEqual(transport.availability().reason, 'unsafe_cli_feature_contract')
            self.assertFalse(transport.availability().available)
        transport = CodexTextTransport('/fixture/codex')
        with patch('zamal_core.providers.subprocess.run', return_value=CompletedProcess([], 0, 'codex-cli 0.150.0\n', '')) as probe:
            self.assertFalse(transport.availability().available)
            self.assertEqual(probe.call_count, 1)

    def test_codex_missing_feature_and_probe_failure_fail_closed(self):
        from subprocess import CompletedProcess, TimeoutExpired
        version = CompletedProcess([], 0, 'codex-cli 0.153.4\n', '')
        for probe in (CompletedProcess([], 0, '', ''), CompletedProcess([], 1, '', '')):
            with self.subTest(probe=probe.returncode):
                transport = CodexTextTransport('/fixture/codex')
                with patch('zamal_core.providers.subprocess.run', side_effect=[version, probe]):
                    self.assertFalse(transport.availability().available)
        transport = CodexTextTransport('/fixture/codex')
        with patch('zamal_core.providers.subprocess.run', side_effect=TimeoutExpired('probe', 5)):
            self.assertFalse(transport.availability().available)
