"""Persistent, trusted-local emergency control plane and execution checks."""
import time
from .store import Denied

FLAGS = frozenset({'stop_everything', 'pause_external_actions', 'read_only_mode',
                   'disable_github_push', 'disable_email_send'})
READ_ONLY = frozenset({'read', 'search', 'inspect', 'analyze', 'research', 'plan',
                       'compare', 'diagnose', 'verify',
                       # Phase 6 read-only
                       'service_status', 'browser_open', 'terminal_run', 'permission_check',
                       # Phase 7 read-only
                       'cpc_plan', 'cpc_snapshot', 'cpc_scope_check',
                       # Phase 8 read-only
                       'git_capture', 'diff_review', 'capture_evidence',
                       # Phase 10 read-only
                       'connection_status', 'event_inspect',
                       # Phase 12 read-only
                       'monitor_sweep', 'security_audit', 'usage_summary', 'output_list',
                       # Phase 13 read-only
                       'automation_observe', 'automation_list'})
EXTERNAL = frozenset({'email_send', 'job_submit', 'profile_mutate', 'git_push',
                      'git_merge', 'deploy', 'payment', 'credential_change',
                      'security_change', 'external_irreversible', 'ai_inference'})


class Controls:
    def __init__(self, store):
        self.store = store

    def status(self):
        row = self.store.db.execute('SELECT * FROM controls WHERE id=1').fetchone()
        if row is None:
            raise Denied('Emergency control record unavailable')
        state = dict(row)
        if any(type(state.get(flag)) is not int or state[flag] not in (0, 1)
               or type(state.get(flag + '_epoch')) is not int or state[flag + '_epoch'] < 0
               for flag in FLAGS):
            raise Denied('Emergency control state invalid')
        return state

    def set(self, flag, enabled, *, actor, reason):
        # Local human control plane only. Never expose disabling to provider tools.
        if flag not in FLAGS or type(enabled) is not bool or not actor or not reason:
            raise ValueError('Known flag, boolean, actor and reason required')
        with self.store.transaction():
            previous = self.status()
            # Epochs are latched: switching ON then OFF still cancels existing work.
            epoch = previous[flag + '_epoch'] + int(enabled and not previous[flag])
            self.store.db.execute(f'UPDATE controls SET {flag}=?,{flag}_epoch=?,revision=revision+1 WHERE id=1', (int(enabled), epoch))
            self.store.db.execute('INSERT INTO control_audit(timestamp,flag,enabled,actor,reason) VALUES(?,?,?,?,?)',
                                  (time.time(), flag, int(enabled), actor, reason))
        return self.status()

    def check(self, kind, baseline=None):
        state = self.status()
        read_only, external = READ_ONLY, EXTERNAL
        if isinstance(kind,str) and kind.startswith('ops_'):
            from .personal_ops import _PHASE14_READ_ONLY, _PHASE14_PROTECTED
            read_only = read_only | _PHASE14_READ_ONLY
            external = external | _PHASE14_PROTECTED
        if isinstance(kind,str) and kind.startswith('biz_'):
            from .business_ops import SAFE, PROTECTED
            read_only = read_only | (SAFE - {'biz_job_prepare','biz_job_track'})
            external = external | PROTECTED
        relevant = ['stop_everything']
        if kind not in read_only:
            relevant.append('read_only_mode')
        if kind in external:
            relevant.append('pause_external_actions')
        if kind in ('git_push','biz_github_write'):
            relevant.append('disable_github_push')
        if kind in ('email_send', 'ops_calendar_external_send', 'ops_content_publish', 'ops_admin_submit','biz_email_send','biz_email_forward','biz_email_delete'):
            relevant.append('disable_email_send')
        for flag in relevant:
            if state[flag] or (baseline is not None and state[flag + '_epoch'] != baseline[flag + '_epoch']):
                raise Denied('Emergency control blocks execution: ' + flag)
        return state

    def history(self):
        return [dict(r) for r in self.store.db.execute('SELECT * FROM control_audit ORDER BY seq')]
