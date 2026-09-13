"""Tests for zamal_core/control_center.py — Control Center HTTP server."""
import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


def _find_free_port():
    """Find a free TCP port on localhost."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class TestControlCenterServer(unittest.TestCase):
    """Tests for ControlCenterServer."""

    @classmethod
    def setUpClass(cls):
        import sys, os
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from zamal_core.control_center import ControlCenterServer
        cls.port = _find_free_port()
        cls.server = ControlCenterServer(port=cls.port)
        cls.server.serve(block=False)
        # Wait for server to be ready
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=0.3):
                    break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.stop()
        except Exception:
            pass

    def _get(self, path, timeout=5):
        url = f'http://127.0.0.1:{self.port}{path}'
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read()

    def _post(self, path, data, timeout=5):
        url = f'http://127.0.0.1:{self.port}{path}'
        body = json.dumps(data).encode()
        req = urllib.request.Request(url, data=body,
                                      headers={'Content-Type': 'application/json'},
                                      method='POST')
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.getcode(), json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_01_server_starts(self):
        """ControlCenterServer starts on a random port without error."""
        s = socket.create_connection(('127.0.0.1', self.port), timeout=2)
        s.close()

    def test_02_root_returns_html(self):
        """GET / returns 200 with HTML containing title."""
        code, body = self._get('/')
        self.assertEqual(code, 200)
        text = body.decode('utf-8')
        self.assertIn('Zamal Operator Control Center', text)
        self.assertIn('DOCTYPE html', text)

    def test_03_status_returns_valid_json(self):
        """GET /api/status returns valid JSON with all expected integration keys."""
        code, body = self._get('/api/status')
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIn('connections', data)
        conn = data['connections']
        expected_keys = {'codex', 'claude', 'gemini', 'chatgpt_web',
                         'browser', 'n8n', 'telegram', 'github', 'cpc'}
        self.assertEqual(set(conn.keys()), expected_keys)

    def test_04_tasks_returns_list(self):
        """GET /api/tasks returns JSON list."""
        code, body = self._get('/api/tasks')
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIsInstance(data, list)

    def test_05_create_task(self):
        """POST /api/tasks with valid text creates a task and returns task_id."""
        code, data = self._post('/api/tasks', {'text': 'Test task from test_control_center'})
        self.assertIn(code, (200, 201))
        self.assertIn('task_id', data)
        self.assertIn('state', data)
        task_id = data['task_id']
        self.assertTrue(len(task_id) > 10)

    def test_06_emergency_stop(self):
        """POST /api/emergency-stop with stop_everything triggers Controls."""
        from zamal_core.store import Store
        from zamal_core.safety import Controls

        # First clear any existing stop (in case of prior test run)
        store = Store()
        controls = Controls(store)
        try:
            controls.set('stop_everything', False, actor='test_setup', reason='test setup clear')
        except Exception:
            pass
        store.close()

        code, data = self._post('/api/emergency-stop', {
            'flag': 'stop_everything',
            'reason': 'test_emergency_stop'
        })
        self.assertEqual(code, 200)
        self.assertEqual(data.get('result'), 'ok')
        self.assertEqual(data.get('flag'), 'stop_everything')

        # Verify flag was actually set
        store = Store()
        controls = Controls(store)
        status = controls.status()
        self.assertEqual(status['stop_everything'], 1)
        # Clear it
        controls.set('stop_everything', False, actor='test_cleanup', reason='test cleanup')
        store.close()

    def test_07_cpc_status_returns_json(self):
        """GET /api/cpc-status returns JSON with path field."""
        code, body = self._get('/api/cpc-status')
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIn('path', data)
        self.assertIn('path_exists', data)
        self.assertIsInstance(data['path'], str)
        self.assertIsInstance(data['path_exists'], bool)

    def test_08_connection_status_valid_values(self):
        """Each connection status value is one of the allowed values."""
        valid = {'ok', 'configured', 'unconfigured', 'unreachable', 'external_only', 'partial'}
        code, body = self._get('/api/status')
        self.assertEqual(code, 200)
        data = json.loads(body)
        conn = data.get('connections', {})
        for key, val in conn.items():
            self.assertIn(val, valid,
                          f'Connection {key!r} has invalid status {val!r}')

    def test_09_create_task_missing_text(self):
        """POST /api/tasks without text returns 400."""
        code, data = self._post('/api/tasks', {})
        self.assertEqual(code, 400)

    def test_10_task_detail_not_found(self):
        """GET /api/task/nonexistent returns 404."""
        import urllib.error
        url = f'http://127.0.0.1:{self.port}/api/task/00000000-0000-0000-0000-000000000000'
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=5):
                self.fail('Expected 404 but got 200')
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_11_activity_returns_list(self):
        """GET /api/activity returns JSON list."""
        code, body = self._get('/api/activity?since=0')
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIsInstance(data, list)

    def test_12_cpc_status_key_fields(self):
        """GET /api/cpc-status returns key_files and readonly_server_port."""
        code, body = self._get('/api/cpc-status')
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIn('key_files', data)
        self.assertIn('readonly_server_port', data)
        self.assertIsInstance(data['key_files'], dict)

    def test_13_chatgpt_web_always_external_only(self):
        """chatgpt_web status is always 'external_only'."""
        code, body = self._get('/api/status')
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data['connections']['chatgpt_web'], 'external_only')

    def test_14_task_created_and_retrievable(self):
        """Task created via POST /api/tasks is retrievable via GET /api/task/{id}."""
        code, created = self._post('/api/tasks', {'text': 'Retrieve test task'})
        self.assertIn(code, (200, 201))
        task_id = created['task_id']
        code2, body2 = self._get(f'/api/task/{task_id}')
        self.assertEqual(code2, 200)
        task_data = json.loads(body2)
        self.assertEqual(task_data['id'], task_id)
        self.assertEqual(task_data['state'], 'RECEIVED')

    def test_15_emergency_stop_clear(self):
        """POST /api/emergency-stop-clear clears the flag."""
        # Set it
        self._post('/api/emergency-stop', {
            'flag': 'pause_external_actions',
            'reason': 'test set'
        })
        # Clear it
        code, data = self._post('/api/emergency-stop-clear', {
            'flag': 'pause_external_actions',
            'reason': 'test clear'
        })
        self.assertEqual(code, 200)
        self.assertEqual(data.get('result'), 'ok')
        self.assertEqual(data.get('enabled'), False)

    def test_16_invalid_emergency_flag_rejected(self):
        """POST /api/emergency-stop with unknown flag returns 400."""
        code, data = self._post('/api/emergency-stop', {
            'flag': 'nonexistent_flag',
            'reason': 'test'
        })
        self.assertEqual(code, 400)


class TestConnectionStatusValues(unittest.TestCase):
    """Unit tests for individual connection status checkers."""

    def test_valid_status_values(self):
        """get_connection_status() returns only valid status values."""
        from zamal_core.control_center import get_connection_status, _VALID_STATUSES
        statuses = get_connection_status()
        for k, v in statuses.items():
            self.assertIn(v, _VALID_STATUSES,
                          f'{k} returned invalid status {v!r}')

    def test_chatgpt_web_external_only(self):
        """chatgpt_web is always external_only."""
        from zamal_core.control_center import get_connection_status
        statuses = get_connection_status()
        self.assertEqual(statuses['chatgpt_web'], 'external_only')

    def test_all_expected_keys_present(self):
        """All expected integration keys are present."""
        from zamal_core.control_center import get_connection_status
        statuses = get_connection_status()
        expected = {'codex', 'claude', 'gemini', 'chatgpt_web',
                    'browser', 'n8n', 'telegram', 'github', 'cpc'}
        self.assertEqual(set(statuses.keys()), expected)


if __name__ == '__main__':
    unittest.main()
