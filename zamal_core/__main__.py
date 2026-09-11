"""Local trusted control-plane CLI. Never expose this command to an AI shell tool."""
import argparse
import getpass
import json
import sys
from .store import Store, Gate, Conflict, Denied
from .safety import Controls, FLAGS
from .worker import LocalWorker
from .context_bus import ContextBus
from .providers import existing_registry
from .routing import RoutingPolicy
from .handoff import HandoffEngine
from .intake import Intake, IntakeError, normalize_issue


def main():
    parser = argparse.ArgumentParser(description='Zamal persistent task and approval control plane')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    sub.add_parser('tasks')
    sub.add_parser('approvals')
    sub.add_parser('connections')
    p = sub.add_parser('event-receive')
    p.add_argument('--type', required=True)
    p.add_argument('--payload', required=True)
    p.add_argument('--key', required=True)
    p = sub.add_parser('gateway-serve')
    p.add_argument('--port', type=int, default=8788)
    p = sub.add_parser('approval')
    p.add_argument('id')
    p = sub.add_parser('resolve-approval')
    p.add_argument('id')
    p.add_argument('choice', choices=['APPROVE', 'EDIT', 'REJECT'])
    p.add_argument('--task-id', required=True)
    p.add_argument('--action-id', required=True)
    p.add_argument('--note', default='')
    sub.add_parser('controls')
    sub.add_parser('ai-providers')
    p = sub.add_parser('ai-context')
    p.add_argument('id')
    p.add_argument('--max-bytes', type=int, default=200000)
    p = sub.add_parser('ai-events')
    p.add_argument('id')
    p.add_argument('--after', type=int, default=0)
    p = sub.add_parser('ai-run')
    p.add_argument('id')
    for name in ('ai-route', 'ai-prepare'):
        p = sub.add_parser(name)
        p.add_argument('id')
        p.add_argument('--allow', default='openai,codex,claude,gemini')
        if name == 'ai-prepare':
            p.add_argument('--key', required=True)
    p = sub.add_parser('control')
    p.add_argument('flag', choices=sorted(FLAGS))
    p.add_argument('value', choices=['on', 'off'])
    p.add_argument('--reason', required=True)
    p = sub.add_parser('execute')
    p.add_argument('id')
    p = sub.add_parser('create')
    p.add_argument('--text', required=True)
    p.add_argument('--type', required=True)
    p.add_argument('--project', required=True)
    p.add_argument('--key', required=True)
    p = sub.add_parser('show')
    p.add_argument('id')
    p = sub.add_parser('transition')
    p.add_argument('id')
    p.add_argument('state', choices=['UNDERSTOOD', 'PLANNED', 'COMPLETED'])
    p.add_argument('--revision', type=int, required=True)
    p = sub.add_parser('context')
    p.add_argument('id')
    p.add_argument('--json', required=True)
    p.add_argument('--revision', type=int, required=True)
    p = sub.add_parser('propose')
    p.add_argument('id')
    p.add_argument('kind')
    p.add_argument('--payload', required=True)
    p.add_argument('--key', required=True)
    p = sub.add_parser('decide')
    p.add_argument('id')
    p.add_argument('choice', choices=['APPROVE', 'EDIT', 'REJECT'])
    p.add_argument('--note', default='')
    p = sub.add_parser('backup')
    p.add_argument('destination')
    # Phase 5: intake commands
    p = sub.add_parser('intake-screenshot')
    p.add_argument('id')
    p.add_argument('--revision', type=int, required=True)
    p.add_argument('--file', required=True)
    p.add_argument('--label', default='current')
    p = sub.add_parser('intake-images')
    p.add_argument('id')
    p.add_argument('--revision', type=int, required=True)
    p.add_argument('--files', required=True, help='JSON array: [{"file":"a.png","label":"current"},...]')
    p = sub.add_parser('intake-document')
    p.add_argument('id')
    p.add_argument('--revision', type=int, required=True)
    p.add_argument('--file', required=True)
    p.add_argument('--description', default='')
    p = sub.add_parser('intake-url')
    p.add_argument('id')
    p.add_argument('--revision', type=int, required=True)
    p.add_argument('--url', required=True)
    p.add_argument('--description', default='')
    p = sub.add_parser('intake-voice')
    p.add_argument('id')
    p.add_argument('--revision', type=int, required=True)
    p.add_argument('--file', required=True)
    p.add_argument('--transcript', default=None, help='Manual transcript; skips STT')
    p = sub.add_parser('normalize-issue')
    p.add_argument('--text', required=True)
    # Phase 6: direct SAFE shortcuts (no task/approval machinery needed)
    p = sub.add_parser('mac-service-status')
    p.add_argument('service')
    p = sub.add_parser('mac-permission')
    p.add_argument('permission')
    # Phase 7: CPC pipeline shortcuts
    p = sub.add_parser('cpc-session')
    p.add_argument('id', help='session ID')
    p = sub.add_parser('cpc-sessions')
    p.add_argument('--task', default=None, help='Filter by task ID')
    p = sub.add_parser('cpc-plan')
    # Phase 8: verification shortcuts
    p = sub.add_parser('ver-snapshots')
    p.add_argument('--task', required=True, help='Task ID to list git snapshots for')
    p = sub.add_parser('ver-records')
    p.add_argument('--session', required=True, help='Session ID to list verification records for')
    p = sub.add_parser('ver-worktrees')
    p.add_argument('--task', default=None, help='Filter by task ID')
    # Phase 11: Telegram remote gateway
    sub.add_parser('telegram-status')
    p = sub.add_parser('telegram-poll')
    p.add_argument('--max-iterations', type=int, default=None, help='Limit poll iterations (tests/debug)')
    # Phase 12: monitoring, security, usage, output retention
    sub.add_parser('monitor-sweep')
    sub.add_parser('monitor-security')
    p = sub.add_parser('monitor-usage')
    p.add_argument('--since-hours', type=float, default=None)
    sub.add_parser('monitor-output-list')
    p = sub.add_parser('monitor-output-purge')
    p.add_argument('id')
    p.add_argument('--sha256', required=True)
    p.add_argument('--reason', required=True)
    # Phase 13: automation discovery
    p = sub.add_parser('automation-observe')
    p.add_argument('--since-hours', type=float, default=None)
    p = sub.add_parser('automation-list')
    p.add_argument('--status', default=None)
    args = parser.parse_args()
    s = Store()
    try:
        if args.command == 'init':
            result = {'database': str(s.path), 'schema_version': s.db.execute('PRAGMA user_version').fetchone()[0], 'integrity': s.db.execute('PRAGMA integrity_check').fetchone()[0]}
        elif args.command == 'connections':
            from .connection_registry import ConnectionRegistry
            result = ConnectionRegistry(store=s).status()
        elif args.command == 'event-receive':
            from .gateway import InboundEventGateway
            result = InboundEventGateway(s).receive(args.type,'cli',json.loads(args.payload),args.key)
        elif args.command == 'gateway-serve':
            from .webhook_gateway import serve
            serve(s.path,args.port)
            result = {'outcome':'STOPPED'}
        elif args.command == 'controls':
            result = {'state': Controls(s).status(), 'history': Controls(s).history()}
        elif args.command == 'control':
            result = Controls(s).set(args.flag, args.value == 'on', actor='local-os:' + getpass.getuser(), reason=args.reason)
        elif args.command == 'execute':
            from .mac_worker import _PHASE6_KINDS
            from .cpc_worker import _PHASE7_KINDS
            from .verifier import VerifierWorker, _PHASE8_KINDS
            action_kind = s.action(args.id)['kind']
            from .gateway import GatewayWorker, _PHASE10_KINDS
            from .personal_ops import PersonalOpsWorker, _PHASE14_KINDS
            if action_kind.startswith('biz_'):
                from .business_ops import BusinessWorker
                result = BusinessWorker(s).gate().execute(args.id)
            elif action_kind in _PHASE14_KINDS:
                result = PersonalOpsWorker(s).gate().execute(args.id)
            elif action_kind in _PHASE10_KINDS:
                result = GatewayWorker(s).gate().execute(args.id)
            elif action_kind in _PHASE8_KINDS:
                result = VerifierWorker(s).gate().execute(args.id)
            elif action_kind in _PHASE7_KINDS:
                from .cpc_worker import CpcWorker
                result = CpcWorker(s).gate().execute(args.id)
            elif action_kind in _PHASE6_KINDS:
                from .mac_worker import MacWorker
                result = MacWorker(s).gate().execute(args.id)
            elif action_kind == 'ai_inference':
                result = HandoffEngine(s, existing_registry()).gate().execute(args.id)
            else:
                result = LocalWorker(s).gate().execute(args.id)
        elif args.command == 'ai-providers':
            result = existing_registry().status()
        elif args.command == 'ai-context':
            result = ContextBus(s).snapshot(args.id, max_bytes=args.max_bytes).data
        elif args.command == 'ai-events':
            result = ContextBus(s).events(args.id, args.after)
        elif args.command in ('ai-route', 'ai-prepare'):
            engine = HandoffEngine(s, existing_registry())
            policy = RoutingPolicy(allowed_providers=tuple(args.allow.split(',')))
            result = engine.preview(args.id, policy) if args.command == 'ai-route' else engine.prepare(args.id, args.key, policy)
        elif args.command == 'ai-run':
            result = HandoffEngine(s, existing_registry()).get(args.id)
        elif args.command == 'tasks':
            result = s.tasks()
        elif args.command == 'approval':
            result = s.approval(args.id)
        elif args.command == 'resolve-approval':
            from .verifier import VerifierWorker
            action = s.action(args.action_id)
            from .gateway import GatewayWorker
            from .personal_ops import PersonalOpsWorker
            from .business_ops import BusinessWorker
            gate = BusinessWorker(s).gate() if action['kind'].startswith('biz_') else HandoffEngine(s, existing_registry()).gate() if action['kind'] == 'ai_inference' else (PersonalOpsWorker(s).gate() if action['kind'].startswith('ops_') else GatewayWorker(s).gate())
            result = gate.resolve_approval(args.id, args.choice, task_id=args.task_id, action_id=args.action_id, actor='local-os:' + getpass.getuser(), note=args.note)
        elif args.command == 'approvals':
            result = s.approval_queue()
        elif args.command == 'create':
            result = s.create_task(args.text, args.type, args.project, args.key)
        elif args.command == 'show':
            result = s.task(args.id)
        elif args.command == 'transition':
            result = s.transition(args.id, args.state, args.revision)
        elif args.command == 'context':
            result = s.patch_context(args.id, json.loads(args.json), args.revision)
        elif args.command == 'propose':
            from .verifier import VerifierWorker
            from .gateway import GatewayWorker
            from .personal_ops import PersonalOpsWorker
            from .business_ops import BusinessWorker
            worker = BusinessWorker(s) if args.kind.startswith('biz_') else PersonalOpsWorker(s) if args.kind.startswith('ops_') else GatewayWorker(s)
            result = worker.gate().propose(args.id, args.kind, json.loads(args.payload), args.key)
        elif args.command == 'decide':
            result = s.decide(args.id, args.choice, actor='local-os:' + getpass.getuser(), note=args.note)
        elif args.command == 'backup':
            result = {'backup': s.backup(args.destination)}
        elif args.command == 'intake-screenshot':
            result = Intake(s).record_screenshot(args.id, args.revision, args.file, args.label)
        elif args.command == 'intake-images':
            result = Intake(s).record_images(args.id, args.revision, json.loads(args.files))
        elif args.command == 'intake-document':
            result = Intake(s).record_document(args.id, args.revision, args.file, args.description)
        elif args.command == 'intake-url':
            result = Intake(s).record_url(args.id, args.revision, args.url, args.description)
        elif args.command == 'intake-voice':
            result = Intake(s).record_voice(args.id, args.revision, args.file, args.transcript)
        elif args.command == 'normalize-issue':
            result = normalize_issue(args.text)
        elif args.command == 'mac-service-status':
            from .mac_worker import MacWorker
            result = MacWorker(s).process_ctrl.status(args.service)
        elif args.command == 'mac-permission':
            from .mac_worker import TccPermissionGate
            result = TccPermissionGate().check(args.permission)
        elif args.command == 'cpc-session':
            from .cpc_worker import CpcSessionStore
            result = CpcSessionStore(s).session(args.id)
        elif args.command == 'cpc-sessions':
            if args.task:
                row = s.db.execute(
                    "SELECT * FROM cpc_coding_sessions WHERE task_id=?", (args.task,)
                ).fetchall()
            else:
                row = s.db.execute(
                    "SELECT * FROM cpc_coding_sessions ORDER BY created DESC LIMIT 50"
                ).fetchall()
            result = [dict(r) for r in row]
        elif args.command == 'cpc-plan':
            from .cpc_worker import CpcTaskPlanner
            result = CpcTaskPlanner().plan()
        elif args.command == 'ver-snapshots':
            from .verifier import GitSnapshotStore
            result = GitSnapshotStore(s).list_for_task(args.task)
        elif args.command == 'ver-records':
            from .verifier import VerificationRecordStore
            store = VerificationRecordStore(s)
            rows = s.db.execute(
                'SELECT * FROM verification_records WHERE session_id=? ORDER BY verified_at',
                (args.session,)
            ).fetchall()
            result = [store._decode(r) for r in rows]
        elif args.command == 'ver-worktrees':
            if args.task:
                rows = s.db.execute(
                    'SELECT * FROM task_worktrees WHERE task_id=? ORDER BY created DESC',
                    (args.task,)
                ).fetchall()
            else:
                rows = s.db.execute(
                    'SELECT * FROM task_worktrees ORDER BY created DESC LIMIT 50'
                ).fetchall()
            result = [dict(r) for r in rows]
        elif args.command == 'telegram-status':
            from .telegram_bot import TelegramBot
            result = TelegramBot(s).health()
        elif args.command == 'telegram-poll':
            from .telegram_bot import TelegramBot
            TelegramBot(s).poll(max_iterations=args.max_iterations)
            result = {'outcome': 'STOPPED'}
        elif args.command == 'monitor-sweep':
            from .monitor import ServiceMonitor
            result = ServiceMonitor(s).sweep()
        elif args.command == 'monitor-security':
            from .monitor import SecurityAuditor
            result = SecurityAuditor(s).full_audit()
        elif args.command == 'monitor-usage':
            from .monitor import UsageTracker
            result = UsageTracker(s).summary(since_hours=args.since_hours)
        elif args.command == 'monitor-output-list':
            from .monitor import OutputReaper
            result = {'outcome': 'SUCCESS', 'ok': True,
                      'eligible': OutputReaper(s).list_eligible()}
        elif args.command == 'monitor-output-purge':
            from .monitor import OutputReaper
            result = OutputReaper(s).purge(
                args.id, actor='local-os:' + getpass.getuser(),
                reason=args.reason, sha256=args.sha256)
        elif args.command == 'automation-observe':
            from .automation import AutomationEngine
            result = AutomationEngine(s).observe_and_propose(since_hours=args.since_hours)
        elif args.command == 'automation-list':
            from .automation import AutomationEngine
            result = AutomationEngine(s).list_proposals(status=args.status)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, Conflict, Denied, IntakeError) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 2
    except RuntimeError:
        print(json.dumps({'error': 'Provider run blocked; inspect persistent AI events before retry'}), file=sys.stderr)
        return 2
    except OSError:
        print(json.dumps({'error': 'Local operation unavailable; no automatic retry'}), file=sys.stderr)
        return 2
    finally:
        s.close()


if __name__ == '__main__':
    raise SystemExit(main())
