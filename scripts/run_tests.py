#!/usr/bin/env python3
"""Run unittest fixtures with production storage and external delivery isolated."""
import contextlib
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from zamal_core.store import Store
    from zamal_core import control_center as cc
    (ROOT / 'outbox').mkdir(exist_ok=True)
    original_init = Store.__init__
    original_connect = socket.socket.connect
    with tempfile.TemporaryDirectory(prefix='step5-tests-', dir=ROOT / 'outbox') as tmp, contextlib.ExitStack() as stack:
        root = Path(tmp)
        def isolated_init(self, path=None):
            if path is None:
                path = root / 'operator.sqlite3'
            elif str(path) == ':memory:':
                path = root / (str(uuid.uuid4()) + '.sqlite3')
            original_init(self, path)
        def fixture_connect(sock, address):
            if isinstance(address, tuple):
                host, port = address[:2]
                if host not in ('127.0.0.1', '::1', 'localhost') or port in (5678, 8766, 8787):
                    raise OSError('External network disabled in automated tests')
            return original_connect(sock, address)
        stack.enter_context(patch.object(Store, '__init__', isolated_init))
        stack.enter_context(patch.object(socket.socket, 'connect', fixture_connect))
        stack.enter_context(patch.object(cc, '_CPC_ROOT', root / 'cpc-fixture'))
        # Preserve real source paths for tests that inspect the checked-out code;
        # prevent live auth loading and real service/browser probes explicitly.
        for name in ('_load_auth_env', '_send_telegram_notify'):
            stack.enter_context(patch.object(cc, name, return_value=None))
        for name in ('_check_n8n', '_check_codex', '_check_claude', '_check_gemini',
                     '_check_telegram', '_check_browser', '_check_cpc', '_check_channel'):
            stack.enter_context(patch.object(cc, name, return_value='unconfigured'))
        stack.enter_context(patch('zamal_core.telegram_bot.TelegramAPI.send_message', return_value={'ok': True}))
        stack.enter_context(patch('zamal_core.ai_browser_transport._run_osascript', return_value=(False, 'fixture_no_browser')))
        stack.enter_context(patch.object(cc._Handler, '_handle_n8n_workflows',
            lambda self: self._send_json({'workflows': [], 'count': 0})))
        # Do not inherit account credentials. Tests provide their own synthetic env.
        safe_env = {k:v for k,v in os.environ.items() if not any(
            term in k.upper() for term in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY', 'CHAT_ID'))}
        stack.enter_context(patch.dict(os.environ, safe_env, clear=True))
        loader = unittest.TestLoader()
        patterns = sys.argv[1:] or ['test_*.py']
        suite = unittest.TestSuite(loader.discover(str(ROOT / 'tests'), pattern=p) for p in patterns)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
