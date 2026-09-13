"""Coding fixture runner — proves the approval gate blocks unapproved changes.

Reads tests/fixtures/safe_sample.py, identifies the bug (a - b instead of a + b),
proposes a fix, then STOPS before applying — creates an approval record.
Returns {task_id, approval_id, proposed_fix, state: 'WAITING_APPROVAL'}.

No file is modified.
"""
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_OPERATOR_ROOT = _HERE.parent
_FIXTURE_PATH = _OPERATOR_ROOT / 'tests' / 'fixtures' / 'safe_sample.py'


def run_fixture(fixture_path=None):
    """
    Run the coding fixture approval proof.

    1. Creates a task in Store for the fixture.
    2. Reads the fixture file (safe, not CPC).
    3. Identifies the bug.
    4. Proposes a fix.
    5. STOPS before applying — creates an approval record via Gate.
    6. Returns {task_id, approval_id, proposed_fix, state: 'WAITING_APPROVAL'}.
    """
    from .store import Store, Gate, Policy

    fixture_path = Path(fixture_path) if fixture_path else _FIXTURE_PATH

    # Step 1: Create task
    store = Store()
    request_key = str(uuid.uuid4())
    task = store.create_task(
        command=f'Fix bug in fixture: {fixture_path.name}',
        task_type='prepare_code',
        project='zamal_operator',
        request_key=request_key,
        context={'inputs': [str(fixture_path)], 'scope': 'fixture_test'},
    )
    task_id = task['id']

    # Step 2: Read fixture file
    content = fixture_path.read_text(encoding='utf-8')
    lines = content.splitlines()

    # Step 3: Identify the bug
    bug_line = None
    bug_line_no = None
    for i, line in enumerate(lines, 1):
        if 'a - b' in line and 'BUG' in line.upper():
            bug_line = line
            bug_line_no = i
            break
        elif 'a - b' in line:
            bug_line = line
            bug_line_no = i

    bug_description = (
        f'Line {bug_line_no}: `{bug_line.strip()}` — '
        f'uses subtraction (a - b) instead of addition (a + b). '
        f'The function is named add_numbers but subtracts.'
    ) if bug_line else 'Bug not found in expected location'

    # Step 4: Propose fix (do NOT apply)
    proposed_fix = content.replace('return a - b', 'return a + b')
    proposed_fix_description = "Replace 'return a - b' with 'return a + b' in add_numbers()"

    # Transition task to PLANNED (required before Gate.propose)
    task = store.task(task_id)
    rev = task['revision']
    # RECEIVED -> UNDERSTOOD
    with store.transaction():
        store._move(task_id, 'UNDERSTOOD')
    # UNDERSTOOD -> PLANNED
    with store.transaction():
        store._move(task_id, 'PLANNED')

    # Patch context with analysis
    task = store.task(task_id)
    store.patch_context(task_id, {
        'decisions': [bug_description],
        'plan': [
            f'Read fixture: {fixture_path}',
            f'Bug found: {bug_description}',
            f'Proposed fix: {proposed_fix_description}',
            'STOP — awaiting approval before applying fix',
        ],
        'affected_files': [str(fixture_path)],
    }, task['revision'])

    # Step 5: Gate.propose — creates approval record, blocks execution
    gate = Gate(store)
    action_request_key = str(uuid.uuid4())
    action = gate.propose(
        task_id=task_id,
        kind='delete',  # Use 'delete' as a protected action kind (file mutation)
        payload={
            'fixture_path': str(fixture_path),
            'proposed_fix': proposed_fix_description,
            'bug_description': bug_description,
            'action': 'apply_fix',
        },
        request_key=action_request_key,
        ttl_seconds=3600,
    )

    # Verify state
    task_after = store.task(task_id)
    approval_id = action['approval']['id'] if action['approval'] else None

    store.close()

    return {
        'task_id': task_id,
        'approval_id': approval_id,
        'proposed_fix': proposed_fix_description,
        'proposed_fix_content': proposed_fix,
        'bug_description': bug_description,
        'state': task_after['state'],
        'fixture_path': str(fixture_path),
        'fixture_content_unchanged': fixture_path.read_text(encoding='utf-8') == content,
    }
