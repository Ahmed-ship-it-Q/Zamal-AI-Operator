"""Phase 11: Telegram remote command, voice and approval gateway.

Credentials NEVER appear in source, logs or exports.
Token: ZAMAL_TELEGRAM_TOKEN (env, >=20 chars)
Allowed chats: ZAMAL_TELEGRAM_CHAT_ID (env, comma-separated integer chat IDs)

Architecture:
  Telegram update → TelegramBot.process_update()
    → dedup by update_id (telegram_updates table)
    → authorized chat gate
    → text   → InboundEventGateway.receive('task_create','telegram',...)
    → voice  → download + Phase 5 Intake.record_voice
    → photo  → download + Phase 5 Intake.record_screenshot
    → doc    → download + Phase 5 Intake.record_document
    → callback → Gate.resolve_approval (Phase 9, exact binding enforced)

Approval buttons are bound to exact approval_id/task/action. Phase 9 enforces
expiry, replay, stale-approval and tamper detection. Button text alone never
authorizes. Emergency controls always override.

STT status: inherits Phase 5 — real transcription remains RED until
macOS Speech Recognition TCC permission is granted and a real audio file
is provided. File recording/storage is GREEN; auto-transcription is BLOCKED.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from .store import Denied, Conflict  # noqa: F401 (Conflict used in callback handler)
from .integration_security import failure

_API_BASE = 'https://api.telegram.org/bot'
_MAX_FILE_BYTES = 20 * 1024 * 1024
_CALLBACK_PREFIX = 'approval:'
_ALLOWED_CHOICES = frozenset({'APPROVE', 'EDIT', 'REJECT'})


# ── Config ────────────────────────────────────────────────────────────────────

class TelegramConfig:
    """Reads credentials from environment at construction.  Token is never logged."""

    def __init__(self):
        token = os.environ.get('ZAMAL_TELEGRAM_TOKEN', '')
        chats_raw = os.environ.get('ZAMAL_TELEGRAM_CHAT_ID', '')
        if token and len(token) >= 20:
            self._token = token
            self._allowed = {c.strip() for c in chats_raw.split(',') if c.strip().lstrip('-').isdigit()}
        else:
            self._token = None
            self._allowed = set()

    @property
    def configured(self):
        return bool(self._token and self._allowed)

    @property
    def health_status(self):
        if not self._token:
            return 'UNCONFIGURED'
        if not self._allowed:
            return 'UNCONFIGURED'
        return 'OK'

    def token(self):
        if not self._token:
            raise Denied('Telegram bot not configured; set ZAMAL_TELEGRAM_TOKEN')
        return self._token

    def is_allowed(self, chat_id):
        return str(chat_id) in self._allowed


# ── API client ────────────────────────────────────────────────────────────────

class TelegramAPI:
    """Stdlib-only Telegram API. Token never appears in logs or error messages."""

    def __init__(self, config):
        self._cfg = config

    def _url(self, method):
        return f'{_API_BASE}{self._cfg.token()}/{method}'

    def call(self, method, payload=None, timeout=10):
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(
            self._url(method), data=data,
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read(65536))
        except urllib.error.HTTPError:
            return {'ok': False, 'description': 'API HTTP error'}
        except Exception:
            return {'ok': False, 'description': 'API call failed'}

    def send_message(self, chat_id, text, reply_markup=None):
        p = {'chat_id': chat_id, 'text': str(text)[:4096], 'parse_mode': 'Markdown'}
        if reply_markup:
            p['reply_markup'] = reply_markup
        return self.call('sendMessage', p)

    def answer_callback(self, callback_query_id, text=''):
        return self.call('answerCallbackQuery', {'callback_query_id': callback_query_id, 'text': str(text)[:200]})

    def get_file_path(self, file_id):
        r = self.call('getFile', {'file_id': file_id})
        if not r.get('ok') or not r.get('result', {}).get('file_path'):
            raise Denied('Cannot retrieve file path from Telegram')
        size = r['result'].get('file_size', 0)
        if size > _MAX_FILE_BYTES:
            raise Denied('File exceeds 20 MB limit')
        return r['result']['file_path']

    def download_file(self, file_path, dest):
        url = f'https://api.telegram.org/file/bot{self._cfg.token()}/{file_path}'
        req = urllib.request.Request(url, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read(_MAX_FILE_BYTES + 1)
        except Exception:
            raise Denied('File download failed')
        if len(data) > _MAX_FILE_BYTES:
            raise Denied('Downloaded file exceeds size limit')
        Path(dest).write_bytes(data)

    def get_updates(self, offset=0, timeout=30, limit=100):
        return self.call('getUpdates', {'offset': offset, 'timeout': timeout, 'limit': limit},
                         timeout=timeout + 5)


# ── Update deduplication ──────────────────────────────────────────────────────

class TelegramUpdateDedup:
    def __init__(self, store):
        self._s = store

    def claim(self, update_id, chat_id, update_type):
        """Returns True on first-seen, False on duplicate."""
        try:
            with self._s.transaction():
                self._s.db.execute(
                    'INSERT INTO telegram_updates(update_id,chat_id,update_type,processed_at) VALUES(?,?,?,?)',
                    (int(update_id), str(chat_id), update_type, time.time()))
            return True
        except Exception:
            return False

    def record_result(self, update_id, result):
        try:
            with self._s.transaction():
                self._s.db.execute(
                    'UPDATE telegram_updates SET result=? WHERE update_id=?',
                    (json.dumps(result), int(update_id)))
        except Exception:
            pass


# ── Approval keyboard ─────────────────────────────────────────────────────────

def _approval_keyboard(approval_id):
    return {'inline_keyboard': [[
        {'text': '✓ Approve', 'callback_data': f'{_CALLBACK_PREFIX}{approval_id}:APPROVE'},
        {'text': '✎ Edit',    'callback_data': f'{_CALLBACK_PREFIX}{approval_id}:EDIT'},
        {'text': '✗ Reject',  'callback_data': f'{_CALLBACK_PREFIX}{approval_id}:REJECT'},
    ]]}


# ── Message router ────────────────────────────────────────────────────────────

class TelegramMessageRouter:
    """Routes one authorized Telegram message to operator intake.

    Files are downloaded into `inbox` so that Intake.record_* can accept them.
    The inbox defaults to the operator's canonical inbox/telegram/ subdirectory.
    """

    def __init__(self, store, api, inbox):
        from .gateway import InboundEventGateway
        self._s = store
        self._api = api
        self._inbox = Path(inbox)
        self._inbox.mkdir(parents=True, exist_ok=True)
        self._gw = InboundEventGateway(store)

    def route(self, chat_id, update_id, msg):
        if 'text' in msg:
            return self._text(chat_id, update_id, msg['text'])
        if 'voice' in msg:
            return self._voice(chat_id, update_id, msg['voice']['file_id'], msg.get('caption'))
        if 'photo' in msg:
            photos = msg['photo']
            fid = max(photos, key=lambda p: p.get('file_size', 0))['file_id']
            return self._photo(chat_id, update_id, fid, msg.get('caption'))
        if 'document' in msg:
            doc = msg['document']
            return self._document(chat_id, update_id, doc['file_id'],
                                  msg.get('caption'), doc.get('file_name'))
        return failure('NOT_CONFIGURED', 'unsupported_message_type')

    def _text(self, chat_id, update_id, text):
        return self._gw.receive('task_create', 'telegram',
                                {'text': text, 'type': 'generic', 'project': 'operator'},
                                f'telegram:text:{update_id}')

    def _voice(self, chat_id, update_id, file_id, caption):
        try:
            fp = self._api.get_file_path(file_id)
        except Denied:
            return failure('UNAVAILABLE', 'file_unavailable')
        suffix = Path(fp).suffix or '.ogg'
        dest = self._inbox / f'tg_voice_{update_id}{suffix}'
        try:
            self._api.download_file(fp, dest)
            task_key = f'telegram:voice:{update_id}'
            task_r = self._gw.receive('task_create', 'telegram',
                                      {'text': caption or 'Voice message', 'type': 'generic', 'project': 'operator'},
                                      task_key)
            if task_r.get('outcome') != 'SUCCESS':
                return task_r
            task_id = task_r['task_id']
            task = self._s.task(task_id)
            from .intake import Intake
            intake_r = Intake(self._s, inbox=self._inbox).record_voice(
                task_id, task['revision'], dest.name, manual_transcript=None)
            return {**task_r, 'intake': intake_r,
                    'stt_status': 'BLOCKED_pending_phase5_tcc_permission'}
        except Denied:
            return failure('FAILED', 'voice_intake_failed')
        except Exception:
            return failure('FAILED', 'voice_intake_failed')

    def _photo(self, chat_id, update_id, file_id, caption):
        try:
            fp = self._api.get_file_path(file_id)
        except Denied:
            return failure('UNAVAILABLE', 'file_unavailable')
        suffix = Path(fp).suffix or '.jpg'
        dest = self._inbox / f'tg_photo_{update_id}{suffix}'
        try:
            self._api.download_file(fp, dest)
            task_key = f'telegram:photo:{update_id}'
            task_r = self._gw.receive('task_create', 'telegram',
                                      {'text': caption or 'Image', 'type': 'generic', 'project': 'operator'},
                                      task_key)
            if task_r.get('outcome') != 'SUCCESS':
                return task_r
            task_id = task_r['task_id']
            task = self._s.task(task_id)
            from .intake import Intake
            intake_r = Intake(self._s, inbox=self._inbox).record_screenshot(
                task_id, task['revision'], dest.name)
            return {**task_r, 'intake': intake_r}
        except Denied:
            return failure('FAILED', 'photo_intake_failed')
        except Exception:
            return failure('FAILED', 'photo_intake_failed')

    def _document(self, chat_id, update_id, file_id, caption, file_name):
        try:
            fp = self._api.get_file_path(file_id)
        except Denied:
            return failure('UNAVAILABLE', 'file_unavailable')
        suffix = Path(file_name or fp).suffix or '.bin'
        dest = self._inbox / f'tg_doc_{update_id}{suffix}'
        try:
            self._api.download_file(fp, dest)
            task_key = f'telegram:doc:{update_id}'
            task_r = self._gw.receive('task_create', 'telegram',
                                      {'text': caption or file_name or 'Document', 'type': 'generic', 'project': 'operator'},
                                      task_key)
            if task_r.get('outcome') != 'SUCCESS':
                return task_r
            task_id = task_r['task_id']
            task = self._s.task(task_id)
            from .intake import Intake
            intake_r = Intake(self._s, inbox=self._inbox).record_document(
                task_id, task['revision'], dest.name, caption or '')
            return {**task_r, 'intake': intake_r}
        except Denied:
            return failure('FAILED', 'document_intake_failed')
        except Exception:
            return failure('FAILED', 'document_intake_failed')


# ── Callback handler (approval) ───────────────────────────────────────────────

class TelegramCallbackHandler:
    """Handles inline keyboard callbacks. Directly invokes Phase 9 Gate."""

    def __init__(self, store):
        self._s = store

    def handle(self, callback_query_id, chat_id, data):
        if not isinstance(data, str) or not data.startswith(_CALLBACK_PREFIX):
            return failure('DENIED', 'unknown_callback')
        rest = data[len(_CALLBACK_PREFIX):]
        parts = rest.rsplit(':', 1)
        if len(parts) != 2 or parts[1] not in _ALLOWED_CHOICES:
            return failure('DENIED', 'invalid_callback_format')
        approval_id, choice = parts[0], parts[1]
        try:
            approval = self._s.approval(approval_id)
        except (Conflict, Denied):
            return failure('DENIED', 'approval_not_found_or_expired')
        task_id = approval['task_id']
        action_id = approval['action_id']
        from .gateway import GatewayWorker
        gate = GatewayWorker(self._s).gate()
        actor = f'telegram:{chat_id}'
        note = 'Edit requested via Telegram' if choice == 'EDIT' else ''
        try:
            gate.resolve_approval(approval_id, choice,
                                  task_id=task_id, action_id=action_id,
                                  actor=actor, note=note)
            return {'outcome': 'SUCCESS', 'ok': True, 'choice': choice,
                    'approval_id': approval_id, 'task_id': task_id}
        except (Denied, Conflict):
            return failure('DENIED', 'approval_resolution_denied')


# ── Main bot ──────────────────────────────────────────────────────────────────

_DEFAULT_INBOX = Path(__file__).resolve().parent.parent / 'inbox' / 'telegram'


class TelegramBot:
    """Telegram remote gateway. Calls migrate_telegram on construction."""

    def __init__(self, store, *, tmp_dir=None, inbox=None):
        self._s = store
        migrate_telegram(store)
        self._cfg = TelegramConfig()
        self._api = TelegramAPI(self._cfg)
        self._dedup = TelegramUpdateDedup(store)
        # inbox = where Telegram files are stored; must match Intake's inbox path
        resolved_inbox = Path(inbox or tmp_dir or _DEFAULT_INBOX)
        self._router = TelegramMessageRouter(store, self._api, resolved_inbox)
        self._callbacks = TelegramCallbackHandler(store)

    @property
    def configured(self):
        return self._cfg.configured

    def health(self):
        return {
            'connector_id': 'telegram_bot',
            'status': self._cfg.health_status,
            'configured': self._cfg.configured,
        }

    def process_update(self, update):
        """Process one Telegram update dict.  Returns structured outcome."""
        from .safety import Controls
        try:
            Controls(self._s).check('inspect')
        except Denied:
            return failure('DENIED', 'emergency_controls_block')

        update_id = update.get('update_id')
        if not isinstance(update_id, int):
            return failure('DENIED', 'invalid_update_id')

        if 'message' in update:
            msg = update['message']
            chat_id = (msg.get('chat') or {}).get('id')
            if not self._cfg.is_allowed(chat_id):
                return failure('DENIED', 'unauthorized_sender')
            if not self._dedup.claim(update_id, chat_id, 'message'):
                return {'outcome': 'DUPLICATE', 'update_id': update_id, 'ok': False}
            result = self._router.route(chat_id, update_id, msg)
            self._dedup.record_result(update_id, result)
            self._reply(chat_id, result)
            return result

        if 'callback_query' in update:
            cb = update['callback_query']
            chat_id = ((cb.get('message') or {}).get('chat') or {}).get('id') \
                      or (cb.get('from') or {}).get('id')
            if not self._cfg.is_allowed(chat_id):
                self._api.answer_callback(cb.get('id', ''), 'Not authorized')
                return failure('DENIED', 'unauthorized_sender')
            if not self._dedup.claim(update_id, chat_id, 'callback'):
                self._api.answer_callback(cb.get('id', ''), 'Already processed')
                return {'outcome': 'DUPLICATE', 'update_id': update_id, 'ok': False}
            result = self._callbacks.handle(cb.get('id', ''), chat_id, cb.get('data', ''))
            self._dedup.record_result(update_id, result)
            ack = self._callback_ack(result)
            self._api.answer_callback(cb.get('id', ''), ack)
            return result

        return failure('NOT_CONFIGURED', 'unhandled_update_type')

    def _reply(self, chat_id, result):
        outcome = result.get('outcome', 'FAILED')
        task_id = result.get('task_id', '')
        approval_id = result.get('approval_id')
        if outcome in ('SUCCESS', 'RECORDED') and task_id:
            msg = f'Task received: `{task_id[:8]}…`'
            if approval_id:
                msg += '\n⏳ Awaiting approval'
                self._api.send_message(chat_id, msg, reply_markup=_approval_keyboard(approval_id))
            else:
                self._api.send_message(chat_id, msg)
        elif outcome == 'APPROVAL_REQUIRED':
            apid = result.get('approval_id') or ''
            msg = f'Action queued for approval: `{apid[:8]}…`'
            if apid:
                self._api.send_message(chat_id, msg, reply_markup=_approval_keyboard(apid))
            else:
                self._api.send_message(chat_id, msg)
        elif outcome == 'DUPLICATE':
            self._api.send_message(chat_id, 'Already received.')
        elif outcome == 'UNAVAILABLE':
            self._api.send_message(chat_id, 'Service unavailable. Mac worker may be offline.')
        elif outcome == 'DENIED':
            self._api.send_message(chat_id, 'Request denied.')
        else:
            self._api.send_message(chat_id, f'Status: {outcome}')

    @staticmethod
    def _callback_ack(result):
        if result.get('outcome') == 'SUCCESS':
            return {'APPROVE': 'Approved ✓', 'REJECT': 'Rejected ✗', 'EDIT': 'Edit requested ✎'}.get(result.get('choice', ''), 'Done')
        if result.get('outcome') == 'DENIED':
            return 'Denied — stale or replayed approval'
        return 'Processed'

    def poll(self, offset=0, poll_timeout=30, max_iterations=None):
        """Long-polling loop. max_iterations is for tests; None = forever."""
        if not self._cfg.configured:
            raise Denied('Bot not configured; set ZAMAL_TELEGRAM_TOKEN and ZAMAL_TELEGRAM_CHAT_ID')
        i = 0
        while max_iterations is None or i < max_iterations:
            from .safety import Controls
            try:
                Controls(self._s).check('inspect')
            except Denied:
                break
            resp = self._api.get_updates(offset=offset, timeout=poll_timeout)
            if resp.get('ok'):
                for upd in resp.get('result', []):
                    self.process_update(upd)
                    offset = max(offset, upd['update_id'] + 1)
            else:
                time.sleep(5)
            if max_iterations is not None:
                i += 1


# ── Schema migration ──────────────────────────────────────────────────────────

def migrate_telegram(store):
    """Idempotent Phase 11 schema extension: telegram_updates table + registry."""
    with store.transaction():
        row = store.db.execute(
            "SELECT version FROM core_extensions WHERE name='telegram'"
        ).fetchone()
        if row:
            if row[0] != 1:
                raise Denied('Unknown telegram schema version')
            store.db.execute(
                'SELECT update_id,chat_id,update_type,processed_at FROM telegram_updates LIMIT 0'
            )
            return
        store.db.execute('''
            CREATE TABLE telegram_updates (
                update_id   INTEGER PRIMARY KEY,
                chat_id     TEXT NOT NULL,
                update_type TEXT NOT NULL,
                processed_at REAL NOT NULL,
                result      TEXT
            )
        ''')
        # Ensure telegram_bot has a row in connection_controls (populated by
        # migrate_gateway for fresh DBs; added here for existing Phase 10 DBs).
        store.db.execute(
            "INSERT OR IGNORE INTO connection_controls(connector_id,enabled,revision) VALUES('telegram_bot',1,0)"
        )
        store.db.execute("INSERT INTO core_extensions VALUES('telegram',1)")
