"""Tests for zamal_core/coding_fixture_runner.py — coding fixture approval gate proof."""
import unittest
from pathlib import Path


_FIXTURE_PATH = Path(__file__).resolve().parent / 'fixtures' / 'safe_sample.py'
_ORIGINAL_CONTENT = 'def add_numbers(a, b):\n    return a - b  # BUG: should be a + b\n'


class TestCodingFixture(unittest.TestCase):
    """Tests for coding_fixture_runner approval gate proof."""

    def setUp(self):
        # Ensure fixture file has original content before each test
        _FIXTURE_PATH.write_text(_ORIGINAL_CONTENT, encoding='utf-8')

    def test_01_fixture_file_has_bug(self):
        """Fixture file contains the original bug (a - b) and not a fix."""
        content = _FIXTURE_PATH.read_text(encoding='utf-8')
        self.assertIn('a - b', content)
        # The fixture has a comment "# BUG: should be a + b" so we check
        # that the return statement uses subtraction, not addition.
        lines = content.splitlines()
        return_lines = [l for l in lines if l.strip().startswith('return')]
        self.assertTrue(len(return_lines) > 0, 'No return statement found')
        self.assertIn('a - b', return_lines[0], 'Return statement should subtract')

    def test_02_runner_creates_task(self):
        """coding_fixture_runner creates a task in Store."""
        from zamal_core.coding_fixture_runner import run_fixture
        result = run_fixture(_FIXTURE_PATH)
        self.assertIn('task_id', result)
        task_id = result['task_id']
        self.assertTrue(len(task_id) > 10)

        # Verify task exists in Store
        from zamal_core.store import Store
        store = Store()
        task = store.task(task_id)
        store.close()
        self.assertEqual(task['id'], task_id)

    def test_03_approval_record_created(self):
        """Approval record is created before any file modification."""
        from zamal_core.coding_fixture_runner import run_fixture
        result = run_fixture(_FIXTURE_PATH)
        approval_id = result.get('approval_id')
        self.assertIsNotNone(approval_id, 'approval_id should be set')
        self.assertTrue(len(approval_id) > 10)

        # Verify approval record exists in Store
        from zamal_core.store import Store
        store = Store()
        approval = store.approval(approval_id)
        store.close()
        self.assertEqual(approval['id'], approval_id)
        self.assertEqual(approval['status'], 'PENDING')

    def test_04_fixture_file_unmodified(self):
        """Fixture file is unmodified after the runner runs."""
        from zamal_core.coding_fixture_runner import run_fixture
        result = run_fixture(_FIXTURE_PATH)

        # Check via result
        self.assertTrue(result.get('fixture_content_unchanged'),
                        'Runner reported fixture was modified')

        # Double-check by reading file directly
        content = _FIXTURE_PATH.read_text(encoding='utf-8')
        self.assertIn('a - b', content,
                      'Fixture file should still contain the original bug')
        self.assertEqual(content, _ORIGINAL_CONTENT,
                         'Fixture file content changed unexpectedly')

    def test_05_proposed_fix_is_correct(self):
        """Proposed fix is correct (a + b not a - b)."""
        from zamal_core.coding_fixture_runner import run_fixture
        result = run_fixture(_FIXTURE_PATH)
        proposed_content = result.get('proposed_fix_content', '')
        self.assertIn('a + b', proposed_content,
                      'Proposed fix should contain a + b')
        self.assertNotIn('a - b', proposed_content,
                         'Proposed fix should not contain a - b')

    def test_06_state_is_waiting_approval(self):
        """Task state is WAITING_APPROVAL after runner runs."""
        from zamal_core.coding_fixture_runner import run_fixture
        result = run_fixture(_FIXTURE_PATH)
        self.assertEqual(result.get('state'), 'WAITING_APPROVAL',
                         f"Expected WAITING_APPROVAL, got {result.get('state')}")

    def test_07_bug_description_accurate(self):
        """Bug description accurately identifies the subtraction bug."""
        from zamal_core.coding_fixture_runner import run_fixture
        result = run_fixture(_FIXTURE_PATH)
        bug_desc = result.get('bug_description', '')
        self.assertIn('a - b', bug_desc,
                      'Bug description should mention a - b')

    def test_08_fixture_file_still_has_bug_after_run(self):
        """Fixture file still has 'a - b' after runner — proves gate blocked the fix."""
        from zamal_core.coding_fixture_runner import run_fixture
        run_fixture(_FIXTURE_PATH)

        content = _FIXTURE_PATH.read_text(encoding='utf-8')
        self.assertIn('a - b', content,
                      'Gate should have blocked fix; file should still have original bug')

    def test_09_task_has_affected_files(self):
        """Task context records affected_files (but they were not modified)."""
        from zamal_core.coding_fixture_runner import run_fixture
        from zamal_core.store import Store
        result = run_fixture(_FIXTURE_PATH)
        task_id = result['task_id']
        store = Store()
        task = store.task(task_id)
        store.close()
        affected = task['context'].get('affected_files', [])
        self.assertIsInstance(affected, list)
        self.assertTrue(len(affected) > 0, 'affected_files should list the fixture')

    def test_10_approval_blocks_protected_action(self):
        """The gate blocks execution — action status is PENDING, not SUCCEEDED."""
        from zamal_core.coding_fixture_runner import run_fixture
        from zamal_core.store import Store
        result = run_fixture(_FIXTURE_PATH)
        task_id = result['task_id']
        store = Store()
        task = store.task(task_id)
        actions = task.get('actions', [])
        store.close()
        # Must have at least one pending action
        self.assertTrue(len(actions) > 0, 'Task should have an action')
        pending = [a for a in actions if a['status'] == 'PENDING']
        self.assertTrue(len(pending) > 0, 'Action should be PENDING (blocked by gate)')


if __name__ == '__main__':
    unittest.main()
