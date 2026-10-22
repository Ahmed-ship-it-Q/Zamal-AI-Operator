"""Phase 11: Telegram remote command, voice and approval gateway.

Credentials NEVER appear in source, logs or exports.
Token: ZAMAL_TELEGRAM_TOKEN (env, >=20 chars)
Allowed chats: ZAMAL_TELEGRAM_CHAT_ID (env, comma-separated integer chat IDs)

Architecture:
  Telegram update → TelegramBot.process_update()
    → dedup by update_id (telegram_updates table)
    → authorized chat gate
    → text   → InboundEventGateway.receive('task_create','telegram',...)
    → voice  → download + local multilingual STT + exact intent proposal
    → photo  → download + Phase 5 Intake.record_screenshot
    → doc    → download + Phase 5 Intake.record_document
    → callback → Gate.resolve_approval (Phase 9, exact binding enforced)

Approval buttons are bound to exact approval_id/task/action. Phase 9 enforces
expiry, replay, stale-approval and tamper detection. Button text alone never
authorizes. Emergency controls always override.

STT uses a local multilingual Whisper model when installed. Missing local
runtime or model pauses the task; no remote transcription is attempted.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from .store import Denied, Conflict  # noqa: F401 (Conflict used in callback handler)
from .integration_security import failure

_API_BASE = 'https://api.telegram.org/bot'

_TIMING_LOG = Path(__file__).resolve().parent.parent / 'logs' / 'timing.log'

def _tlog(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts} [TIMING] {msg}\n'
    try:
        with open(_TIMING_LOG, 'a') as _f:
            _f.write(line)
    except OSError:
        pass
_MAX_FILE_BYTES = 20 * 1024 * 1024   # Telegram Bot API hard limit
_CALLBACK_PREFIX = 'approval:'
_VOICE_CALLBACK_PREFIX = 'voicetxt:'
_ALLOWED_CHOICES = frozenset({'APPROVE', 'EDIT', 'REJECT'})
_VOICE_CHOICES = frozenset({'CONFIRM', 'EDIT', 'CANCEL', 'RETRY'})
_VOICE_DRAFT_TTL = 3600  # discard unresolved voice drafts after 1 hour

_EXACT_WHATSAPP_COMMANDS = (
    re.compile(
        r'^WHATSAPP TO (?P<destination>[^:\n]{1,200}): (?P<content>[^\n]{1,2000})$',
        re.IGNORECASE,
    ),
    re.compile(
        r'^Send (?:this text|this message|message) to '
        r'(?P<destination>[^\n]{1,200}?) (?:in|on|via) WhatsApp '
        r'[-:] (?P<content>[^\n]{1,2000})$',
        re.IGNORECASE,
    ),
    re.compile(
        r'^(?:হোয়াটসঅ্যাপে|হোয়াটসঅ্যাপে|WhatsApp[ -]?এ) '
        r'(?P<destination>[^\n]{1,200}?) কে '
        r'(?P<content>[^\n]{1,2000}?) পাঠাও$',
        re.IGNORECASE,
    ),
)


def parse_exact_whatsapp_intent(text):
    """Parse the small deterministic WhatsApp command grammar.

    This parser intentionally does not call an AI provider. Anything outside
    the exact grammar remains paused for human/provider clarification.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    for pattern in _EXACT_WHATSAPP_COMMANDS:
        match = pattern.fullmatch(stripped)
        if match:
            destination = match.group('destination').strip()
            content = match.group('content').strip()
            if destination and content:
                return {'channel': 'whatsapp', 'destination': destination,
                        'content': content}
    return None


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
        p = {'chat_id': chat_id, 'text': str(text)[:4096]}
        if reply_markup:
            p['reply_markup'] = reply_markup
        result = self.call('sendMessage', p)
        if not result.get('ok'):
            reason = result.get('description', 'unknown')
            self.call('sendMessage', {
                'chat_id': chat_id,
                'text': f'SEND FAILED: {reason}',
            })
        return result

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
                cl = resp.headers.get('Content-Length')
                if cl is not None:
                    cl = int(cl)
                    if cl > _MAX_FILE_BYTES:
                        raise Denied('File exceeds 20 MB limit')
                    data = resp.read(cl)
                else:
                    chunks = []
                    total = 0
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _MAX_FILE_BYTES:
                            raise Denied('Downloaded file exceeds size limit')
                        chunks.append(chunk)
                    data = b''.join(chunks)
        except Denied:
            raise
        except Exception:
            raise Denied('File download failed')
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

def _delete_audio_file(audio_ref: str) -> None:
    try:
        p = Path(audio_ref)
        if p.is_file():
            p.unlink()
    except Exception:
        pass


def _approval_keyboard(approval_id):
    return {'inline_keyboard': [[
        {'text': '✓ Approve', 'callback_data': f'{_CALLBACK_PREFIX}{approval_id}:APPROVE'},
        {'text': '✎ Edit',    'callback_data': f'{_CALLBACK_PREFIX}{approval_id}:EDIT'},
        {'text': '✗ Reject',  'callback_data': f'{_CALLBACK_PREFIX}{approval_id}:REJECT'},
    ]]}


def _voice_keyboard(task_id):
    return {'inline_keyboard': [[
        {'text': '✓ Confirm', 'callback_data': f'{_VOICE_CALLBACK_PREFIX}{task_id}:CONFIRM'},
        {'text': '✎ Edit',   'callback_data': f'{_VOICE_CALLBACK_PREFIX}{task_id}:EDIT'},
        {'text': '✗ Cancel', 'callback_data': f'{_VOICE_CALLBACK_PREFIX}{task_id}:CANCEL'},
    ]]}


def _voice_low_conf_keyboard(task_id):
    """Keyboard shown when transcription confidence is low."""
    return {'inline_keyboard': [[
        {'text': '↺ Retry',  'callback_data': f'{_VOICE_CALLBACK_PREFIX}{task_id}:RETRY'},
        {'text': '✓ Accept', 'callback_data': f'{_VOICE_CALLBACK_PREFIX}{task_id}:CONFIRM'},
        {'text': '✎ Edit',   'callback_data': f'{_VOICE_CALLBACK_PREFIX}{task_id}:EDIT'},
        {'text': '✗ Cancel', 'callback_data': f'{_VOICE_CALLBACK_PREFIX}{task_id}:CANCEL'},
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

    # ── Command table ─────────────────────────────────────────────────────
    _COMMANDS = {
        '/start':  'welcome',
        '/help':   'help',
        '/status': 'status',
        '/stop':   'stop_status',
        '/approve': 'approve_cmd',
        '/reject':  'reject_cmd',
    }

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

    def _pending_voice_edit(self, chat_id):
        """Return task_id if this chat has a pending voice edit request, else None."""
        try:
            row = self._s.db.execute(
                'SELECT task_id FROM telegram_voice_drafts WHERE chat_id=? AND awaiting_edit=1',
                (str(chat_id),),
            ).fetchone()
            if row:
                return row[0]
        except Exception:
            pass
        return None

    def _text(self, chat_id, update_id, text):
        stripped = text.strip()
        # If owner is sending a correction for a pending voice draft, handle it now.
        pending_edit_task = self._pending_voice_edit(chat_id)
        if pending_edit_task:
            return self._apply_voice_edit(chat_id, pending_edit_task, stripped)
        channel_approval = re.fullmatch(
            r'APPROVE ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
            stripped,
        )
        if channel_approval:
            from .channel_adapter import get_channel_adapter
            result = get_channel_adapter().execute_telegram_approval(
                channel_approval.group(1), actor=f'telegram:{chat_id}',
                update_id=update_id,
            )
            return {
                'outcome': 'SUCCESS' if result.get('ok') else 'DENIED',
                'choice': 'APPROVE', 'proposal_id': channel_approval.group(1),
                **result,
            }
        cmd_lower = stripped.lower().split()[0] if stripped else ''
        # Handle slash commands
        if cmd_lower in self._COMMANDS:
            return self._handle_command(chat_id, update_id, stripped, cmd_lower)
        task_r = self._gw.receive(
            'task_create', 'telegram',
            {'text': text, 'type': 'generic', 'project': 'operator'},
            f'telegram:text:{update_id}')
        if task_r.get('outcome') == 'SUCCESS' and (
                'whatsapp' in stripped.casefold()
                or 'হোয়াটসঅ্যাপে' in stripped
                or 'হোয়াটসঅ্যাপে' in stripped):
            return self._route_whatsapp_intent(
                task_r['task_id'], stripped, source='telegram_text')
        return task_r

    def _apply_voice_edit(self, chat_id, task_id, correction):
        """Store corrected transcript, delete audio, reply VOICE TEXT READY."""
        try:
            row = self._s.db.execute(
                'SELECT audio_ref FROM telegram_voice_drafts WHERE task_id=?',
                (task_id,)).fetchone()
            audio_ref = row[0] if row else ''
            task = self._s.task(task_id)
            self._s.patch_context(task_id, {'final_result': {
                'voice_confirmed': True,
                'voice_transcript': correction,
            }}, task['revision'])
            with self._s.transaction():
                self._s.db.execute(
                    'DELETE FROM telegram_voice_drafts WHERE task_id=?', (task_id,))
            _delete_audio_file(audio_ref)
            self._api.send_message(chat_id, 'VOICE TEXT READY')
            return {'outcome': 'VOICE_CONFIRMED', 'ok': True, 'task_id': task_id}
        except Exception:
            return failure('FAILED', 'voice_edit_failed')

    def _route_whatsapp_intent(self, task_id, text, *, source):
        """Create or recover exactly one durable WhatsApp proposal for a task."""
        intent = parse_exact_whatsapp_intent(text)
        if intent is None:
            reason = 'complex_or_ambiguous_intent_requires_available_ai_provider'
            task = self._s.task(task_id)
            checkpoint = task['context'].get('checkpoint')
            if not (checkpoint and checkpoint.get('pending')
                    and checkpoint.get('reason') == reason):
                self._s.patch_context(task_id, {
                    'checkpoint': {
                        'pending': True, 'reason': reason,
                        'required_session': 'ai_provider', 'source': source,
                    },
                    'next_action': 'Restore one AI provider session or resend an exact command: WHATSAPP TO <contact>: <message>',
                }, task['revision'])
            return {
                'outcome': 'SESSION_REQUIRED', 'ok': False,
                'status': 'SESSION_REQUIRED', 'action_required': True,
                'reason': reason, 'task_id': task_id,
            }

        from .channel_adapter import get_channel_adapter
        adapter = get_channel_adapter()
        existing = [p for p in adapter.all_proposals() if p.task_id == task_id]
        if existing:
            proposal = existing[0]
            bound = (proposal.channel == 'whatsapp'
                     and proposal.payload.get('raw_destination') == intent['destination']
                     and proposal.payload.get('content') == intent['content'])
            if len(existing) != 1 or not bound:
                return {'outcome': 'DENIED', 'ok': False,
                        'reason': 'proposal_binding_conflict', 'task_id': task_id}
            return {
                'outcome': 'APPROVAL_REQUIRED' if proposal.status == 'PENDING' else proposal.status,
                'ok': proposal.status == 'PENDING', 'task_id': task_id,
                'proposal_id': proposal.proposal_id,
                'approval_id': proposal.approval_id, 'duplicate': True,
            }

        from .channel_adapter import resolve_whatsapp_contact
        resolution = resolve_whatsapp_contact(intent['destination'])
        if resolution['status'] != 'EXACT_ONE':
            reason = resolution['status']
            task = self._s.task(task_id)
            self._s.patch_context(task_id, {
                'checkpoint': {
                    'pending': True, 'reason': reason,
                    'required_session': 'whatsapp', 'source': source,
                },
                'next_action': 'Clarify the exact saved WhatsApp contact name or restore the WhatsApp session.',
            }, task['revision'])
            return {'outcome': 'ACTION_REQUIRED', 'ok': False,
                    'status': reason, 'reason': reason, 'task_id': task_id,
                    'action_required': True}

        proposal = adapter.propose_send(
            'whatsapp', intent['destination'], intent['content'],
            task_id=task_id, actor=source)
        task = self._s.task(task_id)
        self._s.patch_context(task_id, {
            'decisions': list(task['context'].get('decisions') or []) + [{
                'kind': 'deterministic_whatsapp_intent', 'source': source,
                'proposal_id': proposal.proposal_id,
            }],
            'next_action': f'APPROVE {proposal.proposal_id}',
        }, task['revision'])
        return {
            'outcome': 'APPROVAL_REQUIRED', 'ok': True, 'task_id': task_id,
            'proposal_id': proposal.proposal_id,
            'approval_id': proposal.approval_id, 'duplicate': False,
        }

    def claim_approval_notice(self, task_id, proposal_id):
        """Durably claim the single Telegram notice for an exact proposal."""
        for _ in range(5):
            task = self._s.task(task_id)
            decisions = list(task['context'].get('decisions') or [])
            if any(d.get('kind') == 'telegram_approval_notice'
                   and d.get('proposal_id') == proposal_id for d in decisions):
                return False
            decisions.append({
                'kind': 'telegram_approval_notice',
                'proposal_id': proposal_id, 'status': 'claimed',
            })
            try:
                self._s.patch_context(
                    task_id, {'decisions': decisions}, task['revision'])
                return True
            except Conflict:
                continue
        raise Conflict('Could not claim Telegram approval notice')

    def complete_approval_notice(self, task_id, proposal_id, sent):
        """Commit a sent notice, or release a failed claim for one safe retry."""
        for _ in range(5):
            task = self._s.task(task_id)
            decisions = list(task['context'].get('decisions') or [])
            found = False
            updated = []
            for decision in decisions:
                matches = (decision.get('kind') == 'telegram_approval_notice'
                           and decision.get('proposal_id') == proposal_id)
                if matches:
                    found = True
                    if sent:
                        updated.append({**decision, 'status': 'sent'})
                else:
                    updated.append(decision)
            if not found:
                return False
            try:
                self._s.patch_context(
                    task_id, {'decisions': updated}, task['revision'])
                return True
            except Conflict:
                continue
        raise Conflict('Could not finalize Telegram approval notice')

    def _handle_command(self, chat_id, update_id, text, cmd):
        """Handle slash command — never creates external actions."""
        if cmd == '/start':
            msg = (
                '*Zamal AI Operator*\n'
                'Remote control channel active.\n\n'
                'Commands:\n'
                '/help — list commands\n'
                '/status — channel & task status\n'
                '/stop — emergency stop status\n'
                '/approve <id> — approve a pending action\n'
                '/reject <id> — reject a pending action\n\n'
                'Or send any text to create a task.'
            )
            self._api.send_message(chat_id, msg)
            return {'outcome': 'COMMAND', 'command': 'start', 'ok': True}

        if cmd == '/help':
            msg = (
                '*Commands*\n'
                '/start — show welcome\n'
                '/status — connection & task overview\n'
                '/stop — check/clear emergency stop\n'
                '/approve <approval\\_id> — approve queued action\n'
                '/reject <approval\\_id> — reject queued action\n'
                'Or send any instruction as a task.'
            )
            self._api.send_message(chat_id, msg)
            return {'outcome': 'COMMAND', 'command': 'help', 'ok': True}

        if cmd == '/status':
            return self._cmd_status(chat_id, update_id)

        if cmd == '/stop':
            return self._cmd_stop_status(chat_id, update_id)

        if cmd in ('/approve', '/reject'):
            parts = text.split(maxsplit=1)
            approval_id = parts[1].strip() if len(parts) > 1 else ''
            if not approval_id:
                self._api.send_message(chat_id, f'Usage: {cmd} <approval_id>')
                return {'outcome': 'COMMAND', 'command': cmd, 'ok': False, 'error': 'missing_id'}
            choice = 'APPROVE' if cmd == '/approve' else 'REJECT'
            try:
                approval = self._s.approval(approval_id)
                task_id = approval['task_id']
                action_id = approval['action_id']
                from .gateway import GatewayWorker
                gate = GatewayWorker(self._s).gate()
                actor = f'telegram:{chat_id}'
                gate.resolve_approval(approval_id, choice,
                                      task_id=task_id, action_id=action_id,
                                      actor=actor)
                self._api.send_message(chat_id,
                    f'{"Approved" if choice=="APPROVE" else "Rejected"} action `{action_id[:8]}…`')
                return {'outcome': 'SUCCESS', 'choice': choice, 'approval_id': approval_id, 'ok': True}
            except Exception as exc:
                self._api.send_message(chat_id, f'Could not {cmd[1:]}: {str(exc)[:100]}')
                return {'outcome': 'DENIED', 'ok': False, 'error': str(exc)[:100]}

        return failure('NOT_CONFIGURED', f'unknown_command:{cmd}')

    def _cmd_status(self, chat_id, update_id):
        """Return sanitized status summary — no PII, no tokens."""
        try:
            from .safety import Controls
            ctrl = Controls(self._s)
            st = ctrl.status()
            stop = st.get('stop_everything', 0)
            pause = st.get('pause_external_actions', 0)
            # Task count
            tasks = self._s.db.execute(
                "SELECT state, COUNT(*) c FROM tasks GROUP BY state"
            ).fetchall()
            task_lines = '\n'.join(f'  {r["state"]}: {r["c"]}' for r in tasks) or '  (none)'
            lines = [
                '*Zamal Operator Status*',
                f'Emergency stop: {"ON" if stop else "off"}',
                f'External pause: {"ON" if pause else "off"}',
                '*Tasks:*',
                task_lines,
            ]
            self._api.send_message(chat_id, '\n'.join(lines))
        except Exception as exc:
            self._api.send_message(chat_id, f'Status unavailable: {str(exc)[:80]}')
        return {'outcome': 'COMMAND', 'command': 'status', 'ok': True, 'update_id': update_id}

    def _cmd_stop_status(self, chat_id, update_id):
        """Report emergency stop state — no mutation."""
        try:
            from .safety import Controls
            st = Controls(self._s).status()
            stop = st.get('stop_everything', 0)
            pause = st.get('pause_external_actions', 0)
            ro = st.get('read_only_mode', 0)
            lines = [
                '*Emergency Controls*',
                f'stop\\_everything: {"*ACTIVE*" if stop else "off"}',
                f'pause\\_external\\_actions: {"*ACTIVE*" if pause else "off"}',
                f'read\\_only\\_mode: {"*ACTIVE*" if ro else "off"}',
            ]
            self._api.send_message(chat_id, '\n'.join(lines))
        except Exception as exc:
            self._api.send_message(chat_id, f'Controls unavailable: {str(exc)[:80]}')
        return {'outcome': 'COMMAND', 'command': 'stop_status', 'ok': True}

    # Progress note threshold: send a "Processing…" notice for audio over this many bytes.
    _VOICE_PROGRESS_BYTES = 200_000

    def _voice(self, chat_id, update_id, file_id, caption):
        _t_voice_start = time.monotonic()
        _tlog('voice_start')
        try:
            fp = self._api.get_file_path(file_id)
        except Denied:
            return failure('UNAVAILABLE', 'file_unavailable')
        suffix = Path(fp).suffix or '.ogg'
        dest = self._inbox / f'tg_voice_{update_id}{suffix}'
        try:
            _t0 = time.monotonic()
            self._api.download_file(fp, dest)
            _tlog(f'download_done t={time.monotonic()-_t_voice_start:.2f}s  ({time.monotonic()-_t0:.2f}s for download)')
            # For longer recordings, warn the user that processing may take a moment.
            try:
                if dest.stat().st_size > self._VOICE_PROGRESS_BYTES:
                    self._api.send_message(
                        chat_id,
                        '🎙 Transcribing your voice note — this may take up to a minute for long recordings…',
                    )
            except Exception:
                pass
            task_key = f'telegram:voice:{update_id}'
            task_r = self._gw.receive('task_create', 'telegram',
                                      {'text': caption or 'Voice message', 'type': 'generic', 'project': 'operator'},
                                      task_key)
            if task_r.get('outcome') != 'SUCCESS':
                return task_r
            task_id = task_r['task_id']
            task = self._s.task(task_id)
            from .intake import Intake
            _t1 = time.monotonic()
            _tlog(f'intake_start t={_t1-_t_voice_start:.2f}s')
            intake_r = Intake(self._s, inbox=self._inbox).record_voice(
                task_id, task['revision'], dest.name, manual_transcript=None)
            _tlog(f'intake_done t={time.monotonic()-_t_voice_start:.2f}s  ({time.monotonic()-_t1:.2f}s for intake)')
            voice = next((e for e in reversed(intake_r['context'].get('evidence', []))
                          if e.get('kind') == 'voice' and e.get('reference') == dest.name), None)
            transcript = (voice or {}).get('transcript')
            low_conf = (voice or {}).get('stt_low_confidence', False)
            duration_secs = (voice or {}).get('duration_secs', 0.0)
            detected_lang = (voice or {}).get('detected_lang', 'auto')
            if transcript:
                _t2 = time.monotonic()
                result = self._store_and_reply_draft(
                    chat_id, task_id, transcript, low_conf, dest,
                    duration_secs=duration_secs, detected_lang=detected_lang)
                _tlog(f'tg_send_done t={time.monotonic()-_t_voice_start:.2f}s  ({time.monotonic()-_t2:.2f}s for tg_send)')
                return result
            reason = (voice or {}).get('stt_error') or 'local_transcription_unavailable'
            self._api.send_message(
                chat_id,
                'TRANSCRIPTION UNCLEAR — PLEASE RECORD AGAIN\n'
                f'(Reason: {reason})',
            )
            current = self._s.task(task_id)
            self._s.patch_context(task_id, {
                'checkpoint': {
                    'pending': True, 'reason': reason,
                    'required_session': 'local_transcription',
                    'source': 'telegram_voice',
                },
                'next_action': 'Resend your voice note more clearly, or type the text directly.',
            }, current['revision'])
            return {**task_r, 'outcome': 'SESSION_REQUIRED', 'ok': False,
                    'status': 'SESSION_REQUIRED', 'action_required': True,
                    'reason': reason, 'intake': intake_r,
                    'stt_status': 'pending_transcription'}
        except Denied:
            return failure('FAILED', 'voice_intake_failed')
        except Exception:
            return failure('FAILED', 'voice_intake_failed')

    def _store_and_reply_draft(self, chat_id, task_id, transcript, low_conf, audio_path,
                               *, duration_secs=0.0, detected_lang='auto'):
        """Store draft in DB and send TRANSCRIPT message with appropriate buttons."""
        try:
            with self._s.transaction():
                self._s.db.execute(
                    'INSERT OR REPLACE INTO telegram_voice_drafts'
                    '(task_id, chat_id, created_at, awaiting_edit, transcript, audio_ref)'
                    ' VALUES(?,?,?,0,?,?)',
                    (task_id, str(chat_id), time.time(),
                     transcript, str(audio_path)))
        except Exception:
            pass
        # Build human-readable header: language and duration.
        lang_label = {'bn': 'Bangla', 'en': 'English', 'mixed': 'Bangla+English'}.get(
            detected_lang, 'auto-detected')
        dur_label = f'{int(duration_secs)}s' if duration_secs > 0 else 'unknown'
        header = f'Language: {lang_label} | Duration: {dur_label}'
        if low_conf:
            msg = f'⚠️ Confidence is low — the audio may be unclear.\n{header}\nTRANSCRIPT: {transcript}'
            keyboard = _voice_low_conf_keyboard(task_id)
        else:
            msg = f'{header}\nTRANSCRIPT: {transcript}'
            keyboard = _voice_keyboard(task_id)
        self._api.send_message(chat_id, msg, reply_markup=keyboard)
        return {'outcome': 'VOICE_DRAFT', 'ok': True,
                'task_id': task_id, 'stt_status': 'transcribed',
                'low_confidence': low_conf}

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

    def __init__(self, store, cfg=None, api=None):
        self._s = store
        self._cfg = cfg  # TelegramConfig; if provided, adds defence-in-depth chat_id check
        self._api = api  # TelegramAPI; required for voice callbacks that send replies

    def handle(self, callback_query_id, chat_id, data, update_id=None):
        # Defence-in-depth: re-verify chat_id against the operator allowlist even
        # though process_update() already checked it.  This ensures the job-app
        # approval callback boundary cannot be reached by an unauthorized sender
        # even if the outer check is bypassed or handler is called directly.
        if self._cfg is not None and not self._cfg.is_allowed(chat_id):
            return failure('DENIED', 'unauthorized_sender')
        if isinstance(data, str) and data.startswith(_VOICE_CALLBACK_PREFIX):
            return self._handle_voice_callback(chat_id, data)
        if not isinstance(data, str) or not data.startswith(_CALLBACK_PREFIX):
            return failure('DENIED', 'unknown_callback')
        rest = data[len(_CALLBACK_PREFIX):]
        parts = rest.rsplit(':', 1)
        if len(parts) != 2 or parts[1] not in _ALLOWED_CHOICES:
            return failure('DENIED', 'invalid_callback_format')
        approval_id, choice = parts[0], parts[1]
        from .channel_adapter import get_channel_adapter
        channel_adapter = get_channel_adapter()
        channel_proposal = channel_adapter.proposal_for_approval(approval_id)
        if channel_proposal is not None:
            actor = f'telegram:{chat_id}'
            if choice == 'APPROVE':
                result = channel_adapter.execute_telegram_approval(
                    channel_proposal.proposal_id, actor=actor,
                    update_id=update_id,
                )
            elif choice == 'REJECT':
                result = channel_adapter.execute_rejected(channel_proposal, actor)
            else:
                result = {'ok': False, 'error': 'edit_requires_new_proposal'}
                if self._api is not None:
                    self._api.send_message(
                        chat_id,
                        'EDIT not supported here: send a new message with your revised instructions.',
                    )
            return {
                'outcome': 'SUCCESS' if result.get('ok') else 'DENIED',
                'choice': choice, 'approval_id': approval_id,
                'proposal_id': channel_proposal.proposal_id, **result,
            }
        try:
            approval = self._s.approval(approval_id)
        except (Conflict, Denied):
            return failure('DENIED', 'approval_not_found_or_expired')
        task_id = approval['task_id']
        action_id = approval['action_id']
        if self._s.action(action_id)['kind'] == 'job_app_submit':
            from .job_app import JobApplicationWorker
            gate = JobApplicationWorker(self._s).gate()
        else:
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

    def _handle_voice_callback(self, chat_id, data):
        """Handle Confirm/Edit/Cancel for a pending voice transcript draft."""
        rest = data[len(_VOICE_CALLBACK_PREFIX):]
        parts = rest.rsplit(':', 1)
        if len(parts) != 2 or parts[1] not in _VOICE_CHOICES:
            return failure('DENIED', 'invalid_voice_callback')
        task_id, choice = parts[0], parts[1]
        # Verify the draft belongs to this chat.
        try:
            row = self._s.db.execute(
                'SELECT chat_id, awaiting_edit FROM telegram_voice_drafts WHERE task_id=?',
                (task_id,),
            ).fetchone()
        except Exception:
            return failure('DENIED', 'voice_draft_not_found')
        if not row or str(row[0]) != str(chat_id):
            return failure('DENIED', 'voice_draft_not_found')
        if choice == 'CONFIRM':
            return self._voice_confirm(chat_id, task_id)
        if choice == 'EDIT':
            return self._voice_request_edit(chat_id, task_id)
        if choice == 'RETRY':
            return self._voice_retry(chat_id, task_id)
        # CANCEL
        return self._voice_cancel(chat_id, task_id)

    def _voice_confirm(self, chat_id, task_id):
        try:
            row = self._s.db.execute(
                'SELECT transcript, audio_ref FROM telegram_voice_drafts WHERE task_id=?',
                (task_id,)).fetchone()
            transcript = row[0] if row else ''
            audio_ref = row[1] if row else ''
            task = self._s.task(task_id)
            self._s.patch_context(task_id, {'final_result': {
                'voice_confirmed': True,
                'voice_transcript': transcript,
            }}, task['revision'])
            with self._s.transaction():
                self._s.db.execute(
                    'DELETE FROM telegram_voice_drafts WHERE task_id=?', (task_id,))
            _delete_audio_file(audio_ref)
            try:
                from .gemini_transport import GeminiTextTransport
                from .providers import ProviderUnavailable, ProviderUncertain
                _t_confirm_start = time.monotonic()
                _tlog('confirm_gemini_start')
                gemini = GeminiTextTransport()
                reply = gemini.chat(transcript)
                _t_after_gemini = time.monotonic()
                _tlog(f'confirm_gemini_done t={_t_after_gemini-_t_confirm_start:.2f}s')
                _send_result = self._api.send_message(chat_id, reply)
                _tlog(f'confirm_tg_send_{"ok" if _send_result.get("ok") else "FAILED"} t={time.monotonic()-_t_confirm_start:.2f}s  ({time.monotonic()-_t_after_gemini:.2f}s for tg_send)')
            except Exception as _gemini_exc:
                import logging as _logging
                _logging.getLogger(__name__).error(
                    'Gemini chat failed: %s: %s', type(_gemini_exc).__name__, _gemini_exc)
                self._api.send_message(
                    chat_id, f'GEMINI FAILED: {type(_gemini_exc).__name__}')
                self._api.send_message(chat_id, 'VOICE TEXT READY')
            return {'outcome': 'VOICE_CONFIRMED', 'ok': True, 'task_id': task_id}
        except Exception:
            return failure('FAILED', 'voice_confirm_failed')

    def _voice_request_edit(self, chat_id, task_id):
        try:
            with self._s.transaction():
                self._s.db.execute(
                    'UPDATE telegram_voice_drafts SET awaiting_edit=1 WHERE task_id=?',
                    (task_id,))
            self._api.send_message(chat_id, 'Send your correction text:')
            return {'outcome': 'VOICE_EDIT_REQUESTED', 'ok': True, 'task_id': task_id}
        except Exception:
            return failure('FAILED', 'voice_edit_request_failed')

    def _voice_cancel(self, chat_id, task_id):
        try:
            row = self._s.db.execute(
                'SELECT audio_ref FROM telegram_voice_drafts WHERE task_id=?',
                (task_id,)).fetchone()
            audio_ref = row[0] if row else ''
            with self._s.transaction():
                self._s.db.execute(
                    'DELETE FROM telegram_voice_drafts WHERE task_id=?', (task_id,))
            _delete_audio_file(audio_ref)
            self._api.send_message(chat_id, 'Cancelled.')
            return {'outcome': 'VOICE_CANCELLED', 'ok': True, 'task_id': task_id}
        except Exception:
            return failure('FAILED', 'voice_cancel_failed')

    def _voice_retry(self, chat_id, task_id):
        """Re-transcribe the stored audio file and update the draft."""
        try:
            row = self._s.db.execute(
                'SELECT audio_ref FROM telegram_voice_drafts WHERE task_id=?',
                (task_id,)).fetchone()
            if not row or not row[0]:
                return failure('FAILED', 'voice_retry_audio_missing')
            audio_path = Path(row[0])
            if not audio_path.is_file():
                return failure('FAILED', 'voice_retry_audio_missing')
            from .intake import LocalMultilingualSttAdapter
            stt = LocalMultilingualSttAdapter()
            result = stt.transcribe(audio_path)
            if result.pending or not result.transcript:
                error = result.error or 'local_transcription_failed'
                self._api.send_message(
                    chat_id,
                    f'Retry failed: `{error}`\nSend a text correction or Cancel.',
                    reply_markup=_voice_keyboard(task_id),
                )
                return {'outcome': 'VOICE_RETRY_FAILED', 'ok': False,
                        'task_id': task_id, 'error': error}
            transcript = result.transcript
            with self._s.transaction():
                self._s.db.execute(
                    'UPDATE telegram_voice_drafts SET transcript=?, awaiting_edit=0'
                    ' WHERE task_id=?',
                    (transcript, task_id))
            if result.low_confidence:
                msg = (f'⚠️ Still low confidence after retry.\n'
                       f'TRANSCRIPT: {transcript}')
                keyboard = _voice_low_conf_keyboard(task_id)
            else:
                msg = f'TRANSCRIPT: {transcript}'
                keyboard = _voice_keyboard(task_id)
            self._api.send_message(chat_id, msg, reply_markup=keyboard)
            return {'outcome': 'VOICE_DRAFT', 'ok': True, 'task_id': task_id,
                    'stt_status': 'transcribed', 'low_confidence': result.low_confidence}
        except Exception:
            return failure('FAILED', 'voice_retry_failed')


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
        self._callbacks = TelegramCallbackHandler(store, cfg=self._cfg, api=self._api)

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
            result = self._callbacks.handle(cb.get('id', ''), chat_id,
                                            cb.get('data', ''), update_id=update_id)
            self._dedup.record_result(update_id, result)
            ack = self._callback_ack(result)
            self._api.answer_callback(cb.get('id', ''), ack)
            return result

        return failure('NOT_CONFIGURED', 'unhandled_update_type')

    def _reply(self, chat_id, result):
        outcome = result.get('outcome', 'FAILED')
        task_id = result.get('task_id', '')
        approval_id = result.get('approval_id')
        # Voice outcomes are handled inline (messages already sent); skip here.
        if outcome in ('VOICE_DRAFT', 'VOICE_CONFIRMED', 'VOICE_EDIT_REQUESTED',
                       'VOICE_CANCELLED', 'VOICE_RETRY_FAILED'):
            return
        if result.get('proposal_id') and outcome == 'SUCCESS':
            return
        if outcome in ('SUCCESS', 'RECORDED') and task_id:
            msg = f'Task received: `{task_id[:8]}…`'
            if approval_id:
                msg += '\n⏳ Awaiting approval'
                self._api.send_message(chat_id, msg, reply_markup=_approval_keyboard(approval_id))
            else:
                self._api.send_message(chat_id, msg)
        elif outcome == 'APPROVAL_REQUIRED':
            apid = result.get('approval_id') or ''
            proposal_id = result.get('proposal_id') or ''
            if proposal_id and task_id and not self._router.claim_approval_notice(
                    task_id, proposal_id):
                return
            msg = f'Action queued for approval: `{apid[:8]}…`'
            if apid:
                if proposal_id:
                    from .channel_adapter import get_channel_adapter
                    proposal = get_channel_adapter().get_proposal(proposal_id)
                    if proposal is None:
                        return
                    msg = (f'WHATSAPP APPROVAL REQUIRED\n'
                           f'To: {proposal.payload.get("raw_destination", "")}\n'
                           f'Message: {proposal.payload.get("content", "")}\n'
                           f'Proposal: {proposal_id}')
                    response = self._api.call('sendMessage', {
                        'chat_id': chat_id, 'text': msg[:4096],
                        'reply_markup': _approval_keyboard(apid),
                    })
                    if task_id:
                        self._router.complete_approval_notice(
                            task_id, proposal_id, bool(response.get('ok')))
                    return
                response = self._api.send_message(
                    chat_id, msg, reply_markup=_approval_keyboard(apid))
                if proposal_id and task_id:
                    self._router.complete_approval_notice(
                        task_id, proposal_id, bool(response.get('ok')))
            else:
                self._api.send_message(chat_id, msg)
        elif outcome == 'DUPLICATE':
            self._api.send_message(chat_id, 'Already received.')
        elif outcome == 'UNAVAILABLE':
            self._api.send_message(chat_id, 'Service unavailable. Mac worker may be offline.')
        elif outcome == 'SESSION_REQUIRED':
            reason = str(result.get('reason') or 'required_session_unavailable')[:160]
            self._api.send_message(
                chat_id,
                'HUMAN ACTION REQUIRED\n'
                f'Reason: `{reason}`\n'
                'No proposal or external action was created.')
        elif outcome == 'ACTION_REQUIRED':
            reason = str(result.get('reason') or 'contact_clarification_required')[:160]
            self._api.send_message(chat_id, f'HUMAN ACTION REQUIRED\nReason: `{reason}`')
        elif outcome == 'DENIED':
            self._api.send_message(chat_id, 'Request denied.')
        else:
            self._api.send_message(chat_id, f'Status: {outcome}')

    @staticmethod
    def _callback_ack(result):
        outcome = result.get('outcome', '')
        if outcome == 'SUCCESS':
            return {'APPROVE': 'Approved ✓', 'REJECT': 'Rejected ✗', 'EDIT': 'Edit requested ✎'}.get(result.get('choice', ''), 'Done')
        if outcome == 'VOICE_CONFIRMED':
            return 'Saved ✓'
        if outcome == 'VOICE_EDIT_REQUESTED':
            return 'Send correction ✎'
        if outcome == 'VOICE_CANCELLED':
            return 'Cancelled ✗'
        if outcome in ('VOICE_DRAFT', 'VOICE_RETRY_FAILED'):
            return 'Retrying…'
        if outcome == 'DENIED':
            return 'Denied — stale or replayed approval'
        return 'Processed'

    def notify_approval_required(self, channel: str, action_type: str,
                                  sanitized_preview: str, approval_id: str,
                                  task_id: str = '') -> bool:
        """Send APPROVAL_REQUIRED notice to all authorized chats.

        No real content, sender names, or private identifiers are included.
        Returns True if at least one message was sent successfully.
        """
        if not self._cfg.configured:
            return False
        preview_safe = sanitized_preview[:200].replace('[', '\\[').replace('`', "'")
        channel_safe = str(channel)[:80].replace('_', ' ')
        action_safe = str(action_type)[:80].replace('_', ' ')
        msg = (
            f'*ACTION REQUIRES APPROVAL*\n'
            f'Channel: {channel_safe}\n'
            f'Action: {action_safe}\n'
            f'Preview: `{preview_safe}`\n'
            f'Approval ID: `{approval_id[:16]}…`\n'
            f'Task: `{task_id[:8]}…`' if task_id else
            f'*ACTION REQUIRES APPROVAL*\n'
            f'Channel: {channel_safe}\n'
            f'Action: {action_safe}\n'
            f'Preview: `{preview_safe}`\n'
            f'Approval ID: `{approval_id[:16]}…`'
        )
        keyboard = _approval_keyboard(approval_id)
        sent = False
        for chat_id in self._cfg._allowed:
            r = self._api.send_message(chat_id, msg, reply_markup=keyboard)
            if r.get('ok'):
                sent = True
        return sent

    def notify_action_required(self, channel: str, blocker_type: str,
                                task_id: str, instructions: str = '') -> bool:
        """Send ACTION_REQUIRED (CAPTCHA/OTP/session) notice to authorized chats.

        Returns True if at least one message was sent successfully.
        """
        if not self._cfg.configured:
            return False
        inst_safe = instructions[:200] if instructions else 'Please complete the human action and retry.'
        msg = (
            f'*HUMAN ACTION REQUIRED*\n'
            f'Channel: {channel}\n'
            f'Blocker: {blocker_type}\n'
            f'Task: `{task_id[:8]}…`\n'
            f'{inst_safe}'
        )
        sent = False
        for chat_id in self._cfg._allowed:
            r = self._api.send_message(chat_id, msg)
            if r.get('ok'):
                sent = True
        return sent

    def poll(self, offset=0, poll_timeout=30, max_iterations=None,
             stop_event=None, start_from_latest=False):
        """Long-polling loop. max_iterations is for tests; None = forever."""
        if not self._cfg.configured:
            raise Denied('Bot not configured; set ZAMAL_TELEGRAM_TOKEN and ZAMAL_TELEGRAM_CHAT_ID')
        if start_from_latest:
            latest = self._api.get_updates(offset=-1, timeout=0, limit=1)
            if latest.get('ok') and latest.get('result'):
                offset = latest['result'][-1]['update_id'] + 1
        i = 0
        while ((max_iterations is None or i < max_iterations)
               and not (stop_event and stop_event.is_set())):
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
    """Idempotent Phase 11 schema extension: telegram tables + registry."""
    with store.transaction():
        row = store.db.execute(
            "SELECT version FROM core_extensions WHERE name='telegram'"
        ).fetchone()
        if row:
            if row[0] not in (1, 2, 3):
                raise Denied('Unknown telegram schema version')
            store.db.execute(
                'SELECT update_id,chat_id,update_type,processed_at FROM telegram_updates LIMIT 0'
            )
            if row[0] == 1:
                # Upgrade v1 → v3: add voice drafts table with audio_ref.
                store.db.execute('''
                    CREATE TABLE IF NOT EXISTS telegram_voice_drafts (
                        task_id       TEXT PRIMARY KEY,
                        chat_id       TEXT NOT NULL,
                        created_at    REAL NOT NULL,
                        awaiting_edit INTEGER NOT NULL DEFAULT 0,
                        transcript    TEXT NOT NULL DEFAULT '',
                        audio_ref     TEXT NOT NULL DEFAULT ''
                    )
                ''')
                store.db.execute(
                    "UPDATE core_extensions SET version=3 WHERE name='telegram'")
            elif row[0] == 2:
                # Upgrade v2 → v3: add audio_ref column if missing.
                cols = [c[1] for c in store.db.execute(
                    'PRAGMA table_info(telegram_voice_drafts)').fetchall()]
                if 'audio_ref' not in cols:
                    store.db.execute(
                        "ALTER TABLE telegram_voice_drafts ADD COLUMN audio_ref TEXT NOT NULL DEFAULT ''")
                store.db.execute(
                    "UPDATE core_extensions SET version=3 WHERE name='telegram'")
            else:
                store.db.execute(
                    'SELECT task_id,chat_id,created_at,awaiting_edit,transcript,audio_ref'
                    ' FROM telegram_voice_drafts LIMIT 0'
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
        store.db.execute('''
            CREATE TABLE telegram_voice_drafts (
                task_id       TEXT PRIMARY KEY,
                chat_id       TEXT NOT NULL,
                created_at    REAL NOT NULL,
                awaiting_edit INTEGER NOT NULL DEFAULT 0,
                transcript    TEXT NOT NULL DEFAULT '',
                audio_ref     TEXT NOT NULL DEFAULT ''
            )
        ''')
        # Ensure telegram_bot has a row in connection_controls (populated by
        # migrate_gateway for fresh DBs; added here for existing Phase 10 DBs).
        store.db.execute(
            "INSERT OR IGNORE INTO connection_controls(connector_id,enabled,revision) VALUES('telegram_bot',1,0)"
        )
        store.db.execute("INSERT INTO core_extensions VALUES('telegram',3)")
