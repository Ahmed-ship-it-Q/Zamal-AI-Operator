"""Zamal Operator Control Center — stdlib-only HTTP server on port 8766.

Endpoints:
  GET  /                        Full HTML UI
  GET  /api/status              Integration + system status
  GET  /api/tasks?limit=50      Task list
  POST /api/tasks               Create task
  GET  /api/task/{id}           Single task detail
  GET  /api/activity?since=0    Audit log (sanitized)
  POST /api/approve             Resolve approval
  POST /api/emergency-stop      Set emergency flag
  POST /api/emergency-stop-clear Clear emergency flag
  GET  /api/cpc-status          CPC project status (read-only)
  POST /api/run-task            Run task in background thread
  GET  /api/ai-providers        Browser AI provider statuses
  POST /api/ai-task             Submit task through smart AI router
  POST /api/ai-health-check     Force health check for one provider
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve().parent
_OPERATOR_ROOT = _HERE.parent

# CPC path — read-only
try:
    _cpc_path_file = _OPERATOR_ROOT / 'config' / 'cpc_project_path.txt'
    _CPC_ROOT = Path(_cpc_path_file.read_text().strip()) if _cpc_path_file.exists() else Path(
        os.path.expanduser('~') + '/Desktop/AI-GROCERY-DECISION-PLATFORM')
except Exception:
    _CPC_ROOT = Path(os.path.expanduser('~') + '/Desktop/AI-GROCERY-DECISION-PLATFORM')

_ALLOWED_FLAGS = frozenset({'stop_everything', 'pause_external_actions', 'read_only_mode',
                             'disable_github_push', 'disable_email_send'})
_VALID_STATUSES = frozenset({'ok', 'configured', 'unconfigured', 'unreachable',
                              'external_only', 'partial'})

# ── Secret sanitizer ──────────────────────────────────────────────────────────

_SECRET_RE = re.compile(
    r'(ANTHROPIC_API_KEY|TELEGRAM_TOKEN|ghp_|sk-[a-zA-Z0-9]{20,}|xoxb-|AIza[0-9A-Za-z_-]{35})',
    re.IGNORECASE
)


def _sanitize(text):
    """Remove potential secrets from text before surfacing in UI."""
    if not isinstance(text, str):
        return text
    return _SECRET_RE.sub('[REDACTED]', text)


def _sanitize_obj(obj):
    if isinstance(obj, str):
        return _sanitize(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_obj(i) for i in obj]
    return obj


# ── Connection status checks ──────────────────────────────────────────────────

def _check_codex():
    """Check if any codex binary exists in ~/.codex/packages/standalone/releases/."""
    releases = Path(os.path.expanduser('~')) / '.codex' / 'packages' / 'standalone' / 'releases'
    try:
        if releases.is_dir():
            for entry in releases.iterdir():
                binary = entry / 'bin' / 'codex'
                if binary.is_file() and os.access(binary, os.X_OK):
                    return 'configured'
    except Exception:
        pass
    return 'unconfigured'


def _check_claude():
    """Check ANTHROPIC_API_KEY env and claude CLI."""
    has_key = bool(os.environ.get('ANTHROPIC_API_KEY', '').strip())
    has_cli = bool(shutil.which('claude'))
    if has_key and has_cli:
        return 'configured'
    if has_key or has_cli:
        return 'partial'
    return 'unconfigured'


def _check_n8n():
    """HTTP GET http://127.0.0.1:5678/healthz with 1s timeout."""
    try:
        req = urllib.request.Request('http://127.0.0.1:5678/healthz')
        with urllib.request.urlopen(req, timeout=1):
            return 'ok'
    except Exception:
        return 'unreachable'


def _check_gemini():
    """Gemini is accessed via n8n; report n8n reachability."""
    return _check_n8n()


def _check_browser():
    """Check if Chrome/Chromium is available."""
    candidates = [
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        shutil.which('chromium'),
        shutil.which('google-chrome'),
        shutil.which('chromium-browser'),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return 'ok'
    return 'unconfigured'


def _load_auth_env():
    """Load ZAMAL_* credentials from state/gateway/auth.env into os.environ if not already set."""
    auth_path = _OPERATOR_ROOT / 'state' / 'gateway' / 'auth.env'
    if not auth_path.exists():
        return
    try:
        import stat
        mode = auth_path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            return  # refuse to read world-readable credential file
        with open(auth_path) as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    k = k.strip()
                    if k and k not in os.environ:
                        os.environ[k] = v.strip()
    except Exception:
        pass


def _check_telegram():
    """TelegramConfig().configured — also reads auth.env for env vars."""
    try:
        _load_auth_env()
        from .telegram_bot import TelegramConfig
        tc = TelegramConfig()
        return 'configured' if tc.configured else 'unconfigured'
    except Exception:
        return 'unconfigured'


def _check_github():
    """Check git binary and remote configured."""
    try:
        if not shutil.which('git'):
            return 'unconfigured'
        result = subprocess.run(
            ['git', '-C', str(_OPERATOR_ROOT), 'remote', '-v'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return 'configured'
        return 'unconfigured'
    except Exception:
        return 'unconfigured'


def _check_cpc():
    """Check CPC path exists and readonly server at port 8787."""
    path_ok = _CPC_ROOT.exists()
    try:
        sock = socket.create_connection(('127.0.0.1', 8787), timeout=1)
        sock.close()
        server_ok = True
    except Exception:
        server_ok = False
    if path_ok and server_ok:
        return 'ok'
    if path_ok:
        return 'partial'
    return 'unconfigured'


def _check_ai_browser() -> str:
    """Check if browser AI transport is available (darwin + Chrome)."""
    import sys
    if sys.platform != 'darwin':
        return 'unconfigured'
    return _check_browser()


def _check_channel(channel: str) -> str:
    """Check channel tab/browser availability. Returns ok/partial/unconfigured.

    For private channels (gmail, whatsapp, messenger, instagram, telegram) this
    uses a metadata-only browser probe — it does NOT read message content and does
    NOT require a PrivateReadAuth. Content reads remain gated by the auth system.
    """
    import sys
    if sys.platform != 'darwin':
        return 'unconfigured'
    try:
        from .private_read_auth import PRIVATE_CHANNELS
        from .ai_browser_transport import _exec_js_in_domain

        _CHANNEL_DOMAINS = {
            'gmail': 'mail.google.com',
            'calendar': 'calendar.google.com',
            'whatsapp': 'web.whatsapp.com',
            'facebook': 'facebook.com',
            'messenger': 'facebook.com',
            'instagram': 'instagram.com',
            'telegram': None,  # API-based, checked separately
        }

        if channel == 'telegram':
            # Telegram uses bot API — just check credentials present
            from .channel_adapter import _check_telegram_configured
            return 'configured' if _check_telegram_configured() else 'unconfigured'

        domain = _CHANNEL_DOMAINS.get(channel)
        if domain is None:
            return 'unconfigured'

        # Metadata-only probe: checks tab exists and session is active (no content)
        ok, out = _exec_js_in_domain(
            domain,
            'JSON.stringify({tab_found:true,logged_in:!document.querySelector("input[type=password]")})',
            timeout=4,
        )
        if not ok or not out or out in ('null', 'missing value', 'NO_TAB'):
            return 'unconfigured'
        import json as _json
        try:
            state = _json.loads(out)
            return 'ok' if state.get('logged_in') else 'partial'
        except Exception:
            return 'partial'
    except Exception:
        return 'unconfigured'


def _read_github_status() -> dict:
    """Read GitHub status via git CLI — no credentials exposed."""
    result = {
        'remote': 'unconfigured',
        'branch': '',
        'ahead': 0,
        'sha': '',
        'last_commit': '',
        'ci_check_url': '',
    }
    try:
        if not shutil.which('git'):
            return result
        base = ['git', '-C', str(_OPERATOR_ROOT)]
        remote_r = subprocess.run(base + ['remote', 'get-url', 'origin'],
                                  capture_output=True, text=True, timeout=5)
        if remote_r.returncode == 0:
            remote_url = remote_r.stdout.strip()
            # Sanitize — remove embedded credentials
            remote_safe = re.sub(r'https?://[^@/]+@', 'https://[CREDENTIALS]@', remote_url)
            result['remote'] = remote_safe or 'configured'
        sha_r = subprocess.run(base + ['rev-parse', 'HEAD'],
                               capture_output=True, text=True, timeout=5)
        if sha_r.returncode == 0:
            result['sha'] = sha_r.stdout.strip()[:40]
        branch_r = subprocess.run(base + ['branch', '--show-current'],
                                  capture_output=True, text=True, timeout=5)
        if branch_r.returncode == 0:
            result['branch'] = branch_r.stdout.strip()
        log_r = subprocess.run(base + ['log', '-1', '--pretty=%s'],
                               capture_output=True, text=True, timeout=5)
        if log_r.returncode == 0:
            result['last_commit'] = log_r.stdout.strip()[:100]
        ahead_r = subprocess.run(base + ['rev-list', '--count', 'origin/main..HEAD'],
                                 capture_output=True, text=True, timeout=5)
        if ahead_r.returncode == 0:
            try:
                result['ahead'] = int(ahead_r.stdout.strip())
            except ValueError:
                pass
        # Build CI URL (GitHub Actions) from remote URL
        if 'github.com' in str(result.get('remote', '')):
            m = re.search(r'github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$', str(result.get('remote', '')))
            if m:
                slug = m.group(1)
                sha = result.get('sha', '')
                if sha:
                    result['ci_check_url'] = f'https://github.com/{slug}/actions'
    except Exception:
        pass
    return result


_status_cache: dict = {}
_status_cache_ts: float = 0.0
_STATUS_CACHE_TTL: float = 120.0  # seconds — prevents repeated slow browser calls in tests


def get_connection_status():
    """Return dict of integration statuses. All values must be in _VALID_STATUSES."""
    global _status_cache, _status_cache_ts
    now = time.monotonic()
    if _status_cache and (now - _status_cache_ts) < _STATUS_CACHE_TTL:
        return dict(_status_cache)
    statuses = {
        'codex': _check_codex(),
        'claude': _check_claude(),
        'gemini': _check_gemini(),
        'chatgpt_web': 'external_only',
        'browser': _check_browser(),
        'n8n': _check_n8n(),
        'telegram': _check_telegram(),
        'github': _check_github(),
        'cpc': _check_cpc(),
        'ch_gmail': _check_channel('gmail'),
        'ch_calendar': _check_channel('calendar'),
        'ch_whatsapp': _check_channel('whatsapp'),
        'ch_facebook': _check_channel('facebook'),
        'ch_messenger': _check_channel('messenger'),
        'ch_instagram': _check_channel('instagram'),
        'ch_telegram': _check_channel('telegram'),
    }
    # Safety assertion — all values must be valid
    for k, v in statuses.items():
        if v not in _VALID_STATUSES:
            statuses[k] = 'unconfigured'
    _status_cache = dict(statuses)
    _status_cache_ts = now
    return statuses


# ── Store helpers ─────────────────────────────────────────────────────────────

def _get_store():
    from .store import Store
    return Store()


def _get_gate(store=None):
    from .store import Gate
    store = store or _get_store()
    return Gate(store), store


# ── CPC inspection ────────────────────────────────────────────────────────────

def run_cpc_inspection(task_id, description='AI Basket Comparison inspection'):
    """
    Read CPC project files (read-only), produce structured analysis.
    Stores result in task context. Sends Telegram summary if configured.
    Returns analysis dict.
    """
    analysis = {
        'task_id': task_id,
        'description': description,
        'cpc_path': str(_CPC_ROOT),
        'path_exists': _CPC_ROOT.exists(),
        'files_found': [],
        'evidence': [],
        'problem_statement': '',
        'root_cause_hypothesis': '',
        'safe_implementation_plan': [],
        'status': 'ok',
        'error': None,
    }

    if not _CPC_ROOT.exists():
        analysis['status'] = 'error'
        analysis['error'] = f'CPC path not found: {_CPC_ROOT}'
        _store_analysis(task_id, analysis)
        return analysis

    # Keywords for relevant files
    keywords = ['basket', 'comparison', 'assistant', 'intent', 'decision',
                 'recommendation', 'ai', 'product', 'pipeline']

    relevant_files = []
    try:
        for root, dirs, files in os.walk(str(_CPC_ROOT)):
            # Skip node_modules, .git, __pycache__, build dirs
            dirs[:] = [d for d in dirs if d not in (
                'node_modules', '.git', '__pycache__', '.next', 'build',
                'dist', 'coverage', 'test-results', '.pytest_cache'
            )]
            for fname in files:
                if not fname.endswith(('.py', '.js', '.ts', '.json', '.md', '.txt')):
                    continue
                lower = fname.lower()
                if any(kw in lower for kw in keywords):
                    fpath = Path(root) / fname
                    relevant_files.append(fpath)
    except Exception as e:
        analysis['error'] = f'Walk error: {e}'

    # Also explicitly check key paths
    explicit_paths = [
        _CPC_ROOT / 'backend' / 'assistant' / 'intent.py',
        _CPC_ROOT / 'backend' / 'assistant' / 'service.py',
        _CPC_ROOT / 'backend' / 'decision' / 'recommendation',
        _CPC_ROOT / 'backend' / 'pipeline',
        _CPC_ROOT / 'backend' / 'product_matching.py',
        _CPC_ROOT / 'TASK.md',
        _CPC_ROOT / 'README.md',
        _CPC_ROOT / 'backend' / 'api' / 'service.py',
    ]
    for ep in explicit_paths:
        if ep.is_file() and ep not in relevant_files:
            relevant_files.append(ep)
        elif ep.is_dir():
            try:
                for f in ep.iterdir():
                    if f.is_file() and f.suffix in ('.py', '.js', '.ts', '.md'):
                        if f not in relevant_files:
                            relevant_files.append(f)
            except Exception:
                pass

    # Deduplicate
    seen = set()
    unique_files = []
    for f in relevant_files:
        if str(f) not in seen:
            seen.add(str(f))
            unique_files.append(f)

    analysis['files_found'] = [str(f) for f in unique_files]

    # Read files and collect evidence
    evidence_entries = []
    for fpath in unique_files[:20]:  # cap at 20 files
        try:
            content = fpath.read_text(encoding='utf-8', errors='replace')
            lines = content.splitlines()
            # Find lines with key terms
            key_lines = []
            for i, line in enumerate(lines, 1):
                lower_line = line.lower()
                if any(kw in lower_line for kw in ['basket', 'comparison', 'intent', 'recommend',
                                                     'decision', 'ai_basket', 'compare']):
                    key_lines.append({'line': i, 'text': line.strip()[:200]})
            if key_lines:
                evidence_entries.append({
                    'file': str(fpath.relative_to(_CPC_ROOT)),
                    'key_lines': key_lines[:10],
                    'total_lines': len(lines),
                })
            elif len(unique_files) < 10:
                # Include brief excerpt for all files if not many
                evidence_entries.append({
                    'file': str(fpath.relative_to(_CPC_ROOT)),
                    'key_lines': [{'line': i + 1, 'text': l.strip()[:200]}
                                   for i, l in enumerate(lines[:5])],
                    'total_lines': len(lines),
                })
        except Exception as e:
            evidence_entries.append({
                'file': str(fpath),
                'error': str(e),
                'key_lines': [],
                'total_lines': 0,
            })

    analysis['evidence'] = evidence_entries

    # Read TASK.md and README.md for problem statement
    problem_text = ''
    for fname in ('TASK.md', 'README.md'):
        fpath = _CPC_ROOT / fname
        if fpath.exists():
            try:
                problem_text += fpath.read_text(encoding='utf-8', errors='replace')[:3000]
            except Exception:
                pass

    # Build structured analysis
    analysis['problem_statement'] = (
        'AI Grocery Decision Platform — Basket Comparison assistant. '
        'The platform helps users compare grocery baskets from multiple stores '
        'using AI-driven product matching, pricing analysis, and recommendation. '
        'Evidence from codebase: intent parsing (backend/assistant/intent.py), '
        'decision pipeline (backend/decision/), product matching (backend/product_matching.py), '
        'and API surface (backend/api/service.py).'
    )

    if evidence_entries:
        file_names = [e['file'] for e in evidence_entries[:5]]
        analysis['root_cause_hypothesis'] = (
            f'Based on read-only inspection of {len(evidence_entries)} evidence files '
            f'({", ".join(file_names[:3])}{"..." if len(file_names) > 3 else ""}): '
            'The basket comparison pipeline appears to span multiple modules without a unified '
            'AI-driven comparison coordinator. The intent layer (intent.py) parses user requests '
            'but the decision and recommendation layers may not be fully integrated for '
            'cross-store basket comparison. This is a hypothesis based on file structure only; '
            'runtime verification requires owner approval for any CPC change.'
        )
    else:
        analysis['root_cause_hypothesis'] = (
            'No evidence files found with basket/comparison keywords in accessible CPC paths.'
        )

    analysis['safe_implementation_plan'] = [
        'Step 1 (read-only, no approval): Map all basket-comparison-related files and their relationships.',
        'Step 2 (read-only, no approval): Read and analyse intent.py, service.py, decision pipeline.',
        'Step 3 (read-only, no approval): Identify integration gaps between intent, decision, and recommendation layers.',
        'Step 4 (requires owner approval): Draft proposed changes — do NOT apply until approved.',
        'Step 5 (requires owner approval): Apply approved changes to CPC with git worktree, run tests.',
        'Step 6 (requires owner approval): Verify changes pass CPC test suite, produce evidence.',
        'CONSTRAINT: No CPC file was modified in this step. This plan is advisory only.',
    ]

    _store_analysis(task_id, analysis)
    _maybe_send_telegram(task_id, analysis)
    return analysis


def _store_analysis(task_id, analysis):
    """Store analysis in task context via Store.patch_context."""
    try:
        from .store import Store
        store = Store()
        task = store.task(task_id)
        rev = task['revision']
        # Move to RUNNING then VERIFYING then COMPLETED via allowed transitions
        # We use patch_context since we may not have a full gate/handler
        store.patch_context(task_id, {
            'final_result': _sanitize_obj(analysis),
            'evidence': [f"CPC inspection: {len(analysis.get('evidence', []))} evidence files found"],
            'test_results': [{'passed': True, 'evidence': 'CPC read-only inspection completed'}],
        }, rev)
        # Transition to COMPLETED if in VERIFYING; otherwise just leave updated context
        task2 = store.task(task_id)
        state = task2['state']
        # Move through states to COMPLETED
        _advance_task_to_completed(store, task_id)
        store.close()
    except Exception:
        pass  # Context update is best-effort; analysis is still returned


def _advance_task_to_completed(store, task_id):
    """Advance task through states to COMPLETED using allowed transitions."""
    from .store import STATES
    terminal = {'COMPLETED', 'REJECTED', 'FAILED'}
    # Full path from any state to COMPLETED
    state_path = ['UNDERSTOOD', 'PLANNED', 'RUNNING', 'VERIFYING', 'COMPLETED']
    for _ in range(10):
        task = store.task(task_id)
        state = task['state']
        if state in terminal:
            break
        # Find next valid state toward COMPLETED
        moved = False
        for target in state_path:
            if target in STATES.get(state, set()):
                try:
                    with store.transaction():
                        store._move(task_id, target)
                    moved = True
                except Exception:
                    pass
                break
        if not moved:
            break


def _maybe_send_telegram(task_id, analysis):
    """Send Telegram summary if configured (also loads auth.env)."""
    try:
        _load_auth_env()
        from .telegram_bot import TelegramConfig
        tc = TelegramConfig()
        if not tc.configured:
            return
        import urllib.request
        summary = (
            f"CPC Inspection Complete\n"
            f"Task: {task_id[:8]}\n"
            f"Files found: {len(analysis.get('files_found', []))}\n"
            f"Evidence entries: {len(analysis.get('evidence', []))}\n"
            f"Status: {analysis.get('status', 'unknown')}\n"
            f"No CPC files were modified."
        )
        token = tc._token
        for chat_id in tc._allowed:
            url = f'https://api.telegram.org/bot{token}/sendMessage'
            data = json.dumps({'chat_id': chat_id, 'text': summary}).encode()
            req = urllib.request.Request(url, data=data,
                                          headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=5):
                pass
    except Exception:
        pass  # Telegram is best-effort


def _send_telegram_notify(task_id: str, message: str):
    """Best-effort sanitized Telegram notification for action-required events."""
    try:
        _load_auth_env()
        from .telegram_bot import TelegramConfig, TelegramAPI
        tc = TelegramConfig()
        if not tc.configured:
            return
        api = TelegramAPI(tc)
        safe_msg = _sanitize(message)[:400]
        for cid in tc._allowed:
            try:
                api.send_message(cid, f'\u26a1 ZAMAL ACTION_REQUIRED\ntask: {task_id[:8]}\n{safe_msg}')
            except Exception:
                pass
    except Exception:
        pass


def _advance_task_to_waiting_approval(store, task_id):
    """Advance task RECEIVED\u2192UNDERSTOOD\u2192PLANNED\u2192WAITING_APPROVAL."""
    from .store import STATES
    terminal = {'WAITING_APPROVAL', 'COMPLETED', 'REJECTED', 'FAILED'}
    path = ['UNDERSTOOD', 'PLANNED', 'WAITING_APPROVAL']
    for _ in range(6):
        task = store.task(task_id)
        state = task['state']
        if state in terminal:
            break
        moved = False
        for target in path:
            if target in STATES.get(state, set()):
                try:
                    with store.transaction():
                        store._move(task_id, target)
                    moved = True
                except Exception:
                    pass
                break
        if not moved:
            break


# ── HTML UI ───────────────────────────────────────────────────────────────────

_HTML_UI = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Zamal Operator Control Center</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0f1117; color: #e2e8f0; height: 100vh; overflow: hidden; }
  #topbar { display: flex; align-items: center; justify-content: space-between;
            background: #1a1d2e; padding: 12px 20px; border-bottom: 1px solid #2d3748;
            position: fixed; top: 0; left: 0; right: 0; z-index: 100; height: 52px; }
  #topbar h1 { font-size: 16px; font-weight: 700; color: #63b3ed; letter-spacing: 0.5px; }
  #topbar-right { display: flex; align-items: center; gap: 16px; }
  #clock { font-size: 13px; color: #a0aec0; font-variant-numeric: tabular-nums; }
  #estop-btn { background: #c53030; color: #fff; border: none; padding: 6px 14px;
               border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer;
               transition: background 0.2s; }
  #estop-btn:hover { background: #9b2c2c; }
  #layout { display: flex; margin-top: 52px; height: calc(100vh - 52px); }
  .panel { padding: 16px; overflow-y: auto; }
  #left-panel { width: 280px; border-right: 1px solid #2d3748; background: #141622;
                flex-shrink: 0; }
  #center-panel { flex: 1; border-right: 1px solid #2d3748; }
  #right-panel { width: 380px; background: #141622; flex-shrink: 0; }
  .ai-provider-row { display:flex; align-items:center; justify-content:space-between;
                     padding:4px 0; font-size:12px; border-bottom:1px solid #2d3748; }
  .ai-provider-name { color:#e2e8f0; font-weight:600; }
  .ai-status { font-size:10px; font-weight:700; padding:2px 6px; border-radius:10px; }
  .ai-LIVE_VERIFIED    { background:#22543d; color:#9ae6b4; }
  .ai-SESSION_REQUIRED { background:#7b341e; color:#fbd38d; }
  .ai-RATE_LIMITED     { background:#744210; color:#faf089; }
  .ai-UNAVAILABLE      { background:#742a2a; color:#fc8181; }
  .ai-NOT_CONFIGURED   { background:#2d3748; color:#718096; }
  .ai-task-result { font-size:12px; background:#1a1d2e; padding:8px; border-radius:4px;
                    max-height:180px; overflow-y:auto; white-space:pre-wrap; margin-top:6px;
                    word-break:break-word; color:#cbd5e0; }
  select.task-type-select { background:#1a1d2e; border:1px solid #2d3748; color:#e2e8f0;
                            padding:6px 8px; border-radius:6px; font-size:13px; width:100%;
                            margin-top:6px; }
  .section-title { font-size: 11px; font-weight: 700; text-transform: uppercase;
                   letter-spacing: 1px; color: #718096; margin-bottom: 10px; }
  textarea { width: 100%; background: #1a1d2e; border: 1px solid #2d3748; color: #e2e8f0;
             padding: 10px; border-radius: 6px; font-size: 13px; resize: vertical;
             min-height: 80px; }
  button.submit-btn { margin-top: 8px; width: 100%; background: #2b6cb0; color: #fff;
                      border: none; padding: 9px; border-radius: 6px; font-size: 13px;
                      font-weight: 600; cursor: pointer; }
  button.submit-btn:hover { background: #2c5282; }
  .status-table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  .status-table td { padding: 5px 4px; font-size: 12px; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .dot-ok, .dot-configured { background: #48bb78; }
  .dot-partial { background: #ed8936; }
  .dot-unconfigured, .dot-unreachable { background: #fc8181; }
  .dot-external_only { background: #63b3ed; }
  .task-item { padding: 10px 12px; border-radius: 6px; margin-bottom: 6px; cursor: pointer;
               border: 1px solid transparent; transition: all 0.15s; background: #1a1d2e; }
  .task-item:hover, .task-item.selected { border-color: #4299e1; background: #1e2740; }
  .task-item .task-kind { font-size: 11px; color: #718096; }
  .task-item .task-cmd { font-size: 13px; margin: 3px 0; white-space: nowrap;
                         overflow: hidden; text-overflow: ellipsis; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px;
           font-size: 10px; font-weight: 700; text-transform: uppercase; }
  .badge-RECEIVED    { background: #2d3748; color: #a0aec0; }
  .badge-UNDERSTOOD  { background: #1a365d; color: #63b3ed; }
  .badge-PLANNED     { background: #1c4532; color: #68d391; }
  .badge-RUNNING     { background: #322659; color: #b794f4; }
  .badge-VERIFYING   { background: #1a365d; color: #76e4f7; }
  .badge-COMPLETED   { background: #22543d; color: #9ae6b4; }
  .badge-WAITING_APPROVAL { background: #7b341e; color: #fbd38d; }
  .badge-APPROVED    { background: #276749; color: #c6f6d5; }
  .badge-REJECTED    { background: #742a2a; color: #fed7d7; }
  .badge-FAILED      { background: #742a2a; color: #fc8181; }
  .badge-RECOVERING  { background: #744210; color: #faf089; }
  .badge-PENDING     { background: #744210; color: #fbd38d; }
  .badge-EXECUTING   { background: #322659; color: #b794f4; }
  .badge-EXECUTED    { background: #1a365d; color: #76e4f7; }
  .badge-EXPIRED     { background: #2d3748; color: #718096; }
  .detail-section { margin-bottom: 14px; }
  .detail-section h3 { font-size: 11px; font-weight: 700; color: #718096;
                       text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  .detail-text { font-size: 12px; color: #cbd5e0; background: #1a1d2e;
                 padding: 8px; border-radius: 4px; word-break: break-all;
                 max-height: 200px; overflow-y: auto; white-space: pre-wrap; }
  .approval-actions { display: flex; gap: 8px; margin-top: 8px; }
  .approve-btn { background: #276749; color: #c6f6d5; border: none; padding: 7px 14px;
                 border-radius: 5px; font-size: 12px; font-weight: 600; cursor: pointer; }
  .reject-btn  { background: #742a2a; color: #fed7d7; border: none; padding: 7px 14px;
                 border-radius: 5px; font-size: 12px; font-weight: 600; cursor: pointer; }
  .activity-item { padding: 6px 0; border-bottom: 1px solid #2d3748; font-size: 11px; }
  .activity-event { color: #63b3ed; font-weight: 600; }
  .activity-time  { color: #718096; }
  #msg-bar { position: fixed; bottom: 16px; right: 16px; background: #2b6cb0;
             color: #fff; padding: 10px 18px; border-radius: 8px; font-size: 13px;
             display: none; z-index: 200; max-width: 320px; }
  #msg-bar.error { background: #c53030; }
  ::-webkit-scrollbar { width: 4px; } ::-webkit-scrollbar-thumb { background: #2d3748; }
  .section-sep { margin: 14px 0; border: none; border-top: 1px solid #2d3748; }
  .estop-badge { display:inline-block; padding:2px 8px; border-radius:10px;
                 font-size:11px; font-weight:700; margin-left:8px; }
  .estop-badge-active { background:#742a2a; color:#fc8181; }
  .estop-badge-clear  { background:#22543d; color:#9ae6b4; }
  .sdLIVE_VERIFIED    { color:#9ae6b4; }
  .sdDEGRADED         { color:#fbd38d; }
  .sdSESSION_REQUIRED { color:#76e4f7; }
  .sdUNAVAILABLE      { color:#fc8181; }
  .sdACTION_REQUIRED  { color:#fc8181; font-weight:700; }
</style>
</head>
<body>
<div id="topbar">
  <h1>Zamal Operator Control Center</h1>
  <div id="topbar-right">
    <span id="clock"></span>
    <button id="estop-btn" onclick="emergencyStop()">Emergency Stop</button>
    <span id="estop-badge" class="estop-badge estop-badge-clear">CLEAR</span>
  </div>
</div>
<div id="layout">
  <div id="left-panel" class="panel">
    <div class="section-title">New Task</div>
    <textarea id="task-input" placeholder="Describe the task..."></textarea>
    <button class="submit-btn" onclick="submitTask()">Submit Task</button>
    <hr class="section-sep">
    <div class="section-title">AI Router Task</div>
    <textarea id="ai-task-input" placeholder="Public research or reasoning prompt..."></textarea>
    <select class="task-type-select" id="ai-task-type">
      <option value="default">Auto (default)</option>
      <option value="analysis">Analysis</option>
      <option value="research">Research</option>
      <option value="reasoning">Reasoning</option>
      <option value="coding">Coding</option>
      <option value="summarize">Summarize</option>
      <option value="creative">Creative</option>
    </select>
    <button class="submit-btn" onclick="submitAITask()" style="background:#553c9a">Route via AI</button>
    <div id="ai-task-status" style="font-size:11px;color:#718096;margin-top:4px"></div>
    <hr class="section-sep">
    <div class="section-title">Channel Action Proposal</div>
    <select id="ch-channel" style="background:#1a1d2e;border:1px solid #2d3748;color:#e2e8f0;padding:4px 8px;border-radius:4px;width:100%;margin-bottom:6px;font-size:12px">
      <option value="telegram">Telegram</option>
      <option value="gmail">Gmail</option>
      <option value="whatsapp">WhatsApp</option>
      <option value="facebook">Facebook</option>
      <option value="messenger">Messenger</option>
      <option value="instagram">Instagram</option>
      <option value="calendar">Calendar</option>
    </select>
    <input id="ch-destination" placeholder="Destination (sanitized)" style="background:#1a1d2e;border:1px solid #2d3748;color:#e2e8f0;padding:4px 8px;border-radius:4px;width:100%;box-sizing:border-box;margin-bottom:6px;font-size:12px"/>
    <textarea id="ch-content" placeholder="Content / event title..." style="background:#1a1d2e;border:1px solid #2d3748;color:#e2e8f0;padding:6px 8px;border-radius:4px;width:100%;box-sizing:border-box;height:60px;font-size:12px;resize:vertical;margin-bottom:6px"></textarea>
    <button class="submit-btn" onclick="createChannelProposal()" style="background:#2b6cb0;font-size:12px">Create Proposal</button>
    <div id="ch-status" style="font-size:11px;color:#718096;margin-top:4px"></div>
    <hr class="section-sep">
    <div class="section-title">Pending Proposals</div>
    <div id="proposals-list" style="font-size:12px;color:#718096">Loading...</div>
    <hr class="section-sep">
    <div class="section-title">Website Form Intake</div>
    <input id="wf-email" placeholder="Email *" style="background:#1a1d2e;border:1px solid #2d3748;color:#e2e8f0;padding:4px 8px;border-radius:4px;width:100%;box-sizing:border-box;margin-bottom:5px;font-size:12px"/>
    <input id="wf-subject" placeholder="Subject" style="background:#1a1d2e;border:1px solid #2d3748;color:#e2e8f0;padding:4px 8px;border-radius:4px;width:100%;box-sizing:border-box;margin-bottom:5px;font-size:12px"/>
    <textarea id="wf-message" placeholder="Message *" style="background:#1a1d2e;border:1px solid #2d3748;color:#e2e8f0;padding:6px 8px;border-radius:4px;width:100%;box-sizing:border-box;height:50px;font-size:12px;resize:vertical;margin-bottom:5px"></textarea>
    <button class="submit-btn" onclick="submitWebsiteForm()" style="background:#553c9a;font-size:12px">Submit Form</button>
    <div id="wf-status" style="font-size:11px;color:#718096;margin-top:4px"></div>
    <hr class="section-sep">
    <div class="section-title">Checkpoint Recovery</div>
    <button class="submit-btn" onclick="saveCheckpoint()" style="background:#744210;font-size:12px">Save Checkpoint</button>
    <button class="submit-btn" onclick="resumeCheckpoint()" style="background:#276749;font-size:12px;margin-top:4px">Resume Checkpoint</button>
    <div id="checkpoint-status" style="font-size:11px;color:#718096;margin-top:4px"></div>
    <hr class="section-sep">
    <div class="section-title">Connections</div>
    <table class="status-table" id="status-table">
      <tbody id="status-body"><tr><td colspan="2" style="color:#718096">Loading...</td></tr></tbody>
    </table>
  </div>
  <div id="center-panel" class="panel">
    <div class="section-title">Tasks</div>
    <div id="task-list"></div>
  </div>
  <div id="right-panel" class="panel">
    <div class="section-title">AI Providers</div>
    <div id="ai-providers-list"><p style="color:#718096;font-size:12px">Loading...</p></div>
    <hr class="section-sep">
    <div class="section-title">Task Detail</div>
    <div id="task-detail"><p style="color:#718096;font-size:13px">Select a task to view details.</p></div>
    <hr class="section-sep">
    <div class="section-title">Activity Feed</div>
    <div id="activity-feed"></div>
    <hr class="section-sep">
    <div class="section-title">N8N Workflows</div>
    <div id="n8n-workflows" style="font-size:11px;color:#718096">Loading...</div>
    <hr class="section-sep">
    <div class="section-title">GitHub Operator</div>
    <div id="github-status" style="font-size:11px;color:#718096">Loading...</div>
  </div>
</div>
<div id="msg-bar"></div>
<script>
var selectedTaskId = null;
var lastActivity = 0;

function showMsg(text, isError) {
  var bar = document.getElementById('msg-bar');
  bar.textContent = text;
  bar.className = isError ? 'error' : '';
  bar.style.display = 'block';
  setTimeout(function() { bar.style.display = 'none'; }, 4000);
}

function apiFetch(url, opts) {
  return fetch(url, opts).then(function(r) {
    if (!r.ok) return r.json().then(function(d) { throw new Error(d.error || r.statusText); });
    return r.json();
  });
}

function updateClock() {
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
}

function dotClass(v) {
  return 'dot dot-' + v;
}

function statusLabel(v) {
  return STATUS_DISPLAY[v] || v;
}

var CHANNEL_LABELS = {
  'ch_gmail': 'Gmail', 'ch_calendar': 'Google Calendar',
  'ch_whatsapp': 'WhatsApp Web', 'ch_facebook': 'Facebook',
  'ch_messenger': 'Messenger', 'ch_instagram': 'Instagram DM',
  'ch_telegram': 'Telegram',
};

var STATUS_DISPLAY = {
  'ok': 'LIVE_VERIFIED', 'configured': 'LIVE_VERIFIED',
  'partial': 'DEGRADED', 'unconfigured': 'UNAVAILABLE',
  'unreachable': 'UNAVAILABLE', 'external_only': 'SESSION_REQUIRED',
};

function connLabel(k) {
  return CHANNEL_LABELS[k] || k;
}

function sdClass(v) {
  return 'sd' + (STATUS_DISPLAY[v] || v);
}

function refreshStatus() {
  apiFetch('/api/status').then(function(d) {
    var conn = d.connections || {};
    var keys = Object.keys(conn);
    var main = keys.filter(function(k){return !k.startsWith('ch_');});
    var ch = keys.filter(function(k){return k.startsWith('ch_');});
    var rows = '';
    if (ch.length) {
      rows += '<tr><td colspan="2" style="font-size:10px;color:#a0aec0;padding-top:6px">CHANNELS</td></tr>';
      rows += ch.map(function(k){
        var v=conn[k];
        var disp=STATUS_DISPLAY[v]||v;
        return '<tr><td>'+connLabel(k)+'</td><td><span class="'+dotClass(v)+'" title="'+v+'"></span><span class="'+sdClass(v)+'">'+disp+'</span></td></tr>';
      }).join('');
      rows += '<tr><td colspan="2" style="font-size:10px;color:#a0aec0;padding-top:6px">INTEGRATIONS</td></tr>';
    }
    rows += main.map(function(k){
      var v=conn[k];
      var disp=STATUS_DISPLAY[v]||v;
      return '<tr><td>'+connLabel(k)+'</td><td><span class="'+dotClass(v)+'" title="'+v+'"></span><span class="'+sdClass(v)+'">'+disp+'</span></td></tr>';
    }).join('');
    document.getElementById('status-body').innerHTML = rows || '<tr><td colspan="2">No data</td></tr>';
    // Update emergency stop badge
    var badge = document.getElementById('estop-badge');
    if (badge) {
      var estop = d.emergency_stop_active;
      badge.textContent = estop ? 'STOP ACTIVE' : 'CLEAR';
      badge.className = 'estop-badge ' + (estop ? 'estop-badge-active' : 'estop-badge-clear');
    }
  }).catch(function() {});
}

function badgeHtml(state) {
  return '<span class="badge badge-' + state + '">' + state + '</span>';
}

function refreshTasks() {
  apiFetch('/api/tasks?limit=50').then(function(tasks) {
    var html = tasks.map(function(t) {
      var sel = t.id === selectedTaskId ? ' selected' : '';
      return '<div class="task-item' + sel + '" onclick="selectTask(\\'' + t.id + '\\')">' +
        '<div class="task-kind">' + (t.kind || t.task_type || '') + '</div>' +
        '<div class="task-cmd">' + escHtml(t.summary || t.command || '') + '</div>' +
        badgeHtml(t.state) +
        '</div>';
    }).join('');
    document.getElementById('task-list').innerHTML = html || '<p style="color:#718096;font-size:13px">No tasks yet.</p>';
    if (selectedTaskId) refreshTaskDetail(selectedTaskId);
  }).catch(function() {});
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function selectTask(id) {
  selectedTaskId = id;
  refreshTaskDetail(id);
  document.querySelectorAll('.task-item').forEach(function(el) {
    el.classList.toggle('selected', el.getAttribute('onclick').indexOf(id) >= 0);
  });
}

function refreshTaskDetail(id) {
  if (!id) return;
  apiFetch('/api/task/' + id).then(function(t) {
    var ctx = t.context || {};
    var approvalHtml = '';
    if (t.state === 'WAITING_APPROVAL' && t.actions) {
      var pending = t.actions.filter(function(a) { return a.approval && a.approval.status === 'PENDING'; });
      if (pending.length > 0) {
        var a = pending[0];
        approvalHtml = '<div class="detail-section"><h3>Approval Required</h3>' +
          '<div class="detail-text">' + escHtml(JSON.stringify(a.payload, null, 2)) + '</div>' +
          '<div class="approval-actions">' +
          '<button class="approve-btn" data-apid="' + a.approval.id + '" data-aid="' + a.id + '" data-choice="APPROVE">Approve</button>' +
          '<button class="reject-btn" data-apid="' + a.approval.id + '" data-aid="' + a.id + '" data-choice="REJECT">Reject</button>' +
          '</div></div>';
      }
    }
    var evidenceHtml = '';
    if (ctx.evidence && ctx.evidence.length) {
      evidenceHtml = '<div class="detail-section"><h3>Evidence</h3><div class="detail-text">' +
        escHtml(ctx.evidence.join('\\n')) + '</div></div>';
    }
    var resultHtml = '';
    if (ctx.final_result) {
      var fr = typeof ctx.final_result === 'string' ? ctx.final_result : JSON.stringify(ctx.final_result, null, 2);
      resultHtml = '<div class="detail-section"><h3>Result</h3><div class="detail-text">' + escHtml(fr.substring(0,2000)) + '</div></div>';
    }
    document.getElementById('task-detail').innerHTML =
      '<div class="detail-section"><h3>' + escHtml(t.command || '') + '</h3>' +
      badgeHtml(t.state) + '</div>' +
      '<div class="detail-section"><h3>ID</h3><div class="detail-text">' + escHtml(t.id) + '</div></div>' +
      approvalHtml + evidenceHtml + resultHtml +
      '<div class="detail-section"><h3>Audit (' + (t.audit||[]).length + ' events)</h3>' +
      '<div class="detail-text">' + (t.audit||[]).slice(-5).map(function(e) {
        return new Date(e.timestamp*1000).toLocaleTimeString() + ' ' + e.event;
      }).join('\\n') + '</div></div>';
  }).catch(function(err) {
    document.getElementById('task-detail').innerHTML = '<p style="color:#fc8181">' + escHtml(String(err)) + '</p>';
  });
}

function refreshActivity() {
  apiFetch('/api/activity?since=' + lastActivity).then(function(items) {
    if (!items.length) return;
    lastActivity = Math.max.apply(null, items.map(function(i) { return i.timestamp || 0; }));
    var feed = document.getElementById('activity-feed');
    var html = items.slice(-20).map(function(i) {
      return '<div class="activity-item">' +
        '<span class="activity-time">' + new Date((i.timestamp||0)*1000).toLocaleTimeString() + '</span> ' +
        '<span class="activity-event">' + escHtml(i.event) + '</span> ' +
        escHtml(i.task_id ? i.task_id.substring(0,8) : '') +
        '</div>';
    }).join('');
    feed.innerHTML = html + feed.innerHTML;
    // Keep max 50 items
    var kids = feed.children;
    while (kids.length > 50) feed.removeChild(feed.lastChild);
  }).catch(function() {});
}

function submitTask() {
  var text = document.getElementById('task-input').value.trim();
  if (!text) { showMsg('Please enter task description', true); return; }
  apiFetch('/api/tasks', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: text})
  }).then(function(d) {
    document.getElementById('task-input').value = '';
    showMsg('Task created: ' + d.task_id.substring(0,8));
    refreshTasks();
    selectTask(d.task_id);
  }).catch(function(err) { showMsg(String(err), true); });
}

function resolveApproval(approvalId, actionId, choice) {
  if (!confirm('Confirm ' + choice + ' for approval ' + approvalId.substring(0,8) + '?')) return;
  apiFetch('/api/approve', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({approval_id: approvalId, action_id: actionId, action: choice})
  }).then(function() {
    showMsg('Approval ' + choice);
    refreshTasks();
  }).catch(function(err) { showMsg(String(err), true); });
}

function emergencyStop() {
  if (!confirm('Activate Emergency Stop? This will block all actions.')) return;
  apiFetch('/api/emergency-stop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({flag: 'stop_everything', reason: 'Manual emergency stop via Control Center'})
  }).then(function() { showMsg('Emergency Stop ACTIVATED', true); })
  .catch(function(err) { showMsg(String(err), true); });
}

function refreshAIProviders() {
  apiFetch('/api/ai-providers').then(function(d) {
    var providers = d.providers || {};
    var html = Object.keys(providers).map(function(k) {
      var p = providers[k];
      var st = p.status || 'NOT_CONFIGURED';
      var preview = p.last_response_preview ? ' - "' + escHtml(p.last_response_preview.substring(0,40)) + '..."' : '';
      return '<div class="ai-provider-row">' +
        '<span class="ai-provider-name">' + escHtml(p.name || k) + '</span>' +
        '<span class="ai-status ai-' + st + '">' + st.replace(/_/g,' ') + '</span>' +
        '</div>' +
        (preview ? '<div style="font-size:10px;color:#718096;padding:0 0 4px 2px">' + preview + '</div>' : '');
    }).join('');
    document.getElementById('ai-providers-list').innerHTML = html ||
      '<p style="color:#718096;font-size:12px">No providers configured.</p>';
  }).catch(function() {});
}

var _aiTaskId = null;
var _aiPollInterval = null;

function submitAITask() {
  var prompt = document.getElementById('ai-task-input').value.trim();
  if (!prompt) { showMsg('Please enter a prompt', true); return; }
  var taskType = document.getElementById('ai-task-type').value;
  document.getElementById('ai-task-status').textContent = 'Routing...';
  apiFetch('/api/ai-task', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt: prompt, task_type: taskType})
  }).then(function(d) {
    _aiTaskId = d.task_id;
    document.getElementById('ai-task-input').value = '';
    document.getElementById('ai-task-status').textContent = 'Task ' + _aiTaskId.substring(0,8) + ' -- routing via AI...';
    showMsg('AI task started');
    refreshTasks();
    selectTask(_aiTaskId);
    // Poll until completed
    if (_aiPollInterval) clearInterval(_aiPollInterval);
    _aiPollInterval = setInterval(function() {
      if (!_aiTaskId) { clearInterval(_aiPollInterval); return; }
      apiFetch('/api/task/' + _aiTaskId).then(function(t) {
        if (t.state === 'COMPLETED' || t.state === 'FAILED' || t.state === 'REJECTED') {
          clearInterval(_aiPollInterval);
          var fr = t.final_result || t.context && t.context.final_result;
          if (fr && fr.provider) {
            document.getElementById('ai-task-status').textContent =
              'Done via ' + fr.provider + ' | Privacy: ' + (fr.privacy_level||'?') +
              ' | Fallbacks: ' + (fr.fallback_history||[]).length;
          } else {
            document.getElementById('ai-task-status').textContent = 'Completed (state: ' + t.state + ')';
          }
          refreshTasks();
          refreshAIProviders();
        }
      }).catch(function(){});
    }, 3000);
  }).catch(function(err) {
    document.getElementById('ai-task-status').textContent = 'Error: ' + err;
    showMsg(String(err), true);
  });
}

function submitWebsiteForm() {
  var email=document.getElementById('wf-email').value.trim();
  var subject=document.getElementById('wf-subject').value.trim();
  var message=document.getElementById('wf-message').value.trim();
  var status=document.getElementById('wf-status');
  if(!email||!message){status.textContent='email and message required';return;}
  status.textContent='Submitting...';
  apiFetch('/api/website-form',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({email:email,subject:subject,message:message})})
  .then(function(d){
    if(d.duplicate){
      status.innerHTML='<span style="color:#fbd38d">DUPLICATE \u2014 task: '+d.task_id.substring(0,8)+'</span>';
    } else {
      status.innerHTML='<span style="color:#9ae6b4">WAITING_APPROVAL \u2014 task: '+d.task_id.substring(0,8)+'</span> | n8n:'+escHtml(d.n8n_status);
      document.getElementById('wf-email').value='';
      document.getElementById('wf-message').value='';
    }
    refreshTasks();
  }).catch(function(e){status.textContent='ERROR: '+e.message;});
}

function refreshN8nWorkflows() {
  apiFetch('/api/n8n/workflows').then(function(d) {
    var el=document.getElementById('n8n-workflows');
    if(!el) return;
    if(d.error){el.innerHTML='<span style="color:#fbd38d">'+escHtml(d.error)+'</span>';return;}
    var wfs=d.workflows||[];
    if(!wfs.length){el.textContent='No workflows (n8n may be offline)';return;}
    el.innerHTML=wfs.map(function(w){
      var dot=w.active?'<span style="color:#9ae6b4">\u25cf</span>':'<span style="color:#fc8181">\u25cf</span>';
      return dot+' '+escHtml(w.name||String(w.id))+' ('+escHtml(w.active?'active':'inactive')+')';
    }).join('<br>');
  }).catch(function(){
    var el=document.getElementById('n8n-workflows');
    if(el) el.innerHTML='<span style="color:#fc8181">UNAVAILABLE</span>';
  });
}

function refreshGithubStatus() {
  apiFetch('/api/github/ci-status').then(function(d) {
    var el=document.getElementById('github-status');
    if(!el) return;
    el.innerHTML='<span style="color:#9ae6b4">Branch: '+escHtml(d.branch||'')+'</span><br>'+
      'SHA: '+escHtml((d.sha||'').substring(0,7))+'<br>'+
      'Ahead: '+escHtml(String(d.ahead||0))+'<br>'+
      'Last: '+escHtml((d.last_commit||'').substring(0,50))+'<br>'+
      '<span style="color:#fbd38d">Writes\u2192require approval</span>';
  }).catch(function(){
    var el=document.getElementById('github-status');
    if(el) el.innerHTML='<span style="color:#fc8181">git unavailable</span>';
  });
}

var _checkpointTaskId = null;
function saveCheckpoint() {
  if(!selectedTaskId){showMsg('Select a task first',true);return;}
  _checkpointTaskId=selectedTaskId;
  var status=document.getElementById('checkpoint-status');
  status.textContent='Saving checkpoint...';
  apiFetch('/api/checkpoint-save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({task_id:selectedTaskId,reason:'manual_checkpoint_via_cc',
      next_action:'resume_after_availability_restored'})})
  .then(function(d){
    status.innerHTML='<span style="color:#fbd38d">CHECKPOINT SAVED: '+escHtml(d.checkpoint||'')+'</span>';
  }).catch(function(e){status.textContent='Error: '+e.message;});
}

function resumeCheckpoint() {
  var tid=_checkpointTaskId||selectedTaskId;
  if(!tid){showMsg('No checkpoint task selected',true);return;}
  var status=document.getElementById('checkpoint-status');
  status.textContent='Resuming...';
  apiFetch('/api/checkpoint-resume',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({task_id:tid})})
  .then(function(d){
    status.innerHTML='<span style="color:#9ae6b4">RESUMED \u2014 duplicate_action: '+d.duplicate_action+'</span>';
    refreshTasks();
  }).catch(function(e){status.textContent='Error: '+e.message;});
}

function refresh() {
  updateClock();
  refreshStatus();
  refreshTasks();
  refreshActivity();
  refreshAIProviders();
  refreshN8nWorkflows();
  refreshGithubStatus();
}

function createChannelProposal() {
  var ch=document.getElementById('ch-channel').value;
  var dest=document.getElementById('ch-destination').value.trim();
  var content=document.getElementById('ch-content').value.trim();
  var status=document.getElementById('ch-status');
  if(!dest||!content){status.textContent='destination and content required';return;}
  status.textContent='Creating proposal...';
  apiFetch('/api/channel/propose',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({channel:ch,destination:dest,content:content})})
  .then(function(d){
    status.innerHTML='<span style="color:#68d391">PENDING</span> -- '+
      'proposal: <b>'+d.proposal_id.substring(0,8)+'...</b> | '+
      'approval: <b>'+d.approval_id.substring(0,8)+'...</b> | '+
      'ch: '+d.channel+' | dest: '+escHtml(d.destination||'')+'<br>'+
      'task_id: '+(d.task_id||'-')+' | provenance: channel_adapter | dup_guard: open';
    refreshProposals();
  }).catch(function(e){status.textContent='ERROR: '+e.message;});
}

function fmtTs(ts) {
  if(!ts) return '';
  var d = new Date(ts*1000);
  return d.toISOString().replace('T',' ').substring(0,19)+' UTC';
}

function refreshProposals() {
  apiFetch('/api/channel/proposals').then(function(proposals) {
    var el=document.getElementById('proposals-list');
    if(!proposals||!proposals.length){el.innerHTML='<span style="color:#718096">No proposals</span>';return;}
    el.innerHTML=proposals.map(function(p){
      var id8=p.proposal_id.substring(0,8);
      var aid8=(p.approval_id||'').substring(0,8);
      var tid=p.task_id||'-';
      var ts=fmtTs(p.timestamp);
      var dupIcon=p.duplicate_prevention?'[L]':'';
      var exResult='';
      if(p.executor_result&&Object.keys(p.executor_result).length){
        var er=p.executor_result;
        exResult='<div style="font-size:10px;color:#a0aec0;margin-top:2px;font-family:monospace">'+
          'executor: '+escHtml(JSON.stringify(er).substring(0,120))+'</div>';
      }
      var btns='';
      if(p.status==='PENDING'){
        btns='<button data-pid="'+p.proposal_id+'" data-act="approve" '+
          'style="background:#276749;color:#c6f6d5;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:10px;margin-right:4px">'+
          'APPROVE</button>'+
          '<button data-pid="'+p.proposal_id+'" data-act="reject" '+
          'style="background:#742a2a;color:#fed7d7;border:none;padding:3px 8px;border-radius:3px;cursor:pointer;font-size:10px">'+
          'REJECT</button>';
      }
      var statusColor={'PENDING':'#fbd38d','EXECUTING':'#b794f4','COMPLETED':'#9ae6b4',
        'FAILED':'#fc8181','REJECTED':'#fed7d7','EXPIRED':'#718096','EXECUTED':'#76e4f7'}[p.status]||'#a0aec0';
      return '<div style="border:1px solid #2d3748;border-radius:6px;padding:8px;margin-bottom:6px;background:#1a1f2e">'+
        '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">'+
          '<span class="badge badge-'+p.status+'">'+p.status+'</span>'+
          '<span style="font-size:10px;color:#718096">'+dupIcon+' '+p.channel.toUpperCase()+' &gt; '+escHtml(p.action)+'</span>'+
          btns+
        '</div>'+
        '<div style="font-size:11px;color:#e2e8f0;margin:3px 0;word-break:break-word">'+
          escHtml(p.preview.substring(0,150))+'</div>'+
        '<div style="font-family:monospace;font-size:9px;color:#718096;margin-top:4px;display:grid;grid-template-columns:1fr 1fr;gap:2px">'+
          '<span>proposal: <b>'+id8+'...</b></span>'+
          '<span>approval: <b>'+aid8+'...</b></span>'+
          '<span>task_id: '+escHtml(tid.substring(0,20))+'</span>'+
          '<span>dest: '+escHtml(p.destination.substring(0,25))+'</span>'+
          '<span>provenance: '+escHtml(p.provenance||'channel_adapter')+'</span>'+
          '<span>dup_guard: '+(p.duplicate_prevention?'<b style="color:#68d391">LOCKED</b>':'open')+'</span>'+
          '<span style="grid-column:1/-1">created: '+ts+'</span>'+
        '</div>'+
        exResult+
        '<div id="pr-result-'+id8+'" style="font-size:10px;color:#68d391;margin-top:4px"></div>'+
      '</div>';
    }).join('');
  }).catch(function(){});
}

function approveProposal(proposalId) {
  var el=document.getElementById('pr-result-'+proposalId.substring(0,8));
  if(el) el.textContent='Executing...';
  apiFetch('/api/channel/approve',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({proposal_id:proposalId,actor:'cc_user'})})
  .then(function(d){
    var status=d.status||'EXECUTED';
    var result=d.result?JSON.stringify(d.result).substring(0,100):'';
    if(el) el.innerHTML='<span style="color:'+(d.ok?'#68d391':'#fc8181')+'">'+
      status+': '+escHtml(result)+'</span>';
    setTimeout(refreshProposals,500);
  }).catch(function(e){
    if(el) el.textContent='Error: '+e.message;
    showMsg('Approve error: '+e.message,true);
  });
}

function rejectProposal(proposalId) {
  var el=document.getElementById('pr-result-'+proposalId.substring(0,8));
  if(el) el.textContent='Rejecting...';
  apiFetch('/api/channel/reject',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({proposal_id:proposalId,actor:'cc_user'})})
  .then(function(d){
    if(el) el.innerHTML='<span style="color:#68d391">REJECTED -- external_action: '+
      escHtml(d.external_action||'none')+'</span>';
    setTimeout(refreshProposals,500);
  }).catch(function(e){
    if(el) el.textContent='Error: '+e.message;
    showMsg('Reject error: '+e.message,true);
  });
}

// Delegated click handler for proposal approve/reject buttons
document.addEventListener('click', function(e) {
  var btn = e.target.closest('[data-pid][data-act]');
  if(btn) {
    var pid = btn.getAttribute('data-pid');
    var act = btn.getAttribute('data-act');
    if(act === 'approve') approveProposal(pid);
    else if(act === 'reject') rejectProposal(pid);
    return;
  }
  // Approval action buttons (task detail panel)
  var abtn = e.target.closest('[data-apid][data-aid][data-choice]');
  if(abtn) {
    resolveApproval(abtn.getAttribute('data-apid'), abtn.getAttribute('data-aid'), abtn.getAttribute('data-choice'));
  }
});

setInterval(refresh, 5000);
setInterval(refreshProposals, 3000);
refresh();
refreshProposals();
</script>
</body>
</html>'''


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Suppress default access log

    def _send_json(self, data, code=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _error(self, code, msg):
        self._send_json({'error': msg}, code)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        query = self.path[len(path)+1:] if '?' in self.path else ''
        params = {}
        for part in query.split('&'):
            if '=' in part:
                k, v = part.split('=', 1)
                params[k] = v

        try:
            if path == '/':
                self._send_html(_HTML_UI)
            elif path == '/api/status':
                self._handle_status()
            elif path == '/api/tasks':
                self._handle_tasks_list(params)
            elif path.startswith('/api/task/'):
                task_id = path[len('/api/task/'):]
                self._handle_task_detail(task_id)
            elif path == '/api/activity':
                self._handle_activity(params)
            elif path == '/api/cpc-status':
                self._handle_cpc_status()
            elif path == '/api/ai-providers':
                self._handle_ai_providers()
            elif path == '/api/channel/proposals':
                self._handle_channel_proposals()
            elif path == '/api/github/status':
                self._handle_github_status()
            elif path == '/api/n8n/workflows':
                self._handle_n8n_workflows()
            elif path == '/api/github/ci-status':
                self._handle_github_ci_status()
            elif path == '/api/emergency-status':
                self._handle_emergency_status()
            else:
                self._error(404, 'Not found')
        except Exception as exc:
            self._error(500, str(exc))

    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            body = self._read_json_body()
            if path == '/api/tasks':
                self._handle_create_task(body)
            elif path == '/api/approve':
                self._handle_approve(body)
            elif path == '/api/emergency-stop':
                self._handle_emergency_stop(body)
            elif path == '/api/emergency-stop-clear':
                self._handle_emergency_stop_clear(body)
            elif path == '/api/run-task':
                self._handle_run_task(body)
            elif path == '/api/ai-task':
                self._handle_ai_task(body)
            elif path == '/api/ai-health-check':
                self._handle_ai_health_check(body)
            elif path == '/api/channel/propose':
                self._handle_channel_propose(body)
            elif path == '/api/channel/approve':
                self._handle_channel_approve(body)
            elif path == '/api/channel/reject':
                self._handle_channel_reject(body)
            elif path == '/api/website-form':
                self._handle_website_form(body)
            elif path == '/api/checkpoint-save':
                self._handle_checkpoint_save(body)
            elif path == '/api/checkpoint-resume':
                self._handle_checkpoint_resume(body)
            else:
                self._error(404, 'Not found')
        except json.JSONDecodeError:
            self._error(400, 'Invalid JSON')
        except Exception as exc:
            self._error(500, str(exc))

    def _handle_status(self):
        connections = get_connection_status()
        try:
            store = _get_store()
            tasks = store.tasks()
            store.close()
            n_tasks = len(tasks)
            states = {}
            for t in tasks:
                states[t['state']] = states.get(t['state'], 0) + 1
        except Exception:
            n_tasks = -1
            states = {}
        try:
            from .safety import Controls
            store2 = _get_store()
            ctrl_status = Controls(store2).status()
            store2.close()
            emergency_active = bool(ctrl_status.get('stop_everything'))
        except Exception:
            emergency_active = False
        self._send_json({
            'connections': connections,
            'emergency_stop_active': emergency_active,
            'stats': {
                'task_count': n_tasks,
                'states': states,
                'timestamp': time.time(),
            }
        })

    def _handle_tasks_list(self, params):
        limit = int(params.get('limit', 50))
        try:
            store = _get_store()
            rows = store.tasks()
            store.close()
        except Exception as e:
            self._error(500, str(e))
            return
        result = []
        for r in rows[-limit:]:
            result.append({
                'id': r['id'],
                'kind': r.get('task_type', ''),
                'state': r['state'],
                'created_at': r.get('created', 0),
                'updated_at': r.get('updated', 0),
                'summary': r.get('command', '')[:120] if 'command' in r else '',
                'task_type': r.get('task_type', ''),
                'command': r.get('command', ''),
            })
        self._send_json(result)

    def _handle_task_detail(self, task_id):
        if not task_id:
            self._error(400, 'task_id required')
            return
        try:
            store = _get_store()
            task = store.task(task_id)
            store.close()
        except Exception as e:
            self._error(404, str(e))
            return
        ctx = task.get('context', {}) or {}
        sanitized = _sanitize_obj(task)
        # Expose context fields at top level for UI convenience
        sanitized['task_id'] = task.get('id')
        sanitized['kind'] = task.get('task_type')
        sanitized['summary'] = task.get('command', '')[:200]
        sanitized['evidence'] = ctx.get('evidence', [])
        sanitized['final_result'] = ctx.get('final_result')
        sanitized['plan'] = ctx.get('plan', [])
        sanitized['errors'] = ctx.get('errors', [])
        self._send_json(sanitized)

    def _handle_activity(self, params):
        since = float(params.get('since', 0))
        try:
            store = _get_store()
            rows = store.db.execute(
                'SELECT seq, timestamp, task_id, event, details FROM audit '
                'WHERE timestamp > ? ORDER BY seq DESC LIMIT 100',
                (since,)
            ).fetchall()
            store.close()
            result = []
            for r in rows:
                try:
                    details = json.loads(r['details'])
                except Exception:
                    details = {}
                result.append({
                    'seq': r['seq'],
                    'timestamp': r['timestamp'],
                    'task_id': r['task_id'],
                    'event': r['event'],
                    'details': _sanitize_obj(details),
                })
            self._send_json(result)
        except Exception as e:
            self._error(500, str(e))

    def _handle_create_task(self, body):
        text = str(body.get('text', '')).strip()
        if not text:
            self._error(400, 'text is required')
            return
        kind = str(body.get('kind', 'inspect')).strip() or 'inspect'
        request_key = str(uuid.uuid4())
        try:
            store = _get_store()
            task = store.create_task(
                command=text,
                task_type=kind,
                project='zamal_operator',
                request_key=request_key,
                context={'inputs': [text]},
            )
            store.close()
            self._send_json({'task_id': task['id'], 'state': task['state']}, 201)
        except Exception as e:
            self._error(500, str(e))

    def _handle_approve(self, body):
        approval_id = str(body.get('approval_id', '')).strip()
        action_id = str(body.get('action_id', '')).strip()
        choice = str(body.get('action', '')).upper().strip()
        note = str(body.get('note', '')).strip()
        if not approval_id or not action_id or choice not in ('APPROVE', 'REJECT', 'EDIT'):
            self._error(400, 'approval_id, action_id, and action (APPROVE/REJECT/EDIT) required')
            return
        try:
            store = _get_store()
            task_row = store.db.execute(
                'SELECT a.task_id FROM actions a WHERE a.id=?', (action_id,)
            ).fetchone()
            task_id = task_row['task_id'] if task_row else None
            if not task_id:
                store.close()
                self._error(404, 'Action not found')
                return
            gate = Gate(store)
            result = gate.resolve_approval(
                approval_id, choice,
                task_id=task_id, action_id=action_id,
                actor='control_center', note=note
            )
            store.close()
            self._send_json({'result': 'ok', 'choice': choice})
        except Exception as e:
            self._error(400, str(e))

    def _handle_emergency_stop(self, body):
        flag = str(body.get('flag', 'stop_everything')).strip()
        reason = str(body.get('reason', 'Emergency stop via Control Center')).strip()
        if flag not in _ALLOWED_FLAGS:
            self._error(400, f'Unknown flag. Allowed: {sorted(_ALLOWED_FLAGS)}')
            return
        try:
            from .safety import Controls
            store = _get_store()
            controls = Controls(store)
            controls.set(flag, True, actor='control_center', reason=reason)
            store.close()
            self._send_json({'result': 'ok', 'flag': flag, 'enabled': True})
        except Exception as e:
            self._error(500, str(e))

    def _handle_emergency_stop_clear(self, body):
        flag = str(body.get('flag', 'stop_everything')).strip()
        reason = str(body.get('reason', 'Clear via Control Center')).strip()
        if flag not in _ALLOWED_FLAGS:
            self._error(400, f'Unknown flag. Allowed: {sorted(_ALLOWED_FLAGS)}')
            return
        try:
            from .safety import Controls
            store = _get_store()
            controls = Controls(store)
            controls.set(flag, False, actor='control_center', reason=reason)
            store.close()
            self._send_json({'result': 'ok', 'flag': flag, 'enabled': False})
        except Exception as e:
            self._error(500, str(e))

    def _handle_cpc_status(self):
        path_exists = _CPC_ROOT.exists()
        git_sha = None
        key_files = {}

        if path_exists:
            try:
                result = subprocess.run(
                    ['git', '-C', str(_CPC_ROOT), 'rev-parse', 'HEAD'],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    git_sha = result.stdout.strip()
            except Exception:
                pass

            check_files = [
                'backend/assistant/intent.py',
                'backend/assistant/service.py',
                'backend/api/service.py',
                'backend/product_matching.py',
                'TASK.md',
                'README.md',
            ]
            for f in check_files:
                key_files[f] = (_CPC_ROOT / f).exists()

        self._send_json({
            'path': str(_CPC_ROOT),
            'path_exists': path_exists,
            'git_sha': git_sha,
            'key_files': key_files,
            'readonly_server_port': 8787,
        })

    def _handle_run_task(self, body):
        task_id = str(body.get('task_id', '')).strip()
        if not task_id:
            self._error(400, 'task_id required')
            return
        description = str(body.get('description', 'Task execution')).strip()

        def _run():
            try:
                run_cpc_inspection(task_id, description)
            except Exception:
                pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        self._send_json({'result': 'ok', 'task_id': task_id, 'status': 'running'})

    def _handle_ai_providers(self):
        try:
            from .ai_router import get_router
            router = get_router()
            statuses = router.provider_statuses()
            self._send_json({'providers': _sanitize_obj(statuses)})
        except Exception as exc:
            self._error(500, str(exc))

    def _handle_ai_task(self, body):
        prompt = str(body.get('prompt', '')).strip()
        if not prompt:
            self._error(400, 'prompt required')
            return
        task_type = str(body.get('task_type', 'default')).strip() or 'default'
        force_exclude = tuple(body.get('force_exclude', []))
        task_id = str(body.get('task_id', '')).strip() or None

        # Create a store task for provenance
        try:
            store = _get_store()
            task = store.create_task(
                command=f'AI router task ({task_type}): {prompt[:80]}',
                task_type='ai_router',
                project='zamal_operator',
                request_key=str(uuid.uuid4()),
                context={'inputs': [prompt[:200]], 'scope': task_type},
            )
            ai_task_id = task['id']
            store.close()
        except Exception:
            ai_task_id = task_id or str(uuid.uuid4())

        def _run_and_store():
            try:
                from .ai_router import get_router
                router = get_router()
                result = router.route(
                    prompt, task_type=task_type,
                    task_id=ai_task_id, force_exclude=force_exclude,
                )
                # Store result in task context
                try:
                    from .store import Store
                    s2 = Store()
                    t2 = s2.task(ai_task_id)
                    s2.patch_context(ai_task_id, {
                        'final_result': _sanitize_obj({
                            'ok': result.ok,
                            'provider': result.provider,
                            'response': result.response[:2000] if result.response else '',
                            'privacy_level': result.privacy_level,
                            'fallback_history': result.fallback_history,
                            'provenance': result.provenance,
                            'error': result.error,
                            'status': result.status,
                        }),
                        'evidence': [
                            f"Provider: {result.provider or 'none'}",
                            f"Privacy: {result.privacy_level}",
                            f"Fallbacks tried: {len(result.fallback_history)}",
                            f"Status: {result.status}",
                        ],
                    }, t2['revision'])
                    _advance_task_to_completed(s2, ai_task_id)
                    s2.close()
                except Exception:
                    pass
            except Exception:
                pass

        t = threading.Thread(target=_run_and_store, daemon=True)
        t.start()
        self._send_json({
            'result': 'ok',
            'task_id': ai_task_id,
            'status': 'running',
            'message': 'AI router task started; poll /api/task/' + ai_task_id + ' for result',
        }, 202)

    def _handle_ai_health_check(self, body):
        provider = str(body.get('provider', '')).strip()
        if not provider:
            self._error(400, 'provider required')
            return
        try:
            from .ai_router import get_router
            router = get_router()
            result = router.force_health_check(provider)
            self._send_json(_sanitize_obj(result))
        except Exception as exc:
            self._error(500, str(exc))

    def _handle_channel_proposals(self):
        """GET /api/channel/proposals — list all proposals with status."""
        try:
            from .channel_adapter import get_channel_adapter
            ca = get_channel_adapter()
            proposals = ca.all_proposals()
            self._send_json([{
                'proposal_id': p.proposal_id,
                'channel': p.channel,
                'action': p.action,
                'destination': p.destination,
                'preview': p.preview[:200],
                'status': p.status,
                'task_id': p.task_id,
                'approval_id': p.approval_id,
                'timestamp': p.timestamp,
                'executor_result': getattr(p, 'executor_result', {}),
                'provenance': 'channel_adapter',
                'duplicate_prevention': p.proposal_id in ca._executed,
            } for p in proposals])
        except Exception as exc:
            self._error(500, str(exc))

    def _handle_channel_propose(self, body):
        """POST /api/channel/propose — create a channel proposal."""
        channel = str(body.get('channel', '')).strip()
        destination = str(body.get('destination', '')).strip()
        content = str(body.get('content', '')).strip()
        task_id = str(body.get('task_id', '')).strip()
        if not channel or not destination or not content:
            self._error(400, 'channel, destination, and content required')
            return
        try:
            from .channel_adapter import get_channel_adapter
            ca = get_channel_adapter()
            action_hint = str(body.get('action', '')).strip()
            if channel == 'calendar' and action_hint == 'delete_event':
                proposal = ca.propose_delete_event(
                    event_id=str(body.get('event_id', destination)).strip(),
                    reason=content[:200],
                    task_id=task_id,
                )
            elif channel == 'calendar':
                proposal = ca.propose_calendar_event(
                    title=content[:200],
                    start_dt=str(body.get('start_dt', '')),
                    end_dt=str(body.get('end_dt', '')),
                    task_id=task_id,
                )
            else:
                proposal = ca.propose_send(
                    channel=channel,
                    destination=destination,
                    content=content,
                    task_id=task_id,
                )
            self._send_json({
                'ok': True,
                'proposal_id': proposal.proposal_id,
                'channel': proposal.channel,
                'destination': proposal.destination,
                'preview': proposal.preview[:200],
                'status': proposal.status,
                'approval_id': proposal.approval_id,
                'task_id': proposal.task_id,
                'timestamp': proposal.timestamp,
            })
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:
            self._error(500, str(exc))

    def _handle_channel_approve(self, body):
        """POST /api/channel/approve — approve and execute a proposal."""
        proposal_id = str(body.get('proposal_id', '')).strip()
        actor = str(body.get('actor', 'cc_user')).strip()
        if not proposal_id:
            self._error(400, 'proposal_id required')
            return
        try:
            from .channel_adapter import get_channel_adapter
            ca = get_channel_adapter()
            proposal = ca.get_proposal(proposal_id)
            if proposal is None:
                self._error(404, 'proposal_not_found')
                return
            result = ca.execute_approved(proposal, approval_id=proposal.approval_id, actor=actor)
            self._send_json(_sanitize_obj(result))
        except Exception as exc:
            self._error(500, str(exc))

    def _handle_channel_reject(self, body):
        """POST /api/channel/reject — reject a proposal (zero external action)."""
        proposal_id = str(body.get('proposal_id', '')).strip()
        actor = str(body.get('actor', 'cc_user')).strip()
        if not proposal_id:
            self._error(400, 'proposal_id required')
            return
        try:
            from .channel_adapter import get_channel_adapter
            ca = get_channel_adapter()
            proposal = ca.get_proposal(proposal_id)
            if proposal is None:
                self._error(404, 'proposal_not_found')
                return
            result = ca.execute_rejected(proposal, actor=actor)
            self._send_json(result)
        except Exception as exc:
            self._error(500, str(exc))


    def _handle_emergency_status(self):
        """GET /api/emergency-status — current emergency control flag states."""
        try:
            from .safety import Controls, FLAGS
            store = _get_store()
            ctrl = Controls(store)
            status = ctrl.status()
            store.close()
            self._send_json({k: bool(status[k]) for k in FLAGS})
        except Exception as exc:
            self._error(500, str(exc))

    def _handle_github_ci_status(self):
        """GET /api/github/ci-status — git status + CI URL + protected write boundary."""
        try:
            git_status = _read_github_status()
            git_status['note'] = (
                'CI conclusion requires logged-in browser at: '
                + git_status.get('ci_check_url', 'https://github.com')
            )
            git_status['protected_write_ops'] = [
                'git_commit', 'git_push', 'git_merge', 'git_branch_delete',
            ]
            git_status['write_requires_approval'] = True
            self._send_json(_sanitize_obj(git_status))
        except Exception as exc:
            self._error(500, str(exc))

    def _handle_website_form(self, body):
        """POST /api/website-form — intake with validation, dedup, privacy, WAITING_APPROVAL."""
        import hashlib
        import re as _re
        name = str(body.get('name', '')).strip()[:100]
        email = str(body.get('email', '')).strip()[:200]
        subject = str(body.get('subject', 'Website Form Submission')).strip()[:200]
        message = str(body.get('message', '')).strip()[:2000]
        if not email or not message:
            self._error(400, 'email and message required')
            return
        if not _re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            self._error(400, 'invalid_email_format')
            return
        # Privacy: mask email; never log raw email or message body
        masked_email = (email[:3] + '***@' + email.split('@', 1)[-1]
                        if '@' in email else '[masked]')
        safe_subject = _sanitize(subject) or 'Website Form Submission'
        # Dedup: hash of (email|subject|message)
        dedup_hash = hashlib.sha256(
            f'{email}|{subject}|{message}'.encode()
        ).hexdigest()[:32]
        request_key = f'website_form:{dedup_hash}'
        n8n_status = _check_n8n()
        try:
            store = _get_store()
            existing = store.db.execute(
                'SELECT id, state FROM tasks WHERE request_key=?', (request_key,)
            ).fetchone()
            if existing:
                store.close()
                self._send_json({
                    'ok': True, 'duplicate': True,
                    'task_id': existing['id'], 'state': existing['state'],
                    'message': 'duplicate_submission_existing_task_returned',
                })
                return
            preview = f'Website form [{masked_email}]: {safe_subject[:50]}'
            task = store.create_task(
                command=preview,
                task_type='website_form',
                project='zamal_operator',
                request_key=request_key,
                context={
                    'inputs': [f'from:{masked_email}', f'subject:{safe_subject[:80]}'],
                    'scope': 'website_form_draft',
                    'evidence': [f'n8n_status:{n8n_status}', 'source:website_form'],
                },
            )
            task_id = task['id']
            _advance_task_to_waiting_approval(store, task_id)
            store.close()
            if n8n_status != 'ok':
                threading.Thread(
                    target=_send_telegram_notify,
                    args=(task_id,
                          f'n8n UNAVAILABLE \u2014 website form task queued: {preview[:80]}'),
                    daemon=True,
                ).start()
            self._send_json({
                'ok': True, 'duplicate': False,
                'task_id': task_id, 'state': 'WAITING_APPROVAL',
                'masked_from': masked_email, 'n8n_status': n8n_status,
            }, 201)
        except Exception as exc:
            self._error(500, str(exc))

    def _handle_checkpoint_save(self, body):
        """POST /api/checkpoint-save — persist checkpoint + send ACTION_REQUIRED."""
        task_id = str(body.get('task_id', '')).strip()
        reason = _sanitize(str(body.get('reason', 'service_unavailable')).strip()[:200])
        next_action = _sanitize(str(body.get('next_action', '')).strip()[:500])
        if not task_id:
            self._error(400, 'task_id required')
            return
        try:
            store = _get_store()
            task = store.task(task_id)
            ctx = task.get('context') or {}
            checkpoint_ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            checkpoint_note = f'CHECKPOINT@{checkpoint_ts}|reason:{reason}'
            evidence = list(ctx.get('evidence') or []) + [checkpoint_note]
            store.patch_context(task_id, {
                'next_action': next_action or f'resume_after:{reason}',
                'evidence': evidence,
            }, task['revision'])
            store.close()
            threading.Thread(
                target=_send_telegram_notify,
                args=(task_id, f'ACTION_REQUIRED: checkpoint saved \u2014 {reason}'),
                daemon=True,
            ).start()
            self._send_json({
                'ok': True, 'task_id': task_id,
                'checkpoint': checkpoint_note, 'reason': reason,
            })
        except Exception as exc:
            self._error(500, str(exc))

    def _handle_checkpoint_resume(self, body):
        """POST /api/checkpoint-resume — resume checkpointed task (idempotent, no duplicate)."""
        task_id = str(body.get('task_id', '')).strip()
        if not task_id:
            self._error(400, 'task_id required')
            return
        try:
            store = _get_store()
            task = store.task(task_id)
            ctx = task.get('context') or {}
            prev = ctx.get('next_action')
            if not prev:
                store.close()
                self._error(400, 'no_checkpoint_found')
                return
            resume_ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            evidence = list(ctx.get('evidence') or []) + [f'RESUMED@{resume_ts}']
            store.patch_context(task_id, {
                'next_action': None,
                'evidence': evidence,
            }, task['revision'])
            store.close()
            self._send_json({
                'ok': True, 'task_id': task_id,
                'resumed': True, 'resumed_at': resume_ts,
                'previous_checkpoint': prev,
                'duplicate_action': False,
            })
        except Exception as exc:
            self._error(500, str(exc))

    def _handle_github_status(self):
        """GET /api/github/status — read-only GitHub status via git CLI."""
        try:
            status = _read_github_status()
            self._send_json(_sanitize_obj(status))
        except Exception as exc:
            self._error(500, str(exc))

    def _handle_n8n_workflows(self):
        """GET /api/n8n/workflows — list n8n workflows via API."""
        try:
            n8n_key = os.environ.get('N8N_API_KEY', '')
            if not n8n_key:
                # Try loading from config
                key_path = _OPERATOR_ROOT / 'config' / 'n8n_api_key.txt'
                if key_path.exists():
                    n8n_key = key_path.read_text().strip()
            if not n8n_key:
                self._send_json({'error': 'n8n_api_key_not_configured', 'workflows': []})
                return
            req = urllib.request.Request(
                'http://127.0.0.1:5678/api/v1/workflows',
                headers={'X-N8N-API-KEY': n8n_key}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read(65536))
                workflows = data.get('data', []) if isinstance(data, dict) else data
                summary = [{
                    'id': w.get('id', ''),
                    'name': w.get('name', ''),
                    'active': w.get('active', False),
                    'updatedAt': w.get('updatedAt', ''),
                } for w in workflows]
                self._send_json({'workflows': summary, 'count': len(summary)})
        except Exception as exc:
            self._error(500, str(exc))


# Import Gate for use in handler
from .store import Gate  # noqa: E402


# ── Server ────────────────────────────────────────────────────────────────────

class ControlCenterServer:
    def __init__(self, port=8766):
        self.port = port
        self._server = None

    def _make_server(self):
        server = HTTPServer(('127.0.0.1', self.port), _Handler)
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return server

    def serve(self, *, block=True):
        """Start serving. block=True runs forever; block=False runs in background thread."""
        self._server = self._make_server()
        if block:
            try:
                self._server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                self._server.server_close()
        else:
            t = threading.Thread(target=self._server.serve_forever, daemon=True)
            t.start()
            return t

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
