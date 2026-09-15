"""Durable checkpoint claims; session probes never read private content."""
import time

from .store import Conflict, digest
from .safety import Controls


def save(store, task_id, reason, next_action, required_session):
    key = digest([task_id, reason, next_action, required_session])
    for _ in range(5):
        task = store.task(task_id)
        ctx = task['context']
        previous = ctx.get('checkpoint')
        if previous and previous['key'] == key and previous['pending']:
            return previous, False
        if previous and previous['pending']:
            raise Conflict('Resolve the existing checkpoint before replacing it')
        note = f'CHECKPOINT@{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}|reason:{reason}'
        checkpoint = dict(key=key, pending=True, required_session=required_session,
                          note=note, notification_claimed=True)
        try:
            store.patch_context(task_id, {
                'checkpoint': checkpoint, 'next_action': next_action,
                'evidence': list(ctx.get('evidence') or []) + [note],
            }, task['revision'])
            return checkpoint, True
        except Conflict:
            if store.task(task_id)['revision'] == task['revision']:
                raise
    raise Conflict('Concurrent checkpoint update; retry the same request')


def session_ready(session):
    if session is None or session == 'local':
        return True
    # Missing/unknown targets fail closed. Never infer readiness from an installed
    # browser, configured credentials or cached provider availability.
    from .ai_browser_transport import PROVIDER_CONFIGS, _exec_js_in_domain, _parse_login_result
    session_markers = {
        'linkedin': ('linkedin.com', '.global-nav__me'),
        'qatar_living': ('qatarliving.com', 'a[href*="logout"]'),
        'gmail': ('mail.google.com', '[role="main"]'),
        'whatsapp': ('web.whatsapp.com', '#pane-side'),
        'instagram': ('instagram.com', 'a[href="/direct/inbox/"]'),
        'messenger': ('facebook.com', '[aria-label="Chats"]'),
    }
    config = PROVIDER_CONFIGS.get(session)
    if session in session_markers:
        domain, selector = session_markers[session]
        import json
        config = {'domain': domain, 'login_js':
                  'JSON.stringify({logged_in:!!document.querySelector(' + json.dumps(selector) + ')})'}
    if not config:
        return False
    domain = config['domain']
    challenge_js = '''JSON.stringify({blocked:!!document.querySelector(
      'input[type="password"],input[autocomplete="one-time-code"],iframe[src*="captcha"],iframe[src*="challenge"],input[name="otp"]')
      || /login|signin|challenge|captcha|verify/.test(location.pathname.toLowerCase())})'''
    ok, out = _exec_js_in_domain(domain, challenge_js, timeout=5)
    import json
    try:
        if not ok or json.loads(out).get('blocked') is not False:
            return False
    except (ValueError, AttributeError, TypeError):
        return False
    ok, out = _exec_js_in_domain(domain, config['login_js'], timeout=5)
    return bool(ok and _parse_login_result(out))


def resume(store, task_id, health_check=session_ready):
    task = store.task(task_id)
    ctx = task['context']
    checkpoint = ctx.get('checkpoint')
    if not checkpoint or not checkpoint.get('pending'):
        raise Conflict('no_checkpoint_found')
    baseline = Controls(store).check('read')
    try:
        ready = health_check(checkpoint['required_session'])
    except Exception:
        ready = False
    if ready is not True:
        return {'ok': False, 'task_id': task_id, 'resumed': False,
                'status': 'SESSION_REQUIRED', 'action_required': True}
    # Recheck emergency controls after the bounded probe. The revision check
    # prevents a stale probe from clearing a newer checkpoint.
    Controls(store).check('read', baseline)
    store.patch_context(task_id, {
        'checkpoint': {**checkpoint, 'pending': False}, 'next_action': None,
        'evidence': list(ctx.get('evidence') or []) + ['RESUMED'],
    }, task['revision'])
    return {'ok': True, 'task_id': task_id, 'resumed': True,
            'previous_checkpoint': ctx.get('next_action'), 'duplicate_action': False}
