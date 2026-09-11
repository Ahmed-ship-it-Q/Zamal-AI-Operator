"""Exercise the real CLI in a copied isolated core, never the production database."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zamal-cli-test-')
        self.root = Path(self.tmp.name)
        shutil.copytree(Path(__file__).resolve().parents[1] / 'zamal_core', self.root / 'zamal_core', ignore=shutil.ignore_patterns('__pycache__'))

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, expected=0):
        env = {**os.environ, 'PYTHONPATH': str(self.root), 'PYTHONDONTWRITEBYTECODE': '1'}
        r = subprocess.run([sys.executable, '-m', 'zamal_core', *args], cwd=self.root, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, expected, r.stderr)
        return json.loads(r.stdout if expected == 0 else r.stderr)

    def planned(self):
        t = self.cli('create', '--text', 'draft fixture', '--type', 'draft', '--project', 'local', '--key', 'cli-test')
        self.assertEqual(self.cli('show', t['id'])['command'], 'draft fixture')
        for state in ('UNDERSTOOD', 'PLANNED'):
            t = self.cli('transition', t['id'], state, '--revision', str(t['revision']))
        return t

    def test_cli_persisted_approval_lifecycle_and_backup(self):
        self.assertEqual(self.cli('init')['integrity'], 'ok')
        t = self.planned()
        t = self.cli('context', t['id'], '--json', '{"next_action":"prepare fixture"}', '--revision', str(t['revision']))
        a = self.cli('propose', t['id'], 'email_send', '--payload', '{"to":"fixture","body":"test"}', '--key', 'a')
        self.assertEqual(len(self.cli('approvals')), 1)
        result = self.cli('decide', a['approval']['id'], 'APPROVE')
        self.assertEqual(result['approval']['status'], 'APPROVED')
        self.assertTrue(result['approval']['actor'].startswith('local-os:'))
        self.assertEqual(self.cli('tasks')[0]['state'], 'APPROVED')
        self.assertEqual(self.cli('approvals'), [])
        backup = self.root / 'backup.sqlite3'
        self.assertEqual(self.cli('backup', str(backup))['backup'], str(backup))
        self.assertTrue(backup.exists())

    def test_cli_rejects_unknown_action_and_conversational_choice(self):
        t = self.planned()
        self.assertIn('Unknown action', self.cli('propose', t['id'], 'shell', '--payload', '{}', '--key', 'bad', expected=2)['error'])
        a = self.cli('propose', t['id'], 'git_push', '--payload', '{}', '--key', 'a')
        self.assertIn('EDIT requires', self.cli('decide', a['approval']['id'], 'EDIT', expected=2)['error'])
        self.cli('decide', a['approval']['id'], 'REJECT')
        self.assertEqual(self.cli('show', t['id'])['state'], 'REJECTED')

    def test_worker_execution_and_emergency_cli(self):
        t = self.cli('create', '--text', 'worker fixture', '--type', 'local', '--project', 'operator', '--key', 'worker')
        for state in ('UNDERSTOOD', 'PLANNED'):
            t = self.cli('transition', t['id'], state, '--revision', str(t['revision']))
        a = self.cli('propose', t['id'], 'draft', '--payload', '{"workspace":"operator","name":"cli.md","content":"fixture"}', '--key', 'a')
        self.cli('control', 'stop_everything', 'on', '--reason', 'CLI fixture')
        self.assertEqual(self.cli('controls')['state']['stop_everything'], 1)
        self.assertIn('Emergency control', self.cli('execute', a['id'], expected=2)['error'])
        self.cli('control', 'stop_everything', 'off', '--reason', 'Resume fixture')
        result = self.cli('execute', a['id'])
        self.assertEqual((self.root / result['path']).read_text(), 'fixture')
        self.assertEqual(self.cli('show', t['id'])['state'], 'VERIFYING')
        self.assertIn('already claimed', self.cli('execute', a['id'], expected=2)['error'])

    def test_context_and_route_cli_do_not_send_or_approve(self):
        knowledge = self.root / 'knowledge'
        knowledge.mkdir()
        (knowledge / 'OPERATOR_RULES.md').write_text('Preserve scope. Approval required.')
        t = self.cli('create', '--text', 'Phase 4 CLI fixture', '--type', 'local', '--project', 'operator', '--key', 'phase4')
        for state in ('UNDERSTOOD', 'PLANNED'):
            t = self.cli('transition', t['id'], state, '--revision', str(t['revision']))
        context = self.cli('ai-context', t['id'])
        self.assertEqual(context['context_id'], 'task:' + t['id'])
        report = self.cli('ai-route', t['id'], '--allow', 'codex')
        self.assertEqual(set(report), {'planner', 'coder', 'verifier'})
        run = self.cli('ai-prepare', t['id'], '--key', 'one', '--allow', 'codex')
        self.assertEqual(self.cli('ai-run', run['id'])['id'], run['id'])
        self.assertEqual(self.cli('approvals')[0]['status'], 'PENDING')
        self.assertIn('approval required', self.cli('execute', run['action_id'], expected=2)['error'])
        self.assertEqual(self.cli('show', t['id'])['state'], 'PLANNED')
        self.assertTrue(self.cli('ai-events', t['id']))
