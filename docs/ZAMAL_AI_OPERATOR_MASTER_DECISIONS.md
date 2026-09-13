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
