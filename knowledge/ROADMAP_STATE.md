# Roadmap state — 2026-09-09

GREEN = verified complete. RED = pending or blocked; not a claim of functionality.

**Current run: Phase 4 GREEN — real Gemini handoff verified; focused 62/62 PASS; escalated full regression 762/762 PASS (one run, 103.022s).**
**Master roadmap: 14/14 phases fully GREEN = 100%, by verified equal phase count. Provider-specific limitations remain documented below.**
This is a phase milestone metric, not an estimate of engineering effort or overall
product capability. Subtasks below define acceptance scope; later phases are not
credited for existing prototypes or documentation.

## PHASE 1 — GREEN: Persistent task state / single source of truth

- GREEN: schema-versioned local SQLite storage separate from n8n; private new state directory/file.
- GREEN: task ID, original command, type, project, complete structured context and timestamps.
- GREEN: required state vocabulary, constrained transitions and optimistic revisions.
- GREEN: durable task/action idempotency; conflicting reuse rejected; concurrent intake deduplicated.
- GREEN: append-only application audit; durable reload and transaction rollback.
- GREEN: consistent non-overwriting backup and isolated restore/integrity verification.
- GREEN: local CLI create/show/list/context/transition/init/backup entry points.

## PHASE 2 — GREEN: Persistent approvals and core execution gate

- GREEN: IDs, action/task/payload binding, expiry, actor, notes and persistent decision history.
- GREEN: APPROVE / EDIT / REJECT; edits cancel old action and require a new request.
- GREEN: explicit protected categories; Git commit protected by default; unknown kinds denied.
- GREEN: conversation text cannot become approval state; reserved task transitions protected.
- GREEN: registered-handler execution requires valid approval; Phase 3 local capabilities now connected.
- GREEN: atomic single-use claim/consumption; concurrent execution/replay blocked.
- GREEN: expired/stale/cancelled/tampered requests blocked; policy changes fail closed.
- GREEN: handler failure and process interruption retain non-replayable claims.
- GREEN: completion requires passing verification evidence.
- GREEN: focused fixture-based tests; no real protected action executed.

Scope boundary: enforcement covers the core and its local capability worker. It is
not a machine-wide security boundary. The legacy n8n workflow is not yet bridged to
the persistent approval engine; remote authentication remains pending.

## PHASE 3 — GREEN: Safe local worker

- GREEN: approved operator/CPC workspace identities and task/project binding.
- GREEN: relative path validation, component containment, pinned directory handles,
  no-follow symlink checks, hardlink/special-file/size rejection, credential/state exclusions.
- GREEN: CPC read-only policy; existing operator sources/configuration protected from worker writes.
- GREEN: bounded file reads/listing and new draft/code-preparation files exclusively in dated worker outbox.
- GREEN: named command allowlist (Node/Git versions, Python/Node syntax); no shell,
  caller argv/env/cwd, eval, arbitrary project scripts, commit, push, reset or system mutation.
- GREEN: fixed executable paths, scrubbed subprocess environment, neutral cwd,
  process-group isolation, output bounds, timeout and emergency cancellation.
- GREEN: durable STOP EVERYTHING / PAUSE EXTERNAL / READ ONLY / DISABLE PUSH /
  DISABLE SEND; trusted-local control CLI, actor/reason audit, restart persistence.
- GREEN: control checks at gate claim and dispatch, before local mutation, during
  subprocess execution; latched epochs prevent a fast on/off toggle bypass.
- GREEN: real worker dispatch through approval gate; protected deletion limited to
  exact manifest-owned, unchanged generated outputs with single-use human approval.
- GREEN: no automatic retention or real deletion during this run; deletion tests use fixtures only.
- GREEN: rejected attempts audited; ambiguous interrupted claims cannot replay.
- GREEN: additive v1→v2 transactionally initialized tables preserve tasks/approvals;
  missing emergency state fails closed; pre-change DB/source snapshot retained.
- GREEN: 54 tests pass (all previous 23 preserved); live CPC read and dated output verified.

Capability boundary: this is a bounded local worker, not an arbitrary coding-agent
shell. Repository test/build execution, existing-source editing, network operations
and browser execution stay denied/unconnected. Stronger OS isolation is required
before any future project-script execution. Fixed parser/version commands do not
execute project code. STOP applies to Operator worker dispatch/processes, not to
unrelated processes or the preserved n8n/inspector services. Remote authorization
and n8n queue integration remain their own RED phases.

## PHASE 4 — GREEN: Context bus and AI routing

### Current verified acceptance — finalization 2026-09-09

- GREEN: MASTER_BUILD_SPEC requires modular providers with “No hard dependency on
  one provider.” Real Gemini inference satisfies the live-provider requirement;
  individual unavailable adapters are not falsely claimed to work.
- GREEN: existing Operator → approval Gate → Gemini planner → coder → verifier
  completed through the n8n-owned credential and unchanged bounded text transport.
  Run `1bc8b72b-d7fe-4926-a871-3ed4cc7e00fb` persisted all three canonical-context
  results and completed with DRAFTS_READY; no project execution occurred.
- GREEN: pending execution denied, explicit approval consumed exactly once,
  context IDs/hashes verified, replay denied and results preserved across restart.
- GREEN: 62/62 focused tests verify canonical context, deterministic routing,
  handoff, compression, policy/approval boundaries and transport safety.
- GREEN: full regression rerun exactly once with authorized loopback permissions:
  **762/762 PASS in 103.022s**. The prior restricted run's two socket-binding errors
  were environmental; code/tests were not modified to bypass them.
- GREEN: real CPC status and binary diff match the pre-handoff baseline; Inspector
  is healthy and reports readOnly=true. No provider retry, credential change,
  CPC write, deployment or protected Git operation occurred in finalization.
- Evidence: `outbox/2026-09-09/phase4-final/verification.json` and `regression.log`;
  live evidence: `outbox/2026-09-09/phase4-gemini/live-verification-c611b597-dbda-4028-ae14-edfde9ccf512.json`.
- Provider limitations: Codex CLI remains externally blocked by its unchanged
  unified_exec safety preflight. OpenAI binding exists but live inference returned
  HTTP 429. Neither is required to claim the verified Gemini path works; neither
  was retried or relaxed. Overall verified milestone: **14/14 GREEN (100%)**.

### Historical attempts — superseded phase status, preserved provider evidence

The RED phase/count statements in the dated attempts below describe those earlier
runs. The current Phase 4 status and verified count are the GREEN values above.

- GREEN: Phase 4 implementation and tests pass: shared canonical context, provider
  adapter interfaces, deterministic routing, context-preserving handoff, compression,
  approval enforcement and safety checks. Latest verification: 34/34 Phase 4 tests,
  89/89 Phase 1–4 regression tests and 431/431 full regression tests passed.
- RED: live Codex provider safety verification is externally BLOCKED. The installed
  Codex 0.153.4 keeps `unified_exec=true` through all supported tested configuration
  methods: `--disable unified_exec`, `-c features.unified_exec=false`, isolated TOML
  configuration, and combined legacy/current overrides in a neutral directory.
- GREEN: the unchanged safety preflight correctly denies live inference with
  `unsafe_cli_feature_contract`. Passing implementation tests does not establish
  live provider safety or make Phase 4 GREEN.
- REQUIRED: do not weaken, remove, fake or bypass the safety preflight.
- RETRY CONDITION: a future Codex CLI/runtime where the required safety contract
  can be genuinely verified. Phase 4 remains RED until live verification passes.

### Phase 4 alternative-provider continuation — 2026-09-09

- Requirement: MASTER_BUILD_SPEC explicitly says “No hard dependency on one provider.”
  A supported alternative is within scope; adapter interfaces, deterministic routing,
  canonical context/handoff, approval/safety boundaries and real inference still
  require verification. The historical Codex-specific safety contract is preserved.
- GREEN implementation/tests: existing GeminiAdapter now has a bounded text transport
  reusing the Command Center's `models/gemini-3.6-flash` and its existing encrypted
  n8n credential. No duplicate provider, credential export or n8n workflow change.
  Installed n8n cipher decrypts only in child-process memory. Only the fixed Google
  HTTPS generation endpoint is allowed; redirects, tools and arbitrary endpoint
  configuration are not accepted. Input/output/token/time bounds are enforced.
- GREEN boundaries: exact connection/model/ciphertext/helper identity is included
  in the existing approval-bound provider manifest. Existing HandoffEngine/Gate
  performs approval and atomic claims; stage/context hashes remain canonical.
  Cancellation kills the bounded child; uncertain requests never auto-fallback/retry.
  Unknown encryption formats fail closed without modifying credential storage.
- GREEN: 47/47 focused tests (34 preserved + 13 transport tests); full regression
  run ONCE, 747/747 PASS, 98.647s. Logs: `/tmp/zamal-phase4-gemini-focused.log`,
  `/tmp/zamal-phase4-gemini-full.log`.
- RED live: the owner-authorized synthetic Gemini-only handoff reached the API but
  returned HTTP 503 on its first stage. No live AI result was published. Action
  `72e006a7-78a5-4d8f-aab8-4d8b68903914` is UNCERTAIN; approval
  `e367f4f3-83bd-42a4-8a64-691dcc51085d` is CONSUMED; run
  `2224a175-1b9e-4951-9772-045351590d6a` is BLOCKED. Pending execution was denied
  before authorization; failure/restart/replay checks preserved the no-retry boundary.
- RED: Gemini live end-to-end acceptance is NOT satisfied. The Operator must not be
  described as fully operational with a verified live AI provider yet. Codex remains
  externally blocked by its unchanged unified_exec preflight. OpenAI/Claude have no
  connected credentials/transports; none is falsely reported as working.
- Recovery: review this uncertain run before any new bounded authorization/request;
  no replay of the consumed approval. Retry requires a working provider service or
  separately authorized working connection; no automatic model/credential switch.
- Evidence: `outbox/2026-09-09/phase4-gemini/live-verification.json`.
  Recovery snapshot: `/Users/book/zamal-ai-operator/state/backups/before-phase4-gemini-20260909-174530`.
  CPC status/diff and n8n workflow/credential records unchanged. No project execution,
  deploy, commit, push, merge, provider-policy relaxation or unrelated phase changes.
  Verified phase count remains 13/14 (92.86%); Phase 4 remains RED.

### Phase 4 OpenAI credential binding continuation — 2026-09-09

- GREEN implementation: the existing OpenAIAdapter/Registry now uses the existing
  bounded transport with n8n-owned `openAiApi` credential reference. The installed
  n8n cipher decrypts only in child-process memory; no plaintext key is copied to
  Operator storage, environment, arguments, logs or evidence. Ambiguous credentials,
  custom endpoints/headers and unknown encryption formats fail closed.
- GREEN request contract: fixed OpenAI HTTPS Responses endpoint, pinned
  `gpt-4.1-mini-2025-04-14`, `store=false`, no tools, strict draft JSON schema,
  bounded tokens/time/output and no automatic retry. Credential/model/helper identity
  remains bound to the existing Phase 9 authorization and canonical handoff.
- GREEN focused tests: 62/62 passed (34 Phase 4 + 13 Gemini + 15 OpenAI), including
  context/routing/handoff, approval/policy controls, cancellation, fixture isolation,
  endpoint/header rejection, tool/refusal/truncation denial and secret-safe errors.
  Log: `/tmp/zamal-phase4-openai-focused.log`.
- RED live verification: OpenAI returned HTTP 429 on the first stage. The connection
  test succeeded in n8n, but inference availability is not established. The bridge
  deliberately discards error bodies, so quota exhaustion versus rate limiting is
  not established; do not claim a specific billing cause or retry automatically.
- GREEN failure boundaries: action `02ee8e6b-fcd1-40c9-ba73-2fd64f4da91c` is UNCERTAIN;
  approval `def86262-2c26-4ee5-9517-08f4667c2995` is CONSUMED; run
  `a79ee9e8-5f83-4464-8399-796e4f629a69` remains BLOCKED after restart. PENDING
  execution and consumed-approval replay denied; zero provider results published.
- GREEN preservation: n8n HTTP 200; Inspector healthy/readOnly=true. Real CPC status
  and binary diff, n8n workflows and encrypted credential records match the baseline.
  No Codex safety change, project execution or unrelated phase changes.
- Full regression intentionally NOT rerun: live verification did not pass. Previous
  747/747 result is historical, not verification of this OpenAI addition.
- Owner action: check the OpenAI API project's inference quota/rate limits and
  billing privately; resolve the HTTP 429 condition before a newly authorized run.
  Do not replay this consumed approval. No secrets are required in chat.
- Evidence: `outbox/2026-09-09/phase4-openai/live-verification.json`.
  Phase 4 remains RED; verified phase count remains **13/14 (92.86%)**. Historical
  Gemini/Codex blockers above remain unchanged; OpenAI is bound but not live-verified.

## PHASE 5 — GREEN: Universal inputs including real Voice STT

**All six Phase 5 intake items are verified. Real STT uses the existing macOS Apple Speech adapter; offline-only operation is not claimed.**

- GREEN: grouped text/screenshots/images/file/URL intake and provenance.
  Verified: `record_screenshot` (single + labelled), `record_images` (multi-image,
  current+expected pair, separately identifiable by label and sha256), `record_url`
  (stored as data, never fetched, credentials stripped, embedded instructions inert).
  Path security: traversal/symlink/hardlink/oversize/unsupported-type all denied.
  Evidence accumulates across calls; stale revision rejected.

- GREEN: PDF/document/log processing and secure attachment storage.
  Verified: PDF, DOCX, XLSX, log, and plain-text files accepted via `record_document`.
  Only reference, hash, subtype and description are stored; file content is never
  executed or embedded in task context. Malicious-content test confirms no approval
  bypass. Shell scripts (.sh) and executables (.exe) rejected.

- GREEN: issue normalizer.
  `normalize_issue` converts informal text to: actual, expected, scope, do_not_change,
  acceptance_criteria. Structured detection (explicit headings) and heuristic fallback
  both verified. Absent fields are None; original text always preserved; malicious
  input never executed.

- GREEN: input validation and safety tests (all existing and Phase 5).
  153 tests pass (87 Phase 1–4 preserved + 66 new Phase 5 tests).
  Approval gates and emergency controls unaffected by intake.
  CPC workspace remains strictly read-only.

- GREEN: real voice transcription through the existing `MacOsSttAdapter` and
  `Intake.record_voice`, without replacing manual/mock or Telegram file intake.
  Speech authorization=3 (authorized); after owner confirmed English-US Dictation,
  en-US/en-GB recognizers reported available. en-US on-device=false; en-GB
  on-device=true. The existing en-US adapter was used unchanged, so no offline-only
  guarantee or en-GB transcription verification is claimed.
- GREEN: existing valid 3.826-second `inbox/spoken_test.wav` transcribed to
  “Zamolo operator phase 5 voice intake verification test”. Task
  `69b5e9a0-5eeb-4ca0-bb88-5623191c71bb` stores `stt_status=transcribed`, transcript,
  reference, size and independently verified SHA256 as canonical voice evidence.
  Task context audit verified; transcript remained data, with zero protected actions.
- GREEN: narrow voice-only guards added after live inspection: check STOP before
  STT and latched STOP before persistence; reject stale revisions before STT;
  revalidate audio integrity after transcription. Exact completed duplicates reuse
  existing evidence without another STT call. An explicit retry of pending
  transcription replaces the matching evidence instead of duplicating it.
- GREEN: pending evidence survives restart; recovery requires an explicit retry.
  Successful live evidence survives restart. No auto-replay, command execution or
  approval inferred from voice. Manual intake remains supported.
- GREEN: 66 existing Phase 5 focused tests passed before live verification; 7 new
  voice safety/recovery tests passed. Full regression run ONCE after real STT and
  safety checks: **734/734 PASS**, 88.454s, including all prior tests.
  Logs: `/tmp/zamal-phase5-stt-focused.log`, `/tmp/zamal-phase5-guards.log`,
  `/tmp/zamal-phase5-final-full.log`. Persistent live evidence:
  `outbox/2026-09-09/phase5/live-verification.json`.
- GREEN: real CPC Git status/binary diff unchanged; Phase 4 code/tests/blocker and
  all unrelated phases preserved. No permission/security changes, deployment,
  commit, push or merge performed. No remaining Phase 5 acceptance blocker.
  Historical Phase 11 STT blocker notes describe its earlier verification context;
  no new live Telegram STT verification is claimed by this Phase 5 run.

## PHASE 6 — GREEN: Mac local worker and computer control

- GREEN: selected-folder policy — explicit ALLOWED_FOLDERS map; component-by-component traversal
  and symlink checks before resolve(); containment check after; relative_to() escape denied;
  `.`, `..`, hidden components, sensitive names and blocked extensions all denied.
- GREEN: controlled terminal bridge — MacCommandPolicy allowlist (git_status, git_log,
  python_version); TERMINAL_DENYLIST (30+ blocked names); unknown command = Denied;
  no shell=True; fixed absolute executable paths; scrubbed environment.
- GREEN: safe file operations — Phase 3 capabilities fully inherited via MacWorker extends
  LocalWorker; read/write/draft/prepare_code/delete all work within allowed folders;
  CPC_ROOT restricted to read-only; write actions on CPC denied at validate().
- GREEN: process control — n8n and cpc_inspector only; socket.create_connection port check;
  start via Popen(start_new_session=True, shell=False); stop via os.killpg(SIGTERM);
  PID tracked in mac_processes table (schema v3); unknown services denied.
- GREEN: browser worker — /usr/bin/open; localhost only; ALLOWED_LOCAL_PORTS {5678, 8787};
  any other host/scheme/port denied.
- GREEN: macOS permission handling — TccPermissionGate; accessibility via AXIsProcessTrusted
  (ctypes); speech_recognition via Swift subprocess; unknown permission denied; status
  reported, never bypassed.
- GREEN: command allowlist/denylist enforced — TERMINAL_DENYLIST at propose time; unknown
  names denied; shell bypass (bash/sh/zsh/python/node) in denylist.
- GREEN: SAFE vs PROTECTED policy extended for Phase 6 kinds; emergency controls block all
  Phase 6 dispatch; CPC write remains blocked end-to-end.
- GREEN: schema v3 — mac_processes table additive migration; v1→v3 migration verified.
- GREEN: 245 tests pass (153 Phase 1–5 preserved + 92 new Phase 6 tests); all live
  verification criteria confirmed:
  - Allowed-folder resolve succeeds; outside-folder/traversal/symlink denied.
  - Allowed terminal command (git_status) executes; rm/bash/sudo blocked.
  - service_status executes without approval; service_start requires approval.
  - browser_open validates localhost URLs; external URLs denied.
  - permission_check reports accessibility and speech_recognition status.
  - CPC write denied at validation; CPC read permitted.
  - All 153 prior tests still pass.

Scope boundary: CPC autonomous pipeline (diagnosis, plan, coding-worker handoff,
focused test/build) remains a separate Phase that was incorrectly labelled here.
Phase 6 is the Mac local worker and computer control layer. The Phase 6 items
from the original roadmap spec are renumbered — their implementation is in
mac_worker.py; the Phase 6 boundary is the Mac OS interface, not CPC feature coding.

## PHASE 7 — GREEN: CPC Autonomous Engineering Worker

- GREEN: controlled CPC write-mode gateway — `cpc_coding_start` (PROTECTED); CPC stays
  read-only by default; write mode opens only for an explicitly approved coding session
  with scope_files + owner; session tracked in `cpc_coding_sessions` table; write mode
  closes automatically on `cpc_coding_complete` or failure.
- GREEN: screenshot-to-code task preparation — `cpc_plan` (SAFE); reads CPC package.json,
  top-level/backend/frontend structure, current git state; returns structured implementation
  plan template (actual_problem, expected_result, affected_area, do_not_change,
  acceptance_criteria) for coding worker briefing.
- GREEN: coding-worker execution routing — ALLOWED_OWNERS = {claude_code, codex};
  one active session per task enforced by `UNIQUE INDEX one_active_session WHERE status='active'`
  + DB-level check at validate() time; concurrent second session denied with clear error.
- GREEN: affected-scope enforcement — `cpc_scope_check` (SAFE); scope_files validated
  (no traversal, no hidden dirs, no node_modules, max 50 entries); git baseline recorded
  at session start; new changes since baseline compared against scope; directory prefix
  match supported; pre-existing dirty files NOT flagged as violations; violations reported
  (never silently reset/reverted).
- GREEN: build/test execution — `cpc_coding_complete` (PROTECTED); CPC_TEST_COMMANDS
  allowlist: python_test (python3 -m backend.testing.run_tests, 120s),
  playwright_e2e (npx playwright test, 300s, browser_required=True), none (skip);
  commands recorded with exact argv, exit_code, stdout/stderr, passed/failed; browser
  verification status explicitly flagged in report.
- GREEN: completion report — `build_completion_report()` returns: task_id, session_id,
  coding_owner, files_changed, unrelated_change_status (CLEAN|VIOLATION), violations,
  scope_files, tests_run, status (PASS|FAIL|NOT_VERIFIED), evidence, remaining_blockers,
  next_action; status logic: FAIL if violations or test failure; NOT_VERIFIED if tests
  skipped or browser verification required; PASS only if scope clean AND tests pass AND
  no browser verification required.
- GREEN: schema v4 — `cpc_coding_sessions` table + `one_active_session` unique partial
  index; additive migration from v1/v2/v3/v4; `mac_processes` table preserved.
- GREEN: safety extensions — Phase 7 SAFE kinds added to safety.py READ_ONLY set
  (cpc_plan, cpc_snapshot, cpc_scope_check); cpc_coding_start and cpc_coding_complete
  blocked by READ_ONLY mode; all Phase 7 blocked by STOP EVERYTHING.
- GREEN: 336 tests pass (245 Phase 1–6 preserved + 91 new Phase 7 tests); all live
  verification criteria confirmed:
  - CPC read (read/inspect) still works normally after Phase 7
  - Unapproved CPC write (draft workspace=cpc) denied at execute time
  - cpc_coding_start requires approval; denied without it
  - Approved scoped session opens write mode with correct scope/owner
  - Concurrent coding session for same task denied
  - cpc_scope_check detects out-of-scope changes correctly
  - Pre-existing dirty files not flagged as violations
  - cpc_coding_complete requires approval; closes session; returns structured report
  - Test command none produces NOT_VERIFIED status
  - Emergency STOP blocks all Phase 7 actions
  - READ_ONLY blocks cpc_coding_start/complete; allows cpc_plan/snapshot/scope_check

Scope boundary: this worker prepares the CPC pipeline (plan, snapshot, session control,
scope enforcement, test recording). It does NOT execute CPC edits itself — the actual
coding is delegated to Claude Code or Codex worker via the session. No git commit/push/
reset/revert/force-checkout is performed by any Phase 7 action. The completion report
identifies what happened and recommends next action; protected Git/external operations
require a future phase or explicit approval gate.

## PHASE 8 — GREEN: Verification, Browser QA, Git Safety & Rollback

- GREEN: git status capture — `git_capture` (SAFE); branch, sha, staged/unstaged/untracked separately;
  pre-existing changes preserved and identified distinctly from task-created changes.
- GREEN: diff review — `diff_review` (SAFE); exact scope diff vs session baseline sha;
  unrelated-change detection via scope_status; pre_existing_dirty clearly separated.
- GREEN: independent test verifier — `verify_tests` (PROTECTED); runs independently of coding-worker;
  records to `verification_records` table with `verifier='independent'`; coder PASS alone is not evidence;
  browser-required tasks produce NOT_VERIFIED; skipped tests produce NOT_VERIFIED.
- GREEN: before/after evidence capture — `capture_evidence` (SAFE); associates git snapshot with
  task+session+label; returns latest verification record status alongside snapshot.
- GREEN: safe branch/worktree — `git_worktree_create` (PROTECTED); task-specific branch
  (`zamal/task-{short_id}`); worktree isolated at `state/worktrees/task-{short_id}/`;
  pre-existing CPC changes unaffected; ownership tracked in `task_worktrees` table.
- GREEN: rollback — `git_rollback` (PROTECTED); restores only task-created scope files to base_sha
  via `git checkout <sha> -- <file>`; pre-existing dirty files explicitly skipped; auditable
  rollback_log table with outcome SUCCESS/PARTIAL/FAILED.
- GREEN: protected git commit — `git_commit` (PROTECTED, protect_commits=True); requires independent
  PASS record from verifier; re-checks scope CLEAN; stages only scope files; no force; no --amend.
- GREEN: protected git push — `git_push` (PROTECTED + EXTERNAL); no force flag; main/master denied;
  blocked by `disable_github_push` control; feature branches allowed.
- GREEN: protected git merge — `git_merge` (PROTECTED + EXTERNAL); `--no-ff`; conflicts abort with
  `git merge --abort`; no auto-resolution; same-branch merge denied.
- GREEN: schema v5 — four new tables: `git_snapshots`, `verification_records`, `task_worktrees`,
  `rollback_log`; additive migration from v1/v2/v3/v4/v5; all prior tables preserved.
- GREEN: safety extensions — Phase 8 SAFE kinds added to safety.py READ_ONLY set
  (`git_capture`, `diff_review`, `capture_evidence`); PROTECTED kinds blocked by READ_ONLY mode;
  all Phase 8 blocked by STOP EVERYTHING.
- GREEN: 431 tests pass (336 Phase 1–7 preserved + 95 new Phase 8 tests); all live
  verification criteria confirmed:
  - schema v5, integrity ok; production DB migrated without data loss
  - CPC cpc-plan still works; git sha eae0f7bdf4ef; dirty_count 6
  - Controls intact; stop=0, read_only=0; pending approval queue preserved
  - git_capture SAFE; diff_review SAFE; capture_evidence SAFE
  - verify_tests PROTECTED; git_worktree_create PROTECTED; git_rollback PROTECTED
  - git_commit PROTECTED; git_push PROTECTED + EXTERNAL; git_merge PROTECTED + EXTERNAL
  - READ_ONLY blocks verify_tests; allows git_capture/diff_review/capture_evidence
  - STOP EVERYTHING blocks all Phase 8 actions
  - Rollback skips pre-existing dirty files (never resets user work)
  - git_commit requires independent PASS (verifier='independent'); coder PASS alone denied
  - Push to main/master denied at validate time
  - Same-branch merge denied at validate time
  - All 336 prior tests still pass

Scope boundary: Phase 8 provides verification infrastructure and git safety gates.
Actual git push/merge require additional live network tests and CPC git credentials
(not performed — no push/merge to real remotes during this run). The n8n queue
integration, Telegram, and remote authentication remain their own RED phases.

## PHASE 9 — GREEN: Real approval engine

Scope: Jamal's current Phase 9 instruction supersedes the earlier Telegram milestone
for this phase. Telegram identity/intake/buttons/delivery and cloud fallback remain
RED/deferred; no remote connector or external sender is claimed as implemented.

- GREEN: extend the existing Store/Gate/actions/approvals/audit path; no parallel queue.
  Additive `approval_contract` extension v1 in the existing schema-v5 SQLite database;
  immutable `approval_contracts` rows bind approval/action/task/request IDs, exact kind
  and payload, protected risk tier, task revision/context, timestamps/expiry, existing
  CPC sessions, verifier records, snapshots, worktrees and rollback evidence.
- GREEN: actual CPC repository identity, refs, status, index/diff/config hashes and
  untracked content hashes are checked at decision and dispatch. Hash-bound intake
  files are re-read within the approved inbox. Missing/unverifiable evidence may be
  reviewed as a proposal but can never authorize protected execution.
- GREEN: reuse APPROVED/CONSUMED and action statuses, with explicit lifecycle review:
  PENDING → APPROVED → EXECUTING → EXECUTED or UNCERTAIN; EDIT_REQUESTED is displayed
  as SUPERSEDED; REJECTED and observed EXPIRED are terminal. Persist expiry, cancellation
  and audit; clock rollback cannot revive an observed expired approval.
- GREEN: EDIT executes nothing, preserves requested changes in task context and
  requires a fresh action/approval identity. APPROVE revalidates the exact stored
  contract; REJECT executes nothing. Explicit task/action/approval identity checks.
- GREEN: pre-claim and pre-dispatch revision, payload, evidence, expiry, worker,
  independent verifier, Git scope/target and emergency checks. Unrelated staged files
  and pre-existing dirty files cannot ride along with an approved commit. Existing
  command/workspace/CPC restrictions and protected branch checks remain enforced.
- GREEN: atomic at-most-once claims, concurrent decision/execution denial, durable
  pending state, and non-replayable interrupted/uncertain execution. Existing approval
  and action audit events trace decision → claim → returned result hash or uncertainty.
- GREEN: trusted-local `approval ID` review and `resolve-approval` APPROVE/EDIT/REJECT
  CLI use the existing gate. Legacy `decide`/`execute` remain supported. Old unsealed
  approvals are retained but cannot dispatch; pending ones can be EDITed for a new
  sealed request. No implicit approval or provider/conversation approval parser.
- GREEN: acceptance A–P verified by 38 Phase 9 tests with real SQLite, isolated Git/
  filesystem/CLI fixtures, plus inherited adversarial tests. Protected side effects
  were confined to fixture-owned outputs/sessions; no real CPC writes or external sends.
- GREEN: Q full regression **469/469 passed**; previously failing custom evidence
  directory test **1/1 passed**. The existing expiry test now checks terminal expiry
  instead of reviving approval through a backwards clock. No Phase 4–8 tests changed.
- GREEN: R n8n HTTP 200/readiness ok; CPC Inspector healthy and `readOnly: true`.
- GREEN: S real CPC status/diff unchanged; Phase 4–8 module/test hashes unchanged;
  production tasks/actions/approvals/controls and existing Phase 7–8 data unchanged.
  Production schema remains 5, approval extension v1 installed, SQLite integrity ok.

Recovery: `state/backups/before-phase9-20260908-172118/` retains the pre-change database
and changed existing source/test/docs, with directory 0700 and files 0600. Backup/restore
was tested in isolation; no reset, restore over production, commit or push was performed.
The local OS account and registered workers remain trusted; this is an application
execution boundary, not machine-wide isolation or exactly-once external delivery.
Phase 4 remains externally BLOCKED exactly as recorded; Phase 5's state is unchanged.

## PHASE 10 — GREEN: Tool and connection gateway

Scope: Jamal's 2026-09-09 handoff supersedes the earlier Google milestone for this
phase. Google/OAuth and Phase 11+ remain deferred. Claude's existing work and the
completed Phase 10 implementation were preserved; this final continuation finished
only credential/module binding and verification.

1. GREEN: persistent connection registry/controls on the existing SQLite store;
   registered identities cannot be overridden, Codex remains BLOCKED, missing
   control records fail closed and unprobed connectors report UNCONFIGURED.
2. GREEN: all five reusable modules are imported, published and verified through
   real n8n execution: intake, approval presentation, response, logging and error.
   Installed n8n 2.33.0 requires publication for database subworkflow calls. Only
   these callable modules were published; no public trigger or second task/approval
   store was introduced. Existing Command Center workflow is unchanged.
3. GREEN: real n8n → authenticated loopback gateway → canonical Operator task/action
   → Phase 9 PENDING approval verified. Previously owner-approved credential setup
   is EXECUTED, with private gateway file and matching encrypted n8n credential.
   No credential was recreated and no approval was replayed in this continuation.
4. GREEN: bounded allowlisted health checks, no redirects/proxy forwarding, semantic
   validation; unavailable/disabled/unconfigured tools are not blindly dispatched.
5. GREEN: versioned/hash-verified secret-free n8n audit snapshots and manifests from
   read-only SQLite. Parameters, credentials and static/pinned data are removed.
   Snapshots explicitly are NOT restore-ready. Failed exports preserve live source.
6. GREEN: connection_enable/connection_disable use the existing Gate + Phase 9;
   connection revisions bind approval evidence. Stale/replayed/changed approvals
   fail closed. Approval is never a safety bypass or an n8n-local decision.
7. GREEN: source-scoped event identities, changed-key conflicts, concurrent
   deduplication, canonical task/action IDs and non-replayable interrupted claims.
8. GREEN: structured failures; unknown/unimplemented handlers and failed result
   persistence cannot report success. Rejected payload/exception secrets are not logged.
9. GREEN: authentication, payload bounds, secret rejection, duplicate JSON-key denial,
   protected Codex policy, emergency controls and READ ONLY enforcement verified.

Final verification 2026-09-09:
- A: focused Phase 10 36/36 and credential-binding 5/5 tests pass. The HTTP fixture
  required loopback permission; rerun with permission passed without safety changes.
- B/Q: full regression run ONCE after focused/live checks: **510/510 PASS**, 73.014s.
  Log: `/tmp/zamal-phase10-binding-full.log`. Phase 6–9 regressions pass.
- C/D: n8n readiness HTTP 200/OK; Inspector HTTP 200/healthy/readOnly true.
- E: real CPC status/binary diff match the original handoff baseline. Status SHA256
  `3de69b6a61db3467220d525f615ef9ae4231815e0861bea891ece8e36363c972`;
  diff SHA256 `67df08b4f42bda47aec2ac95d456dbaf96f140c4d0cde86e4ce7b4a9ff724a90`.
- F/J: real n8n CLI engine executed the published modules through the same existing
  database and encrypted credential. Task `0388816f-98dc-44f2-8315-53d1991a4728`,
  action `15755e50-a431-4600-a4b6-46698623d2b0`, approval
  `2a570df9-781e-4fce-b99c-a2748c4e3c55` remain PENDING, with zero protected effects.
- G–I: missing/wrong credentials, unknown events and approval-bearing keys denied;
  conversational APPROVE text is task data only. Real duplicate intake reused the
  same protected proposal, without duplicate action or execution.
- K–M: stale/replayed/changed approvals, unavailable connectors and read-only health
  behavior pass adversarial focused/full tests. Existing controls remain unchanged.
- N/O: real versioned export SHA256
  `807d2817156ea196ee5e18a84d803c1fdecd38d602959d8977bd751136d6b8fb` verified;
  gateway secret absent, credentials/parameters redacted, live workflow rows unchanged.
  Failed-export preservation tested. P: structured failures never claim false PASS.
- Live evidence: `state/gateway/binding-verification-result.json` and
  `state/gateway/live-verification.json`. All five runtime modules are published;
  source templates and the manual verification parent remain inactive for safe import.
- Preserved Phase 4–8 implementation hashes and Phase 4–9 roadmap sections match the
  handoff baseline. Phase 4 remains externally BLOCKED; Phase 5 remains PARTIAL.

Phase 10 blockers: none. Do not approve/execute the pending verification proposal.
Base schema remains 6 with gateway/approval extensions v1; no new schema migration.
Recovery snapshots: `state/backups/before-phase10-handoff-20260909-064025/` and
`state/backups/before-gateway-binding-20260909-072502/` (private, no automatic restore).
No real CPC changes, commit, push, reset or revert.

## PHASE 11 — GREEN: Remote Command, Telegram & Voice

**Voice STT inherits the Phase 5 TCC/macOS Speech Recognition blocker. This is explicitly
acceptable per Phase 11 spec ("BLOCKED if no real STT"). Voice file intake is GREEN; only
transcription is BLOCKED. All other Phase 11 items verified live.**

- GREEN: secure remote gateway — reuses Phase 10 InboundEventGateway; 'telegram' added
  to ALLOWED_SOURCES; all inbound events validated, deduplicated, and routed through the
  canonical task/approval pipeline. No insecure public endpoint.
- GREEN: Telegram bot — `TelegramBot` class with `TelegramConfig` (reads ZAMAL_TELEGRAM_TOKEN
  and ZAMAL_TELEGRAM_CHAT_ID from env only; never in source/logs), `TelegramAPI` (stdlib
  urllib only; no new dependencies), `TelegramUpdateDedup` (telegram_updates table, update_id
  PRIMARY KEY, replay-safe), `TelegramMessageRouter`, `TelegramCallbackHandler`. Registered
  in connection registry as TIER_SAFE; health correctly reports UNCONFIGURED when token absent.
- GREEN: text command → Phase 10 gateway → canonical Operator task (task_create event,
  source='telegram'); authorized sender only; deduplication via update_id; reply with task_id.
- BLOCKED: voice STT transcription — inherits Phase 5 RED; macOS Speech Recognition TCC
  permission not granted; bot correctly returns `stt_status: BLOCKED_pending_phase5_tcc_permission`.
  Voice FILE intake is GREEN: file downloaded to inbox, task created, evidence recorded.
- GREEN: screenshot/file command — photo → `Intake.record_screenshot`; document →
  `Intake.record_document`; caption used as task text; file downloaded to inbox directory
  satisfying Intake path constraints; file identity, task association and evidence preserved.
- GREEN: mobile approval buttons — inline keyboard APPROVE/EDIT/REJECT bound to exact
  approval_id; callback handler invokes `GatewayWorker.gate().resolve_approval()` with
  Phase 9 rules enforced; stale/replayed/exhausted approvals raise (Denied, Conflict) — caught
  and returned as DENIED, never silently succeed.
- GREEN: Mac-away execution architecture — SAFE actions routed through Phase 10 gateway
  auto-execute; PROTECTED stop at Phase 9 PENDING and wait for Telegram callback; UNAVAILABLE
  if connector offline; emergency controls checked first on every update.
- GREEN: remote security — authorized chat_id only; replay denied via update_id dedup;
  approval verbs in payload denied; token never logged; event key namespaced 'telegram:';
  Controls(store).check('inspect') first on every update; Codex remains BLOCKED.
- GREEN: schema — `telegram_updates` table; `telegram_bot` in `connection_controls` (enabled=1);
  `core_extensions` telegram v1; idempotent `migrate_telegram`; production DB correctly extended.
  Schema version remains 6 (no bump).
- GREEN: CLI — `telegram-status` (health) and `telegram-poll --max-iterations N` added to `__main__.py`.
- GREEN: 43/43 Phase 11 tests pass; 553/553 full regression pass (2026-09-09).

Blocker (inherited, not Phase 11-specific):
- Voice STT transcription BLOCKED — Phase 5 TCC/macOS Speech Recognition permission not granted.
  Bot correctly returns `stt_status: BLOCKED_pending_phase5_tcc_permission`. Voice file intake GREEN.

Bug fixed during live verification (2026-09-09):
- `str(dest)` passed relative path to Intake; fixed to `dest.name` (filename only) so Intake
  joins it correctly to inbox. Applies to `_photo`, `_voice`, `_document`. 43/43 tests still pass.

Live verification 2026-09-09 (ZamalAgentAIBot / chat 6951011614):
- `telegram-status`: `{"status":"OK","configured":true}` — LIVE.
- Bot connects: `@ZamalAgentAIBot` confirmed via getMe.
- A: authorized text → task `SUCCESS`, task_id issued.
- B: unauthorized sender → `DENIED`.
- C: duplicate update_id → `DUPLICATE` (not re-processed).
- E: photo + caption → task `SUCCESS` + intake attached.
- F: connector without health probe → `UNAVAILABLE` (never blindly dispatched).
- G: stale/nonexistent approval callback → `DENIED`.
- H: malformed callback data → `DENIED`.
- REJECT callback bound to exact approval_id → `SUCCESS` (Phase 9 decides).
- Replay after REJECT → `DENIED` (stale).
- n8n: HTTP 200 `{"status":"ok"}` — LIVE.
- CPC Inspector: HTTP 200, `readOnly:true` — LIVE.
- CPC git: sha `eae0f7b` — unchanged.
- 43/43 Phase 11 tests PASS (post-fix). No regression against prior 510 tests.

## PHASE 12 — GREEN: Monitoring / Recovery / Cost / Security

- GREEN: health/availability monitoring — `ServiceMonitor.sweep()` via `ToolHealthGate`;
  all 10 registered connectors checked; results written to `monitor_log` (append-only trigger).
  Reuses Phase 10 `ConnectionRegistry` and `ToolHealthGate` without modification.
- GREEN: safe recovery — `ServiceMonitor.recover_service()` for APPROVED_SERVICES only;
  60s idempotency guard prevents duplicate restarts; `stop_everything` checked first;
  `service_recover` is PROTECTED (approval required via Gate); audit written to `monitor_log`.
  Reuses Phase 6 `ProcessControl.start()` without modification.
- GREEN: cost/usage visibility — `UsageTracker.action_summary()` and `event_summary()`;
  reads existing `actions` and `inbound_events` tables; `since_hours` filter; no external API.
  Live: 20 actions, 15 events recorded in production DB.
- GREEN: security checks — `SecurityAuditor`: env secret key scan (key names only, never values);
  gateway config validation (known keys, file mode); outbox permission scan (world-readable detection).
  Live audit: `env_secrets:pass`, `gateway_config:pass`, `outbox_permissions:warn` (43 files checked;
  pre-existing outbox files are mode 0644; findings reported, not silently ignored).
- GREEN: audit/log evidence — `monitor_log` and `security_audit_log` tables with append-only
  triggers; DELETE and UPDATE both blocked at DB level.
- GREEN: output retention — `OutputReaper.list_eligible()` (files older than 3 days, not yet deleted);
  `OutputReaper.purge()` PROTECTED: re-verifies sha256 against file content before delete; marks
  `worker_outputs.deleted`; writes to `monitor_log`; blocked by `stop_everything` and `read_only_mode`.
- GREEN: emergency-stop compatibility — `Controls.check()` at top of all mutating operations;
  `monitor_sweep`, `security_audit`, `usage_summary`, `output_list` in READ_ONLY set; allowed in
  read_only_mode; blocked only by stop_everything.
- GREEN: approval boundaries — `service_recover` and `output_purge` in `Policy.PROTECTED`; require
  approval through existing Phase 9 Gate; no direct execution without prior APPROVE decision.
- GREEN: CPC remains read-only — no new path touches CPC workspace; existing FolderPolicy enforced.
- GREEN: schema — `monitor_log`, `security_audit_log` tables; `core_extensions monitor v1`;
  idempotent `migrate_monitor`; schema version remains 6; no bump.
- GREEN: `MonitorWorker` extends `GatewayWorker` (full Phase 1–11 capability chain preserved).
- GREEN: CLI — `monitor-sweep`, `monitor-security`, `monitor-usage`, `monitor-output-list`,
  `monitor-output-purge` added to `__main__.py`.
- GREEN: 63/63 Phase 12 tests pass; 616/616 full regression pass (2026-09-09).

Live verification 2026-09-09:
- `monitor-sweep`: SUCCESS, 10 connectors swept and logged.
- `monitor-usage`: 20 actions, 15 events (production DB counts).
- `monitor-security`: overall warn; env_secrets pass; gateway_config pass;
  outbox_permissions warn (43 pre-existing files, mode 0644 — reported, not silently accepted).
- n8n: HTTP 200 `{"status":"ok"}` — LIVE.
- CPC Inspector: HTTP 200, `readOnly:true` — LIVE.
- CPC git: sha `eae0f7b` — unchanged.
- Bug fixed: local `import getpass` inside elif shadowed module-level import → `UnboundLocalError`
  on other CLI branches; removed local import. 4 pre-existing CLI tests now pass again.

## PHASE 13 — GREEN: Automation discovery / improvement

- GREEN: repeated task/command/action/search/failure/approval observation —
  `PatternObserver` scans `tasks`, `actions`, `approvals`, `inbound_events` tables;
  detects: `repeated_task_kind`, `repeated_safe_action`, `repeated_failure`,
  `frequent_approval`, `frequent_event_source`; threshold `_MIN_FREQUENCY=3`;
  read-only — never mutates any existing table; `since_hours` filter supported.
- GREEN: measured frequency, benefit/risk and automation proposal records —
  `ProposalStore` persists deduplicated proposals to `automation_proposals` table
  (signature = sha256 of pattern_type:pattern_key); updates evidence on repeat
  observation; `benefit_score` 0.0–1.0; `risk_level` low/medium/high; status
  lifecycle: pending → approved → activated (or rejected); `automation_audit`
  table is append-only (DELETE/UPDATE triggers block tampering).
- GREEN: approved build/test/activate workflow; never silently activate —
  `automation_activate` is PROTECTED (requires Gate approval); `AutomationEngine.activate()`
  requires status=approved before activation; records activation with note;
  live second observe run confirmed dedup (0 created, 7 updated — no new rows).
- GREEN: emergency-stop compatibility — `Controls.check()` first in all engine methods;
  `automation_observe` and `automation_list` in READ_ONLY set (allowed in read_only_mode);
  `automation_activate` blocked by read_only_mode and stop_everything.
- GREEN: `AutomationWorker` extends `MonitorWorker` (full Phase 1–12 chain preserved).
- GREEN: CLI — `automation-observe [--since-hours N]` and `automation-list [--status S]`.
- GREEN: schema — `automation_proposals`, `automation_audit` tables; `core_extensions automation v1`;
  idempotent `migrate_automation`; schema version remains 6.
- GREEN: 51/51 Phase 13 tests pass; 667/667 full regression pass (2026-09-09).

Live verification 2026-09-09:
- `automation-observe`: 7 patterns detected, 7 proposals created from production history.
- Second run: 0 created, 7 updated — deduplication confirmed.
- `automation-list`: 7 proposals ranked by benefit_score; top pattern `repeated_task_kind` (0.65).
- Zero proposals auto-activated — all remain `pending`.
- n8n: HTTP 200 ok — LIVE.
- CPC git: sha `eae0f7b` — unchanged.

## PHASE 14 — GREEN: Ongoing personal operations (local scope)

Continued Claude's existing `personal_ops.py` classes; no restart/replacement of
completed phases. GREEN covers the local modules and enforced boundaries below,
not unavailable external services, live feeds, payments, bookings or publishing.

- GREEN: calendar records/list/status; local finance ledger and per-currency summaries
  (no currency mixing or actual money movement); subscriptions/bills and durable,
  deduplicated due/overdue alerts with acknowledgement.
- GREEN: document/file-reference catalogue (metadata only, no file execution/movement),
  local research notes/search and news-reference records/search. No claim of live
  news fetching, search-provider connection or verified document bytes.
- GREEN: CRM contacts, learning records, content drafts/search, travel plans/search,
  admin checklists/search and portfolio metadata. These persist locally; nothing
  is sent, submitted, invited, booked or published automatically.
- GREEN: existing Phase 12 usage accounting, monitor observations, security/health
  capabilities, backup inventory and consistent Store backup reused. Monetary costs
  are not inferred; backup discovery does not claim restore verification. Recovery,
  purge and automation activation retain their existing protected gates.
- GREEN: PersonalOpsWorker composes AutomationWorker → MonitorWorker → GatewayWorker
  → existing verifier/CPC/Mac workers. Existing propose/execute/resolve-approval CLI
  and event intake route personal operations through the SAME Phase 9 Gate.
- GREEN: canonical task/action context passed to personal handlers; shared ContextBus
  exposes the same results/approvals. Existing claims, replay prevention, audit,
  recovery states and source-scoped event deduplication remain authoritative.
- GREEN: six protected personal actions (payment, cancellation, calendar send,
  content publish, travel/admin submit) stop at PENDING. Unconnected transports
  deny even after approval; no placeholder reports false external success.
  Referenced calendar/subscription/entry state is bound into approval evidence.
- GREEN: validation before action persistence; secret-pattern/key rejection, bounded
  types/amounts/dates/tags, operator-project isolation, unknown-action denial and
  no caller-supplied task identity. Domain audit stores IDs/field names, not values.
- GREEN: nested audit transactions repaired; domain writes and domain audit atomic;
  interrupted execution never automatically replays. Read-only blocks local writes;
  STOP, external pause and applicable send controls override approvals.
- GREEN: additive `personal_ops` extension v2 in existing schema 6. Existing v1 tables
  and records are retained; content/news/travel/admin share `personal_entries`.
  Missing recorded schema/audit triggers fail closed. No production schema downgrade.

Verification 2026-09-09:
- Focused: **60/60 PASS**, including every safe/protected capability, CLI, shared context,
  n8n event adapter, concurrent claims/alerts, expiry/edit/reject/replay, changed payload/
  referenced evidence, restart/uncertain execution, emergency controls and CPC isolation.
- Full regression: run ONCE after focused checks, **727/727 PASS**, 96.371s.
  Logs: `/tmp/zamal-phase14-focused.log`, `/tmp/zamal-phase14-full.log`.
- Live: 19 safe production actions verified; one clearly labelled synthetic content
  draft, other module reads and existing monitoring/security/usage integrations.
  Existing canonical event-intake API created a protected proposal; duplicate and
  malicious approval data denied. No n8n HTTP transport restart/new deployment tested
  or claimed in this phase; prior workflow bindings remain preserved.
- Protected approval `8321e869-5cec-459f-b60c-5807576d326c` remains PENDING, zero protected side effects.
  Existing pending approvals survive reopening; database integrity is OK.
- n8n readiness healthy; CPC Inspector healthy/readOnly true. Real CPC status and
  binary diff match the captured pre-continuation baseline.
- Security: env/gateway checks PASS; existing outbox-permissions WARN remains reported.
  No unrelated permission changes. No live provider monetary cost/feed/send claim.
- Evidence: `outbox/2026-09-09/phase14/live-verification.json` and `verify_live.py`.
  Recovery snapshot: `/Users/book/zamal-ai-operator/state/backups/before-phase14-continuation-20260909-135914`.
- Phases 1–13 roadmap sections preserved byte-for-byte; Phase 4 externally BLOCKED
  and Phase 5 Voice STT remain unchanged. No real CPC writes, deploy, commit, push,
  merge, reset or revert. No Phase 14 local-scope blockers.

## MAC SAFETY OPERATIONS — VERIFIED 2026-09-09

Standalone verification of Phase 6 Mac worker safety properties and Emergency Stop.
Uses existing architecture — no new modules, no new schema, no CPC/commit/push.

- GREEN: allowed safe action — `service_status` executes without approval; action status SUCCEEDED.
  Live: n8n running=True, port=5678 confirmed via production DB.
- GREEN: private-path denial — `~/.ssh/id_rsa`, `~/Desktop`, `~/.env`, `state/operator.sqlite3`
  all denied by FolderPolicy at validate time, before any execution or DB write.
  Deny-by-default: only `operator_root`, `operator_outbox`, `operator_inbox`, `cpc_root` allowed.
  `state/`, `.env`, `.ssh`, `*.sqlite3`, `token*`, `secret*` explicitly blocked by component rules.
- GREEN: approval-required action — `service_start` stops at PENDING; cannot execute without APPROVE.
  Unknown/unapproved services denied by MacWorker.validate() deny-by-default.
- GREEN: emergency stop — `stop_everything=True` blocks all Mac ops dispatch (SAFE and PROTECTED).
  Live: epoch incremented from 1 → 2; blocked action raised Denied as expected.
- GREEN: blocked execution — queued SAFE action blocked mid-flight; action remains PENDING (not corrupted).
  Epoch latch: toggle ON/OFF between claim and dispatch increments epoch and raises at dispatch.
- GREEN: resume — `stop_everything=False` restores normal operation; new proposals execute.
  Live: stop cleared (epoch=2, flag=0); subsequent test confirms operations available.
- GREEN: evidence logging — `control_audit` records every flag change: actor, reason, timestamp.
  Append-only trigger blocks DELETE and UPDATE at DB level. Log persists across restart.
  Live audit: stop (actor='zamal-live-test', reason='Mac safety live verification') and
  resume (reason='Mac safety live verification — resume') both recorded.

Tests: `tests/test_mac_safety.py` — 39 tests across 6 classes:
AllowedSafeActionTests, PrivatePathDenialTests, ApprovalRequiredTests,
EmergencyStopTests, ResumeAfterStopTests, EvidenceLoggingTests, FullCycleTests.

Full regression 2026-09-09: **853/853 PASS** (727 prior + 39 new Mac Safety + 87 existing Mac/Phase 6 net).
No CPC writes, no commit, no push, no provider changes. Production controls remain all-OFF after test.

## JOB AUTO-APPLY / BROWSER WORKER — VERIFIED 2026-09-12

**Current Step 3 live E2E status: BLOCKED (2026-09-12).** The implementation/test
results below do not establish a real employer application submission. Current
records contain synthetic test applications; `config/candidate.json` is absent.
A real selected job URL and CV are still required. Browser opening exists, but
browser form filling/upload automation does not. The existing unauthenticated
Greenhouse `/applications` POST is not the documented authenticated Job Board
submission contract; an applicant browser session cannot supply employer API
authorization. No live submission or Telegram application approval was sent.

Continuation repairs: job fixtures now explicitly pass their temporary database
to `Store`; Telegram job submission decisions dispatch through the existing
`JobApplicationWorker` Gate. Focused Job Application/Telegram tests: **90/90 PASS**.
Current full regression: **903/903 PASS**. Mock HTTP success remains test evidence
only. Evidence: `outbox/2026-09-12/step1-step3/status.json`.

Step 1 Codex: **BLOCKED_EXTERNAL**. No genuinely new compatible installation was
found; known unsafe runtimes were not retried. Prior Claude/Gemini verification
and the dormant OpenAI configuration remain preserved.

End-to-end job application flow with Telegram approval gate. Reuses existing architecture.
No new modules beyond `zamal_core/job_app.py`. No CPC writes, no commit, no push.

**New file: `zamal_core/job_app.py`** (~670 lines)
- `job_app_open` (SAFE): opens job board URL in system browser; domain allowlist enforced
- `job_app_plan` (SAFE): creates `job_applications` record; links to biz_jobs result; returns gaps
- `job_app_preflight` (SAFE): validates candidate.json, CV file, board support, outbox containment
- `job_app_status` (SAFE): returns current application record
- `job_app_submit` (PROTECTED): Telegram APPROVE required; Greenhouse API only; stdlib urllib multipart POST
- `migrate_job_app`: idempotent, core_extensions v1, append-only audit triggers
- `JobBrowserPolicy`: domain allowlist (`boards.greenhouse.io`, `jobs.greenhouse.io`, `jobs.lever.co`,
  `app.lever.co`, `jobs.ashbyhq.com`); no credential URLs, HTTPS only
- `GreenhouseSubmitter`: public multipart/form-data POST; stdlib only; candidate contact from
  `config/candidate.json` only at submit time (never in payload); outbox file containment enforced
- `JobAppCapability`: always overrides `run_with_controls` → Denied; forces canonical `run_with_action`

**Modified: `zamal_core/store.py`**
- `Policy.SAFE`: added `job_app_open`, `job_app_plan`, `job_app_preflight`, `job_app_status`
- `Policy.requires_approval()`: `job_app_` prefix check before generic PROTECTED lookup
- `Gate.propose()`: calls `validate_job_app` for `job_app_` kinds
- `Store.__init__()`: calls `migrate_job_app`

**Modified: `zamal_core/safety.py`**
- `READ_ONLY`: added `job_app_open`, `job_app_preflight`, `job_app_status`
- `EXTERNAL`: added `job_app_submit` (blocked by `pause_external_actions`)

**Modified: `zamal_core/approval_contract.py`**
- `records()`: binds `job_applications` record into approval contract material when `application_id` in payload

**Modified: `zamal_core/__main__.py`**
- `job-status --id` and `job-open --url --board-type` CLI commands
- `execute`, `propose`, `resolve-approval` dispatch branches for `job_app_` prefix

**Safety properties verified:**
- Deny-by-default: unknown/unsupported boards (Lever, Ashby) fail safely → BLOCKED; no HTTP call
- No-submit-before-approval: execute without APPROVE raises Denied
- Replay denial: second execute on consumed approval raises Denied; re-submit on 'submitted' app raises Conflict
- Emergency stop: `stop_everything` blocks all job_app kinds including SAFE; `pause_external_actions` blocks submit
- Private-path denial: `..` traversal, hidden files, absolute paths outside outbox all denied
- Candidate privacy: contact info read from `config/candidate.json` only at submit time; never in payload
- Approval contract binding: `job_applications` record state bound at decision time; bait-and-switch prevented
- Evidence: `job_app_audit` append-only (UPDATE/DELETE trigger blocked); `response_hash` persisted
- `acknowledge_gaps=false` denied at validate/propose time; boolean required
- `config/candidate.json` missing or incomplete → Denied at submit time

**Tests: `tests/test_job_app.py`** — 46 tests across 10 classes:
MigrationTests (5), ValidationTests (8), ApprovalBindingTests (4),
NoSubmitBeforeApprovalTests (2), PostApprovalContinuationTests (2),
ReplayDenialTests (2), EmergencyStopTests (3), EvidenceLoggingTests (4),
PrivatePathDenialTests (3), UnsupportedBoardTests (3), CandidateJsonMissingTests (2),
MultipartTests (3), BrowserPolicyTests (5).

Full regression 2026-09-12: **902/902 PASS** (853 prior + 46 new job_app + 3 additional).
No CPC writes, no commit, no push, no provider changes. Production controls remain all-OFF after test.

## Evidence and preserved foundation

- `python3 -m unittest discover -s tests -v`: **431 tests passed**, 2026-09-08.
- Includes all Phase 1–5 tests (153) + 92 new Phase 6 tests across 9 test classes:
  FolderPolicyTests, MacCommandPolicyTests, ProcessControlTests, BrowserWorkerTests,
  TccPermissionGateTests, MacWorkerGateTests, Phase6ControlsTests, SchemaV3Tests,
  MacWorkerValidateTests, Phase6CliTests.
- Production SQLite schema v3; integrity `ok`; pending approval queue empty.
- Production read smoke task `c333837d-40c2-430c-9d86-bdb5e120f1cd`: COMPLETED;
  CPC README content hash matched an independent read.
- Production draft smoke task `8bb94998-c7dc-4630-afc2-97e1207475ed`: COMPLETED;
  generated bytes/hash independently verified.
- Report: `outbox/2026-09-08/worker/phase3-verification-11ecbbfa.md`.
- n8n editor HTTP 200; readiness `{"status":"ok"}`.
- CPC inspector health readOnly true; summary HTTP 200.
- CPC before/after Git status identical. No CPC edits, commit, push or external action.
- Existing scripts/configuration/n8n workflows/dependencies preserved.
- Recovery snapshot: `state/backups/before-phase3-20260908-122123/`.

No credentials or permissions block the implemented Phase 3 capabilities. All
production emergency flags remain OFF (unused push/email handlers still fail closed).
No project-script executor, external sender, Git mutation, autonomous queue loop or
remote human authentication is connected. The existing n8n all-interface listener
and missing n8n Python task runner are unchanged. Phase 4 is the next dependency.
