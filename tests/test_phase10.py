"""Phase 10 continuation: real SQLite, worker gates, HTTP and export fixtures."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import HTTPServer
import urllib.request
import urllib.error
from zamal_core.store import Store,Gate,Denied,Conflict,uid
from zamal_core.gateway import GatewayWorker,InboundEventGateway,InboundEventStore
from zamal_core.connection_registry import ConnectionRegistry,ConnectionRecord,ToolHealthGate,TIER_SAFE
from zamal_core.workflow_export import WorkflowExporter,_strip_credentials
from zamal_core.webhook_gateway import WebhookBoundary,handler_for
from zamal_core.safety import Controls


class Phase10Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='zamal-phase10-')
        self.root=Path(self.tmp.name).resolve();self.op=self.root/'operator';self.op.mkdir();self.cpc=self.root/'cpc';self.cpc.mkdir()
        self.s=Store(self.op/'state/operator.sqlite3')
        self.w=GatewayWorker(self.s,operator=self.op,cpc=self.cpc);self.g=self.w.gate();self.events=InboundEventGateway(self.s)
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def task(self):
        t=self.s.create_task('integration fixture','local','operator',uid())
        for state in ('UNDERSTOOD','PLANNED'):t=self.s.transition(t['id'],state,t['revision'])
        return t
    def action(self,kind='connection_disable',cid='n8n'):
        return self.g.propose(self.task()['id'],kind,{'connector_id':cid,'reason':'fixture'},uid())
    def approve(self,a):return self.s.decide(a['approval']['id'],'APPROVE',actor='fixture-human')
    def test_valid_event_creates_one_canonical_task(self):
        p={'text':'inspect','project':'operator'};r=self.events.receive('task_create','n8n',p,'one')
        self.assertEqual(r['outcome'],'SUCCESS');self.assertEqual(self.s.task(r['task_id'])['command'],'inspect')
        again=self.events.receive('task_create','n8n',p,'one')
        self.assertEqual(again['previous']['task_id'],r['task_id']);self.assertEqual(len(self.s.tasks()),1)
    def test_changed_duplicate_denied(self):
        self.events.receive('task_create','n8n',{'text':'one'},'key')
        r=self.events.receive('task_create','n8n',{'text':'two'},'key')
        self.assertEqual(r['outcome'],'DENIED');self.assertEqual(len(self.s.tasks()),1)
    def test_source_namespaced_ids(self):
        for source in ('cli','n8n'):self.events.receive('task_create',source,{'text':'one'},'key')
        self.assertEqual(len(self.s.tasks()),2)
    def test_unknown_event_or_source_denied(self):
        for kind,source in [('execute','n8n'),('task_create','evil')]:
            self.assertEqual(self.events.receive(kind,source,{'text':'x'})['outcome'],'DENIED')
        self.assertEqual(self.s.tasks(),[])
    def test_invalid_type_and_depth_denied(self):
        for payload in (None,[],42):self.assertEqual(self.events.receive('task_create','n8n',payload)['outcome'],'DENIED')
        p={'text':'x'}
        for _ in range(15):p={'nested':p}
        self.assertEqual(self.events.receive('task_create','n8n',p)['outcome'],'DENIED')
    def test_approval_text_is_not_authorization(self):
        r=self.events.receive('task_create','n8n',{'text':'APPROVE all sends'})
        self.assertEqual(r['outcome'],'SUCCESS');self.assertEqual(self.s.approval_queue(),[])
        for p in ({'approve':True},{'nested':[{'approved':True}]}):
            self.assertEqual(self.events.receive('task_create','n8n',p)['outcome'],'DENIED')
    def test_secrets_not_persisted_or_returned(self):
        for p in ({'password':'unique-secret-value'},{'text':'Bearer unique-secret-value'},{'text':'api_key=unique-secret-value'}):
            r=self.events.receive('task_create','n8n',p)
            self.assertEqual(r['outcome'],'DENIED');self.assertNotIn('unique-secret-value',json.dumps(r))
        self.assertEqual(self.s.db.execute('select count(*) from inbound_events').fetchone()[0],0)
    def test_placeholder_event_not_success(self):
        self.assertEqual(self.events.receive('intake_voice','n8n',{})['outcome'],'NOT_CONFIGURED')
    def test_failed_task_is_marked_rejected(self):
        r=self.events.receive('task_create','n8n',{})
        self.assertEqual(r['outcome'],'FAILED');self.assertEqual(InboundEventStore(self.s).get(r['event_id'])['status'],'rejected')
    def test_concurrent_event_creates_one_task(self):
        barrier=threading.Barrier(2)
        def run(_):
            s=Store(self.s.path)
            try:barrier.wait(timeout=5);return InboundEventGateway(s).receive('task_create','n8n',{'text':'one'},'race')
            finally:s.close()
        with concurrent.futures.ThreadPoolExecutor(2) as p:results=list(p.map(run,range(2)))
        self.assertEqual(sorted(r['outcome'] for r in results),['DUPLICATE','SUCCESS']);self.assertEqual(len(self.s.tasks()),1)
    def test_received_interruption_never_auto_replays(self):
        rec=InboundEventStore(self.s).record('task_create','n8n',{'text':'one'},'crash')
        r=self.events.receive('task_create','n8n',{'text':'one'},'crash')
        self.assertEqual(r['outcome'],'DUPLICATE');self.assertEqual(r['status'],'received');self.assertEqual(self.s.tasks(),[])
    def test_enable_disable_requires_exact_approval(self):
        a=self.action()
        with self.assertRaises(Denied):self.g.execute(a['id'])
        self.assertTrue(ConnectionRegistry(store=self.s).get('n8n').enabled)
        self.approve(a);self.assertTrue(self.g.execute(a['id'])['ok'])
        self.assertFalse(ConnectionRegistry(store=self.s).get('n8n').enabled)
        with self.assertRaises(Denied):self.g.execute(a['id'])
        b=self.action('connection_enable');self.approve(b);self.g.execute(b['id'])
        self.assertTrue(ConnectionRegistry(store=self.s).get('n8n').enabled)
    def test_connector_state_survives_restart(self):
        a=self.action();self.approve(a);self.g.execute(a['id']);s=Store(self.s.path)
        try:self.assertFalse(ConnectionRegistry(store=s).get('n8n').enabled)
        finally:s.close()
    def test_connector_revision_invalidates_approval(self):
        a=self.action();b=self.action();self.approve(a);self.approve(b);self.g.execute(a['id'])
        with self.assertRaises(Denied):self.g.execute(b['id'])
    def test_changed_payload_denied(self):
        a=self.action();self.approve(a);self.s.db.execute("update actions set payload='{}' where id=?",(a['id'],))
        with self.assertRaises(Denied):self.g.execute(a['id'])
    def test_codex_cannot_be_enabled_or_overridden(self):
        a=self.action('connection_enable','codex');self.approve(a)
        with self.assertRaises(Denied):self.g.execute(a['id'])
        with self.assertRaises(Denied):ConnectionRegistry(extra=[ConnectionRecord('codex','fake',TIER_SAFE)])
        self.assertEqual(ToolHealthGate(ConnectionRegistry(store=self.s)).check('codex')['status'],'BLOCKED')
    def test_missing_connector_state_denied(self):
        self.s.db.execute("delete from connection_controls where connector_id='n8n'")
        with self.assertRaises(Denied):ConnectionRegistry(store=self.s).get('n8n')
    def test_emergency_controls_override_toggle(self):
        a=self.action();self.approve(a)
        for flag in ('stop_everything','read_only_mode'):
            Controls(self.s).set(flag,True,actor='fixture',reason='test')
            with self.assertRaises(Denied):self.g.execute(a['id'])
            Controls(self.s).set(flag,False,actor='fixture',reason='test')
    def test_unknown_and_unprobed_connectors_unavailable(self):
        h=ToolHealthGate(ConnectionRegistry())
        self.assertEqual(h.check('unknown')['status'],'UNCONFIGURED')
        self.assertEqual(h.check('ai_inference')['status'],'UNCONFIGURED')
    def test_unavailable_connector_never_routes_action(self):
        r=self.events.receive('workflow_trigger','n8n',{'connector_id':'codex','kind':'connection_enable','arguments':{'connector_id':'n8n','reason':'fixture'},'text':'fixture','project':'operator'})
        self.assertEqual(r['outcome'],'UNAVAILABLE');self.assertEqual(self.s.tasks(),[])
    def test_protected_event_stops_at_phase9_and_deduplicates(self):
        p={'connector_id':'n8n','kind':'connection_disable','arguments':{'connector_id':'n8n','reason':'fixture'},'text':'fixture','project':'operator'}
        with patch.object(ToolHealthGate,'check',return_value={'status':'OK'}):
            r=self.events.receive('workflow_trigger','n8n',p,'protected')
            repeat=self.events.receive('workflow_trigger','n8n',p,'protected')
        self.assertEqual(r['outcome'],'APPROVAL_REQUIRED');self.assertEqual(repeat['outcome'],'DUPLICATE')
        self.assertEqual(len(self.s.approval_queue()),1);self.assertTrue(ConnectionRegistry(store=self.s).get('n8n').enabled)
    def test_health_does_not_follow_arbitrary_urls(self):
        h=ToolHealthGate(ConnectionRegistry(extra=[ConnectionRecord('evil','evil',TIER_SAFE,'http://example.invalid/send')]))
        with patch('urllib.request.build_opener') as opener:
            self.assertEqual(h.check('evil')['status'],'BLOCKED');opener.assert_not_called()
    def test_health_response_contract_and_no_error_leak(self):
        h=ToolHealthGate(ConnectionRegistry())
        with patch('urllib.request.build_opener',side_effect=RuntimeError('unique-secret')):
            r=h.check('n8n')
        self.assertEqual(r['status'],'UNAVAILABLE');self.assertNotIn('unique-secret',json.dumps(r))
    def test_wrong_webhook_auth_rejected_without_state(self):
        b=WebhookBoundary(self.s,'x'*32)
        r=b.receive(b'{}','Bearer wrong');self.assertEqual(r['outcome'],'DENIED');self.assertEqual(self.s.tasks(),[])
    def test_webhook_does_not_accept_decisions(self):
        b=WebhookBoundary(self.s,'x'*32)
        r=b.receive(json.dumps({'event_type':'approve','event_key':'x','payload':{'approval_id':'x'}}).encode(),'Bearer '+'x'*32)
        self.assertEqual(r['outcome'],'DENIED')
    def test_duplicate_json_keys_rejected(self):
        b=WebhookBoundary(self.s,'x'*32)
        self.assertEqual(b.receive(b'{"event_type":"task_create","event_type":"approve"}','Bearer '+'x'*32)['outcome'],'DENIED')
    def test_real_loopback_webhook_fixture(self):
        secret='fixture-'+'x'*32
        server=HTTPServer(('127.0.0.1',0),handler_for(self.s.path,secret))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            body=json.dumps({'event_type':'task_create','event_key':'http','payload':{'text':'fixture'}}).encode()
            req=urllib.request.Request('http://127.0.0.1:%d/events'%server.server_port,data=body,headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=5) as r:result=json.load(r)
            self.assertEqual(result['outcome'],'SUCCESS');self.assertEqual(len(self.s.tasks()),1)
        finally:server.shutdown();server.server_close();thread.join()
    def export_db(self):
        path=self.root/'n8n.sqlite';db=sqlite3.connect(path)
        db.execute('create table workflow_entity(id,name,active,nodes,connections,settings,createdAt,updatedAt)')
        db.execute('insert into workflow_entity values(?,?,?,?,?,?,?,?)',('id','fixture',1,json.dumps([{'name':'node','parameters':{'jsCode':'opaque secret value'},'credentials':{'id':'secret'}}]),'{}','{}','now','now'));db.commit();db.close();return path
    def test_export_versioned_hashed_secret_free(self):
        db=self.export_db();before=db.read_bytes();e=WorkflowExporter(self.s,self.root/'exports',db)
        a=e.export();b=e.export();self.assertNotEqual(a['version'],b['version'])
        data=Path(a['file']).read_bytes();self.assertEqual(hashlib.sha256(data).hexdigest(),a['sha256'])
        self.assertNotIn(b'opaque secret value',data);self.assertNotIn(b'"secret"',data)
        self.assertEqual(db.read_bytes(),before);self.assertFalse(a['manifest']['restore_ready'])
        self.assertTrue(Path(a['file']+'.manifest.json').exists())
    def test_export_rejects_depth_instead_of_leaking(self):
        p={'password':'secret'}
        for _ in range(25):p={'child':p}
        with self.assertRaises(Denied):_strip_credentials(p)
    def test_failed_export_leaves_workflows_unchanged(self):
        db=self.export_db();before=db.read_bytes();e=WorkflowExporter(self.s,self.root/'exports',db)
        with patch('zamal_core.workflow_export.os.open',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):e.export()
        self.assertEqual(db.read_bytes(),before);self.assertEqual(self.s.db.execute('select count(*) from n8n_exports').fetchone()[0],0)
    def test_export_symlink_denied(self):
        link=self.root/'link';link.symlink_to(self.op,target_is_directory=True)
        with self.assertRaises(Denied):WorkflowExporter(outbox=link)
    def test_gateway_worker_health_action_no_approval(self):
        a=self.g.propose(self.task()['id'],'connection_status',{'connector_id':'codex'},uid())
        self.assertIsNone(a['approval']);self.assertEqual(self.g.execute(a['id'])['status'],'BLOCKED')
    def test_schema6_preserves_missing_emergency_failure(self):
        self.assertEqual(self.s.db.execute('pragma user_version').fetchone()[0],6)
        self.s.db.execute('delete from controls');s=Store(self.s.path)
        try:
            with self.assertRaises(Denied):Controls(s).check('inspect')
        finally:s.close()
    def test_n8n_modules_are_inactive_and_have_no_approval_store(self):
        root=Path(__file__).resolve().parents[1]/'n8n/phase10'
        modules=list(root.glob('*.json'));self.assertEqual(len(modules),5)
        for p in modules:
            w=json.loads(p.read_text());self.assertFalse(w['active'])
            self.assertTrue(all(n['type'] in {'n8n-nodes-base.executeWorkflowTrigger','n8n-nodes-base.code','n8n-nodes-base.httpRequest'} for n in w['nodes']))
            for node in w['nodes']:
                for reference in node.get('credentials', {}).values():
                    self.assertEqual(p.name, 'intake.json')
                    self.assertEqual(set(reference), {'id', 'name'})
                    self.assertTrue(all(isinstance(v, str) and v for v in reference.values()))
                    self.assertEqual(reference['name'], 'Zamal Operator Gateway')

    def test_read_only_blocks_export_file_creation(self):
        a=self.g.propose(self.task()['id'],'workflow_export',{},uid())
        Controls(self.s).set('read_only_mode',True,actor='fixture',reason='test')
        with self.assertRaises(Denied):self.g.execute(a['id'])
        self.assertEqual(self.s.db.execute('select count(*) from n8n_exports').fetchone()[0],0)

    def test_result_storage_failure_is_structured_and_not_success(self):
        def route(*args):
            self.s.db.execute('pragma query_only=on')
            return {'outcome':'SUCCESS','ok':True}
        try:
            with patch.object(self.events._router,'route',side_effect=route):
                r=self.events.receive('notification','n8n',{'text':'fixture'},'storage-failure')
            self.assertEqual(r['outcome'],'FAILED');self.assertFalse(r['ok'])
        finally:self.s.db.execute('pragma query_only=off')
        again=self.events.receive('notification','n8n',{'text':'fixture'},'storage-failure')
        self.assertEqual(again['outcome'],'DUPLICATE');self.assertEqual(again['status'],'received')


# ── L-5: SIGTERM/SIGINT graceful shutdown test ────────────────────────────────

class WebhookSIGTERMTests(unittest.TestCase):
    """The loopback webhook server must shut down cleanly on SIGTERM."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-sigterm-')
        self.root = Path(self.tmp.name)
        self.s = Store(self.root / 'store.db')

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_sigterm_handler_calls_server_shutdown(self):
        """The handler installed for SIGTERM must call server.shutdown() when invoked."""
        import signal as _signal
        import time as _time
        import os as _os

        secret = 'x' * 32
        shutdown_called = []

        class _FakeServer:
            def __init__(self):
                self._running = True
            def serve_forever(self):
                # Exits immediately to keep the test fast; we inspect the handlers below.
                pass
            def shutdown(self):
                shutdown_called.append(True)
            def __enter__(self): return self
            def __exit__(self, *args): pass

        installed_handlers = {}
        original_signal = _signal.signal

        def capturing_signal(signum, handler):
            installed_handlers[signum] = handler
            # Don't actually install on the OS so the test process is unaffected.

        from zamal_core.webhook_gateway import serve as _serve

        with patch('zamal_core.webhook_gateway.HTTPServer', return_value=_FakeServer()), \
             patch.dict(_os.environ, {'ZAMAL_N8N_GATEWAY_SECRET': secret}), \
             patch('zamal_core.webhook_gateway.signal.signal', side_effect=capturing_signal):
            try:
                _serve(self.root / 'store.db', port=18788)
            except Exception:
                pass

        # The handler was registered; now invoke it directly (simulating OS delivery)
        sigterm_handler = installed_handlers.get(_signal.SIGTERM)
        self.assertIsNotNone(sigterm_handler,
                             'serve() must register a SIGTERM handler')
        sigterm_handler(_signal.SIGTERM, None)
        # Give the daemon shutdown thread a moment to call server.shutdown()
        import time as _time2
        _time2.sleep(0.1)
        self.assertTrue(shutdown_called,
                        'SIGTERM handler must call server.shutdown()')

    def test_serve_installs_signal_handlers(self):
        """serve() must install SIGTERM and SIGINT handlers (verified via captured calls)."""
        import signal as _signal

        installed = {}

        def capturing_signal(signum, handler):
            installed[signum] = handler

        secret = 'x' * 32
        with patch('zamal_core.webhook_gateway.HTTPServer') as FakeHTTPServer, \
             patch.dict(os.environ, {'ZAMAL_N8N_GATEWAY_SECRET': secret}), \
             patch('zamal_core.webhook_gateway.signal.signal',
                   side_effect=capturing_signal):
            # Make the context-manager-wrapped server exit immediately
            cm = FakeHTTPServer.return_value.__enter__.return_value
            cm.serve_forever.return_value = None
            from zamal_core.webhook_gateway import serve as _serve
            try:
                _serve(self.root / 'store.db', port=18789)
            except Exception:
                pass

        self.assertIn(_signal.SIGTERM, installed,
                      'serve() must install a SIGTERM handler')
        self.assertIn(_signal.SIGINT, installed,
                      'serve() must install a SIGINT handler')


# ── L-4: Telegram callback chat_id defence-in-depth ──────────────────────────

class TelegramCallbackAuthTests(unittest.TestCase):
    """TelegramCallbackHandler must re-verify chat_id even with cfg provided."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-tg-auth-')
        self.s = Store(Path(self.tmp.name) / 'store.db')

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_callback_handler_blocks_unauthorized_chat_id_with_cfg(self):
        """Handler with cfg must deny callbacks from non-allowed chat IDs."""
        from zamal_core.telegram_bot import TelegramCallbackHandler, TelegramConfig

        cfg = TelegramConfig.__new__(TelegramConfig)
        cfg._token = 'fixture:token'
        cfg._allowed = {'99999'}  # only this chat is allowed

        handler = TelegramCallbackHandler(self.s, cfg=cfg)
        result = handler.handle('cb-id', 55555, 'approval:fake-id:APPROVE')
        self.assertEqual(result['outcome'], 'DENIED')
        self.assertEqual(result['code'], 'unauthorized_sender')

    def test_callback_handler_allows_authorized_chat_id_with_cfg(self):
        """Handler with cfg must NOT deny authorized chat IDs at this check."""
        from zamal_core.telegram_bot import TelegramCallbackHandler, TelegramConfig

        cfg = TelegramConfig.__new__(TelegramConfig)
        cfg._token = 'fixture:token'
        cfg._allowed = {'99999'}

        handler = TelegramCallbackHandler(self.s, cfg=cfg)
        # approval_id doesn't exist → will fail at approval lookup, but NOT at auth check
        result = handler.handle('cb-id', 99999, 'approval:nonexistent-id:APPROVE')
        # Must NOT be unauthorized_sender — auth check passed, fails at approval lookup
        self.assertNotEqual(result.get('code'), 'unauthorized_sender',
                            'Authorized chat ID must pass the chat_id auth check')

    def test_callback_handler_without_cfg_skips_auth_check(self):
        """Backward compatibility: handler without cfg must not break on None cfg."""
        from zamal_core.telegram_bot import TelegramCallbackHandler

        handler = TelegramCallbackHandler(self.s, cfg=None)
        # Should proceed past auth check (cfg is None), fail only at approval lookup
        result = handler.handle('cb-id', 12345, 'approval:nonexistent-id:APPROVE')
        self.assertNotEqual(result.get('code'), 'unauthorized_sender')
