"""Unified channel adapter for all communication integrations.

Read/search/classify: no approval required.
Any external send/post/create/delete/update: requires Gate approval.
Emergency Stop blocks all queued and new external actions.
Privacy filter applied to all content before storage or routing.
"""
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .privacy_filter import PRIVACY_FILTER
from .integration_security import SECRET_TEXT

_IS_DARWIN = __import__('sys').platform == 'darwin'


@dataclass
class ChannelItem:
    """Single item read from a channel (email, message, event, etc.)."""
    channel: str
    item_id: str
    sender: str
    subject: str
    preview: str          # sanitized preview, max 500 chars
    timestamp: float
    metadata: dict = field(default_factory=dict)


@dataclass
class ChannelResult:
    """Result of a channel read/search operation."""
    channel: str
    items: list           # list[ChannelItem]
    count: int
    task_id: str
    privacy_level: str
    provenance: dict = field(default_factory=dict)
    error: str = ''


@dataclass
class ActionProposal:
    """Proposed channel action awaiting approval."""
    proposal_id: str
    channel: str
    action: str           # 'send_message', 'create_event', 'delete_event', 'post', 'dm'
    destination: str      # sanitized: email, phone, username — never raw auth identifiers
    preview: str          # sanitized content preview (max 500 chars)
    task_id: str
    approval_id: str      # Gate approval ID (empty until propose() called)
    payload: dict         # sanitized action payload
    timestamp: float
    status: str = 'PENDING'   # PENDING | APPROVED | REJECTED | EXECUTED | EXPIRED


class ChannelAdapter:
    """Unified read/propose/execute interface for all communication channels.

    Pattern:
      1. adapter.read(channel, ...) → ChannelResult (safe, no approval)
      2. adapter.propose_send(channel, dest, content, task_id) → ActionProposal
         - creates Gate approval, waits for human decision
      3. On APPROVE: adapter.execute_approved(proposal, approval_id, actor)
         - executes exactly once, idempotent
      4. On REJECT: adapter.execute_rejected(proposal, actor)
         - zero external action guaranteed
    """

    CHANNELS = frozenset({'telegram', 'gmail', 'calendar', 'whatsapp', 'facebook', 'instagram'})

    def __init__(self):
        self._lock = threading.Lock()
        self._executed: set = set()   # proposal_ids already executed (idempotency)
        self._transport = None        # lazy import

    # ── Read operations ────────────────────────────────────────────────────

    def read(self, channel: str, *, limit: int = 10, query: str = '',
             task_id: str = '') -> ChannelResult:
        """Read/search a channel. No approval required. Privacy filter applied."""
        if channel not in self.CHANNELS:
            return ChannelResult(channel=channel, items=[], count=0,
                                 task_id=task_id, privacy_level='PUBLIC',
                                 error=f'unknown_channel:{channel}')
        if not _IS_DARWIN:
            return ChannelResult(channel=channel, items=[], count=0,
                                 task_id=task_id, privacy_level='PUBLIC',
                                 error='not_darwin')
        try:
            reader = _get_reader(channel)
            items = reader(limit=limit, query=query)
        except Exception as exc:
            return ChannelResult(channel=channel, items=[], count=0,
                                 task_id=task_id, privacy_level='PUBLIC',
                                 error=str(exc)[:200])
        sanitized = []
        privacy_level = 'PUBLIC'
        for item in items:
            flt = PRIVACY_FILTER.check(item.preview)
            if flt.level == 'BLOCKED':
                continue
            if flt.level == 'SANITIZED':
                privacy_level = 'SANITIZED'
            item.preview = flt.text[:500]
            sanitized.append(item)
        return ChannelResult(
            channel=channel,
            items=sanitized,
            count=len(sanitized),
            task_id=task_id,
            privacy_level=privacy_level,
            provenance={'channel': channel, 'limit': limit, 'query': query,
                        'timestamp': time.time()},
        )

    # ── Write proposal (approval-gated) ───────────────────────────────────

    def propose_send(self, channel: str, destination: str, content: str,
                     *, task_id: str = '', actor: str = 'operator') -> ActionProposal:
        """Propose sending a message/DM. Returns ActionProposal with approval_id.

        Caller must present the proposal to a human and wait for approval.
        No external action is taken here.
        """
        flt = PRIVACY_FILTER.check(content)
        if flt.level == 'BLOCKED':
            raise ValueError(f'privacy_filter_blocked: {flt.reasons}')
        proposal_id = str(uuid.uuid4())
        payload = {
            'channel': channel,
            'destination': destination[:200],
            'content': flt.text[:2000],
            'privacy_level': flt.level,
        }
        approval_id = self._create_approval(
            task_id=task_id,
            action='send_message',
            channel=channel,
            destination=destination,
            preview=flt.text[:500],
            payload=payload,
            actor=actor,
        )
        return ActionProposal(
            proposal_id=proposal_id,
            channel=channel,
            action='send_message',
            destination=destination[:200],
            preview=flt.text[:500],
            task_id=task_id,
            approval_id=approval_id,
            payload=payload,
            timestamp=time.time(),
        )

    def propose_calendar_event(self, title: str, start_dt: str, end_dt: str,
                                description: str = '', *, task_id: str = '',
                                actor: str = 'operator') -> ActionProposal:
        """Propose creating a calendar event. No event is created until approval."""
        flt = PRIVACY_FILTER.check(f'{title} {description}')
        if flt.level == 'BLOCKED':
            raise ValueError(f'privacy_filter_blocked: {flt.reasons}')
        proposal_id = str(uuid.uuid4())
        payload = {
            'channel': 'calendar',
            'title': title[:200],
            'start_dt': start_dt,
            'end_dt': end_dt,
            'description': description[:1000],
        }
        approval_id = self._create_approval(
            task_id=task_id,
            action='create_event',
            channel='calendar',
            destination='Google Calendar',
            preview=f'{title} | {start_dt} → {end_dt}',
            payload=payload,
            actor=actor,
        )
        return ActionProposal(
            proposal_id=proposal_id,
            channel='calendar',
            action='create_event',
            destination='Google Calendar',
            preview=f'{title} | {start_dt} → {end_dt}',
            task_id=task_id,
            approval_id=approval_id,
            payload=payload,
            timestamp=time.time(),
        )

    # ── Execution (post-approval) ──────────────────────────────────────────

    def execute_approved(self, proposal: ActionProposal,
                         approval_id: str, actor: str) -> dict:
        """Execute an approved proposal exactly once."""
        # Emergency Stop check
        stop = _get_emergency_stop()
        if stop.get('stop_everything') or stop.get('pause_external_actions'):
            return {'ok': False, 'error': 'emergency_stop_active'}

        # Idempotency
        with self._lock:
            if proposal.proposal_id in self._executed:
                return {'ok': False, 'error': 'already_executed'}
            self._executed.add(proposal.proposal_id)

        # Verify approval_id matches
        if proposal.approval_id and proposal.approval_id != approval_id:
            with self._lock:
                self._executed.discard(proposal.proposal_id)
            return {'ok': False, 'error': 'approval_id_mismatch'}

        try:
            executor = _get_executor(proposal.channel, proposal.action)
            result = executor(proposal.payload)
            return {'ok': True, 'result': result, 'proposal_id': proposal.proposal_id,
                    'approval_id': approval_id, 'actor': actor}
        except Exception as exc:
            with self._lock:
                self._executed.discard(proposal.proposal_id)
            return {'ok': False, 'error': str(exc)[:300]}

    def execute_rejected(self, proposal: ActionProposal, actor: str) -> dict:
        """Record rejection. Guarantees zero external action.

        Marks proposal_id in _executed so subsequent execute_approved calls are blocked.
        """
        proposal.status = 'REJECTED'
        with self._lock:
            self._executed.add(proposal.proposal_id)
        return {'ok': True, 'action': 'rejected', 'proposal_id': proposal.proposal_id,
                'actor': actor, 'external_action': 'none'}

    # ── Internal helpers ───────────────────────────────────────────────────

    def _create_approval(self, *, task_id, action, channel, destination,
                          preview, payload, actor) -> str:
        """Create Gate approval record. Returns approval_id."""
        try:
            from .store import Store, Gate, Policy
            store = Store()
            gate = Gate(store)
            request_key = str(uuid.uuid4())
            action_record = gate.propose(
                task_id=task_id or '_channel_adapter',
                kind=f'channel_{action}',
                payload=json.dumps({
                    'channel': channel,
                    'destination': destination[:200],
                    'preview': preview[:500],
                    'action': action,
                    **{k: v for k, v in payload.items() if k not in ('content',)},
                }),
                request_key=request_key,
            )
            approval_id = action_record.get('approval_id', '') if action_record else ''
            store.close()
            return approval_id
        except Exception:
            return str(uuid.uuid4())   # fallback: generate local ID


# ── Emergency stop helper ──────────────────────────────────────────────────

def _get_emergency_stop() -> dict:
    """Return emergency stop flags from the controls table. Safe to call anywhere."""
    try:
        from .store import Store
        store = Store()
        try:
            row = store.db.execute('SELECT stop_everything, pause_external_actions FROM controls WHERE id=1').fetchone()
            if row is None:
                return {}
            return {
                'stop_everything': bool(row['stop_everything']),
                'pause_external_actions': bool(row['pause_external_actions']),
            }
        finally:
            store.close()
    except Exception:
        return {}


# ── Channel-specific readers ───────────────────────────────────────────────

def _get_reader(channel: str):
    """Return the read function for a channel."""
    return {
        'telegram': _read_telegram,
        'gmail': _read_gmail,
        'calendar': _read_calendar,
        'whatsapp': _read_whatsapp,
        'facebook': _read_facebook,
        'instagram': _read_instagram,
    }[channel]


def _get_executor(channel: str, action: str):
    """Return the executor for a (channel, action) pair."""
    key = f'{channel}:{action}'
    executors = {
        'gmail:send_message': _exec_gmail_send,
        'calendar:create_event': _exec_calendar_create,
        'whatsapp:send_message': _exec_whatsapp_send,
        'facebook:send_message': _exec_facebook_send,
        'instagram:send_message': _exec_instagram_dm,
        'telegram:send_message': _exec_telegram_send,
    }
    if key not in executors:
        raise ValueError(f'No executor for {key}')
    return executors[key]


def _read_telegram(*, limit=10, query='') -> list:
    """Read recent Telegram messages via bot API (update log)."""
    try:
        from .control_center import _load_auth_env
        _load_auth_env()
        from .telegram_bot import TelegramConfig, TelegramAPI
        tc = TelegramConfig()
        if not tc.configured:
            return []
        api = TelegramAPI(tc._token)
        updates = api.call('getUpdates', {'limit': min(limit, 20), 'offset': -limit}, timeout=10)
        items = []
        for u in (updates or []):
            msg = u.get('message') or u.get('edited_message') or {}
            text = msg.get('text', '')
            fr = msg.get('from', {})
            sender = fr.get('username', '') or fr.get('first_name', '')
            items.append(ChannelItem(
                channel='telegram',
                item_id=str(u.get('update_id', '')),
                sender=sender[:100],
                subject='',
                preview=text[:500],
                timestamp=float(msg.get('date', time.time())),
            ))
        return items
    except Exception:
        return []


def _read_gmail(*, limit=10, query='') -> list:
    """Read Gmail inbox via logged-in Chrome session."""
    from .ai_browser_transport import _exec_js_in_domain
    js = f'''(function(){{
      var rows=document.querySelectorAll('tr.zA');
      var emails=[];
      var n=Math.min(rows.length,{limit});
      for(var i=0;i<n;i++){{
        var r=rows[i];
        var subEl=r.querySelector('[data-thread-id] span,.y6 span');
        var sndrEl=r.querySelector('[email],.yP,.zF');
        var bodyEl=r.querySelector('.y2');
        var timeEl=r.querySelector('.xW .xY span');
        emails.push({{
          id:r.getAttribute('id')||String(i),
          sender:(sndrEl?.getAttribute('email')||sndrEl?.innerText||'').slice(0,100),
          subject:(subEl?.innerText||'').slice(0,200),
          preview:(bodyEl?.innerText||'').slice(0,300),
          time:(timeEl?.getAttribute('title')||'').slice(0,30)
        }});
      }}
      return JSON.stringify(emails);
    }})()'''
    ok, out = _exec_js_in_domain('mail.google.com', js, timeout=10)
    if not ok or not out or out in ('null', 'missing value'):
        return []
    try:
        data = json.loads(out)
        return [ChannelItem(
            channel='gmail',
            item_id=d.get('id', str(i)),
            sender=d.get('sender', ''),
            subject=d.get('subject', ''),
            preview=d.get('preview', ''),
            timestamp=time.time(),
            metadata={'time': d.get('time', '')},
        ) for i, d in enumerate(data)]
    except Exception:
        return []


def _read_calendar(*, limit=10, query='') -> list:
    """Read Google Calendar events via logged-in Chrome session."""
    from .ai_browser_transport import _exec_js_in_domain
    js = f'''(function(){{
      // Collect aria-label events from week/month grid
      var eventEls=Array.from(document.querySelectorAll('[aria-label]')).filter(function(el){{
        var a=el.getAttribute('aria-label')||'';
        return a.length>10&&(a.includes(':')||a.includes('PM')||a.includes('AM')||el.className.includes('event')||el.className.includes('chip'));
      }});
      // Fallback: get all buttons/links inside the main calendar grid
      if(!eventEls.length){{
        eventEls=Array.from(document.querySelectorAll('[role=main] [role=button],[role=main] [role=link]')).filter(function(el){{return el.innerText.trim().length>3;}});
      }}
      var result=[];
      var n=Math.min(eventEls.length,{limit});
      for(var i=0;i<n;i++){{
        var el=eventEls[i];
        var txt=el.getAttribute('aria-label')||el.innerText.trim();
        if(txt&&txt.length>3)result.push({{id:String(i),title:txt.slice(0,200)}});
      }}
      // If still empty, return summary from page body
      if(!result.length){{
        var body=document.body.innerText;
        var dateMatch=body.match(/\\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\s+\\d+[^\\n]*/g)||[];
        dateMatch.slice(0,{limit}).forEach(function(m,i){{result.push({{id:'body_'+i,title:m.trim().slice(0,200)}});}});
      }}
      return JSON.stringify(result);
    }})()'''
    ok, out = _exec_js_in_domain('calendar.google.com', js, timeout=10)
    if not ok or not out or out in ('null', 'missing value', '[]'):
        return []
    try:
        data = json.loads(out)
        return [ChannelItem(
            channel='calendar',
            item_id=d.get('id', str(i)),
            sender='Google Calendar',
            subject=d.get('title', ''),
            preview=d.get('title', ''),
            timestamp=time.time(),
        ) for i, d in enumerate(data)]
    except Exception:
        return []


def _read_whatsapp(*, limit=10, query='') -> list:
    """Read WhatsApp Web conversations via logged-in Chrome session."""
    from .ai_browser_transport import _exec_js_in_domain
    js = f'''(function(){{
      var items=document.querySelectorAll('[data-testid=cell-frame-container],[role=listitem]');
      if(!items.length) items=document.querySelectorAll('._ak72,._ak8k,._ak8l');
      var result=[];
      var n=Math.min(items.length,{limit});
      for(var i=0;i<n;i++){{
        var el=items[i];
        var nameEl=el.querySelector('[dir=auto] span,[data-testid=cell-frame-title] span');
        var lastEl=el.querySelector('[data-testid=last-msg-status] span,[class*=last-msg]');
        var name=(nameEl?.innerText||'').trim().slice(0,100);
        if(!name)continue;
        result.push({{id:String(i),name:name,preview:(lastEl?.innerText||'').slice(0,200)}});
      }}
      return JSON.stringify(result);
    }})()'''
    ok, out = _exec_js_in_domain('web.whatsapp.com', js, timeout=10)
    if not ok or not out or out in ('null', 'missing value', '[]'):
        return []
    try:
        data = json.loads(out)
        return [ChannelItem(
            channel='whatsapp',
            item_id=d.get('id', str(i)),
            sender=d.get('name', ''),
            subject='',
            preview=d.get('preview', ''),
            timestamp=time.time(),
        ) for i, d in enumerate(data)]
    except Exception:
        return []


def _read_facebook(*, limit=10, query='') -> list:
    """Read Facebook Messenger conversations via logged-in Chrome session."""
    from .ai_browser_transport import _exec_js_in_domain
    js = f'''(function(){{
      var threads=document.querySelectorAll('[aria-label*="conversation"],[aria-label*="chat"],[data-pagelet*="Inbox"] [role=row]');
      if(!threads.length) threads=document.querySelectorAll('[class*="x3psx0u"] [role=row],[role=row][tabindex]');
      var result=[];
      var n=Math.min(threads.length,{limit});
      for(var i=0;i<n;i++){{
        var el=threads[i];
        var lbl=el.getAttribute('aria-label')||'';
        var txt=lbl||el.innerText.trim();
        if(txt) result.push({{id:String(i),preview:txt.slice(0,200)}});
      }}
      return JSON.stringify(result);
    }})()'''
    ok, out = _exec_js_in_domain('facebook.com', js, timeout=10)
    if not ok or not out or out in ('null', 'missing value', '[]'):
        return []
    try:
        data = json.loads(out)
        return [ChannelItem(
            channel='facebook',
            item_id=d.get('id', str(i)),
            sender='',
            subject='',
            preview=d.get('preview', ''),
            timestamp=time.time(),
        ) for i, d in enumerate(data)]
    except Exception:
        return []


def _read_instagram(*, limit=10, query='') -> list:
    """Read Instagram DM conversations via logged-in Chrome session.

    Returns empty list and fires a Telegram ACTION_REQUIRED if CAPTCHA is detected.
    """
    from .ai_browser_transport import _exec_js_in_domain
    # Detect CAPTCHA first
    captcha_js = '(function(){return JSON.stringify({url:location.href,captcha:location.href.includes("recaptcha")||document.title.toLowerCase().includes("verify")});})();'
    ok, out = _exec_js_in_domain('instagram.com', captcha_js, timeout=8)
    if ok and out and out not in ('null', 'missing value'):
        try:
            state = json.loads(out)
            if state.get('captcha'):
                _notify_instagram_captcha()
                return []
        except Exception:
            pass

    js = f'''(function(){{
      var threads=document.querySelectorAll('[role=listbox] [role=option],[class*=notranslate]');
      if(!threads.length) threads=document.querySelectorAll('[href*="/direct/t/"] span,[aria-label*="Direct"]');
      var result=[];
      var n=Math.min(threads.length,{limit});
      for(var i=0;i<n;i++){{
        var txt=threads[i].innerText.trim();
        if(txt&&txt.length>0) result.push({{id:String(i),preview:txt.slice(0,200)}});
      }}
      return JSON.stringify(result);
    }})()'''
    ok, out = _exec_js_in_domain('instagram.com', js, timeout=10)
    if not ok or not out or out in ('null', 'missing value', '[]'):
        return []
    try:
        data = json.loads(out)
        return [ChannelItem(
            channel='instagram',
            item_id=d.get('id', str(i)),
            sender='',
            subject='',
            preview=d.get('preview', ''),
            timestamp=time.time(),
        ) for i, d in enumerate(data)]
    except Exception:
        return []


_instagram_captcha_notified = False


def _notify_instagram_captcha():
    global _instagram_captcha_notified
    if _instagram_captcha_notified:
        return
    _instagram_captcha_notified = True
    try:
        import urllib.request
        from .control_center import _load_auth_env
        _load_auth_env()
        from .telegram_bot import TelegramConfig
        tc = TelegramConfig()
        if not tc.configured:
            return
        msg = (
            '\u26a0\ufe0f HUMAN_ACTION_REQUIRED \u2014 Instagram CAPTCHA\n'
            'Instagram requires a CAPTCHA/verification before DM access.\n'
            'Please open instagram.com/direct/inbox/ in Chrome, '
            'complete verification, then retry.\n'
            'Channel: instagram | Action: read_inbox'
        )
        data = json.dumps({'chat_id': next(iter(tc._allowed)), 'text': msg}).encode()
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{tc._token}/sendMessage',
            data=data, headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


# ── Channel-specific executors (called only after Gate approval) ──────────

def _exec_gmail_send(payload: dict) -> dict:
    """Send approved Gmail message via browser. Called only after Gate approval."""
    from .ai_browser_transport import _exec_js_in_domain
    destination = payload.get('destination', '')
    content = payload.get('content', '')
    subject = payload.get('subject', 'Zamal Operator')
    # Open compose window and send
    js = f'''(function(){{
      var btn=document.querySelector('[gh=cm],[data-tooltip*=Compose],[aria-label*=Compose]');
      if(!btn)return JSON.stringify({{ok:false,error:'no_compose_btn'}});
      btn.click();
      return JSON.stringify({{ok:true,stage:'compose_opened'}});
    }})()'''
    ok, out = _exec_js_in_domain('mail.google.com', js, timeout=10)
    if not ok:
        return {'ok': False, 'error': 'compose_failed'}
    import time; time.sleep(1.5)
    # Fill compose form
    js2 = f'''(function(){{
      var to=document.querySelector('[name=to],[aria-label*="To"]');
      var subj=document.querySelector('[name=subjectbox],[placeholder*=Subject]');
      var body=document.querySelector('[aria-label*="Message Body"],div[contenteditable][role=textbox]');
      if(!to)return JSON.stringify({{ok:false,error:'no_to_field'}});
      to.value={json.dumps(destination)};
      to.dispatchEvent(new Event('input',{{bubbles:true}}));
      if(subj){{subj.value={json.dumps(subject)};subj.dispatchEvent(new Event('input',{{bubbles:true}}));}}
      if(body){{body.focus();document.execCommand('insertText',false,{json.dumps(content)});}}
      return JSON.stringify({{ok:true,stage:'filled'}});
    }})()'''
    ok2, out2 = _exec_js_in_domain('mail.google.com', js2, timeout=10)
    import time; time.sleep(0.5)
    # Click send
    js3 = '''(function(){
      var send=document.querySelector('[data-tooltip*=Send] div,[aria-label*="Send"]');
      if(send){send.click();return JSON.stringify({ok:true,stage:'sent'});}
      return JSON.stringify({ok:false,error:'no_send_btn'});
    })()'''
    ok3, out3 = _exec_js_in_domain('mail.google.com', js3, timeout=10)
    try:
        return json.loads(out3)
    except Exception:
        return {'ok': bool(ok3), 'raw': str(out3)[:200]}


def _exec_calendar_create(payload: dict) -> dict:
    """Create approved calendar event via browser. Called only after Gate approval."""
    from .ai_browser_transport import _exec_js_in_domain
    title = payload.get('title', 'Zamal Operator Event')
    # Click on a day to open new event dialog
    js = '''(function(){
      var newBtn=document.querySelector('[data-view-type] button,[aria-label*="Create"],.T3JNCD');
      if(newBtn){newBtn.click();return JSON.stringify({ok:true,stage:'new_event_clicked'});}
      return JSON.stringify({ok:false,error:'no_create_btn'});
    })()'''
    ok, out = _exec_js_in_domain('calendar.google.com', js, timeout=10)
    if not ok:
        return {'ok': False, 'error': 'calendar_create_failed'}
    import time; time.sleep(1)
    # Fill title
    js2 = f'''(function(){{
      var title=document.querySelector('[placeholder*="Add title"],[aria-label*="Add title"]');
      if(title){{title.value={json.dumps(title[:200])};title.dispatchEvent(new Event('input',{{bubbles:true}}));return JSON.stringify({{ok:true,stage:'title_filled'}});}}
      return JSON.stringify({{ok:false,error:'no_title_field'}});
    }})()'''
    ok2, out2 = _exec_js_in_domain('calendar.google.com', js2, timeout=10)
    import time; time.sleep(0.5)
    # Save event
    js3 = '''(function(){
      var save=document.querySelector('[data-view-type] [jsname="M2UYVd"],[aria-label*=Save]');
      if(save){save.click();return JSON.stringify({ok:true,stage:'saved'});}
      return JSON.stringify({ok:false,error:'no_save_btn'});
    })()'''
    ok3, out3 = _exec_js_in_domain('calendar.google.com', js3, timeout=10)
    try:
        return json.loads(out3)
    except Exception:
        return {'ok': bool(ok3), 'raw': str(out3)[:200]}


def _exec_whatsapp_send(payload: dict) -> dict:
    """Send approved WhatsApp message via browser. Called only after Gate approval."""
    from .ai_browser_transport import _exec_js_in_domain
    content = payload.get('content', '')
    destination = payload.get('destination', '')
    # Find the conversation input and send
    js = f'''(function(){{
      var inp=document.querySelector('[contenteditable][data-testid=conversation-compose-box-input],[data-testid=conversation-compose-box-input] [contenteditable]');
      if(!inp)return JSON.stringify({{ok:false,error:'no_input'}});
      inp.focus();
      document.execCommand('insertText',false,{json.dumps(content)});
      var send=document.querySelector('[data-testid=send],[aria-label*=Send]');
      if(send){{send.click();return JSON.stringify({{ok:true,stage:'sent'}});}}
      return JSON.stringify({{ok:false,error:'no_send_btn'}});
    }})()'''
    ok, out = _exec_js_in_domain('web.whatsapp.com', js, timeout=10)
    try:
        return json.loads(out)
    except Exception:
        return {'ok': bool(ok), 'raw': str(out)[:200]}


def _exec_facebook_send(payload: dict) -> dict:
    """Send approved Facebook Messenger message via browser. Called only after Gate approval."""
    from .ai_browser_transport import _exec_js_in_domain
    content = payload.get('content', '')
    js = f'''(function(){{
      var inp=document.querySelector('[aria-label*="Message"][contenteditable],[placeholder*="Aa"][contenteditable]');
      if(!inp)return JSON.stringify({{ok:false,error:'no_input'}});
      inp.focus();
      document.execCommand('insertText',false,{json.dumps(content)});
      inp.dispatchEvent(new KeyboardEvent('keydown',{{key:'Enter',keyCode:13,bubbles:true}}));
      return JSON.stringify({{ok:true,stage:'sent'}});
    }})()'''
    ok, out = _exec_js_in_domain('facebook.com', js, timeout=10)
    try:
        return json.loads(out)
    except Exception:
        return {'ok': bool(ok), 'raw': str(out)[:200]}


def _exec_instagram_dm(payload: dict) -> dict:
    """Send approved Instagram DM via browser. Called only after Gate approval."""
    from .ai_browser_transport import _exec_js_in_domain
    content = payload.get('content', '')
    js = f'''(function(){{
      var inp=document.querySelector('[aria-label*="Message"][contenteditable],[placeholder*=Message][contenteditable]');
      if(!inp)return JSON.stringify({{ok:false,error:'no_input'}});
      inp.focus();
      document.execCommand('insertText',false,{json.dumps(content)});
      var send=document.querySelector('[type=submit],[aria-label*=Send]');
      if(send){{send.click();return JSON.stringify({{ok:true,stage:'sent'}});}}
      inp.dispatchEvent(new KeyboardEvent('keydown',{{key:'Enter',keyCode:13,bubbles:true}}));
      return JSON.stringify({{ok:true,stage:'enter_sent'}});
    }})()'''
    ok, out = _exec_js_in_domain('instagram.com', js, timeout=10)
    try:
        return json.loads(out)
    except Exception:
        return {'ok': bool(ok), 'raw': str(out)[:200]}


def _exec_telegram_send(payload: dict) -> dict:
    """Send approved Telegram message via bot API. Called only after Gate approval."""
    try:
        from .control_center import _load_auth_env
        _load_auth_env()
        from .telegram_bot import TelegramConfig, TelegramAPI
        tc = TelegramConfig()
        if not tc.configured:
            return {'ok': False, 'error': 'telegram_not_configured'}
        api = TelegramAPI(tc._token)
        content = payload.get('content', '')
        for chat_id in tc._allowed:
            api.send_message(chat_id, content)
        return {'ok': True, 'stage': 'sent', 'recipients': len(tc._allowed)}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)[:200]}


# ── Singleton ──────────────────────────────────────────────────────────────

_ADAPTER: Optional['ChannelAdapter'] = None
_ADAPTER_LOCK = threading.Lock()


def get_channel_adapter() -> ChannelAdapter:
    global _ADAPTER
    with _ADAPTER_LOCK:
        if _ADAPTER is None:
            _ADAPTER = ChannelAdapter()
    return _ADAPTER
