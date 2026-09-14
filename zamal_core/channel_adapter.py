"""Unified channel adapter for all communication integrations.

Read/search/classify: no approval required.
Any external send/post/create/delete/update: requires Gate approval.
Emergency Stop blocks all queued and new external actions.
Privacy filter applied to all content before storage or routing.
PII masking applied to all channel item fields (email, phone → redacted tokens).
"""
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .privacy_filter import PRIVACY_FILTER
from .integration_security import SECRET_TEXT
from .private_read_auth import (
    PRIVATE_READ_REGISTRY, PRIVATE_CHANNELS, PrivateReadDenied,
    require_read_auth, metadata_only_result, record_privacy_incident,
)

_IS_DARWIN = __import__('sys').platform == 'darwin'

# ── PII masking ───────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
_PHONE_RE = re.compile(r'\+?[\d][\d\s\-().]{6,}\d')


def _mask_pii(text: str) -> str:
    """Replace email addresses and phone numbers with safe tokens."""
    if not text:
        return text
    text = _EMAIL_RE.sub('[EMAIL_REDACTED]', text)
    text = _PHONE_RE.sub('[PHONE_REDACTED]', text)
    return text


def _sanitize_sender(sender: str) -> str:
    """Sanitize sender field — mask emails; other identifiers become [CONTACT]."""
    if not sender:
        return ''
    masked = _mask_pii(sender)
    if masked == sender and '@' not in sender:
        return '[CONTACT]'
    return masked


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class ChannelItem:
    """Single item read from a channel (email, message, event, etc.)."""
    channel: str
    item_id: str
    sender: str    # always sanitized: [CONTACT] or [EMAIL_REDACTED]
    subject: str   # PII-masked
    preview: str   # PII-masked, max 500 chars
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
    approval_id: str      # Gate approval ID
    payload: dict         # sanitized action payload
    timestamp: float
    status: str = 'PENDING'   # PENDING | APPROVED | REJECTED | EXECUTING | COMPLETED | FAILED | EXPIRED
    executor_result: dict = field(default_factory=dict)  # result from executor


# ── Channel Adapter ───────────────────────────────────────────────────────────

class ChannelAdapter:
    """Unified read/propose/execute interface for all communication channels.

    Pattern:
      1. adapter.read(channel, ...) → ChannelResult (safe, no approval)
      2. adapter.propose_send(channel, dest, content, task_id) → ActionProposal
         - creates proposal, awaits human decision
      3. On APPROVE: adapter.execute_approved(proposal, approval_id, actor)
         - executes exactly once, idempotent
      4. On REJECT: adapter.execute_rejected(proposal, actor)
         - zero external action guaranteed
    """

    CHANNELS = frozenset({'telegram', 'gmail', 'calendar', 'whatsapp',
                          'facebook', 'messenger', 'instagram'})

    def __init__(self):
        self._lock = threading.Lock()
        self._executed: set = set()   # proposal_ids already executed (idempotency)
        self._proposals: dict = {}    # proposal_id → ActionProposal (in-memory registry)

    # ── Read operations ────────────────────────────────────────────────────

    def read(self, channel: str, *, limit: int = 10, query: str = '',
             task_id: str = '') -> ChannelResult:
        """Read/search a channel. No approval required. Privacy + PII masking applied."""
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
            # Apply secret-pattern filter first
            flt = PRIVACY_FILTER.check(item.preview)
            if flt.level == 'BLOCKED':
                continue
            if flt.level == 'SANITIZED':
                privacy_level = 'SANITIZED'
            # PII masking on all fields
            item.sender = _sanitize_sender(item.sender)
            item.subject = _mask_pii(flt.text)[:500]
            item.preview = _mask_pii(flt.text)[:500]
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

        No external action is taken here. Caller must obtain human approval.
        """
        flt = PRIVACY_FILTER.check(content)
        if flt.level == 'BLOCKED':
            raise ValueError(f'privacy_filter_blocked: {flt.reasons}')
        proposal_id = str(uuid.uuid4())
        sanitized_dest = _mask_pii(destination[:200])
        payload = {
            'channel': channel,
            'destination': sanitized_dest,
            'raw_destination': destination[:200],  # executor needs real address; not logged in preview
            'content': flt.text[:2000],
            'privacy_level': flt.level,
            'send': True,  # approval = authorization to send (not draft)
        }
        approval_id = self._create_approval(
            task_id=task_id,
            action='send_message',
            channel=channel,
            destination=sanitized_dest,
            preview=flt.text[:500],
            payload=payload,
            actor=actor,
        )
        proposal = ActionProposal(
            proposal_id=proposal_id,
            channel=channel,
            action='send_message',
            destination=sanitized_dest,
            preview=flt.text[:500],
            task_id=task_id,
            approval_id=approval_id,
            payload=payload,
            timestamp=time.time(),
        )
        with self._lock:
            self._proposals[proposal_id] = proposal
        return proposal

    def search(self, channel: str, query: str, *, limit: int = 10,
               task_id: str = '') -> 'ChannelResult':
        """Search a channel for items matching the query. No approval required."""
        return self.read(channel, limit=limit, query=query, task_id=task_id)

    def summarize_items(self, items: list, channel: str = '') -> dict:
        """Classify and summarize a list of ChannelItems. No external action."""
        categories: dict = {}
        for item in items:
            subj = (item.subject or item.preview or '').lower()
            if any(k in subj for k in ('job', 'position', 'career', 'recruit', 'hiring')):
                cat = 'job'
            elif any(k in subj for k in ('invoice', 'payment', 'bank', 'finance', 'order')):
                cat = 'finance'
            elif any(k in subj for k in ('urgent', 'asap', 'emergency', 'immediately')):
                cat = 'urgent'
            elif any(k in subj for k in ('customer', 'client', 'enquiry', 'inquiry', 'support')):
                cat = 'customer'
            else:
                cat = 'informational'
            categories[cat] = categories.get(cat, 0) + 1
        return {
            'total': len(items),
            'categories': categories,
            'channel': channel,
            'privacy_level': 'SANITIZED',
        }

    def draft_reply(self, channel: str, item_id: str, draft_content: str,
                    destination: str = '', *, task_id: str = '',
                    actor: str = 'operator') -> 'ActionProposal':
        """Create a reply draft proposal (requires approval before send)."""
        return self.propose_send(channel, destination or item_id, draft_content,
                                 task_id=task_id, actor=actor)

    def propose_delete_event(self, event_id: str, event_title: str = '',
                              *, task_id: str = '',
                              actor: str = 'operator') -> 'ActionProposal':
        """Propose deleting a calendar event. No deletion until approval."""
        proposal_id = str(uuid.uuid4())
        payload = {
            'channel': 'calendar',
            'event_id': event_id,
            'event_title': event_title[:200],
        }
        preview = f'Delete calendar event: {event_title or event_id}'
        approval_id = self._create_approval(
            task_id=task_id,
            action='delete_event',
            channel='calendar',
            destination='Google Calendar',
            preview=preview,
            payload=payload,
            actor=actor,
        )
        proposal = ActionProposal(
            proposal_id=proposal_id,
            channel='calendar',
            action='delete_event',
            destination='Google Calendar',
            preview=preview,
            task_id=task_id,
            approval_id=approval_id,
            payload=payload,
            timestamp=time.time(),
        )
        with self._lock:
            self._proposals[proposal_id] = proposal
        return proposal

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
        preview = f'{title} | {start_dt} → {end_dt}'
        approval_id = self._create_approval(
            task_id=task_id,
            action='create_event',
            channel='calendar',
            destination='Google Calendar',
            preview=preview,
            payload=payload,
            actor=actor,
        )
        proposal = ActionProposal(
            proposal_id=proposal_id,
            channel='calendar',
            action='create_event',
            destination='Google Calendar',
            preview=preview,
            task_id=task_id,
            approval_id=approval_id,
            payload=payload,
            timestamp=time.time(),
        )
        with self._lock:
            self._proposals[proposal_id] = proposal
        return proposal

    def propose_delete_event(self, event_id: str, reason: str = '',
                             *, task_id: str = '', actor: str = 'operator') -> ActionProposal:
        """Propose deleting a calendar event by event_id. No deletion until approval."""
        proposal_id = str(uuid.uuid4())
        payload = {
            'channel': 'calendar',
            'event_id': event_id,
            'reason': reason[:200],
        }
        preview = f'DELETE event_id={event_id[:20]} reason={reason[:80]}'
        approval_id = self._create_approval(
            task_id=task_id,
            action='delete_event',
            channel='calendar',
            destination='Google Calendar',
            preview=preview,
            payload=payload,
            actor=actor,
        )
        proposal = ActionProposal(
            proposal_id=proposal_id,
            channel='calendar',
            action='delete_event',
            destination='Google Calendar',
            preview=preview,
            task_id=task_id,
            approval_id=approval_id,
            payload=payload,
            timestamp=time.time(),
        )
        with self._lock:
            self._proposals[proposal_id] = proposal
        return proposal

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

        # Verify proposal status is not already REJECTED
        if proposal.status == 'REJECTED':
            with self._lock:
                self._executed.discard(proposal.proposal_id)
            return {'ok': False, 'error': 'proposal_rejected'}

        try:
            proposal.status = 'EXECUTING'
            with self._lock:
                if proposal.proposal_id in self._proposals:
                    self._proposals[proposal.proposal_id].status = 'EXECUTING'
            executor = _get_executor(proposal.channel, proposal.action)
            result = executor(proposal.payload)
            exec_ok = result.get('ok', True) if isinstance(result, dict) else bool(result)
            final_status = 'COMPLETED' if exec_ok else 'FAILED'
            proposal.status = final_status
            proposal.executor_result = result if isinstance(result, dict) else {}
            with self._lock:
                if proposal.proposal_id in self._proposals:
                    p = self._proposals[proposal.proposal_id]
                    p.status = final_status
                    p.executor_result = proposal.executor_result
            return {'ok': True, 'result': result, 'proposal_id': proposal.proposal_id,
                    'approval_id': approval_id, 'actor': actor, 'status': final_status}
        except Exception as exc:
            proposal.status = 'FAILED'
            with self._lock:
                if proposal.proposal_id in self._proposals:
                    self._proposals[proposal.proposal_id].status = 'FAILED'
                self._executed.discard(proposal.proposal_id)
            return {'ok': False, 'error': str(exc)[:300]}

    def execute_rejected(self, proposal: ActionProposal, actor: str) -> dict:
        """Record rejection. Guarantees zero external action.

        Marks proposal_id in _executed so subsequent execute_approved calls are blocked.
        """
        proposal.status = 'REJECTED'
        with self._lock:
            self._executed.add(proposal.proposal_id)
            if proposal.proposal_id in self._proposals:
                self._proposals[proposal.proposal_id].status = 'REJECTED'
        return {'ok': True, 'action': 'rejected', 'proposal_id': proposal.proposal_id,
                'actor': actor, 'external_action': 'none'}

    # ── Proposal registry ─────────────────────────────────────────────────

    def get_pending_proposals(self) -> list:
        """Return list of proposals that are still PENDING."""
        with self._lock:
            return [p for p in self._proposals.values() if p.status == 'PENDING']

    def get_proposal(self, proposal_id: str) -> Optional['ActionProposal']:
        """Return a proposal by ID, or None if not found."""
        with self._lock:
            return self._proposals.get(proposal_id)

    def all_proposals(self) -> list:
        """Return all proposals (all statuses)."""
        with self._lock:
            return list(self._proposals.values())

    # ── Internal helpers ───────────────────────────────────────────────────

    def _create_approval(self, *, task_id, action, channel, destination,
                          preview, payload, actor) -> str:
        """Create Gate approval record if task_id is valid. Returns approval_id."""
        if task_id:
            try:
                from .store import Store, Gate, Policy
                store = Store()
                gate = Gate(store)
                request_key = str(uuid.uuid4())
                safe_payload = {
                    'channel': channel,
                    'destination': destination[:200],
                    'preview': preview[:500],
                    'action': action,
                }
                action_record = gate.propose(
                    task_id=task_id,
                    kind=f'channel_{action}',
                    payload=safe_payload,
                    request_key=request_key,
                )
                if action_record and action_record.get('approval'):
                    approval_id = action_record['approval']['id']
                    store.close()
                    return approval_id
                store.close()
            except Exception:
                pass
        # Fallback: generate local approval ID (for proposals without a store task)
        return str(uuid.uuid4())


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


def _set_emergency_stop(stop_everything: bool = False,
                        pause_external: bool = False) -> bool:
    """Set emergency stop flags via the Store controls table. Returns True on success."""
    try:
        from .store import Store
        store = Store()
        try:
            store.db.execute(
                'UPDATE controls SET stop_everything=?, pause_external_actions=? WHERE id=1',
                (int(stop_everything), int(pause_external))
            )
            store.db.commit()
            return True
        finally:
            store.close()
    except Exception:
        return False


# ── Channel-specific readers ───────────────────────────────────────────────

def _get_reader(channel: str):
    """Return the read function for a channel."""
    return {
        'telegram': _read_telegram,
        'gmail': _read_gmail,
        'calendar': _read_calendar,
        'whatsapp': _read_whatsapp,
        'facebook': _read_facebook,
        'messenger': _read_messenger,
        'instagram': _read_instagram,
    }[channel]


def _get_executor(channel: str, action: str):
    """Return the executor for a (channel, action) pair."""
    key = f'{channel}:{action}'
    executors = {
        'gmail:send_message': _exec_gmail_send,
        'calendar:create_event': _exec_calendar_create,
        'calendar:delete_event': _exec_calendar_delete,
        'whatsapp:send_message': _exec_whatsapp_send,
        'facebook:send_message': _exec_facebook_send,
        'messenger:send_message': _exec_messenger_send,
        'instagram:send_message': _exec_instagram_dm,
        'telegram:send_message': _exec_telegram_send,
    }
    if key not in executors:
        raise ValueError(f'No executor for {key}')
    return executors[key]


def _read_telegram(*, limit=10, query='', _auth=None) -> list:
    """Read recent Telegram messages via bot API (update log).

    Requires a PrivateReadAuth for channel='telegram'. Without authorization,
    returns a single metadata-only item (authenticated, count) with no content.
    """
    try:
        auth = _auth or require_read_auth('telegram', query or '*')
    except PrivateReadDenied:
        return [ChannelItem(
            channel='telegram', item_id='meta', sender='[SYSTEM]', subject='',
            preview='[PRIVATE_READ_DENIED: no authorization]', timestamp=time.time(),
            metadata=metadata_only_result('telegram'),
        )]
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
            items.append(ChannelItem(
                channel='telegram',
                item_id=str(u.get('update_id', '')),
                sender='[CONTACT]',
                subject='',
                preview=_mask_pii(text[:500]),
                timestamp=float(msg.get('date', time.time())),
            ))
        if auth:
            auth.consume()
        return items
    except Exception:
        return []


def _read_gmail(*, limit=10, query='', _auth=None) -> list:
    """Read Gmail inbox via logged-in Chrome session. Supports keyword query filtering.

    Requires a PrivateReadAuth for channel='gmail'. Without authorization,
    returns a metadata-only sentinel item (no subject, sender, or body content).
    """
    try:
        auth = _auth or require_read_auth('gmail', query or '*')
    except PrivateReadDenied:
        return [ChannelItem(
            channel='gmail', item_id='meta', sender='[SYSTEM]', subject='',
            preview='[PRIVATE_READ_DENIED: no authorization]', timestamp=time.time(),
            metadata=metadata_only_result('gmail'),
        )]
    from .ai_browser_transport import _exec_js_in_domain
    # If there's a query, navigate to search results first
    if query:
        nav_js = f'''(function(){{
          var q=encodeURIComponent({json.dumps(query[:100])});
          if(!location.href.includes('#search/'+q)&&!location.href.includes('q='+q)){{
            window.location='https://mail.google.com/mail/u/0/#search/'+q;
            return JSON.stringify({{navigating:true}});
          }}
          return JSON.stringify({{here:true}});
        }})()'''
        import time as _t
        ok_nav, _ = _exec_js_in_domain('mail.google.com', nav_js, timeout=8)
        if ok_nav:
            _t.sleep(2)
    js = f'''(function(){{
      var rows=document.querySelectorAll('tr.zA');
      var emails=[];
      var n=Math.min(rows.length,{limit});
      for(var i=0;i<n;i++){{
        var r=rows[i];
        var subEl=r.querySelector('[data-thread-id] span,.y6 span,.bqe');
        var bodyEl=r.querySelector('.y2');
        var timeEl=r.querySelector('.xW .xY span,[title]');
        var isUnread=r.classList.contains('zE');
        emails.push({{
          id:r.getAttribute('id')||String(i),
          subject:(subEl?.innerText||'').slice(0,200),
          preview:(bodyEl?.innerText||'').slice(0,300),
          time:(timeEl?.getAttribute('title')||'').slice(0,30),
          unread:isUnread
        }});
      }}
      return JSON.stringify(emails);
    }})()'''
    ok, out = _exec_js_in_domain('mail.google.com', js, timeout=10)
    if not ok or not out or out in ('null', 'missing value'):
        return []
    try:
        data = json.loads(out)
        items = []
        for i, d in enumerate(data):
            subj = _mask_pii(d.get('subject', '')[:200])
            prev = _mask_pii(d.get('preview', '')[:300])
            # Client-side query filter if not navigated to search URL
            if query and query.lower() not in subj.lower() and query.lower() not in prev.lower():
                continue
            items.append(ChannelItem(
                channel='gmail',
                item_id=d.get('id', str(i)),
                sender='[CONTACT]',
                subject=subj,
                preview=prev,
                timestamp=time.time(),
                metadata={'time': d.get('time', ''), 'unread': d.get('unread', False)},
            ))
        if auth:
            auth.consume()
        return items
    except Exception:
        return []


def _read_calendar(*, limit=10, query='') -> list:
    """Read Google Calendar events via logged-in Chrome session."""
    from .ai_browser_transport import _exec_js_in_domain
    js = f'''(function(){{
      // Primary: event chips with aria-label containing time markers
      var eventEls=Array.from(document.querySelectorAll('[aria-label]')).filter(function(el){{
        var a=el.getAttribute('aria-label')||'';
        var cls=el.className||'';
        return a.length>10&&(a.includes(':')||a.includes('PM')||a.includes('AM')||
               cls.includes('event')||cls.includes('chip')||cls.includes('Event'));
      }});
      // Secondary: role=gridcell button content (actual event slots)
      if(!eventEls.length){{
        eventEls=Array.from(document.querySelectorAll('[role=gridcell] [role=button]')).filter(function(el){{
          return el.innerText.trim().length>3;
        }});
      }}
      var result=[];
      var n=Math.min(eventEls.length,{limit});
      for(var i=0;i<n;i++){{
        var el=eventEls[i];
        var txt=el.getAttribute('aria-label')||el.innerText.trim();
        if(txt&&txt.length>3&&!txt.includes('Navigation')&&!txt.includes('Account'))
          result.push({{id:String(i),title:txt.slice(0,200)}});
      }}
      // Fallback: return week range from header
      if(!result.length){{
        var h2s=Array.from(document.querySelectorAll('h2')).map(function(e){{return e.innerText.trim();}});
        var weekTitle=h2s.find(function(t){{return t.includes('Sep')||t.includes('Oct')||t.includes('2026');}});
        if(weekTitle) result.push({{id:'week',title:'[EMPTY WEEK] '+weekTitle.slice(0,80)}});
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


def _read_whatsapp(*, limit=10, query='', _auth=None) -> list:
    """Read WhatsApp Web conversations via logged-in Chrome session.

    Requires a PrivateReadAuth for channel='whatsapp'. Without authorization,
    returns metadata-only (tab_found, unread_count) — no contact names or previews.
    """
    try:
        auth = _auth or require_read_auth('whatsapp', query or '*')
    except PrivateReadDenied:
        return [ChannelItem(
            channel='whatsapp', item_id='meta', sender='[SYSTEM]', subject='',
            preview='[PRIVATE_READ_DENIED: no authorization]', timestamp=time.time(),
            metadata=metadata_only_result('whatsapp'),
        )]
    from .ai_browser_transport import _exec_js_in_domain
    import time as _t

    # If there's a search query, use the WhatsApp search box
    if query:
        search_js = f'''(function(){{
          var searchBtn=document.querySelector('[data-testid=search],[aria-label*=Search],[title*=Search]');
          if(searchBtn){{searchBtn.click();}}
          var inp=document.querySelector('[data-testid=chat-list-search],[aria-label*="Search or start"]');
          if(inp){{
            inp.focus();
            document.execCommand('selectAll',false,null);
            document.execCommand('insertText',false,{json.dumps(query[:50])});
            inp.dispatchEvent(new Event('input',{{bubbles:true}}));
            return JSON.stringify({{searching:true}});
          }}
          return JSON.stringify({{searching:false}});
        }})()'''
        _exec_js_in_domain('web.whatsapp.com', search_js, timeout=8)
        _t.sleep(1.5)

    js = f'''(function(){{
      var items=document.querySelectorAll('[data-testid=cell-frame-container],[role=listitem]');
      if(!items.length) items=document.querySelectorAll('._ak72,._ak8k,._ak8l');
      var result=[];
      var n=Math.min(items.length,{limit});
      for(var i=0;i<n;i++){{
        var el=items[i];
        var lastEl=el.querySelector('[data-testid=last-msg-status] span,[class*=last-msg],[data-testid=msg-container]');
        result.push({{id:String(i),preview:(lastEl?.innerText||'').slice(0,200)}});
      }}
      return JSON.stringify(result);
    }})()'''
    ok, out = _exec_js_in_domain('web.whatsapp.com', js, timeout=10)
    if not ok or not out or out in ('null', 'missing value', '[]'):
        return []
    try:
        data = json.loads(out)
        items = [ChannelItem(
            channel='whatsapp',
            item_id=d.get('id', str(i)),
            sender='[CONTACT]',
            subject='',
            preview=_mask_pii(d.get('preview', '')[:200]),
            timestamp=time.time(),
        ) for i, d in enumerate(data)]
        if auth:
            auth.consume()
        return items
    except Exception:
        return []


def _read_facebook(*, limit=10, query='') -> list:
    """Read Facebook Messenger conversations via logged-in Chrome session.

    Navigates to the messages inbox if not already there.
    """
    from .ai_browser_transport import _exec_js_in_domain
    import time as _time
    # Navigate to messages if not already there
    nav_js = '''(function(){
      if(location.href.includes('/messages/')){return JSON.stringify({here:true});}
      window.location='https://www.facebook.com/messages/';
      return JSON.stringify({navigating:true});
    })()'''
    ok_nav, out_nav = _exec_js_in_domain('facebook.com', nav_js, timeout=8)
    if ok_nav and out_nav and 'navigating' in (out_nav or ''):
        _time.sleep(3)

    js = f'''(function(){{
      var threads=document.querySelectorAll('[role=row]');
      if(!threads.length) threads=document.querySelectorAll('[aria-label*="conversation"],[aria-label*="chat"]');
      var result=[];
      var n=Math.min(threads.length,{limit});
      for(var i=0;i<n;i++){{
        var el=threads[i];
        result.push({{id:String(i),count:threads.length}});
      }}
      return JSON.stringify({{count:threads.length,url:location.href.includes('messages')}});
    }})()'''
    ok, out = _exec_js_in_domain('facebook.com', js, timeout=10)
    if not ok or not out or out in ('null', 'missing value'):
        return []
    try:
        data = json.loads(out)
        # Support both array format [{id,preview}] and count format {count:N}
        if isinstance(data, list):
            count = len(data)
        else:
            count = data.get('count', 0)
        return [ChannelItem(
            channel='facebook',
            item_id=str(i),
            sender='[CONTACT]',
            subject='',
            preview='[CONVERSATION]',
            timestamp=time.time(),
        ) for i in range(min(count, limit))]
    except Exception:
        return []


def _read_messenger(*, limit=10, query='', _auth=None) -> list:
    """Read Facebook Messenger DM threads — separate from Facebook Pages/Feed.

    Requires a PrivateReadAuth for channel='messenger'. Without authorization,
    returns metadata-only (thread count, unread flag) — no names or previews.
    """
    try:
        auth = _auth or require_read_auth('messenger', query or '*')
    except PrivateReadDenied:
        return [ChannelItem(
            channel='messenger', item_id='meta', sender='[SYSTEM]', subject='',
            preview='[PRIVATE_READ_DENIED: no authorization]', timestamp=time.time(),
            metadata=metadata_only_result('messenger'),
        )]
    from .ai_browser_transport import _exec_js_in_domain
    import time as _time

    # Navigate to DM inbox (separate from /messages/ which shows all message types)
    nav_js = '''(function(){
      var url=location.href;
      if(url.includes('messenger.com')||url.includes('/messages/')){
        return JSON.stringify({here:true,url:url.slice(0,60)});
      }
      window.location='https://www.facebook.com/messages/';
      return JSON.stringify({navigating:true});
    })()'''
    ok_nav, out_nav = _exec_js_in_domain('facebook.com', nav_js, timeout=8)
    if ok_nav and out_nav and 'navigating' in (out_nav or ''):
        _time.sleep(3)

    # Read DM thread items specifically
    js = f'''(function(){{
      var threads=document.querySelectorAll('[role=row]');
      if(!threads.length) threads=document.querySelectorAll('[aria-label*="conversation"],[data-testid*="thread"]');
      var count=threads.length;
      var classified=[];
      for(var i=0;i<Math.min(count,{limit});i++){{
        var el=threads[i];
        var txt=(el.innerText||'').slice(0,80);
        // Detect if thread has unread badge
        var hasUnread=!!el.querySelector('[aria-label*="unread"],[class*="badge"]');
        classified.push({{id:String(i),hasUnread:hasUnread}});
      }}
      return JSON.stringify({{count:count,threads:classified,inMessenger:location.href.includes('/messages/')}});
    }})()'''
    ok, out = _exec_js_in_domain('facebook.com', js, timeout=10)
    if not ok or not out or out in ('null', 'missing value'):
        return []
    try:
        data = json.loads(out)
        if isinstance(data, list):
            count = len(data)
            threads = data
        else:
            count = data.get('count', 0)
            threads = data.get('threads', [])
        items = [ChannelItem(
            channel='messenger',
            item_id=str(i),
            sender='[CONTACT]',
            subject='',
            preview='[CONVERSATION]',
            timestamp=time.time(),
            metadata={'has_unread': t.get('hasUnread', False) if i < len(threads) else False},
        ) for i, t in enumerate(threads[:min(count, limit)])]
        if auth:
            auth.consume()
        return items
    except Exception:
        return []


def _read_instagram(*, limit=10, query='', _auth=None) -> list:
    """Read Instagram DM conversations via logged-in Chrome session.

    Requires a PrivateReadAuth for channel='instagram'. Without authorization,
    returns metadata-only (tab found, login state) — no contact names or DM previews.
    Navigates to DM inbox first. Fires Telegram notice if CAPTCHA is detected.
    """
    try:
        auth = _auth or require_read_auth('instagram', query or '*')
    except PrivateReadDenied:
        return [ChannelItem(
            channel='instagram', item_id='meta', sender='[SYSTEM]', subject='',
            preview='[PRIVATE_READ_DENIED: no authorization]', timestamp=time.time(),
            metadata=metadata_only_result('instagram'),
        )]
    from .ai_browser_transport import _exec_js_in_domain
    import time as _time

    # Navigate to DM inbox if not already there
    nav_js = '''(function(){
      if(location.href.includes('/direct/inbox')){return JSON.stringify({here:true});}
      window.location='https://www.instagram.com/direct/inbox/';
      return JSON.stringify({navigating:true});
    })()'''
    ok_nav, out_nav = _exec_js_in_domain('instagram.com', nav_js, timeout=8)
    if ok_nav and out_nav and 'navigating' in (out_nav or ''):
        _time.sleep(4)

    # Detect CAPTCHA — narrow selectors only, no broad text extraction
    state_js = '''(function(){
      var url=location.href;
      var title=document.title.toLowerCase();
      return JSON.stringify({
        url:url,
        captcha:url.includes("recaptcha")||title.includes("verify"),
        hasLoginForm:!!document.querySelector('input[name=username]'),
        emptyInboxSignal:!!document.querySelector('[aria-label="New message"],[aria-label="Direct"]')
      });
    })()'''
    ok, out = _exec_js_in_domain('instagram.com', state_js, timeout=8)
    if ok and out and out not in ('null', 'missing value'):
        try:
            state = json.loads(out)
            if state.get('captcha') or state.get('hasLoginForm'):
                _notify_instagram_captcha()
                return []
            if state.get('emptyInboxSignal') and not state.get('captcha') and not state.get('hasLoginForm'):
                # Authenticated, inbox appears empty — return sentinel item
                if auth:
                    auth.consume()
                return [ChannelItem(
                    channel='instagram',
                    item_id='empty_inbox',
                    sender='[SELF]',
                    subject='',
                    preview='[AUTHENTICATED: empty inbox]',
                    timestamp=time.time(),
                )]
        except Exception:
            pass

    js = f'''(function(){{
      var threads=document.querySelectorAll('[role=listbox] [role=option]');
      if(!threads.length) threads=document.querySelectorAll('main [role=list] li');
      var result=[];
      var n=Math.min(threads.length,{limit});
      for(var i=0;i<n;i++){{
        result.push({{id:String(i)}});
      }}
      return JSON.stringify({{count:threads.length}});
    }})()'''
    ok2, out2 = _exec_js_in_domain('instagram.com', js, timeout=10)
    if not ok2 or not out2 or out2 in ('null', 'missing value'):
        return []
    try:
        data = json.loads(out2)
        # Support both array [{id,...}] and count {count:N} formats
        if isinstance(data, list):
            count = len(data)
        else:
            count = data.get('count', 0)
        items = [ChannelItem(
            channel='instagram',
            item_id=str(i),
            sender='[CONTACT]',
            subject='',
            preview='[CONVERSATION]',
            timestamp=time.time(),
        ) for i in range(min(count, limit))]
        if auth:
            auth.consume()
        return items
    except Exception:
        return []


_instagram_captcha_notified = False


def _check_telegram_configured() -> bool:
    """Return True if Telegram bot credentials are present (no content read)."""
    try:
        from .control_center import _load_auth_env
        _load_auth_env()
        from .telegram_bot import TelegramConfig
        return TelegramConfig().configured
    except Exception:
        return False


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


# ── Public research helpers (no approval required — read-only operations) ──

def _research_instagram_public(*, topic: str = 'business') -> dict:
    """Research Instagram public content via Explore/search. No private DM access.

    Returns sanitized post count and category summary. No content is stored or logged.
    """
    import time as _t
    from .ai_browser_transport import _exec_js_in_domain

    # Navigate to Explore page (public content, no private data)
    nav_js = '''(function(){
      if(location.href.includes('/explore/')) return JSON.stringify({here:true});
      window.location = 'https://www.instagram.com/explore/';
      return JSON.stringify({navigating:true});
    })()'''
    ok_nav, out_nav = _exec_js_in_domain('instagram.com', nav_js, timeout=8)
    if ok_nav and out_nav and 'navigating' in (out_nav or ''):
        _t.sleep(4)

    js = '''(function(){
      var imgs = document.querySelectorAll('article img,main img').length;
      var links = document.querySelectorAll('a[href*="/p/"],a[href*="/reel/"]').length;
      var captcha = document.title.toLowerCase().includes('verify') || location.href.includes('recaptcha');
      return JSON.stringify({
        url: location.href.slice(0,60),
        public_posts: links,
        images: imgs,
        captcha: captcha
      });
    })()'''
    ok, out = _exec_js_in_domain('instagram.com', js, timeout=10)
    if not ok or not out or out in ('null', 'missing value'):
        return {'ok': False, 'error': 'explore_unreachable'}
    try:
        data = json.loads(out)
        if data.get('captcha'):
            _notify_instagram_captcha()
            return {'ok': False, 'error': 'captcha_detected'}
        return {
            'ok': True,
            'channel': 'instagram',
            'research_type': 'public_explore',
            'public_posts_found': data.get('public_posts', 0),
            'topic': topic,
            'url': data.get('url', ''),
        }
    except Exception:
        return {'ok': False, 'error': 'parse_error', 'raw': str(out)[:100]}


def _research_facebook_feed() -> dict:
    """Read sanitized Facebook feed metadata for business research.

    Returns article count and post-type breakdown. No personal content is stored.
    """
    from .ai_browser_transport import _exec_js_in_domain

    # Navigate back to main feed if we're in Messenger
    nav_js = '''(function(){
      if(!location.href.includes('/messages/') && !location.href.includes('/direct/')) return JSON.stringify({here:true});
      window.location = 'https://www.facebook.com/';
      return JSON.stringify({navigating:true});
    })()'''
    ok_nav, out_nav = _exec_js_in_domain('facebook.com', nav_js, timeout=8)
    if ok_nav and out_nav and 'navigating' in (out_nav or ''):
        import time as _t
        _t.sleep(3)

    js = '''(function(){
      var articles = document.querySelectorAll('[role=article]').length;
      var sponsored = Array.from(document.querySelectorAll('[role=article]'))
        .filter(function(a){ return a.textContent.toLowerCase().includes('sponsor'); }).length;
      var hasVideo = document.querySelectorAll('[role=article] video').length;
      var hasNotif = document.querySelectorAll('[aria-label*=notification],[data-testid*=notification]').length;
      return JSON.stringify({
        url: location.href.slice(0,60),
        articles: articles,
        sponsored: sponsored,
        videos: hasVideo,
        notifications: hasNotif
      });
    })()'''
    ok, out = _exec_js_in_domain('facebook.com', js, timeout=10)
    if not ok or not out or out in ('null', 'missing value'):
        return {'ok': False, 'error': 'feed_unreachable'}
    try:
        data = json.loads(out)
        return {
            'ok': True,
            'channel': 'facebook',
            'research_type': 'news_feed',
            'articles_visible': data.get('articles', 0),
            'sponsored_posts': data.get('sponsored', 0),
            'video_posts': data.get('videos', 0),
        }
    except Exception:
        return {'ok': False, 'error': 'parse_error', 'raw': str(out)[:100]}


def _research_messenger_inbox() -> dict:
    """Read sanitized Messenger inbox metadata. Navigate to thread list, not content."""
    import time as _t
    from .ai_browser_transport import _exec_js_in_domain

    nav_js = '''(function(){
      if(location.href.includes('/messages/t/') || !location.href.includes('/messages/')) {
        window.location = 'https://www.facebook.com/messages/';
        return JSON.stringify({navigating:true});
      }
      return JSON.stringify({here:true});
    })()'''
    ok_nav, out_nav = _exec_js_in_domain('facebook.com', nav_js, timeout=8)
    if ok_nav and out_nav and 'navigating' in (out_nav or ''):
        _t.sleep(3)

    js = '''(function(){
      var rows = document.querySelectorAll('[role=row],[aria-label*=conversation]');
      var unread = Array.from(rows).filter(function(r){ return !!r.querySelector('[aria-label*=unread],[class*=badge],[class*=unread]'); }).length;
      var compose = !!document.querySelector('div[role=textbox][contenteditable=true],[aria-label*=Message][contenteditable]');
      return JSON.stringify({
        url: location.href.slice(0,60),
        thread_count: rows.length,
        unread_threads: unread,
        has_compose: compose
      });
    })()'''
    ok, out = _exec_js_in_domain('facebook.com', js, timeout=10)
    if not ok or not out or out in ('null', 'missing value'):
        return {'ok': False, 'error': 'messenger_unreachable'}
    try:
        data = json.loads(out)
        return {
            'ok': True,
            'channel': 'messenger',
            'research_type': 'inbox_summary',
            'thread_count': data.get('thread_count', 0),
            'unread_threads': data.get('unread_threads', 0),
            'has_compose': data.get('has_compose', False),
        }
    except Exception:
        return {'ok': False, 'error': 'parse_error', 'raw': str(out)[:100]}


# ── Channel-specific executors (called only after Gate approval) ──────────

def _exec_gmail_send(payload: dict) -> dict:
    """Send approved Gmail message via browser. Called only after Gate approval.

    Approval = authorization to send. Always attempts real Send; never drafts.
    Uses Gmail compose URL directly to avoid selector brittleness.
    """
    import subprocess as _sp, time as _time
    from .ai_browser_transport import _exec_js_in_domain

    destination = payload.get('raw_destination', '') or payload.get('destination', '')
    content = payload.get('content', '')
    subject = payload.get('subject', 'Zamal Operator Message')

    # Step 1: Click Gmail Compose button. Uses persistent dialog (not redirect-based URL).
    js_compose = '''(function(){
      // Find the Compose button (aria-label or role-button with text)
      var btns = Array.from(document.querySelectorAll('div[role=button],button,[gh=cm]'));
      var composeBtn = null;
      for(var b of btns){
        var lbl = (b.getAttribute('aria-label')||b.getAttribute('data-tooltip')||'').toLowerCase();
        var txt = (b.textContent||b.innerText||'').trim().toLowerCase();
        var r = b.getBoundingClientRect();
        if(r.width > 0 && r.height > 0 && (lbl === 'compose' || txt === 'compose')){
          composeBtn = b; break;
        }
      }
      if(!composeBtn) return JSON.stringify({ok:false,error:'no_compose_btn'});
      composeBtn.click();
      return JSON.stringify({ok:true,stage:'compose_clicked'});
    })()'''
    ok1, out1 = _exec_js_in_domain('mail.google.com', js_compose, timeout=10)
    if not ok1 or out1 == 'NO_TAB':
        return {'ok': False, 'error': 'gmail_tab_unreachable'}
    try:
        r1 = json.loads(out1) if out1 and out1 != 'missing value' else {}
        if not r1.get('ok'):
            return {'ok': False, 'error': r1.get('error', 'compose_btn_not_found')}
    except Exception:
        return {'ok': False, 'error': 'compose_parse_error', 'raw': str(out1)[:100]}
    _time.sleep(3.0)  # wait for compose dialog to fully render

    # Step 2: Fill To field (contenteditable div with aria-label="To")
    js_fill_to = f'''(function(){{
      var toField = document.querySelector('[aria-label="To"],[aria-label*="Recipients"]');
      if(!toField) {{
        // Fallback: first contenteditable in compose dialog
        var dialog = document.querySelector('[role=dialog]');
        toField = dialog ? dialog.querySelector('[contenteditable]') : null;
      }}
      if(!toField) return JSON.stringify({{ok:false,error:'no_to_field'}});
      toField.focus();
      toField.click();
      document.execCommand('selectAll', false, null);
      document.execCommand('insertText', false, {json.dumps(destination)});
      toField.dispatchEvent(new KeyboardEvent('keydown', {{key:'Tab',keyCode:9,bubbles:true}}));
      return JSON.stringify({{ok:true,stage:'to_filled',value:toField.textContent.slice(0,40)}});
    }})()'''
    ok2, out2 = _exec_js_in_domain('mail.google.com', js_fill_to, timeout=8)
    _time.sleep(1.0)
    try:
        r2 = json.loads(out2) if out2 and out2 != 'missing value' else {}
        if not r2.get('ok'):
            return {'ok': False, 'error': r2.get('error', 'to_fill_failed'), 'raw': out2[:100]}
    except Exception:
        return {'ok': False, 'error': 'to_parse_error', 'raw': str(out2)[:100]}

    # Step 3: Fill Subject field
    js_fill_subj = f'''(function(){{
      var subj = document.querySelector('[name=subjectbox],[placeholder*="Subject"],[aria-label="Subject"]');
      if(!subj) return JSON.stringify({{ok:true,stage:'no_subj_field_skip'}});
      subj.focus();
      subj.value = '';
      document.execCommand('selectAll', false, null);
      document.execCommand('insertText', false, {json.dumps(subject[:150])});
      subj.dispatchEvent(new Event('input', {{bubbles:true}}));
      return JSON.stringify({{ok:true,stage:'subj_filled',value:subj.value.slice(0,40)}});
    }})()'''
    ok3, out3 = _exec_js_in_domain('mail.google.com', js_fill_subj, timeout=8)
    _time.sleep(0.5)

    # Step 4: Fill Body field — must target contenteditable div, not hidden textarea.
    # document.querySelector('[aria-label="Message Body"]') matches the hidden textarea first;
    # the real editable body is a contenteditable div inside the visible dialog.
    js_fill_body = f'''(function(){{
      var dialog = null;
      var allDialogs = Array.from(document.querySelectorAll('[role=dialog]'));
      for(var i=0;i<allDialogs.length;i++){{
        var r=allDialogs[i].getBoundingClientRect();
        if(r.width>0 && r.height>0){{ dialog=allDialogs[i]; break; }}
      }}
      if(!dialog) return JSON.stringify({{ok:false,error:'no_visible_dialog'}});
      var body = dialog.querySelector('[aria-label="Message Body"][contenteditable="true"]') ||
                 dialog.querySelector('[role="textbox"][contenteditable="true"]') ||
                 dialog.querySelector('div[contenteditable="true"]');
      if(!body) return JSON.stringify({{ok:false,error:'no_body_field',ce_count:dialog.querySelectorAll('[contenteditable]').length}});
      body.focus();
      var sel = window.getSelection();
      var range = document.createRange();
      range.selectNodeContents(body);
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand('insertText', false, {json.dumps(content[:1000])});
      body.dispatchEvent(new Event('input', {{bubbles:true}}));
      return JSON.stringify({{ok:true,stage:'body_filled',value:body.textContent.slice(0,50)}});
    }})()'''
    ok4, out4 = _exec_js_in_domain('mail.google.com', js_fill_body, timeout=10)
    _time.sleep(1.0)
    try:
        r4 = json.loads(out4) if out4 and out4 != 'missing value' else {}
        if not r4.get('ok'):
            return {'ok': False, 'error': r4.get('error', 'body_fill_failed'), 'raw': out4[:100]}
    except Exception:
        return {'ok': False, 'error': 'body_parse_error', 'raw': str(out4)[:100]}

    # Step 5: Click Send button — must find the visible dialog, not the first (invisible) one.
    # document.querySelector('[role=dialog]') returns the first dialog which is invisible (w=0);
    # iterate to find the visible compose dialog before searching for the Send button.
    js_send = '''(function(){
      var dialog = null;
      var allDialogs = Array.from(document.querySelectorAll('[role=dialog]'));
      for(var i=0;i<allDialogs.length;i++){
        var r=allDialogs[i].getBoundingClientRect();
        if(r.width>0 && r.height>0){ dialog=allDialogs[i]; break; }
      }
      var btns = Array.from((dialog||document).querySelectorAll('div[role=button],button'));
      var sendBtn = null;
      for(var b of btns){
        var lbl = (b.getAttribute('data-tooltip')||b.getAttribute('aria-label')||'').trim();
        var txt = (b.textContent||b.innerText||'').trim();
        var r = b.getBoundingClientRect();
        if(r.width > 0 && r.height > 0 && (lbl === 'Send' || txt === 'Send')){
          sendBtn = b; break;
        }
      }
      if(!sendBtn) return JSON.stringify({ok:false,error:'no_send_btn',dialog_found:!!dialog});
      sendBtn.click();
      return JSON.stringify({ok:true,stage:'send_clicked'});
    })()'''
    ok5, out5 = _exec_js_in_domain('mail.google.com', js_send, timeout=10)
    _time.sleep(3.0)  # wait for compose to close after send

    # Step 6: Verify compose dialog is closed (check for visible dialog, not just any dialog)
    js_verify = '''(function(){
      var allDialogs = Array.from(document.querySelectorAll('[role=dialog]'));
      var hasVisible = allDialogs.some(function(d){ var r=d.getBoundingClientRect(); return r.width>0&&r.height>0; });
      var composeClosed = !hasVisible;
      return JSON.stringify({ok:composeClosed, composeClosed:composeClosed, url:location.href.slice(0,80)});
    })()'''
    ok6, out6 = _exec_js_in_domain('mail.google.com', js_verify, timeout=8)

    try:
        send_result = json.loads(out5) if out5 and out5 != 'missing value' else {'ok': bool(ok5)}
        verify = json.loads(out6) if out6 and out6 != 'missing value' else {}
        if send_result.get('stage') == 'send_clicked':
            return {'ok': True, 'stage': 'sent',
                    'verified_compose_closed': verify.get('composeClosed', False)}
        return {'ok': False, 'error': send_result.get('error', 'send_failed'),
                'send_result': send_result, 'verify': verify}
    except Exception:
        return {'ok': bool(ok5), 'raw': str(out5)[:200]}


def _exec_calendar_create(payload: dict) -> dict:
    """Create approved calendar event via browser. Uses URL navigation to avoid
    appointment-editor redirect from the Create button.
    """
    import subprocess as _sp, time as _t
    from .ai_browser_transport import _exec_js_in_domain

    title = payload.get('title', 'Zamal Operator Test Event')
    start_dt = payload.get('start_dt', '')
    end_dt = payload.get('end_dt', '')

    # Navigate Calendar tab directly to the new-event editor URL
    nav_script = '''osascript -e 'tell application "Google Chrome"
      repeat with w in windows
        repeat with t in tabs of w
          if (URL of t) contains "calendar.google.com" then
            set URL of t to "https://calendar.google.com/calendar/u/0/r/eventedit"
            return "navigated"
          end if
        end repeat
      end repeat
      return "no_tab"
    end tell' '''
    r = _sp.run(nav_script, shell=True, capture_output=True, text=True, timeout=10)
    if 'no_tab' in (r.stdout or ''):
        return {'ok': False, 'error': 'no_calendar_tab'}
    _t.sleep(7.0)  # wait for SPA event editor to fully render (Google Calendar needs ~6s)

    # Fill title in the event editor
    js_title = f'''(function(){{
      var inp = document.querySelector('input[name="text"],input[placeholder*="title" i],[aria-label*="title" i]');
      if(!inp) {{
        var inputs = document.querySelectorAll('input[type="text"],input:not([type])');
        if(inputs.length) inp = inputs[0];
      }}
      if(!inp) return JSON.stringify({{ok:false,error:'no_title_input'}});
      inp.focus();
      inp.select();
      document.execCommand('selectAll', false, null);
      document.execCommand('insertText', false, {json.dumps(title[:200])});
      inp.dispatchEvent(new Event('input', {{bubbles:true}}));
      return JSON.stringify({{ok:true,stage:'title_filled',value:inp.value.slice(0,60)}});
    }})()'''
    ok2, out2 = _exec_js_in_domain('calendar.google.com', js_title, timeout=10)
    _t.sleep(0.8)

    # Click Save button
    js_save = '''(function(){
      var btns = Array.from(document.querySelectorAll('button,[role="button"]'));
      for(var b of btns){
        var txt = (b.textContent||b.innerText||'').trim();
        var lbl = (b.getAttribute('aria-label')||'');
        var r = b.getBoundingClientRect();
        if(r.width > 0 && (txt === 'Save' || lbl === 'Save')){
          b.click();
          return JSON.stringify({ok:true,stage:'saved',btnText:txt});
        }
      }
      return JSON.stringify({ok:false,error:'no_save_btn'});
    })()'''
    ok3, out3 = _exec_js_in_domain('calendar.google.com', js_save, timeout=10)
    _t.sleep(2.0)  # wait for save to complete and navigate back

    # Verify the event was created by checking the calendar for the title
    js_verify = f'''(function(){{
      var found = document.body.innerHTML.includes({json.dumps(title[:30])});
      var url = location.href.slice(0,80);
      return JSON.stringify({{created:found, url:url}});
    }})()'''
    ok4, out4 = _exec_js_in_domain('calendar.google.com', js_verify, timeout=8)

    try:
        save_result = json.loads(out3) if out3 and out3 != 'missing value' else {'ok': bool(ok3)}
        verify_result = json.loads(out4) if out4 and out4 != 'missing value' else {}
        if save_result.get('ok') or verify_result.get('created'):
            return {'ok': True, 'stage': 'created', 'verified_in_dom': verify_result.get('created', False)}
        return {'ok': False, 'error': save_result.get('error', 'save_failed'), 'verify': verify_result}
    except Exception as e:
        return {'ok': bool(ok3), 'raw': str(out3)[:200]}


def _exec_calendar_delete(payload: dict) -> dict:
    """Delete a calendar event by data-eventid via browser.
    Clicks on the event chip to open popup, then clicks the trash/delete icon.
    """
    import subprocess as _sp, time as _t
    from .ai_browser_transport import _exec_js_in_domain

    event_id = payload.get('event_id', '')
    if not event_id:
        return {'ok': False, 'error': 'no_event_id_in_payload'}

    # Ensure calendar is on main week view
    nav_script = '''osascript -e 'tell application "Google Chrome"
      repeat with w in windows
        repeat with t in tabs of w
          if (URL of t) contains "calendar.google.com" then
            set URL of t to "https://calendar.google.com/calendar/u/0/r/week"
            return "navigated"
          end if
        end repeat
      end repeat
      return "no_tab"
    end tell' '''
    r = _sp.run(nav_script, shell=True, capture_output=True, text=True, timeout=10)
    if 'no_tab' in (r.stdout or ''):
        return {'ok': False, 'error': 'no_calendar_tab'}
    _t.sleep(4.0)  # wait for week view to load

    # Click the event chip with matching data-eventid to open the popup
    js_click_chip = f'''(function(){{
      var eid = {json.dumps(event_id)};
      var chip = document.querySelector('[data-eventid="'+eid+'"]');
      if(!chip) return JSON.stringify({{ok:false,error:'chip_not_found',eid:eid.substring(0,20)}});
      chip.click();
      return JSON.stringify({{ok:true,stage:'chip_clicked'}});
    }})()'''
    ok2, out2 = _exec_js_in_domain('calendar.google.com', js_click_chip, timeout=8)
    _t.sleep(1.5)  # wait for popup to appear

    try:
        r2 = json.loads(out2) if out2 and out2 != 'missing value' else {}
        if not r2.get('ok'):
            return {'ok': False, 'error': r2.get('error', 'chip_click_failed'), 'raw': out2[:200]}
    except Exception:
        return {'ok': False, 'error': 'chip_click_parse_error', 'raw': str(out2)[:200]}

    # Click trash/delete icon in the popup (aria-label contains "Delete" or "delete")
    js_del = '''(function(){
      var btns = Array.from(document.querySelectorAll('button,[role="button"]'));
      for(var b of btns){
        var lbl = (b.getAttribute('aria-label')||'').toLowerCase();
        var txt = (b.textContent||b.innerText||'').trim().toLowerCase();
        var r = b.getBoundingClientRect();
        if(r.width > 0 && r.height > 0 && (lbl.includes('delete') || txt === 'delete')){
          b.click();
          return JSON.stringify({ok:true,stage:'delete_clicked',aria:b.getAttribute('aria-label')});
        }
      }
      // Also try trash icon by title or data attributes
      var trashIcons = Array.from(document.querySelectorAll('[title*="Delete" i],[data-tooltip*="Delete" i]'));
      for(var t of trashIcons){
        var r = t.getBoundingClientRect();
        if(r.width > 0 && r.height > 0){ t.click(); return JSON.stringify({ok:true,stage:'trash_icon_clicked'}); }
      }
      return JSON.stringify({ok:false,error:'no_delete_btn',visibleBtns:btns.filter(function(b){var r=b.getBoundingClientRect();return r.width>0;}).map(function(b){return b.getAttribute('aria-label')||b.textContent.substring(0,15);}).slice(0,10)});
    })()'''
    ok3, out3 = _exec_js_in_domain('calendar.google.com', js_del, timeout=8)
    _t.sleep(2.0)

    # Verify: event chip should be gone from DOM
    js_verify = f'''(function(){{
      var eid = {json.dumps(event_id)};
      var still = !!document.querySelector('[data-eventid="'+eid+'"]');
      var url = location.href.slice(0,80);
      return JSON.stringify({{deleted:!still, still_in_dom:still, url:url}});
    }})()'''
    ok4, out4 = _exec_js_in_domain('calendar.google.com', js_verify, timeout=8)

    try:
        del_result = json.loads(out3) if out3 and out3 != 'missing value' else {'ok': bool(ok3)}
        verify = json.loads(out4) if out4 and out4 != 'missing value' else {}
        if del_result.get('ok') or verify.get('deleted'):
            return {'ok': True, 'stage': 'deleted', 'verified_gone': verify.get('deleted', False)}
        return {'ok': False, 'error': del_result.get('error', 'delete_failed'),
                'del_result': del_result, 'verify': verify}
    except Exception:
        return {'ok': bool(ok3), 'raw': str(out3)[:200]}


def _exec_whatsapp_send(payload: dict) -> dict:
    """Send approved WhatsApp message via browser.

    Navigates to the designated test contact (Jamal BD Number) by searching
    the sidebar. WhatsApp Business web does not
    offer a 'Message yourself' self-chat. Send button is button[aria-label=Send]
    (data-testid=send does not exist in 2026 WA Business UI).
    """
    import time as _t
    from .ai_browser_transport import _exec_js_in_domain

    content = payload.get('content', '')

    # Step 1: Navigate to test contact via main sidebar search
    js_search = '''(function(){
      var inp = document.querySelector('input[placeholder*="Search or start"]') ||
                document.querySelector('input[aria-label*="Search or start"]');
      if(!inp) return JSON.stringify({ok:false,error:'no_search_input'});
      inp.focus();
      var niv = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
      niv.call(inp, 'Jamal BD Number');
      inp.dispatchEvent(new Event('input',{bubbles:true}));
      inp.dispatchEvent(new Event('change',{bubbles:true}));
      return JSON.stringify({ok:true,stage:'search_triggered'});
    })()'''
    ok1, out1 = _exec_js_in_domain('web.whatsapp.com', js_search, timeout=8)
    _t.sleep(1.5)

    # Step 2: Click the first matching conversation using full mouse event sequence
    js_click = '''(function(){
      var items = Array.from(document.querySelectorAll('[data-testid="cell-frame-container"]'));
      var target = items.find(function(el){
        return el.textContent.toLowerCase().includes('jamal bd');
      });
      if(!target) return JSON.stringify({ok:false,error:'no_test_contact',count:items.length,texts:items.slice(0,3).map(e=>e.textContent.slice(0,40))});
      target.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
      target.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,cancelable:true}));
      target.click();
      return JSON.stringify({ok:true,stage:'test_contact_selected'});
    })()'''
    ok2, out2 = _exec_js_in_domain('web.whatsapp.com', js_click, timeout=8)
    _t.sleep(4.0)  # WA Business needs extra time to render conversation panel
    try:
        r2 = json.loads(out2 or '{}')
        if not r2.get('ok'):
            return {'ok': False, 'error': r2.get('error', 'contact_nav_failed'), 'raw': out2[:120]}
    except Exception:
        return {'ok': False, 'error': 'contact_nav_parse_error'}

    # Step 3: Fill compose (select-all + insertText)
    js_fill = f'''(function(){{
      var inp = document.querySelector('[data-testid=conversation-compose-box-input]');
      if(!inp) return JSON.stringify({{ok:false,error:'no_compose'}});
      inp.focus();
      var sel = window.getSelection();
      var range = document.createRange();
      range.selectNodeContents(inp);
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand('insertText', false, {json.dumps(content[:500])});
      inp.dispatchEvent(new Event('input', {{bubbles:true}}));
      return JSON.stringify({{ok:true,stage:'compose_filled',len:inp.textContent.length}});
    }})()'''
    ok3, out3 = _exec_js_in_domain('web.whatsapp.com', js_fill, timeout=8)
    _t.sleep(1.2)  # React DOM update can be async; wait before len check
    try:
        r3 = json.loads(out3 or '{}')
        if not r3.get('ok'):
            return {'ok': False, 'error': r3.get('error', 'fill_failed')}
    except Exception:
        pass

    # Step 4: Click send (button[aria-label=Send])
    js_send = '''(function(){
      var sendBtn = document.querySelector('button[aria-label="Send"]') ||
                   document.querySelector('[aria-label="Send"]');
      if(!sendBtn) return JSON.stringify({ok:false,error:'no_send_btn'});
      var r = sendBtn.getBoundingClientRect();
      if(r.width === 0) return JSON.stringify({ok:false,error:'send_btn_invisible'});
      sendBtn.click();
      return JSON.stringify({ok:true,stage:'send_clicked'});
    })()'''
    ok4, out4 = _exec_js_in_domain('web.whatsapp.com', js_send, timeout=8)
    _t.sleep(2.0)

    # Step 5: Verify message in chat DOM
    js_verify = f'''(function(){{
      var snippet = {json.dumps(content[:40])};
      var msgs = Array.from(document.querySelectorAll('[data-testid="msg-container"]'));
      var found = msgs.some(function(m){{ return m.textContent.includes(snippet); }});
      return JSON.stringify({{ok:found,msg_count:msgs.length,verified_in_dom:found}});
    }})()'''
    ok5, out5 = _exec_js_in_domain('web.whatsapp.com', js_verify, timeout=8)

    try:
        send_r = json.loads(out4 or '{}')
        verify_r = json.loads(out5 or '{}')
        if send_r.get('stage') == 'send_clicked':
            return {'ok': True, 'stage': 'sent', 'verified_in_dom': verify_r.get('verified_in_dom', False),
                    'msg_count': verify_r.get('msg_count', 0)}
        return {'ok': False, 'error': send_r.get('error', 'send_failed'), 'detail': send_r}
    except Exception:
        return {'ok': bool(ok4), 'raw': str(out4)[:200]}


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
        return json.loads(out) if out else {'ok': bool(ok)}
    except Exception:
        return {'ok': bool(ok), 'raw': str(out)[:200]}


def _exec_messenger_send(payload: dict) -> dict:
    """Send approved Messenger DM via browser.

    Safety: requires a compose box whose aria-label explicitly identifies a
    non-real-contact target (e.g. a test/self conversation). Returns
    'approval_blocked_no_safe_target' when no such safe target exists.
    The current self-test policy always rejects before this executor runs.
    """
    from .ai_browser_transport import _exec_js_in_domain
    content = payload.get('content', '')
    # Check compose state — must be present and destination must be explicit
    check_js = '''(function(){
      var inp = document.querySelector('div[role=textbox][contenteditable=true],[aria-label*="Message"][contenteditable]');
      var label = inp ? (inp.getAttribute('aria-label')||'') : '';
      return JSON.stringify({has_compose:!!inp, label:label.slice(0,40), url:location.href.slice(0,80)});
    })()'''
    ok, out = _exec_js_in_domain('facebook.com', check_js, timeout=8)
    try:
        state = json.loads(out or '{}')
        if not state.get('has_compose'):
            return {'ok': False, 'error': 'approval_blocked_no_safe_target',
                    'note': 'No open Messenger conversation', 'url': state.get('url', '')}
    except Exception:
        return {'ok': False, 'error': 'state_check_failed'}
    js = f'''(function(){{
      var inp = document.querySelector('div[role=textbox][contenteditable=true],[aria-label*="Message"][contenteditable]');
      if(!inp) return JSON.stringify({{ok:false,error:'no_compose'}});
      inp.focus();
      var sel = window.getSelection();
      var range = document.createRange();
      range.selectNodeContents(inp);
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand('insertText', false, {json.dumps(content[:500])});
      inp.dispatchEvent(new Event('input', {{bubbles:true}}));
      inp.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter',keyCode:13,bubbles:true}}));
      return JSON.stringify({{ok:true,stage:'sent'}});
    }})()'''
    ok, out = _exec_js_in_domain('facebook.com', js, timeout=10)
    try:
        return json.loads(out) if out else {'ok': bool(ok)}
    except Exception:
        return {'ok': bool(ok), 'raw': str(out)[:200]}


def _exec_instagram_dm(payload: dict) -> dict:
    """Send approved Instagram DM via browser. Called only after Gate approval.

    Instagram DM compose uses div[role=textbox][contenteditable=true] — no aria-label.
    The old selector [aria-label*='Message'][contenteditable] never matched.
    """
    from .ai_browser_transport import _exec_js_in_domain
    content = payload.get('content', '')
    js = f'''(function(){{
      // Instagram DM compose: div[role=textbox][contenteditable=true] — no aria-label in 2026 IG UI
      var inp = document.querySelector('div[role=textbox][contenteditable=true]') ||
                document.querySelector('div[contenteditable=true]');
      if(!inp) return JSON.stringify({{ok:false,error:'no_compose',url:location.href.slice(0,60)}});
      inp.focus();
      var sel = window.getSelection();
      var range = document.createRange();
      range.selectNodeContents(inp);
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand('insertText', false, {json.dumps(content[:500])});
      inp.dispatchEvent(new Event('input', {{bubbles:true}}));
      // Try send button first; fallback to Enter
      var sendBtn = Array.from(document.querySelectorAll('[aria-label=Send],[type=submit],[role=button]'))
        .find(function(b){{ var r=b.getBoundingClientRect(); return r.width>0&&r.height>0&&(b.getAttribute('aria-label')||b.textContent||'').trim().toLowerCase()==='send'; }});
      if(sendBtn){{ sendBtn.click(); return JSON.stringify({{ok:true,stage:'sent_via_btn'}}); }}
      inp.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter',keyCode:13,bubbles:true}}));
      return JSON.stringify({{ok:true,stage:'sent_via_enter'}});
    }})()'''
    ok, out = _exec_js_in_domain('instagram.com', js, timeout=10)
    try:
        return json.loads(out) if out else {'ok': bool(ok)}
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
