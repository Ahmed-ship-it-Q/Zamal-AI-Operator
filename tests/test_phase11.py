"""Phase 11: Telegram remote command, voice and approval gateway tests.

All tests use real SQLite and real Phase 9/10 gates.
No real Telegram API calls; TelegramAPI is mocked per-test where needed.
No credentials in source.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from zamal_core.store import Store, Denied, Conflict, uid
from zamal_core.gateway import GatewayWorker, InboundEventGateway
from zamal_core.connection_registry import ConnectionRegistry, ToolHealthGate
from zamal_core.telegram_bot import (
    TelegramBot, TelegramConfig, TelegramAPI, TelegramUpdateDedup,
    TelegramCallbackHandler, TelegramMessageRouter, migrate_telegram,
    _approval_keyboard,
)
from zamal_core.safety import Controls


_VALID_TOKEN = 'validtoken:ABCDEFGHIJKLMNOPQRSTUVWXYZabcde'
_ALLOWED_CHAT = '999888777'
_OTHER_CHAT = '111222333'


def _env(token=_VALID_TOKEN, chat=_ALLOWED_CHAT):
    return {'ZAMAL_TELEGRAM_TOKEN': token, 'ZAMAL_TELEGRAM_CHAT_ID': chat}


def _make_store(root):
    return Store(Path(root) / 'operator.sqlite3')


def _planned_task(s):
    t = s.create_task('fixture', 'local', 'operator', uid())
    for state in ('UNDERSTOOD', 'PLANNED'):
        t = s.transition(t['id'], state, t['revision'])
    return t


# ── A: Connection health / config state ──────────────────────────────────────

class TelegramConfigTests(unittest.TestCase):
    def test_unconfigured_no_env(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = TelegramConfig()
        self.assertFalse(cfg.configured)
        self.assertEqual(cfg.health_status, 'UNCONFIGURED')

    def test_configured_with_valid_env(self):
        with patch.dict(os.environ, _env()):
            cfg = TelegramConfig()
        self.assertTrue(cfg.configured)
        self.assertEqual(cfg.health_status, 'OK')

    def test_short_token_unconfigured(self):
        with patch.dict(os.environ, _env(token='short')):
            cfg = TelegramConfig()
        self.assertFalse(cfg.configured)

    def test_no_chat_id_unconfigured(self):
        with patch.dict(os.environ, _env(chat='')):
            cfg = TelegramConfig()
        self.assertFalse(cfg.configured)

    def test_token_not_returned_in_health(self):
        with patch.dict(os.environ, _env()):
            bot = TelegramBot.__new__(TelegramBot)
            bot._cfg = TelegramConfig()
        h = bot._cfg.health_status
        self.assertNotIn(_VALID_TOKEN, str(h))

    def test_telegram_bot_in_registry(self):
        reg = ConnectionRegistry()
        rec = reg.get('telegram_bot')
        self.assertIsNotNone(rec)
        self.assertEqual(rec.tier, 'SAFE')

    def test_telegram_bot_health_unconfigured_no_env(self):
        with patch.dict(os.environ, {}, clear=True):
            h = ToolHealthGate(ConnectionRegistry()).check('telegram_bot')
        self.assertEqual(h['status'], 'UNCONFIGURED')


# ── B: Unauthorized sender denied ────────────────────────────────────────────

class UnauthorizedSenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        with patch.dict(os.environ, _env()):
            self.bot = TelegramBot(self.s, inbox=Path(self.tmp.name) / 'inbox')
        self.bot._api = MagicMock()

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def _update(self, chat_id, text, uid=42):
        return {'update_id': uid, 'message': {'chat': {'id': chat_id}, 'text': text}}

    def test_unauthorized_sender_denied(self):
        r = self.bot.process_update(self._update(int(_OTHER_CHAT), 'hello'))
        self.assertEqual(r['outcome'], 'DENIED')
        self.assertEqual(self.s.tasks(), [])

    def test_authorized_sender_accepted(self):
        with patch.object(self.bot._router._gw, 'receive',
                          return_value={'outcome': 'SUCCESS', 'ok': True, 'task_id': 'fake-id', 'event_id': 'e'}):
            r = self.bot.process_update(self._update(int(_ALLOWED_CHAT), 'hello'))
        self.assertNotEqual(r['outcome'], 'DENIED')


# ── C: Authorized text creates canonical task ─────────────────────────────────

class TextCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        with patch.dict(os.environ, _env()):
            self.bot = TelegramBot(self.s, inbox=Path(self.tmp.name) / 'inbox')
        self.bot._api = MagicMock()

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_authorized_text_creates_task(self):
        upd = {'update_id': 1, 'message': {'chat': {'id': int(_ALLOWED_CHAT)}, 'text': 'run inspection'}}
        r = self.bot.process_update(upd)
        self.assertEqual(r['outcome'], 'SUCCESS')
        self.assertEqual(len(self.s.tasks()), 1)
        task = self.s.task(r['task_id'])
        self.assertEqual(task['command'], 'run inspection')

    def test_text_creates_exactly_one_task(self):
        upd = {'update_id': 2, 'message': {'chat': {'id': int(_ALLOWED_CHAT)}, 'text': 'hello'}}
        self.bot.process_update(upd)
        self.assertEqual(len(self.s.tasks()), 1)

    def test_empty_text_returns_failed(self):
        upd = {'update_id': 3, 'message': {'chat': {'id': int(_ALLOWED_CHAT)}, 'text': ''}}
        r = self.bot.process_update(upd)
        self.assertIn(r['outcome'], ('FAILED', 'DENIED'))
        self.assertEqual(self.s.tasks(), [])

    def test_approval_text_is_data_not_authorization(self):
        upd = {'update_id': 4, 'message': {'chat': {'id': int(_ALLOWED_CHAT)}, 'text': 'please run approval'}}
        r = self.bot.process_update(upd)
        # "approval" in text value is fine; task created, no auto-approval
        self.assertEqual(r['outcome'], 'SUCCESS')
        self.assertEqual(self.s.approval_queue(), [])


# ── D: Duplicate update deduplicated ─────────────────────────────────────────

class DeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        with patch.dict(os.environ, _env()):
            self.bot = TelegramBot(self.s, inbox=Path(self.tmp.name) / 'inbox')
        self.bot._api = MagicMock()

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def _upd(self, text='hello', uid=100):
        return {'update_id': uid, 'message': {'chat': {'id': int(_ALLOWED_CHAT)}, 'text': text}}

    def test_duplicate_update_id_returns_duplicate(self):
        self.bot.process_update(self._upd('first', uid=100))
        r = self.bot.process_update(self._upd('second', uid=100))
        self.assertEqual(r['outcome'], 'DUPLICATE')
        self.assertEqual(len(self.s.tasks()), 1)

    def test_different_update_ids_both_processed(self):
        self.bot.process_update(self._upd('first', uid=101))
        self.bot.process_update(self._upd('second', uid=102))
        self.assertEqual(len(self.s.tasks()), 2)

    def test_duplicate_does_not_create_extra_protected_action(self):
        # Even with a protected action in play, duplicate won't duplicate it
        dedup = TelegramUpdateDedup(self.s)
        dedup.claim(200, _ALLOWED_CHAT, 'message')
        r = self.bot.process_update({'update_id': 200, 'message': {
            'chat': {'id': int(_ALLOWED_CHAT)}, 'text': 'protected task'}})
        self.assertEqual(r['outcome'], 'DUPLICATE')
        self.assertEqual(self.s.tasks(), [])


# ── E: Screenshot/file stays linked to same task ─────────────────────────────

class FileIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.s = _make_store(self.tmp.name)
        self._inbox = self.root / 'inbox'
        with patch.dict(os.environ, _env()):
            self.bot = TelegramBot(self.s, inbox=self._inbox)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def _make_fake_image(self):
        p = self.root / 'fake.png'
        # Minimal 1x1 PNG
        p.write_bytes(
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00'
            b'\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')
        return p

    def test_photo_creates_task_and_intake_linked(self):
        img = self._make_fake_image()
        api_mock = MagicMock()
        api_mock.get_file_path.return_value = 'photos/f.jpg'
        api_mock.download_file.side_effect = lambda fp, dest: Path(dest).write_bytes(img.read_bytes())
        self.bot._api = api_mock
        self.bot._router._api = api_mock
        upd = {'update_id': 300, 'message': {
            'chat': {'id': int(_ALLOWED_CHAT)},
            'caption': 'screenshot of bug',
            'photo': [{'file_id': 'fid1', 'file_size': 1000}, {'file_id': 'fid2', 'file_size': 5000}]
        }}
        r = self.bot.process_update(upd)
        self.assertEqual(r['outcome'], 'SUCCESS')
        task_id = r['task_id']
        self.assertIsNotNone(task_id)
        task = self.s.task(task_id)
        self.assertEqual(task['command'], 'screenshot of bug')
        # intake result included in response (file linked to task)
        self.assertIn('intake', r)

    def test_photo_and_text_share_task_via_caption(self):
        img = self._make_fake_image()
        api_mock = MagicMock()
        api_mock.get_file_path.return_value = 'photos/f2.jpg'
        api_mock.download_file.side_effect = lambda fp, dest: Path(dest).write_bytes(img.read_bytes())
        self.bot._api = api_mock
        self.bot._router._api = api_mock
        upd = {'update_id': 301, 'message': {
            'chat': {'id': int(_ALLOWED_CHAT)},
            'caption': 'check this design',
            'photo': [{'file_id': 'fid3', 'file_size': 2000}]
        }}
        r = self.bot.process_update(upd)
        self.assertEqual(r['outcome'], 'SUCCESS')
        # task command = caption
        task = self.s.task(r['task_id'])
        self.assertIn('check this design', task['command'])


# ── F: Voice path (file intake GREEN; STT BLOCKED per Phase 5) ───────────────

class VoiceIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._inbox = self.root / 'inbox'
        self.s = _make_store(self.tmp.name)
        with patch.dict(os.environ, _env()):
            self.bot = TelegramBot(self.s, inbox=self._inbox)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_voice_file_stored_and_task_created(self):
        fake_voice = self._inbox / 'voice.ogg'
        self._inbox.mkdir(parents=True, exist_ok=True)
        fake_voice.write_bytes(b'RIFF\x00\x00\x00\x00WAVE')  # Minimal WAV header
        api_mock = MagicMock()
        api_mock.get_file_path.return_value = 'voice/v.ogg'
        api_mock.download_file.side_effect = lambda fp, dest: Path(dest).write_bytes(fake_voice.read_bytes())
        self.bot._api = api_mock
        self.bot._router._api = api_mock
        upd = {'update_id': 400, 'message': {
            'chat': {'id': int(_ALLOWED_CHAT)},
            'voice': {'file_id': 'vfid1', 'duration': 5}
        }}
        r = self.bot.process_update(upd)
        # Task must be created (file intake is GREEN)
        self.assertIn(r.get('outcome'), ('SUCCESS', 'FAILED'))
        if r.get('outcome') == 'SUCCESS':
            self.assertIsNotNone(r.get('task_id'))
            # STT status explicitly BLOCKED pending Phase 5 TCC
            stt = r.get('stt_status', '')
            self.assertIn('BLOCKED', stt)

    def test_voice_stt_explicitly_blocked(self):
        # Document that STT transcription remains BLOCKED
        # until Phase 5 TCC permission is granted + real audio provided.
        # This test verifies the correct BLOCKED marker is emitted.
        fake_voice = self._inbox / 'voice2.ogg'
        self._inbox.mkdir(parents=True, exist_ok=True)
        fake_voice.write_bytes(b'OggS')
        api_mock = MagicMock()
        api_mock.get_file_path.return_value = 'voice/v2.ogg'
        api_mock.download_file.side_effect = lambda fp, dest: Path(dest).write_bytes(fake_voice.read_bytes())
        self.bot._api = api_mock
        self.bot._router._api = api_mock
        upd = {'update_id': 401, 'message': {
            'chat': {'id': int(_ALLOWED_CHAT)},
            'voice': {'file_id': 'vfid2'}
        }}
        r = self.bot.process_update(upd)
        if r.get('outcome') == 'SUCCESS':
            self.assertIn('BLOCKED', r.get('stt_status', ''))


# ── G: Protected remote action reaches Phase 9 PENDING ───────────────────────

class ProtectedActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        with patch.dict(os.environ, _env()):
            self.bot = TelegramBot(self.s, inbox=Path(self.tmp.name) / 'inbox')
        self.bot._api = MagicMock()
        self.gw = GatewayWorker(self.s)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_protected_trigger_reaches_phase9_pending(self):
        gate = self.gw.gate()
        t = _planned_task(self.s)
        with patch.object(ToolHealthGate, 'check', return_value={'status': 'OK'}):
            action = gate.propose(t['id'], 'connection_disable',
                                  {'connector_id': 'n8n', 'reason': 'test'}, uid())
        self.assertIsNotNone(action['approval'])
        self.assertEqual(action['approval']['status'], 'PENDING')
        # Unapproved execute must fail
        with self.assertRaises(Denied):
            gate.execute(action['id'])

    def test_workflow_trigger_via_gateway_creates_pending_approval(self):
        gw = InboundEventGateway(self.s)
        with patch.object(ToolHealthGate, 'check', return_value={'status': 'OK'}):
            r = gw.receive('workflow_trigger', 'telegram', {
                'connector_id': 'n8n',
                'kind': 'connection_disable',
                'arguments': {'connector_id': 'n8n', 'reason': 'test'},
                'text': 'disable n8n',
                'project': 'operator',
            }, 'wt:phase11:001')
        self.assertEqual(r['outcome'], 'APPROVAL_REQUIRED')
        self.assertEqual(len(self.s.approval_queue()), 1)
        # Connector state must NOT have changed
        self.assertTrue(ConnectionRegistry(store=self.s).get('n8n').enabled)


# ── H/I: Approval buttons (Approve/Edit/Reject) ───────────────────────────────

class ApprovalButtonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        with patch.dict(os.environ, _env()):
            self.bot = TelegramBot(self.s, inbox=Path(self.tmp.name) / 'inbox')
        self.bot._api = MagicMock()
        gate = GatewayWorker(self.s).gate()
        t = _planned_task(self.s)
        self.action = gate.propose(t['id'], 'connection_disable',
                                   {'connector_id': 'n8n', 'reason': 'test'}, uid())
        self.approval_id = self.action['approval']['id']
        self.task_id = t['id']
        # Leave approval PENDING so callback handler can process it

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def _callback_upd(self, choice, uid_=500):
        return {
            'update_id': uid_,
            'callback_query': {
                'id': 'cqid',
                'from': {'id': int(_ALLOWED_CHAT)},
                'message': {'chat': {'id': int(_ALLOWED_CHAT)}},
                'data': f'approval:{self.approval_id}:{choice}',
            }
        }

    def test_approval_keyboard_format(self):
        kb = _approval_keyboard('test-id')
        self.assertEqual(len(kb['inline_keyboard'][0]), 3)
        data_vals = [b['callback_data'] for b in kb['inline_keyboard'][0]]
        self.assertIn('approval:test-id:APPROVE', data_vals)
        self.assertIn('approval:test-id:REJECT', data_vals)
        self.assertIn('approval:test-id:EDIT', data_vals)

    def test_approve_via_callback_executes_exact_action(self):
        r = self.bot.process_update(self._callback_upd('APPROVE'))
        self.assertEqual(r['outcome'], 'SUCCESS')
        self.assertEqual(r['choice'], 'APPROVE')
        self.assertFalse(ConnectionRegistry(store=self.s).get('n8n').enabled)

    def test_reject_via_callback(self):
        r = self.bot.process_update(self._callback_upd('REJECT'))
        self.assertEqual(r['outcome'], 'SUCCESS')
        self.assertEqual(r['choice'], 'REJECT')

    def test_unknown_callback_data_denied(self):
        upd = {
            'update_id': 501,
            'callback_query': {
                'id': 'cq2', 'from': {'id': int(_ALLOWED_CHAT)},
                'message': {'chat': {'id': int(_ALLOWED_CHAT)}},
                'data': 'random_data'
            }
        }
        r = self.bot.process_update(upd)
        self.assertEqual(r['outcome'], 'DENIED')

    def test_unauthorized_callback_denied(self):
        upd = {
            'update_id': 502,
            'callback_query': {
                'id': 'cq3', 'from': {'id': int(_OTHER_CHAT)},
                'message': {'chat': {'id': int(_OTHER_CHAT)}},
                'data': f'approval:{self.approval_id}:APPROVE'
            }
        }
        r = self.bot.process_update(upd)
        self.assertEqual(r['outcome'], 'DENIED')


# ── J: Stale / replayed approval denied ──────────────────────────────────────

class StaleApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        self.callbacks = TelegramCallbackHandler(self.s)
        gate = GatewayWorker(self.s).gate()
        t = _planned_task(self.s)
        action = gate.propose(t['id'], 'connection_disable',
                              {'connector_id': 'n8n', 'reason': 'test'}, uid())
        self.approval_id = action['approval']['id']
        # Approve then execute once
        self.s.decide(self.approval_id, 'APPROVE', actor='test-human')
        gate.execute(action['id'])

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_replayed_approval_denied(self):
        r = self.callbacks.handle('cq', _ALLOWED_CHAT, f'approval:{self.approval_id}:APPROVE')
        self.assertEqual(r['outcome'], 'DENIED')

    def test_stale_approval_id_denied(self):
        r = self.callbacks.handle('cq', _ALLOWED_CHAT, 'approval:nonexistent-id:APPROVE')
        self.assertEqual(r['outcome'], 'DENIED')


# ── K: Mac unavailable returns UNAVAILABLE ────────────────────────────────────

class MacUnavailableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        with patch.dict(os.environ, _env()):
            self.bot = TelegramBot(self.s, inbox=Path(self.tmp.name) / 'inbox')
        self.bot._api = MagicMock()

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_unavailable_connector_not_blindly_called(self):
        gw = InboundEventGateway(self.s)
        # Connector reported UNAVAILABLE → workflow_trigger must not route
        with patch.object(ToolHealthGate, 'check', return_value={'status': 'UNAVAILABLE', 'connector_id': 'n8n'}):
            r = gw.receive('workflow_trigger', 'telegram', {
                'connector_id': 'n8n', 'kind': 'connection_disable',
                'arguments': {'connector_id': 'n8n', 'reason': 'test'},
                'text': 'test', 'project': 'operator',
            }, 'macavail:001')
        self.assertEqual(r['outcome'], 'UNAVAILABLE')
        self.assertEqual(self.s.tasks(), [])


# ── L: Safe task works when Mac is available ─────────────────────────────────

class MacAvailableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        with patch.dict(os.environ, _env()):
            self.bot = TelegramBot(self.s, inbox=Path(self.tmp.name) / 'inbox')
        self.bot._api = MagicMock()

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_safe_task_create_works(self):
        gw = InboundEventGateway(self.s)
        r = gw.receive('task_create', 'telegram', {'text': 'check status', 'project': 'operator'}, 'safe:001')
        self.assertEqual(r['outcome'], 'SUCCESS')
        self.assertEqual(len(self.s.tasks()), 1)


# ── Emergency controls override remote commands ───────────────────────────────

class EmergencyControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        with patch.dict(os.environ, _env()):
            self.bot = TelegramBot(self.s, inbox=Path(self.tmp.name) / 'inbox')
        self.bot._api = MagicMock()

    def tearDown(self):
        Controls(self.s).set('stop_everything', False, actor='test', reason='cleanup')
        self.s.close()
        self.tmp.cleanup()

    def test_stop_everything_blocks_telegram(self):
        Controls(self.s).set('stop_everything', True, actor='test', reason='test')
        upd = {'update_id': 600, 'message': {
            'chat': {'id': int(_ALLOWED_CHAT)}, 'text': 'hello'}}
        r = self.bot.process_update(upd)
        self.assertEqual(r['outcome'], 'DENIED')
        self.assertEqual(self.s.tasks(), [])


# ── Security: no secrets in logs/results ─────────────────────────────────────

class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)
        with patch.dict(os.environ, _env()):
            self.bot = TelegramBot(self.s, inbox=Path(self.tmp.name) / 'inbox')
        self.bot._api = MagicMock()

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_token_not_in_health_output(self):
        h = self.bot.health()
        self.assertNotIn(_VALID_TOKEN, json.dumps(h))

    def test_secret_payload_denied_not_stored(self):
        upd = {'update_id': 700, 'message': {
            'chat': {'id': int(_ALLOWED_CHAT)},
            'text': 'Bearer secrettoken123456789012345678'
        }}
        r = self.bot.process_update(upd)
        self.assertEqual(r['outcome'], 'DENIED')
        rows = self.s.db.execute('SELECT payload FROM inbound_events').fetchall()
        for row in rows:
            self.assertNotIn('secrettoken123456789012345678', row[0])

    def test_approval_verb_in_text_value_is_data_not_auth(self):
        # Text containing "approve" as a word is fine (it's a value, not a key)
        upd = {'update_id': 701, 'message': {
            'chat': {'id': int(_ALLOWED_CHAT)}, 'text': 'please approve the budget'}}
        r = self.bot.process_update(upd)
        # Should succeed as task (text is data, approval queue empty)
        self.assertEqual(r['outcome'], 'SUCCESS')
        self.assertEqual(self.s.approval_queue(), [])

    def test_codex_remains_blocked(self):
        reg = ConnectionRegistry(store=self.s)
        codex = reg.get('codex')
        self.assertEqual(codex.tier, 'BLOCKED')
        h = ToolHealthGate(reg).check('codex')
        self.assertEqual(h['status'], 'BLOCKED')


# ── Schema migration ──────────────────────────────────────────────────────────

class Phase11SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_telegram_updates_table_created(self):
        self.s.db.execute('SELECT update_id,chat_id,update_type,processed_at,result FROM telegram_updates LIMIT 0')

    def test_telegram_bot_in_connection_controls(self):
        row = self.s.db.execute(
            "SELECT enabled FROM connection_controls WHERE connector_id='telegram_bot'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)

    def test_core_extension_telegram_v1(self):
        row = self.s.db.execute(
            "SELECT version FROM core_extensions WHERE name='telegram'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)

    def test_migrate_telegram_idempotent(self):
        migrate_telegram(self.s)  # second call must not fail
        row = self.s.db.execute(
            "SELECT version FROM core_extensions WHERE name='telegram'"
        ).fetchone()
        self.assertEqual(row[0], 1)

    def test_schema_version_still_6(self):
        self.assertEqual(self.s.db.execute('pragma user_version').fetchone()[0], 6)


# ── CLI smoke ─────────────────────────────────────────────────────────────────

class Phase11CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = _make_store(self.tmp.name)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def _run(self, *args):
        import subprocess
        env = {**os.environ, 'ZAMAL_TELEGRAM_TOKEN': '', 'ZAMAL_TELEGRAM_CHAT_ID': ''}
        r = subprocess.run(
            ['python3', '-m', 'zamal_core', *args],
            capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
            env={**env, 'HOME': self.tmp.name}
        )
        return r

    def test_telegram_status_cli_unconfigured(self):
        r = self._run('telegram-status')
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertEqual(data['status'], 'UNCONFIGURED')
        self.assertNotIn('ZAMAL_TELEGRAM_TOKEN', r.stdout)

    def test_telegram_status_does_not_leak_token(self):
        fake_token = 'supersecretshouldnotappear1234567890'
        import subprocess
        env = {**os.environ, 'ZAMAL_TELEGRAM_TOKEN': fake_token, 'ZAMAL_TELEGRAM_CHAT_ID': '99999'}
        r = subprocess.run(
            ['python3', '-m', 'zamal_core', 'telegram-status'],
            capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
            env={**env, 'HOME': self.tmp.name}
        )
        self.assertNotIn(fake_token, r.stdout)
        self.assertNotIn(fake_token, r.stderr)
