# Zamal AI Operator — Master Architecture Decisions

## 1. Project Purpose and Guiding Principles

Zamal AI Operator is a local, autonomous agent system that understands, plans, executes, and verifies tasks on behalf of the owner (Jamal). The Operator is the single source of truth for all orchestration — it routes work to the best AI provider, enforces safety and approval gates, and maintains a complete audit trail.

**Guiding principles:**
- Owner supplies intent and approval; the Operator handles all planning, execution, recovery, and verification.
- Approval is required before any irreversible, external, or destructive action.
- Private filesystem is DENY by default; CPC project is read-only from Operator.
- Never claim success without verified evidence.
- Never weaken safety checks to make a provider work.
- Keep context compact; do not load unrelated files.
- All routing, approval, and safety decisions are logged immutably.

---

## 2. Architecture Decisions

### Central Agent
The Operator is a single, persistent Python process. All tasks flow through `zamal_core/store.py` (SQLite, task DB) and `zamal_core/safety.py` (Controls, FLAGS, audit). No external database, no cloud state.

### Shared Context
Each task carries a structured context dict (`inputs`, `plan`, `decisions`, `evidence`, `test_results`, `final_result`, etc.) that all stages (planner, coder, verifier) read and update atomically via `Store.patch_context`.

### Intent Router
`zamal_core/routing.py` maps task stage to the best provider using `PREFERENCES` per stage. The router never forces a mid-task reroute — the provider assigned at stage start completes that stage.

### Planner / Coder / Verifier
Three-stage handoff enforced by `zamal_core/handoff.py`:
- **Planner**: Claude (preferred). Produces structured plan.
- **Coder**: Codex (preferred). Executes plan, produces code/output.
- **Verifier**: Claude (preferred). Verifies, captures evidence.
Fallbacks: Gemini, then the next available provider in registry.

### Capability Registry
`zamal_core/providers.py` maintains a live registry of providers (OpenAI, Codex, Claude, Gemini). Each provider is queried for availability at task start; unavailable providers are skipped without error.

### Resumable Checkpoints
Every task state transition (`RECEIVED` → `UNDERSTOOD` → `PLANNED` → `RUNNING` → `VERIFYING` → `COMPLETED`) is persisted atomically. Tasks can be resumed from any checkpoint without data loss.

### Risk-Based Approval
`zamal_core/store.py` (`Gate`, `Policy`) classifies every action:
- **SAFE**: read, search, analyze, draft — executes automatically.
- **PROTECTED**: send, post, apply, pay, delete, push, irreversible — requires explicit human approval before execution.
Approval is single-use, bound to the exact action payload (tamper-evident binding hash), and expires after 24 hours by default.

### Telegram Control
`zamal_core/telegram_bot.py` (`TelegramConfig`) provides remote command and approval interface via Telegram. Credentials are read from environment variables only (`ZAMAL_TELEGRAM_TOKEN`, `ZAMAL_TELEGRAM_CHAT_ID`). Token is never logged or exported.

### Isolated Project Registry
CPC (`/Users/book/Desktop/AI-GROCERY-DECISION-PLATFORM`) is registered as a read-only external project. The Operator inspects it but never writes to it. The registry tracks CPC at approximately 60% completion (user estimate at registration time).

### Smart AI Router
Provider selection uses stage-specific preferences:
- Planner stage: Claude → Gemini → Codex fallback
- Coder stage: Codex → Claude → Gemini fallback
- Verifier stage: Claude → Gemini → Codex fallback
The router checks provider availability before assigning; if all providers are unavailable, the task pauses with a clear error.

### Audit Trail
`zamal_core/store.py` writes all task events, state transitions, approval decisions, and control changes to append-only audit tables (`audit`, `control_audit`). These tables have DELETE/UPDATE triggers that raise an abort, ensuring immutability.

### Emergency Stop
`zamal_core/safety.py` (`Controls`) provides five flags:
- `stop_everything` — blocks all actions
- `pause_external_actions` — blocks external side effects
- `read_only_mode` — blocks writes
- `disable_github_push` — blocks git push
- `disable_email_send` — blocks email send
Each flag change is logged with actor and reason. `Controls.check()` is called before every action execution; if any relevant flag is set, execution raises `Denied`.

---

## 3. Provider Routing Decisions

### PREFERENCES by Stage (from `zamal_core/routing.py`)
| Stage    | Primary | Secondary | Tertiary |
|----------|---------|-----------|----------|
| planner  | claude  | gemini    | codex    |
| coder    | codex   | claude    | gemini   |
| verifier | claude  | gemini    | codex    |

### Fallback Rules
- If the primary provider is unavailable (binary missing, API key absent, service unreachable), try secondary, then tertiary.
- Fallback is logged to the task audit trail.
- If all providers are unavailable, task transitions to `FAILED` with a clear error; no silent degradation.

### Never Force Reroute Mid-Task
Once a provider is selected for a stage, it completes that stage. Rerouting mid-stage (e.g., switching from Codex to Claude mid-coder execution) is not permitted; the task must fail/recover first.

---

## 4. Approval Policy

### No Approval Required (SAFE)
- read, search, analyze, inspect, compare, diagnose, verify
- draft, prepare_code, plan, research
- service_status, browser_open, permission_check
- cpc_plan, cpc_snapshot, cpc_scope_check (CPC read-only)
- git_capture, diff_review, capture_evidence (read-only verification)
- monitor_sweep, security_audit, usage_summary
- job_app_open, job_app_plan, job_app_preflight

### Approval Required (PROTECTED)
- email_send, job_submit, profile_mutate
- git_push, git_merge, git_commit (when protect_commits=True), git_reset, git_revert, git_rollback
- deploy, delete, payment, credential_change, security_change
- external_irreversible, ai_inference
- service_start, service_stop
- cpc_coding_start, cpc_coding_complete (CPC write — currently blocked)
- verify_tests, git_worktree_create
- automation_activate

**Summary**: Read / search / analyse / draft operations never require approval. Send / post / apply / pay / delete / push / any irreversible action always requires approval.

---

## 5. Security Boundaries

### CPC Read-Only
The CPC project (`AI-GROCERY-DECISION-PLATFORM`) is inspected read-only. The Operator lists files, reads content, and produces analysis. No CPC file is ever written, renamed, deleted, or moved by the Operator. This is enforced by rule, audit logging, and the `cpc_plan`/`cpc_snapshot` action kinds being SAFE (read-only), while `cpc_coding_start`/`cpc_coding_complete` are PROTECTED and require approval.

### Private Filesystem DENY by Default
The Operator only accesses:
- Its own workspace: `/Users/book/zamal-ai-operator/`
- CPC project (read-only): `/Users/book/Desktop/AI-GROCERY-DECISION-PLATFORM/`
- Specific tool binaries under `~/.nvm`, `~/.codex`
Access to `/Users/book` broadly, Photos, Documents, Downloads, iCloud, browser data, or passwords is denied.

### Secret Scanning on All Outputs
`zamal_core/integration_security.py` scans all provider outputs for secrets (API keys, tokens, passwords) before logging or returning them. Any detected secret triggers a `failure()` call that sanitizes the output and records a security audit event.

---

## 6. CPC Registration

The AI Grocery Decision Platform (`AI-GROCERY-DECISION-PLATFORM`) is registered in the Operator as a read-only external project at approximately **60% completion** (user-estimated at registration time, 2026-09-13).

- **Registration time**: 2026-09-13
- **CPC HEAD at registration**: eae0f7b
- **Operator access**: read-only inspection only
- **Operator writes**: zero (enforced)
- **Control Center**: exposes `/api/cpc-status` (read-only: path exists, git SHA, key files)
- **CPC inspection**: `run_cpc_inspection()` reads and analyses CPC files; never writes

---

## 7. Five-Step Roadmap

### Step 1 (Current): Master Foundation + Control Center + Live Proof
- Master decisions document (this file)
- Control Center web UI (port 8766, stdlib only)
- CPC basket comparison inspection (read-only, evidence-backed)
- Coding fixture approval gate proof
- GitHub Actions CI
- Full regression: 996+ tests passing

### Step 2: Full Provider Integration + Coding Pipeline + Approval UX
- Live Codex/Claude/Gemini integration with real API calls
- Full planner→coder→verifier pipeline execution
- Control Center approval UX with inline contract display
- Telegram approval button integration in Control Center

### Step 3: CPC Active Assistance + Browser Automation + Job Pipeline
- CPC coding assistance (with owner approval for each change)
- Browser automation for job applications (open→fill→pause→approve→submit)
- Full job pipeline: discover→match→shortlist→Telegram delivery→approval-gated submission
- Zoho CRM integration

### Step 4: Monitoring + Autonomy + Multi-Project Registry
- Autonomous daily monitoring sweeps
- Multi-project registry (beyond CPC)
- ServiceMonitor and SecurityAuditor integration in Control Center
- LaunchAgent scheduling for daily job search and monitoring

### Step 5: Production Hardening + External Integrations + Launch
- WhatsApp Business (when Meta restriction lifts)
- LinkedIn automation with proper OAuth
- End-to-end encrypted Telegram command channel
- Full external integration suite: Gmail, Calendar, Drive, Zoho CRM, GitHub
- Production hardening: rate limiting, credential rotation, backup automation

---

## 9. Private-Message Read Authorization Policy

**Decision date:** 2026-09-14  
**Authority:** USER MASTER EXECUTION FORM — PRIVATE MESSAGE ACCESS LOCK  
**Implemented in:** `zamal_core/private_read_auth.py`, `zamal_core/channel_adapter.py`, `zamal_core/ai_browser_transport.py`

### Default-Deny Rule

Zamal must never read private message bodies, previews, inbox rows, notifications, email content or AI chat history unless the caller holds a valid `PrivateReadAuth` for the exact channel and target.

**Private channels:** `gmail`, `telegram`, `whatsapp`, `messenger`, `instagram`

### Authorization Requirements

A valid `PrivateReadAuth` must contain:
1. `task_id` — binds to an active task
2. `channel` — exact channel name
3. `target` — exact conversation/contact or `'*'` for inbox count only
4. `purpose` — human-readable reason
5. `scope` — permitted range (e.g. `'last_10_messages'`)
6. `expires_at` — TTL (default 120 s); auto-consumed on use
7. `authorized_by` — reference to originating operator command

### Authorization Lifecycle

- Authorization is single-use: `auth.consume()` is called after the read completes.
- Authorization expires when the TTL elapses, even if unused.
- `PRIVATE_READ_REGISTRY.revoke_task(task_id)` revokes all auths when a task completes.
- An auth for `channel=gmail, target=STEP3A_SELF_TEST` cannot be reused for `channel=gmail, target=inbox`.

### Default Response Without Auth

Without authorization, `_read_*` functions for private channels return a single metadata-only `ChannelItem`:
```
preview='[PRIVATE_READ_DENIED: no authorization]'
metadata={'authenticated': True, 'tab_found': True, 'content_access': 'DENIED', ...}
```
No names, subjects, senders, or content are returned.

### Broad Extraction Prohibition

The following patterns are blocked in all JS injected into private-domain browser tabs (`_exec_js_in_domain`), regardless of authorization:
- `document.body.innerText` / `textContent`
- `document.body.innerHTML` / `outerHTML`
- `document.documentElement.*`
- `.querySelectorAll("*")`
- `getAccessibilityTree` / `getComputedAccessibilityTree`

Violation → `(False, 'BROAD_EXTRACTION_BLOCKED:<pattern>')` returned before AppleScript runs.

### CC Channel Health Check

`_check_channel()` in `control_center.py` uses a narrow metadata-only browser probe (no content, no names). It does NOT require a `PrivateReadAuth`. Content reads remain gated.

### Incident Handling

Accidental private-content exposure triggers `record_privacy_incident()`:
1. Stops the operation immediately
2. Logs `PRIVACY_BLOCKED` (no raw content in log)
3. Sends sanitized Telegram warning (async, non-blocking)
4. Requires a new scoped instruction before continuing

### Separation of Public vs Private

`_research_facebook_feed()`, `_research_instagram_public()` and public-web functions do not require `PrivateReadAuth` — they target public pages only. They cannot unlock private DMs, inboxes, or email.

---

## 10. Step 3C — Operator Orchestration (2026-09-15)

### Website Form Intake

`POST /api/website-form` in `control_center.py` implements the complete intake pipeline:
- **Validation**: email format, required fields (email + message), length limits
- **Privacy**: email is masked (e.g. `ste***@example.com`) in all responses and stored only as a SHA-256 dedup hash; raw email is never logged
- **Deduplication**: `request_key = 'website_form:sha256(email|subject|message)[:32]'`; duplicate submissions return the existing task without creating a new one
- **State machine**: task advances `RECEIVED→UNDERSTOOD→PLANNED→WAITING_APPROVAL` atomically
- **n8n awareness**: if n8n is unreachable, sends a sanitized Telegram ACTION_REQUIRED notification

### GitHub Operator

`GET /api/github/ci-status` returns local git state (branch, SHA, last commit, ahead count) plus:
- `write_requires_approval: true` — all write operations (commit, push, merge, branch-delete) require operator approval
- `note` — reminder that CI conclusion for private repos requires a logged-in browser tab

The `safety.py` Controls class enforces write protection via `disable_github_push` and `stop_everything` flags. Read-only operations (`git_capture`, `diff_review`) always pass.

### Emergency Stop + Checkpoint Recovery

`POST /api/checkpoint-save` persists a checkpoint note in the task's `next_action` and `evidence` fields, then sends a sanitized Telegram ACTION_REQUIRED notification asynchronously.

`POST /api/checkpoint-resume` reads the checkpoint, clears `next_action`, appends a RESUMED evidence note, and returns `duplicate_action: false` — proving the same task_id resumes without spawning a duplicate.

### Unified Control Center Status

All seven channels are now shown in the CC status table: Telegram, Gmail, Calendar, WhatsApp, Facebook, Messenger, Instagram (`ch_telegram` added to `get_connection_status()`).

Status values from the API (`ok`, `configured`, `partial`, `unconfigured`, `unreachable`, `external_only`) are mapped in the CC UI to semantic labels:
- `ok`/`configured` → **LIVE_VERIFIED**
- `partial` → **DEGRADED**
- `unconfigured`/`unreachable` → **UNAVAILABLE**
- `external_only` → **SESSION_REQUIRED**

The Emergency Stop badge in the topbar shows **STOP ACTIVE** (red) or **CLEAR** (green) in real time. The `/api/status` response includes `emergency_stop_active: bool`.

### AI Provider Initial Status (Live Closure Fix)

Browser-based AI providers (ChatGPT, Claude, Gemini, DeepSeek, Qwen, Kimi) start as `SESSION_REQUIRED` on macOS (not `UNAVAILABLE`). `UNAVAILABLE` is reserved for providers where Chrome is not running or the tab cannot be opened. `SESSION_REQUIRED` correctly indicates a browser session is needed but Chrome is reachable.

### n8n Webhook Integration (Live Closure Fix)

`POST /api/website-form` now forwards a sanitized lead payload to `http://127.0.0.1:5678/webhook/zamal-lead` when n8n is reachable. Raw PII is never forwarded — only `email_masked`, `subject[:80]`, `classification`, `task_type`, `task_id`, `privacy_level`. The webhook response `exec_id` is returned in the API response as `n8n_exec_id`. Webhook failure is non-fatal; task is already at WAITING_APPROVAL before the webhook call.

### Gate A — n8n Original Account Restored (2026-09-15)

n8n was incorrectly running from `zamal-ai-operator-backup/node_modules` with no `N8N_USER_FOLDER` set, causing it to fall back to `~/.n8n` (account: `zamal@operator.local`, 3 workflows). The original database is at `zamal-ai-operator-backup-20260913-164656/.n8n-data/.n8n/database.sqlite` (account: `zamalahmed006@gmail.com`, 11 workflows). Restart command uses `N8N_USER_FOLDER` pointing at the backup data folder with Node 22 via nvm. Session verified by owner login.

### Gate C — AI Provider Health: LIVE_VERIFIED (2026-09-15)

After force health check, all 6 browser AI providers return `LIVE_VERIFIED`: ChatGPT, Claude, Gemini, DeepSeek, Qwen, Kimi. Chrome sessions are active. The `SESSION_REQUIRED` initial-state fix (committed in 45ae049) ensures that un-checked providers show `SESSION_REQUIRED` rather than `UNAVAILABLE` on a fresh process start.

### Gate D — Emergency Stop + CC Components Verified (2026-09-15)

Live demo sequence:
1. `/api/status` → `emergency_stop_active: false` (CLEAR)
2. `POST /api/emergency-stop` `stop_everything` → `enabled: true`
3. `Controls.check('email_send')` → `Denied: Emergency control blocks execution: stop_everything`
4. `POST /api/emergency-stop-clear` → `enabled: false`
5. `controls.status()['stop_everything']` → `0` (CLEAR restored)
6. `/api/github/ci-status` → branch `main`, SHA `45ae049`, `write_requires_approval: true`
7. CC HTML: all 11 component checks PASS (title, AI providers, estop-badge, ch_telegram, Website Form, GitHub Operator, Checkpoint, N8N Workflows, emergency-stop, LIVE_VERIFIED, SESSION_REQUIRED)

### Test Counts

| Phase | Tests Added | Cumulative |
|-------|------------|-----------|
| Step 3C | 44 | 1325 |
| Step 3C live closure | 0 (live evidence, no new tests) | 1325 |
