# Actual architecture — verified Phases 1–14, 2026-09-09

## Current verification status — Phase 4 finalized

All 14 phase milestones are GREEN within their recorded acceptance scope. Phase 4
uses the existing Gemini adapter and n8n-owned credential for a verified real
planner → coder → verifier handoff through canonical ContextBus and Phase 9 Gate.
Approval, context identity, replay denial and persistence passed live checks;
62 focused tests and 762 full regression tests passed. CPC remains unchanged and
read-only. Final evidence: outbox/2026-09-09/phase4-final/verification.json.

Codex CLI's unified_exec preflight remains fail-closed; OpenAI's last live request
returned HTTP 429. These provider-specific limitations are not represented as
working connections. Earlier dated RED/13-of-14 statements below are historical;
this current verification status supersedes them without changing their evidence.

## Preserved existing lane

Desktop launchers/scripts → Node 22 via NVM → n8n 2.33.0 (:5678). Existing Command
Center: chat trigger → Gemini/simple memory → CPC Inspector HTTP summary → Markdown
conversion → disk write → response. n8n data remain in `.n8n-data/.n8n/`; no workflow,
configuration, launcher or dependency was edited in this run.

`scripts/cpc_readonly_server.js` exposes loopback :8787 health and read-only summary
using `config/cpc_project_path.txt`. It reads known documents, structure and Git
status with optional locks disabled; it cannot edit/install/commit/push. CPC remains
read-only. n8n editor/readiness and inspector health/summary were verified after
implementation. CPC Git status matched the before-run snapshot.

Legacy outbox, inbox, approvals folder, logs and knowledge remain intact. The
legacy `approvals/` folder is not the authoritative queue. The legacy workflow's
conversational approval language does not grant authorization in the new core.
New worker output directories are created automatically; the legacy n8n writer
still needs its future date-directory integration.

## Core and persistence

- `zamal_core/store.py`: transactional task context, approval policy, execution gate.
- `zamal_core/safety.py`: durable emergency controls and execution checks.
- `zamal_core/worker.py`: bounded local filesystem/command capabilities.
- `zamal_core/__main__.py`: trusted local CLI for tasks, approvals, controls, execution.
- `tests/test_foundation.py`, `test_cli.py`, `test_worker.py`: 54 tests.

Python 3.10+ standard library, no new installed dependency. Separate authoritative
SQLite database: `state/operator.sqlite3`, schema v2. New state directory/file use
0700/0600. Foreign keys, synchronous FULL, BEGIN IMMEDIATE transactions, optimistic
revisions and unique idempotency keys. Additive schema migration creates new tables
without replacing tasks/approvals; migration and integrity were tested.

Tables:

- tasks: original command, ID/type/project/state, structured context, timestamps,
  revision, intake key/fingerprint.
- actions: bound immutable proposal, task/key, action kind, payload hash, protection,
  execution claim/status and result.
- approvals: action-bound ID/hash, pending/approved/rejected/edit-requested/consumed
  status, expiry, decision actor/time and note.
- audit: ordered task/context/proposal/approval/execution/denial events.
- controls: persistent emergency flags, per-flag cancellation epochs and revision.
- control_audit: ordered flag changes with actor, reason and timestamp.
- worker_outputs: IDs, exact relative paths and hashes for worker-created outputs;
  deletion timestamp records consumption. Existing outbox files are not enrolled.

Application audit tables have SQL update/delete rejection triggers. The local DB
owner remains trusted; these are not tamper-proof against that owner. Missing or
invalid existing emergency state fails closed, including after reopen.

Task context retains inputs, rules, decisions, plan, selected AI/tool, affected
files, Git state, tests, errors, retries, result and next action. `Store.task()`
reconstructs context, action/approval records and history. Context patches require
a current revision and cannot grant approval or mutate reserved states.

State flow: RECEIVED → UNDERSTOOD → PLANNED; protected proposal → WAITING_APPROVAL;
APPROVE → APPROVED; claim → RUNNING; handler return → VERIFYING; passing verification
evidence → COMPLETED. REJECT → REJECTED. EDIT cancels the old action and returns to
PLANNED for a fresh proposal/approval. Errors/interruption can leave UNCERTAIN/
RECOVERING or EXECUTING/RUNNING; neither can replay automatically. FAILED remains
in the state graph for later verification/recovery orchestration. One open action
per task; subtask scheduling is not implemented yet.

## Approval boundary

`Policy` enumerates safe/protected kinds, protects commits by default and denies
unknown kinds. `Gate.propose()` binds task, kind and complete concrete payload.
`Store.decide()` is exclusively a trusted human control-plane method; the CLI
records the current local OS account. No chat parser, provider tool or remote
endpoint is connected to decision/control methods. Future gateways must authenticate
and authorize the human before invoking them.

`Gate.execute()` rechecks policy, emergency flags, task state, exact binding, expiry,
actor, approval decision and registered capability. Worker preflight validates payload
and workspace before the claim. The gate atomically consumes approval and records
execution before dispatch. It rechecks controls before dispatch; the worker checks
again and polls while a subprocess runs. Missing capabilities fail closed even with
approval. Rejected execution attempts are audited without payload/error-text logging.

Approval cannot enable an arbitrary shell, bypass CPC read-only policy, authorize
unowned-file deletion or reset emergency controls. The production handler registry
contains only the bounded Phase 3 capabilities below. Future handlers must preserve
these enforcement boundaries. Provider adapters must not expose `decide`, controls,
SQLite, handler registration, or the control CLI to untrusted model output.

At-most-once dispatch is enforced per action, not exactly-once external delivery.
A crash after a side effect can leave an ambiguous claim. A crash while creating
an output can leave an unregistered partial/orphan file; it is not automatically
deleted or overwritten. Recovery must reconcile effects before proposing new work.

## Local capability surface

Tasks use `operator` / `cpc` or their exact configured absolute root as project.
Payload `workspace` is the corresponding alias. Roots are fixed by trusted startup,
not supplied by action payload. Operator root is this installation; CPC root is
`/Users/book/Desktop/AI-GROCERY-DECISION-PLATFORM`.

| Kind | Payload | Actual effect |
|---|---|---|
| read | workspace, path | Read at most 256 KiB of one regular text file; return text/hash |
| inspect | workspace, path | List at most 200 permitted entries |
| draft / prepare_code | operator workspace, name, content | Exclusively create a new owned output |
| verify | workspace, command, optional path | Fixed version or syntax check only |
| delete | operator workspace, output_id, sha256 | Delete exact unchanged worker-owned output, approval required |

Reads/listings exclude hidden paths, credentials/key-like names, state, approvals,
logs, backups, dependency trees and databases. Only the public CPC path config is
readable under `config`. Absolute paths, `..`, alternate separators and unknown
payload fields are rejected. File reads reject hardlinks, symlinks, FIFOs/devices
and oversized files. Directory descriptors are pinned with component-by-component
O_NOFOLLOW checks; symlink replacement attacks are tested. Results apply heuristic
redaction for obvious secret assignments/tokens/credential URLs; this is not a
complete detector of arbitrary secrets embedded in ordinary source documents.

Writes are restricted to new files in
`outbox/YYYY-MM-DD/worker/<plain-name>.{md,txt,json,py,js}` (Asia/Qatar date).
No existing source/configuration is editable by this worker. Creation uses O_EXCL,
private permissions, fsync, and manifest registration. The final bounded mutation
is serialized with emergency-control updates. No writes or deletion are permitted
in CPC. Deletion requires a valid approval plus an owned-output ID/hash and unchanged
file bytes. No recursive deletion, unowned output deletion or retention timer exists.
Deletion was exercised only in temporary fixtures; no user output was deleted.

Command allowlist:

- `node_version`: installed Node 22.23.2 executable with `--version`.
- `git_version`: `/usr/bin/git --version`.
- `node_syntax`: installed Node `--check --input-type=commonjs`, bounded input on stdin.
- `python_syntax`: `ast.parse` of bounded input; source is never executed.

Subprocess argv/executable/cwd/environment cannot be supplied by payload. No shell,
Node eval, Python eval, npm/test/build script, Git mutation, network command or
project code execution is available. Fixed processes use cwd `/`, a small scrubbed
environment, closed inherited descriptors, their own process group, 10-second
limit, 64-KiB combined output limit and control checks normally every 50 ms.
Timeout/STOP/output-limit paths kill the process group, including descendants.

This is command and capability containment, not an OS-wide sandbox for arbitrary
untrusted code. The existing OS user, runtime binaries, module code and startup
registry remain trusted. Do not add project test/build commands until stronger OS
isolation is implemented and verified. A same-account actor able to modify code,
SQLite or move open workspace directories is outside this process-level boundary.

## Emergency controls

- STOP EVERYTHING blocks all new worker/gate dispatch and interrupts running worker subprocesses.
- PAUSE EXTERNAL ACTIONS blocks external categories while allowing safe local work.
- READ ONLY MODE blocks writes/deletion and other mutating categories; inspection/syntax checks remain available.
- DISABLE GITHUB PUSH blocks git_push even with approval.
- DISABLE EMAIL SEND blocks email_send even with approval.

Flags persist across restarts. Each enabling transition increments its own epoch;
a quick ON/OFF cannot let already-running affected work evade cancellation. Resume
never resets consumed approvals/claims. Control changes require a trusted local
actor/reason and have separate append-only history. All production flags remain
OFF after verification; push/email still have no registered capability.

STOP is scoped to the Operator worker, not unrelated processes or the independently
running n8n/inspector. It is not an undo of already-completed actions. Bounded local
mutations serialize with the control update; in-flight processes poll the persisted
state. Database contention may delay acknowledgement up to the configured lock
wait. Arbitrary future handlers must implement cooperative cancellation too.

## CLI and recovery

From the operator root:

```sh
python3 -m zamal_core init
python3 -m zamal_core tasks
python3 -m zamal_core approvals
python3 -m zamal_core controls
python3 -m zamal_core --help
python3 -m unittest discover -s tests -v
```

Additional commands: create/show/context/transition/propose/decide/backup,
`execute ACTION_ID`, and `control FLAG on|off --reason TEXT`. The CLI is local and
trusted; it is not a remote/API authentication mechanism. Decisions require exact
APPROVE/EDIT/REJECT choices; EDIT requires a note. The execute command accepts an
existing persisted action ID, not shell text.

Snapshot before Phase 3:
`state/backups/before-phase3-20260908-122123/` contains the consistent pre-migration
DB and modified pre-change core/master files. The SQLite backup API exclusively
creates new backup files, refuses overwrite, and was restore-tested in isolation.
Do not restore old code against a newer live schema or automatically overwrite
state. To roll back adoption, stop using the standalone worker/core and preserve
its DB; the unchanged legacy services continue. Any actual state restore requires
a quiescent worker and explicit review of work performed since the snapshot.

## Verified status and deferred work

54 tests pass, including all 23 earlier tests. Live CPC read task and dated-report
creation task both COMPLETED after independent byte/hash checks; no production
approval was manufactured, no user data deleted, and no CPC code changed.
Report: `outbox/2026-09-08/worker/phase3-verification-11ecbbfa.md`.
Production schema v2 integrity is OK and pending approvals are empty.

Phases 4–14 remain RED: AI routing/context events, autonomous queue, project coding,
independent verifier/Git safety, browser, n8n bridge, Telegram, Google OAuth,
career/LinkedIn, monitoring/recovery/cost, discovery and personal operations.
Existing Codex/Gemini availability is not yet a core provider adapter. n8n's current
all-interface listener and missing n8n Python task runner remain unchanged; neither
prevents this Python standard-library worker from running.


## Phase 9 approval extension — verified 2026-09-08

This section supersedes the earlier approval/schema status descriptions above. The
current production base schema is v5; Phase 9 adds version 1 of `approval_contract`
in `core_extensions` and the immutable `approval_contracts` table in the SAME database.
The existing approvals/actions remain the only queue and execution claim authority.
`zamal_core/approval_contract.py` captures immutable action/task/revision/evidence
bindings and checks actual repository and intake-file identity. `Store.decide` and
`Gate.execute` enforce those bindings; all existing worker gates inherit enforcement.
The proposal CLI uses the existing VerifierWorker registry to capture CPC evidence.

`approval ID` exposes the stored contract and derived lifecycle. `resolve-approval`
requires approval ID, task ID, action ID, and an explicit choice; APPROVE uses the
existing atomic gate, EDIT/REJECT execute nothing. `decide` and `execute` remain the
compatible two-step local interface. These methods are trusted-local only, without
a conversational parser or remote authentication claim.

Existing CONSUMED approvals map through action status to EXECUTING, EXECUTED, or
UNCERTAIN. EDIT_REQUESTED maps to SUPERSEDED. Expiry is persisted terminally and
checked at decision, claim and dispatch. Legacy unsealed approvals are preserved
but denied execution; a pending legacy request can be EDITed and reproposed. No
authorization is inferred during migration. Emergency controls and Phase 6/7/8
worker/verifier checks continue to apply; Git commit additionally denies unrelated
staged and pre-existing dirty files, and merge checks the actual target branch.

Verification: 38 Phase 9 tests and 469 full regression tests pass. Live protected
effects use isolated fixtures only; production CPC remains unchanged/read-only by
default. n8n and Inspector are healthy. Phase 4 remains externally BLOCKED, untouched.
Recovery snapshot: `state/backups/before-phase9-20260908-172118/`. No automatic replay
or production restore is permitted for uncertain effects. Local OS/registered code
remain trusted; this is not machine-wide isolation or exactly-once network delivery.


## Phase 10 continuation — 2026-09-09, GREEN

Current base schema remains 6, with gateway extension v1 and connection_controls
in the SAME Operator database. GatewayWorker composes VerifierWorker; connection
toggles use existing Gate/Phase 9, including registry-revision approval binding.
The Operator remains the sole canonical task/action/approval authority.

Authenticated loopback POST `127.0.0.1:8788/events` accepts event data, never approval
decisions. `scripts/run_gateway.py` loads the existing private owner-approved
`state/gateway/auth.env`; missing/unsafe credential state fails closed. The matching
HTTP Header Auth credential is encrypted in existing n8n storage. Provisioning
approval 9c74d7ef-7a51-486a-a7e9-26db73c0bd08 is already EXECUTED, never replayed.

Five Phase 10 modules are imported and published at their current versions in the
existing n8n database: ZamalP10Intake01, ZamalP10Approval01, ZamalP10Response01,
ZamalP10Error01, ZamalP10Logging01. The installed n8n 2.33.0 engine requires published
versions for database subworkflow calls. Source templates remain inactive for safe
import; runtime publication is separate. No public/scheduled trigger was added.
The existing Command Center workflow remains unchanged.

Real n8n execution invoked all five modules: authenticated intake → canonical
Operator proposal → Phase 9 PENDING → approval presentation/response/logging;
duplicate intake reused the proposal, and task/error contracts ran successfully.
Approval text is data only; missing/wrong credentials and approval keys are denied.
No protected verification action executed. The inactive manual verification parent
ZamalP10Verify01 and secret-free execution evidence are under `state/gateway/`.

Source-scoped event keys prevent duplicates; changed payloads conflict. Received
claims survive interruption and never automatically replay. Unknown/unimplemented
handlers and failed persistence cannot report success. Health checks are bounded,
allowlisted, no-redirect and read-only; missing probes report UNCONFIGURED.

Workflow export uses read-only n8n SQLite, writing exclusive private versioned/hash
manifest audit snapshots without mutating live workflows. Parameters, credentials
and static/pinned data are redacted; snapshots are not restore-ready. READ ONLY mode
denies export creation. Private recovery snapshots remain separate from audit exports.

Final verification: 36 Phase 10 + 5 credential-binding focused tests pass; one full
regression run after real live checks passes 510/510. n8n and read-only Inspector are
healthy; real CPC status/diff and existing workflows are unchanged. Phase 4 remains
externally BLOCKED exactly as recorded; no Phase 5–9 behavior or Phase 11+ work added.


## Phase 14 continuation — verified local personal operations, 2026-09-09

PersonalOpsWorker retains Claude's calendar/finance/subscription/research/document/
CRM/learning/portfolio classes and composes the existing AutomationWorker. Local
content/news/travel/admin records share personal_entries. The same production
SQLite schema 6 gains personal_ops extension v2; no parallel task/approval store.
Existing tables are preserved through the v1→v2 migration; audits remain append-only.

Personal capabilities are routed by existing CLI and event intake into the Phase 9
Gate. Gate passes immutable action/task identity to the personal handler after the
normal execution claim; ContextBus continues to expose canonical actions/results.
Local safe operations remain automatic and idempotent by the existing action key.
Bounded validation rejects secrets before action persistence. Only the operator
project is accepted; document references are metadata, not file access or execution.

Six protected personal mutation proposals reuse Phase 9. Their external transports
are deliberately unconnected and deny execution even after approval. Referenced
personal rows participate in the existing approval evidence binding. No remote
text grants approval, and emergency/read-only/send restrictions remain authoritative.

System summary reuses Phase 12 usage counts, monitor observations, controls and
backup inventory; existing health/security/recovery and Phase 13 automation handlers
are retained. No monetary provider-cost estimation, live news service, booking,
payment or publication is implied. No automatic backup restore or automation activation.

Verification: 60 focused tests, 727 full regressions (one run), 19 safe live actions,
PENDING protected event proposal, no protected effects, unchanged CPC status/diff,
healthy n8n/read-only Inspector. Existing outbox-permission warning remains visible.
Live evidence: outbox/2026-09-09/phase14/live-verification.json. Phase 4 and Voice STT
blockers are untouched. No commit/push/deploy or unrelated application changes.


## Phase 5 real voice verification — 2026-09-09, GREEN

The existing macOS Swift/SFSpeechRecognizer en-US adapter successfully transcribed
inbox/spoken_test.wav through Intake.record_voice into canonical task evidence.
Authorization is granted and recognizers are available. en-US reports no on-device
support; this is real Apple Speech verification, not an offline-only guarantee.
No adapter replacement, TCC bypass or security-setting change was made.

Voice intake now checks emergency STOP before STT and its latched state before
persisting evidence, rejects stale task revisions before recognition, and checks
file integrity again after recognition. Exact completed duplicates reuse evidence
without another STT request. Explicit pending-transcription retries replace the
matching evidence. Existing task context/audit/revisions remain authoritative;
transcripts are data and never approvals. Manual and Telegram file intake remain
preserved. Successful and pending evidence persist across restart; no auto-replay.

Verified: real transcript/audio hash/audit/dedup/restart, seven new safety tests,
and one full suite of 734 passing tests. CPC status/diff and Phase 4 remain unchanged.
Evidence: outbox/2026-09-09/phase5/live-verification.json.


## Phase 4 alternative-provider transport — 2026-09-09, live BLOCKED

The master spec permits provider-independent operation. The existing GeminiAdapter
is now wired to GeminiTextTransport (gemini_transport.py) and a fixed local Node 22
helper (gemini_bridge.cjs). The helper reads the existing n8n Command Center Gemini
node and credential via read-only SQLite. n8n's installed AES cipher decrypts only
in memory; no plaintext credential enters Operator storage, argv, logs or Python.
Unknown formats, unsafe endpoint overrides or changed connection identity fail closed.

Generation uses the documented fixed Google HTTPS generateContent endpoint, empty
tools, JSON text response, bounded tokens/bytes/time and no redirect/retry. Native
HTTPS ignores proxy environment overrides; child environment excludes NODE_OPTIONS.
Tool-call/non-text/incomplete responses are denied; the existing adapter validates
canonical context/hash/stage and exact response fields before publishing any result.
See https://ai.google.dev/api/generate-content for the API contract.

Connection/model/credential/helper fingerprints participate in the existing provider
manifest and approval binding. The original HandoffEngine owns planner/coder/verifier
stages, canonical context, approval carrier, audit, deduplication and recovery. No
parallel approval or provider architecture was introduced. CodexTextTransport and
its unified_exec/version safety contract are unchanged.

47 focused and 747 full regression tests pass. The real authorized synthetic request
using existing models/gemini-3.6-flash returned HTTP 503. Zero AI stage results were
published; the action remains UNCERTAIN/approval CONSUMED/run BLOCKED, including after
restart. Replay was denied and no automatic retry/fallback occurred. Therefore this
transport is implemented, but real Gemini inference is NOT verified working and
Phase 4 remains RED. Other provider interfaces do not imply connected credentials.
CPC and existing n8n workflows/credentials are unchanged.
Evidence: outbox/2026-09-09/phase4-gemini/live-verification.json.

## Phase 4 OpenAI binding — 2026-09-09, live BLOCKED

The existing OpenAIAdapter receives OpenAITextTransport, reusing the bounded child
runner from GeminiTextTransport through a fixed bridge property. OpenAI's bridge
reads the sole existing n8n openAiApi credential using n8n's installed cipher in
memory. n8n remains credential owner; neither key nor decrypted credential is
persisted/exported. The fixed Responses endpoint accepts text-only draft requests
with no tools, store=false and a strict output schema. Model/credential/helper
identity binds the existing provider manifest and Phase 9 approval. Existing
HandoffEngine, canonical ContextBus, atomic execution claims, emergency controls
and fail-closed recovery remain authoritative. No alternate approval path exists.

The real owner-authorized request returned HTTP 429 before any draft result. Its
approval is consumed and action uncertain; replay/restart checks deny reexecution.
62 focused tests pass; no new full regression was run because live inference did
not pass. Phase 4 remains RED, 13/14 phases GREEN. See Phase 4 roadmap continuation
and outbox/2026-09-09/phase4-openai/live-verification.json. Codex's independent
unified_exec safety blocker remains unchanged.

API contract references: https://developers.openai.com/api/docs/models/gpt-4.1-mini
and https://developers.openai.com/api/reference/cli/resources/responses/methods/create.
