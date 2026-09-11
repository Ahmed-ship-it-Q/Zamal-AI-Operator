# Zamal AI Operator — Authoritative Master Build Specification

Source: Jamal's MASTER AUTONOMOUS BUILD SPEC, 2026-09-08.
This file preserves the requirements for future sessions. Implementation status
belongs in ROADMAP_STATE.md; actual connections belong in ARCHITECTURE.md.

## Product and operating contract

Build a personal AI operating system: the user gives commands, reviews important
results, and approves sensitive/external actions. The Operator independently
understands intent, plans tasks/subtasks, shares context, routes AI/tools, executes,
verifies, requests approval when required, completes, logs, learns, and discovers
future automations. Default user-facing language is Bangla; use English for code,
prompts, technical documents, resumes and communications where appropriate.

Work autonomously through authorized scope. Audit before editing, reuse existing
components, preserve working functionality and working trees, keep changes modular,
and test affected behavior. Do not reset/revert/rewrite unrelated existing work.
Never claim completion without verification. Stop only for approval, credentials,
login/OAuth/MFA, macOS permission, destructive/external decisions, or a genuinely
ambiguous safety decision. Never bypass platform security.

Primary workspace: `/Users/book/zamal-ai-operator`.
CPC workspace: `/Users/book/Desktop/AI-GROCERY-DECISION-PLATFORM`.
Protect unrelated directories, credentials, OS files and existing source files.

Preserve the existing Node 22/NVM, Codex CLI, Git/npm, n8n 2.33.0, Gemini
configuration, Command Center/chat memory, CPC read-only inspector, prompt
creation, date-wise outbox, desktop start/stop launchers and knowledge rules.
Do not make n8n the brain: it remains the workflow/event/integration layer.
The Operator is the central orchestrator.

## Logical architecture (18 layers)

1. Central AI Orchestrator.
2. Shared Context Bus.
3. Single Source of Truth / Persistent Task State.
4. AI Provider Router.
5. Tool / Connection Gateway.
6. Mac Local Worker.
7. Browser / Computer Worker interface.
8. Universal Input Layer.
9. Persistent Approval Engine.
10. Verification Engine.
11. Git Safety / Rollback Engine.
12. Task Queue + Priority Engine.
13. Scheduler / Event Engine.
14. Remote Command Gateway.
15. Monitoring / Logging / Audit.
16. Credential / Permission abstraction.
17. Cost / Usage Controller.
18. Automation Discovery & Self-Improvement Engine.

## Task context and state

Persist original command, ID, type, state, project, input files/screenshots/references,
applicable rules, previous decisions, plan/subtasks, selected AI/tool, affected files,
Git state, tests, approvals, errors, retries, final result and next action.

States: RECEIVED, UNDERSTOOD, PLANNED, RUNNING, VERIFYING, WAITING_APPROVAL,
APPROVED, REJECTED, COMPLETED, FAILED, RECOVERING.
Use durable local structured storage, audit history and idempotency to prevent
duplicate execution. Preserve context across AI-to-AI handoff and restarts.

## Mandatory approval policy

Automatic: read, search, inspect, analyze, research, plan, draft, safe local code
preparation, test, build, compare, diagnose, verify.

Protected: email send; job application submission; LinkedIn/profile mutation;
Git commit when protected by configuration; Git push; merge; deployment; delete;
destructive reset/revert; payment; credential/security changes; irreversible
external action.

Choices: APPROVE / EDIT / REJECT. Persist approval IDs, exact action scope, status,
actor and audit history. Conversational text alone never authorizes execution.
No protected action may execute without a valid record. Editing requires a revised
request and fresh approval. Unknown capabilities fail closed. Approval never grants
an unimplemented tool or overrides workspace/command policy.

## Local safety and emergency controls

Future Mac workers may operate only in approved locations. Block arbitrary
system-wide modifications, credential exposure, force push, destructive reset,
and `rm -rf` outside explicitly eligible temporary/output areas by default.
Enforce STOP EVERYTHING, PAUSE EXTERNAL ACTIONS, READ ONLY MODE,
DISABLE GITHUB PUSH and DISABLE EMAIL SEND at execution boundaries.

CPC coding begins only after state, approvals and worker safety are operational.
Pipeline: command/screenshots → inspect existing tree → exact scope and expected
versus actual behavior → plan → coding worker → only affected files → focused
tests/build where relevant → Git diff/unrelated-change detection → browser/UI
verification where available → before/after evidence → report → approval for
protected Git/external actions. Preserve uncommitted existing work. Recovery must
not reset/revert unrelated changes. No CPC feature coding is authorized in this run.

## AI providers and context handoff

Prepare modular adapters for OpenAI/ChatGPT models, Codex, Claude, Claude Code,
Gemini and future providers. Route by capability, coding quality, reasoning need,
context size, availability, rate limits, cost and fallback state. No hard dependency
on one provider. Planner → coding AI → test worker → verification AI → approval
engine must share task context without manual prompt copying.

## Input, remote and integrations

Universal input: text, one/many screenshots, images, PDFs/documents, logs, URLs,
reference designs, transcribed voice and later Telegram messages/files. Associate
related inputs with one task.

Telegram is the future primary remote channel: secure authenticated text/voice/
file intake → Operator → local/cloud worker → results and APPROVE/EDIT/REJECT
buttons bound to persistent approvals. Authenticate and authorize identities,
reject replays, and keep worker permissions separate. The Mac must be powered on,
connected, and running its worker. Prepare cloud/VPS fallback only; deployment
requires approval.

Google modules: Gmail, Calendar, Drive and Contacts where useful; read/classify/
summarize, recruiter detection, attachments, drafts, follow-ups and calendar
extraction. OAuth/credentials are user-connected later. Sending email requires
approval. Do not expose credentials in task context or logs.

Career modules: Qatar/GCC accounting jobs; CV matching/scoring; duplicate detection;
job-specific CVs; cover letters; recruiter drafts; application tracking; interviews;
follow-ups; post-employment career development. Submission requires approval.

LinkedIn: consistency, headline/About/experience/skills, recruiter/job analysis,
content drafts, permitted browser assistance. No login/MFA/CAPTCHA bypass.
External profile mutation requires approval.

## Monitoring, costs, recovery and personal operations

Monitor n8n, inspector, workers, AI providers/APIs, remote gateway, queue, storage
and failed workflows. Detect → bounded retry → fallback → recovery → alert only
if unresolved. Never blindly retry an ambiguous protected side effect.
Track provider costs/tokens and configurable budgets. Separate credential references
from secret storage, enforce permissions and preserve auditable decisions.

Allow future calendar/schedule, personal finance, documents/files, research,
news/alerts, subscriptions/bills, CRM/contacts, learning/career, content,
travel/admin, project portfolio, health, backups, security and cost monitoring.

## Automation discovery (mandatory future core)

Observe repetitive tasks/commands/manual actions/searches, recurring failures and
repeated approvals. Propose evidence-based opportunities, frequency/time window,
estimated benefit and risks (e.g. six occurrences in seven days, forty minutes/week).
Flow: DETECT → PROPOSE → BENEFIT/RISK → APPROVE/EDIT/REJECT → BUILD → TEST → ACTIVATE.
Do not silently activate discovered automations.

## Outputs and retention

Create today's date-wise outbox directory when needed. Intended default retention:
eligible temporary/outbox outputs older than three days. Never apply retention to
protected project/source files. Define explicit eligibility and deletion approval
policy before activation. No retention deletion is authorized in this Phase 1–2 run.

## Dependency order and authorization

1. Persistent Task State / Single Source of Truth.
2. Persistent Approval Engine and enforced execution gate.
3. Safe local worker, command/workspace policy and emergency controls.
4. Shared Context Bus, provider adapters and routing.
5. Universal screenshot/file intake.
6. CPC autonomous coding pipeline.
7. Verification, Git safety/rollback, browser interfaces.
8. n8n integration cleanup, task queue, scheduler/events.
9. Telegram remote text/voice/file/approval architecture.
10. Gmail/Google integration architecture.
11. Jobs/Career/LinkedIn architecture.
12. Monitoring, recovery, cost and security.
13. Automation Discovery & Self-Improvement.
14. Ongoing personal operations modules.

Initial Phase 1–2 authorization (completed): audit as needed; create these three master files;
implement and test Phases 1–2 ONLY; verify existing n8n/inspector; accurately update
roadmap. No CPC edits, commits, pushes, external sends, deployments, integration
credentials or subsequent-phase implementation in this run.

Every future session must read these master files and applicable existing knowledge
rules. GREEN means verified complete; RED means pending/blocked. Completion metrics
must disclose their denominator and must not imply unimplemented connectors work.


## Phase 3 authorization — 2026-09-08

Jamal authorized continuation through Phase 3: Safe Local Execution Worker,
command policy, approval integration and emergency controls. Preserve all GREEN
work, do not modify CPC code, do not commit or push, test the implementation and
update ROADMAP_STATE.md. This supersedes the earlier Phase 1–2-only scope for this
run; Phases 4–14 remain pending. Approvals used in safety tests apply exclusively
to temporary test fixtures, never to user files or external operations.


## Phase 10 handoff authorization — 2026-09-09

Jamal transferred Claude's CURRENT partial Phase 10 to Codex: continue the existing
connection registry/gateway, reusable n8n intake/approval/error/response/logging
modules, authenticated event intake, tool health gate, versioned secret-free workflow
exports, canonical Phase 9 approval integration, idempotency and structured failures.
This current task supersedes the earlier Google scope for Phase 10; no Phase 11+
implementation is authorized. Preserve Phase 4's external blocker and Phase 5–9
work; CPC remains read-only by default. No reset/revert/commit/push. Phase 10 is
GREEN only after focused/full tests and genuine live integration verification;
missing credentials or unverified runtime connections remain explicit RED items.


## Phase 14 continuation authorization — 2026-09-09

Continue Claude's partial personal_ops implementation from the current working tree,
without rebuilding completed phases. Complete local calendar, ledger/finance,
documents/files, research, news/alerts, subscriptions/bills, CRM, learning, content,
travel/admin and portfolio modules, integrating existing health/backups/security/
usage systems. Safe local records/read/analyze/search/drafts follow existing policy.
Protected external sends/submissions/invites, money movement, irreversible mutations,
security/credential/permission changes and protected Git/deploy actions require the
existing Phase 9 Gate. Unconnected external transports must deny, not simulate success.
Reuse canonical context, monitoring, automation, event intake, audit/recovery and
idempotency; no parallel architecture. Run focused tests, one full regression, then
live verification. Preserve CPC read-only, Phase 4 blocker and Voice STT. No deploy,
commit, push, merge, reset/revert or unrelated work. Progress counts verified local
module boundaries without implying that deferred external integrations are connected.
