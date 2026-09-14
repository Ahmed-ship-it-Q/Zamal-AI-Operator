"""Browser-based AI provider transport — macOS only via osascript/AppleScript.

Reuses the user's existing logged-in Chrome sessions.
Never extracts cookies, tokens, session data or credentials.
Interacts through normal UI as the user would, via JavaScript injection in Chrome tabs.

Provider status states:
  LIVE_VERIFIED    — accessible and returned a real response this session
  SESSION_REQUIRED — URL loads but shows login / CAPTCHA
  RATE_LIMITED     — provider indicated rate limit
  UNAVAILABLE      — not reachable or Chrome not running
  NOT_CONFIGURED   — non-darwin platform; browser integration not available
"""
import json
import sys
import subprocess
import threading
import time
from dataclasses import dataclass

_IS_DARWIN = sys.platform == 'darwin'

# ── Provider definitions ───────────────────────────────────────────────────

PROVIDER_CONFIGS: dict = {
    'chatgpt': {
        'name': 'ChatGPT',
        'url': 'https://chatgpt.com/',
        'domain': 'chatgpt.com',
        'free_api_note': 'No free API tier. Browser session required.',
        'login_js': (
            '(function(){'
            'var inp=document.querySelector("#prompt-textarea,textarea[placeholder]");'
            'var noLogin=!document.querySelector("input[type=password],a[href*=login]");'
            'return JSON.stringify({logged_in:noLogin&&inp!==null,has_input:inp!==null});'
            '})()'
        ),
        'inject_js': (
            '(function(p){'
            'var el=document.querySelector("#prompt-textarea");'
            'if(!el)return false;'
            'el.focus();'
            'document.execCommand("selectAll",false,null);'
            'document.execCommand("insertText",false,p);'
            'el.dispatchEvent(new Event("input",{bubbles:true}));'
            'return true;})'
        ),
        'submit_js': (
            '(function(){'
            'var b=document.querySelector("button[data-testid=send-button]");'
            'if(!b)b=document.querySelector("button[aria-label=\'Send prompt\']");'
            'if(b&&!b.disabled){b.click();return true;}return false;})()'
        ),
        'response_js': (
            '(function(){'
            'var m=document.querySelectorAll(".markdown,[data-message-author-role=assistant] .markdown");'
            'return m.length?m[m.length-1].innerText.trim():null;})()'
        ),
        'loading_js': 'document.querySelector("[data-testid=stop-button]")!==null',
    },
    'claude': {
        'name': 'Claude',
        'url': 'https://claude.ai/new',
        'domain': 'claude.ai',
        'free_api_note': 'API requires payment. Browser session via claude.ai used.',
        'login_js': (
            '(function(){'
            'var inp=document.querySelector("[contenteditable][data-placeholder],.ProseMirror");'
            'var noLogin=!document.querySelector("input[type=password],a[href*=login]");'
            'return JSON.stringify({logged_in:noLogin&&inp!==null,has_input:inp!==null});'
            '})()'
        ),
        'inject_js': (
            '(function(p){'
            'var el=document.querySelector("[contenteditable][data-placeholder],.ProseMirror");'
            'if(!el)return false;'
            'el.focus();'
            'document.execCommand("selectAll",false,null);'
            'document.execCommand("insertText",false,p);'
            'return true;})'
        ),
        'submit_js': (
            '(function(){'
            'var b=document.querySelector("[data-testid=chat-input-send]");'
            'if(b&&!b.disabled){b.click();return true;}return false;})()'
        ),
        'response_js': (
            '(function(){'
            'var m=document.querySelectorAll("[data-is-streaming=false] .prose");'
            'return m.length?m[m.length-1].innerText.trim():null;})()'
        ),
        'loading_js': 'document.querySelector("[data-is-streaming=true]")!==null',
    },
    'gemini': {
        'name': 'Gemini',
        'url': 'https://gemini.google.com/app',
        'domain': 'gemini.google.com',
        'free_api_note': 'Free API tier available via Google AI Studio. Browser session used as default.',
        'login_js': (
            # SignOutOptions link is present when logged in; absent when not
            '(function(){'
            'var inp=document.querySelector("rich-textarea div[contenteditable],rich-textarea p");'
            'var loggedIn=document.querySelector("a[href*=SignOutOptions]")!==null;'
            'return JSON.stringify({logged_in:loggedIn&&inp!==null,has_input:inp!==null});'
            '})()'
        ),
        'inject_js': (
            # execCommand works on Gemini's Quill contenteditable in Chrome
            '(function(p){'
            'var el=document.querySelector(".ql-editor");'
            'if(!el)return false;'
            'el.focus();'
            'document.execCommand("selectAll",false,null);'
            'document.execCommand("insertText",false,p);'
            'return true;})'
        ),
        'submit_js': (
            # gem-icon-button with send-button class; click its shadow-DOM inner button
            '(function(){'
            'var sb=null;'
            'var all=document.querySelectorAll("gem-icon-button");'
            'for(var i=0;i<all.length;i++){if(all[i].className.includes("send-button")){sb=all[i];break;}}'
            'if(sb){var inner=sb.shadowRoot&&sb.shadowRoot.querySelector("button");'
            '(inner||sb).click();return true;}'
            'var b=document.querySelector("button[aria-label=\'Send message\']");'
            'if(b&&!b.disabled){b.click();return true;}return false;})()'
        ),
        'response_js': (
            # model-response contains AI reply; extract p text to skip "Gemini said" header
            '(function(){'
            'var mrs=document.querySelectorAll("model-response");'
            'if(!mrs.length)return null;'
            'var last=mrs[mrs.length-1];'
            'var paras=Array.from(last.querySelectorAll("p")).filter(function(p){return p.innerText.trim();});'
            'return paras.length?paras.map(function(p){return p.innerText.trim();}).join("\\n"):last.innerText.trim();})()'
        ),
        'loading_js': (
            'document.querySelector("model-response")!==null&&'
            'document.querySelector("[class*=pending],[class*=streaming],loading-indicator")!==null'
        ),
    },
    'deepseek': {
        'name': 'DeepSeek',
        'url': 'https://chat.deepseek.com/',
        'domain': 'chat.deepseek.com',
        'free_api_note': (
            'DeepSeek API has a free tier (deepseek-chat). '
            'Set DEEPSEEK_API_KEY in auth.env to use API instead of browser.'
        ),
        'login_js': (
            '(function(){'
            'var inp=document.querySelector("textarea");'
            'var noLogin=!document.querySelector("input[type=password]");'
            'return JSON.stringify({logged_in:noLogin&&inp!==null,has_input:inp!==null});'
            '})()'
        ),
        'inject_js': (
            '(function(p){'
            'var el=document.querySelector("textarea");'
            'if(!el)return false;'
            'var s=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,"value").set;'
            's.call(el,p);'
            'el.dispatchEvent(new Event("input",{bubbles:true}));'
            'return true;})'
        ),
        'submit_js': (
            '(function(){'
            # DeepSeek uses div[role=button] with class ds-button--primary for send
            'var b=document.querySelector("div.ds-button--primary.ds-button--filled,[data-testid=send-button]");'
            'if(!b)b=document.querySelector("div.ds-button--primary");'
            'if(b){b.click();return true;}return false;})()'
        ),
        'response_js': (
            '(function(){'
            'var m=document.querySelectorAll(".ds-markdown,.markdown-body,[class*=assistant] .markdown");'
            'return m.length?m[m.length-1].innerText.trim():null;})()'
        ),
        'loading_js': (
            'document.querySelector("._streaming,[class*=loading],[class*=generating]")!==null'
        ),
    },
    'qwen': {
        'name': 'Qwen',
        'url': 'https://chat.qwen.ai/',
        'domain': 'chat.qwen.ai',
        'free_api_note': (
            'Qwen API available via Alibaba Cloud DashScope with free quota. '
            'Set QWEN_API_KEY in auth.env to use API instead of browser.'
        ),
        'login_js': (
            '(function(){'
            'var inp=document.querySelector("textarea");'
            'var noLogin=!document.querySelector("input[type=password]");'
            'return JSON.stringify({logged_in:noLogin&&inp!==null,has_input:inp!==null});'
            '})()'
        ),
        'inject_js': (
            '(function(p){'
            'var el=document.querySelector("textarea");'
            'if(!el)return false;'
            'var s=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,"value").set;'
            's.call(el,p);'
            'el.dispatchEvent(new Event("input",{bubbles:true}));'
            'return true;})'
        ),
        'submit_js': (
            '(function(){'
            'var b=document.querySelector("button[aria-label=Send],button.send-button");'
            'if(b&&!b.disabled){b.click();return true;}return false;})()'
        ),
        'response_js': (
            '(function(){'
            'var m=document.querySelectorAll(".qwen-markdown,.custom-qwen-markdown");'
            'return m.length?m[m.length-1].innerText.trim():null;})()'
        ),
        'loading_js': (
            'document.querySelector("button[aria-label=Send]:disabled,[class*=generating],[class*=streaming]")!==null'
        ),
    },
    'kimi': {
        'name': 'Kimi',
        'url': 'https://www.kimi.ai/',
        'domain': 'kimi.ai',
        'free_api_note': (
            'Moonshot AI has an API. '
            'Set KIMI_API_KEY in auth.env to use API instead of browser.'
        ),
        'login_js': (
            '(function(){'
            'var inp=document.querySelector(".chat-input-editor,[contenteditable]");'
            'var noLogin=!document.querySelector("input[type=password]");'
            'return JSON.stringify({logged_in:noLogin&&inp!==null,has_input:inp!==null});'
            '})()'
        ),
        'inject_js': (
            '(function(p){'
            'var el=document.querySelector(".chat-input-editor");'
            'if(!el)return false;'
            'el.focus();'
            'el.innerHTML="<p>"+p+"</p>";'
            'el.dispatchEvent(new InputEvent("input",{bubbles:true,data:p}));'
            'return true;})'
        ),
        'submit_js': (
            # Kimi send button is a div.send-button-container with an SVG icon
            '(function(){'
            'var btn=document.querySelector(".send-button-container");'
            'if(btn){btn.click();return true;}'
            'var el=document.querySelector(".chat-input-editor");'
            'if(!el)return false;'
            'el.focus();'
            'el.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",keyCode:13,bubbles:true,cancelable:true}));'
            'return true;})()'
        ),
        'response_js': (
            # chat-content-item-assistant contains full response including thinking
            '(function(){'
            'var items=document.querySelectorAll(".chat-content-item-assistant");'
            'if(!items.length)return null;'
            'var last=items[items.length-1];'
            'var txt=last.innerText.trim();'
            'var thinkEnd=txt.indexOf("\\n\\n");'
            'return thinkEnd>0&&txt.startsWith("Think\\n")?txt.slice(thinkEnd+2).trim():txt;})()'
        ),
        'loading_js': (
            'document.querySelector(".chat-content-item-assistant [class*=loading],'
            '[class*=generating],[class*=streaming],.segment-assistant [class*=pending]")!==null'
        ),
    },
}

PROVIDER_STATUSES = frozenset({
    'LIVE_VERIFIED', 'SESSION_REQUIRED', 'RATE_LIMITED', 'UNAVAILABLE', 'NOT_CONFIGURED',
})


@dataclass
class ProviderHealth:
    provider: str
    status: str = 'NOT_CONFIGURED'
    last_check: float = 0.0
    last_response_preview: str = ''
    consecutive_failures: int = 0
    unavailable_until: float = 0.0


# ── AppleScript + JS helpers ───────────────────────────────────────────────

def _run_osascript(script: str, timeout: int = 15) -> tuple:
    """Run AppleScript. Returns (success: bool, output: str)."""
    if not _IS_DARWIN:
        return False, 'not_darwin'
    try:
        r = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=timeout, shell=False,
        )
        output = (r.stdout or '').strip() or (r.stderr or '').strip()
        return r.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, 'osascript_timeout'
    except Exception as exc:
        return False, str(exc)[:200]


def _chrome_running() -> bool:
    ok, out = _run_osascript(
        'tell application "System Events" to '
        'return (name of processes) contains "Google Chrome"'
    )
    return ok and 'true' in out.lower()


def _open_tab(url: str) -> bool:
    script = (
        f'tell application "Google Chrome"\n'
        f'  activate\n'
        f'  if (count windows) = 0 then make new window\n'
        f'  make new tab at end of tabs of front window with properties {{URL:"{url}"}}\n'
        f'  set active tab index of front window to count tabs of front window\n'
        f'  return "ok"\n'
        f'end tell'
    )
    ok, out = _run_osascript(script, timeout=10)
    return ok and 'ok' in out


def _exec_js_in_domain(domain: str, js: str, timeout: int = 12) -> tuple:
    """Execute JS in the active Chrome tab matching domain; fall back to last match.

    Broad DOM/page-text extraction patterns are blocked for private-message domains
    (mail, WhatsApp, Messenger, Instagram, Facebook). The call is rejected with
    (False, 'BROAD_EXTRACTION_BLOCKED:<pattern>') before any AppleScript runs.
    """
    try:
        from .private_read_auth import check_js_for_broad_extraction, record_privacy_incident
        bad = check_js_for_broad_extraction(domain, js)
        if bad:
            record_privacy_incident(domain, 'broad_extraction_blocked', f'pattern={bad[:60]}')
            return False, f'BROAD_EXTRACTION_BLOCKED:{bad[:80]}'
    except ImportError:
        pass
    js_safe = js.replace('\\', '\\\\').replace('"', '\\"')
    # Prefer active tab first, then last matching tab
    script = (
        f'tell application "Google Chrome"\n'
        f'  -- Check active tab first\n'
        f'  repeat with w in windows\n'
        f'    set activeTb to active tab of w\n'
        f'    if (URL of activeTb) contains "{domain}" then\n'
        f'      set r to execute activeTb javascript "{js_safe}"\n'
        f'      return r as string\n'
        f'    end if\n'
        f'  end repeat\n'
        f'  -- Fall back to last matching tab\n'
        f'  set lastResult to "NO_TAB"\n'
        f'  repeat with w in windows\n'
        f'    repeat with t in tabs of w\n'
        f'      if (URL of t) contains "{domain}" then\n'
        f'        set lastResult to execute t javascript "{js_safe}"\n'
        f'      end if\n'
        f'    end repeat\n'
        f'  end repeat\n'
        f'  return lastResult as string\n'
        f'end tell'
    )
    ok, out = _run_osascript(script, timeout=timeout)
    return ok and out != 'NO_TAB', out


def _wait_tab_loaded(domain: str, timeout: int = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        script = (
            f'tell application "Google Chrome"\n'
            f'  repeat with w in windows\n'
            f'    set activeTb to active tab of w\n'
            f'    if (URL of activeTb) contains "{domain}" then\n'
            f'      return loading of activeTb as string\n'
            f'    end if\n'
            f'  end repeat\n'
            f'  repeat with w in windows\n'
            f'    repeat with t in tabs of w\n'
            f'      if (URL of t) contains "{domain}" then\n'
            f'        return loading of t as string\n'
            f'      end if\n'
            f'    end repeat\n'
            f'  end repeat\n'
            f'  return "not_found"\n'
            f'end tell'
        )
        ok, out = _run_osascript(script)
        if ok and out.lower() == 'false':
            return True
        if ok and out == 'not_found':
            return False
        time.sleep(0.8)
    return False


def _parse_login_result(js_output: str) -> bool:
    """Parse the JSON output of login_js. Returns True if logged in."""
    try:
        if not js_output or js_output == 'null':
            return False
        data = json.loads(js_output)
        return bool(data.get('logged_in'))
    except Exception:
        # Fallback: look for 'true' after 'logged_in'
        lower = js_output.lower()
        idx = lower.find('logged_in')
        if idx >= 0:
            snippet = lower[idx:idx + 20]
            return 'true' in snippet
        return False


# ── Transport class ────────────────────────────────────────────────────────

class BrowserAITransport:
    """Interact with AI providers via existing Chrome sessions. macOS-only."""

    HEALTH_TTL = 60
    CIRCUIT_FAILURES = 3
    CIRCUIT_TIMEOUT = 300
    RESPONSE_TIMEOUT = 90
    POLL_INTERVAL = 2.0

    def __init__(self):
        self._health: dict = {
            k: ProviderHealth(k, 'NOT_CONFIGURED' if not _IS_DARWIN else 'UNAVAILABLE')
            for k in PROVIDER_CONFIGS
        }
        self._lock = threading.Lock()

    def health_check(self, provider: str, force: bool = False) -> ProviderHealth:
        with self._lock:
            h = self._health.get(provider)
            if h is None:
                return ProviderHealth(provider, 'NOT_CONFIGURED')
            now = time.monotonic()
            if h.unavailable_until > now:
                return h
            if not force and (now - h.last_check) < self.HEALTH_TTL:
                return h
            h.last_check = now

        status = self._check_provider(provider)

        with self._lock:
            self._health[provider].status = status
        return self._health[provider]

    def _check_provider(self, provider: str) -> str:
        if not _IS_DARWIN:
            return 'NOT_CONFIGURED'
        if not _chrome_running():
            return 'UNAVAILABLE'

        config = PROVIDER_CONFIGS[provider]
        domain = config['domain']
        url = config['url']

        ok, result = _exec_js_in_domain(domain, config['login_js'], timeout=10)
        # Fast-fail: Chrome JS via Apple Events is disabled — user must enable it once
        if not ok and 'apple events' in result.lower():
            return 'NOT_CONFIGURED'
        if not ok:
            if not _open_tab(url):
                return 'UNAVAILABLE'
            time.sleep(3)
            _wait_tab_loaded(domain, timeout=20)
            ok, result = _exec_js_in_domain(domain, config['login_js'], timeout=10)
            if not ok and 'apple events' in result.lower():
                return 'NOT_CONFIGURED'
            if not ok:
                return 'UNAVAILABLE'

        return 'LIVE_VERIFIED' if _parse_login_result(result) else 'SESSION_REQUIRED'

    def submit_task(self, provider: str, prompt: str,
                    timeout: int = 90) -> dict:
        """Submit prompt to provider browser session. Returns result dict."""
        if provider not in PROVIDER_CONFIGS:
            return {'ok': False, 'error': 'unknown_provider', 'status': 'NOT_CONFIGURED'}
        if not _IS_DARWIN:
            return {'ok': False, 'error': 'not_darwin', 'status': 'NOT_CONFIGURED'}

        config = PROVIDER_CONFIGS[provider]
        domain = config['domain']
        url = config['url']

        if not _chrome_running():
            return {'ok': False, 'error': 'chrome_not_running', 'status': 'UNAVAILABLE'}

        # Open fresh tab for a new conversation
        if not _open_tab(url):
            return {'ok': False, 'error': 'tab_open_failed', 'status': 'UNAVAILABLE'}
        time.sleep(3)
        if not _wait_tab_loaded(domain, timeout=20):
            return {'ok': False, 'error': 'page_load_timeout', 'status': 'UNAVAILABLE'}

        # Verify login
        ok, login_result = _exec_js_in_domain(domain, config['login_js'], timeout=10)
        if not ok and 'apple events' in login_result.lower():
            return {'ok': False, 'error': 'js_apple_events_disabled', 'status': 'NOT_CONFIGURED'}
        if not ok or not _parse_login_result(login_result):
            self._mark(provider, 'SESSION_REQUIRED')
            return {'ok': False, 'error': 'session_required', 'status': 'SESSION_REQUIRED'}

        # Inject prompt (call the inject function with the prompt argument)
        inject_call = config['inject_js'] + f'({json.dumps(prompt)})'
        ok, inject_result = _exec_js_in_domain(domain, inject_call, timeout=12)
        if not ok or inject_result.lower().strip() == 'false':
            return {'ok': False, 'error': 'inject_failed', 'status': 'UNAVAILABLE'}

        time.sleep(0.5)

        # Submit
        ok, _ = _exec_js_in_domain(domain, config['submit_js'], timeout=10)
        if not ok:
            return {'ok': False, 'error': 'submit_failed', 'status': 'UNAVAILABLE'}

        # Poll for response completion
        deadline = time.monotonic() + timeout
        last_response = None
        stable_since = None

        while time.monotonic() < deadline:
            time.sleep(self.POLL_INTERVAL)

            ok_l, loading = _exec_js_in_domain(domain, config['loading_js'], timeout=5)
            is_loading = ok_l and 'true' in loading.lower()

            ok_r, resp = _exec_js_in_domain(domain, config['response_js'], timeout=5)
            _skip = ('null', 'false', '', 'missing value', 'undefined', 'none')
            if ok_r and resp and resp.lower().strip() not in _skip:
                last_response = resp

            if not is_loading and last_response:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since > 2.5:
                    break
            else:
                stable_since = None

        if not last_response:
            self._mark_failure(provider)
            return {'ok': False, 'error': 'no_response_received', 'status': 'UNAVAILABLE'}

        # Check for rate-limit indicators
        rl_keywords = ('rate limit', 'too many requests', 'slow down', 'quota exceeded')
        if any(k in last_response.lower() for k in rl_keywords):
            self._mark(provider, 'RATE_LIMITED')
            return {'ok': False, 'error': 'rate_limited', 'status': 'RATE_LIMITED',
                    'response': last_response}

        self._mark_success(provider, last_response)
        return {
            'ok': True,
            'provider': provider,
            'status': 'LIVE_VERIFIED',
            'response': last_response,
            'response_length': len(last_response),
        }

    def _mark(self, provider: str, status: str):
        with self._lock:
            if provider in self._health:
                self._health[provider].status = status

    def _mark_failure(self, provider: str):
        with self._lock:
            h = self._health.get(provider)
            if h:
                h.consecutive_failures += 1
                h.status = 'UNAVAILABLE'
                if h.consecutive_failures >= self.CIRCUIT_FAILURES:
                    h.unavailable_until = time.monotonic() + self.CIRCUIT_TIMEOUT

    def _mark_success(self, provider: str, response: str):
        with self._lock:
            h = self._health.get(provider)
            if h:
                h.status = 'LIVE_VERIFIED'
                h.last_response_preview = response[:120]
                h.consecutive_failures = 0
                h.unavailable_until = 0.0

    def all_statuses(self) -> dict:
        result = {}
        for provider, config in PROVIDER_CONFIGS.items():
            h = self._health.get(provider)
            result[provider] = {
                'name': config['name'],
                'url': config['url'],
                'status': h.status if h else 'NOT_CONFIGURED',
                'last_check': h.last_check if h else 0.0,
                'last_response_preview': h.last_response_preview if h else '',
                'consecutive_failures': h.consecutive_failures if h else 0,
                'free_api_note': config.get('free_api_note', ''),
            }
        return result

    def expire_health_cache(self, provider: str):
        """Force re-check on next health_check() call."""
        with self._lock:
            h = self._health.get(provider)
            if h:
                h.last_check = 0.0
                h.unavailable_until = 0.0


# ── Singleton ──────────────────────────────────────────────────────────────

_TRANSPORT: 'BrowserAITransport | None' = None
_TRANSPORT_LOCK = threading.Lock()


def get_browser_transport() -> BrowserAITransport:
    global _TRANSPORT
    with _TRANSPORT_LOCK:
        if _TRANSPORT is None:
            _TRANSPORT = BrowserAITransport()
    return _TRANSPORT
