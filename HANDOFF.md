# Zamal AI Operator — Current Handoff

## Current Step 5 acceptance — 2026-09-15

[Step 5 evidence](docs/STEP5_ACCEPTANCE_EVIDENCE.md) is authoritative for the
current delivery and supersedes historical completion claims below.
The recovery defects are repaired; 191 focused checks and 1454 full-regression
tests passed. The delivered commit's exact-SHA CI result is recorded by GitHub
Actions and the delivery receipt. A clean ordinary push and successful exact-SHA CI are
mandatory acceptance gates.

Step 4 is **FOUNDATION/STRUCTURE COMPLETE**. CV editing, LinkedIn optimization,
live LinkedIn/Qatar Living matching and applications remain deferred. AIApply
remains removed. n8n health/readiness passed; its original database has one
account and 11 workflows, inspected read-only. Claude currently requires a
session; historical live-provider claims must not be read as current health.
CPC was not accessed or changed. The empty root `:memory:` artifact is preserved
with an exact ignore rule. Use `python3 scripts/run_tests.py` for isolated tests.

## Historical status — retained as prior evidence
All steps GREEN — 996/996 PASS

- Full regression: 996/996 PASS (2026-09-13 Final Evidence Gate — confirmed)
- Historical: 996/996 PASS (2026-09-13 Quality Elevation Re-Audit), 924/924 PASS (pre-re-audit 2026-09-13), 920/920 PASS (pre-Codex step)
- Step 1 (Codex): LIVE_VERIFIED via ChatGPT subscription OAuth
- Step 3 (browser→approval→resume→evidence): LIVE_VERIFIED
- Step 4 (closure records): GREEN
- Step 5 (capability map): complete
- CPC project HEAD: eae0f7b
- CPC baseline diff hash unchanged
- CPC baseline status hash unchanged
- No CPC write/edit from Operator
- No Git commit/push/merge
- No paid OpenAI API usage

## Core Rules
- Operator is the central brain and source of truth.
- n8n is workflow/event layer only.
- Routine safe internal work may be automatic.
- Approval required for credentials, payments, destructive actions, deployments, external commitments, protected Git actions, security/permission changes.
- Private filesystem is DENY BY DEFAULT.
- Do not access /Users/book broadly, Photos, Documents, Downloads, iCloud, browser data, passwords, or unrelated private files.
- CPC project is read-only from Operator.
- Never weaken safety checks just to make a provider work.
- Never claim success without verification evidence.

## Provider State
- Claude: LIVE_VERIFIED (CLI 2.1.112; three-stage planner/coder/verifier handoff verified 2026-09-12)
- Gemini: LIVE_VERIFIED (planner/coder/verifier handoff via n8n-owned credential verified 2026-09-12)
- Codex: LIVE_VERIFIED (0.149.0 standalone; ChatGPT subscription OAuth; coder-stage primary; verified 2026-09-13)
- Provider Router: LIVE_VERIFIED (planner→Claude, coder→Codex, verifier→Claude; Gemini/Codex fallback across stages)
- OpenAI production API: DORMANT / inactive by choice (not required; Codex covers inference via ChatGPT subscription)
- Free fallback agents may be used only if they preserve all safety boundaries

## Verified Capabilities
- Central orchestration
- Planner → coder → verifier flow
- Canonical context
- Approval enforcement
- Replay/duplicate denial
- Persistence and recovery
- Telegram interface
- Voice/STT
- CPC read-only inspection
- Verification/evidence engine
- Provider registry
- Capability expansion security boundaries

## Verified Integrations (beyond core)
All items below are LIVE_VERIFIED unless noted:
- Gmail, Google Calendar, Google Drive, Google Docs, Google Sheets (n8n OAuth)
- GitHub (public reads + authorized private read; least-privilege credential)
- Zoho CRM: AWAITING_INTEGRATION (node type present in n8n package; no deployed workflow or zamal_core integration found as of 2026-09-13 audit)
- Job Automation (public discovery, match/shortlist, Telegram delivery)
- Browser application workflow (open→fill→pause→Telegram approve→submit→verify; Step 3 LIVE_VERIFIED)
- Business Research (public source collection, hash/cite, extractive comparison)
- n8n (10 workflows, 10 credentials; event/scheduling layer)
- LinkedIn local automation: IMPLEMENTED_AWAITING_AUTH (no scraping, no cookie reuse)
- LinkedIn ChatGPT lookup: CONNECTED_LIVE_EXTERNAL_ONLY (external only, not local Operator)
- WhatsApp Business: BLOCKED_BY_META_RESTRICTION

## Continuation Rule For Any Agent
Inspect current files first.
Do not restart or rewrite completed work.
Do not add unrelated features.
Preserve all GREEN tests.
Run focused tests for affected area.
Run full regression before declaring GREEN.
Record evidence.


## Personal Job Search — LIVE VERIFIED

- Status: LIVE_VERIFIED (machinery); L-3 DECISION_BLOCKED (Qatar/GCC source coverage — not a release blocker)
- Job search machinery: LIVE_VERIFIED (fetch, score, dedup, Telegram delivery, fallback preservation)
- Qatar public job discovery (machinery): verified at time of original capability build
- Match and shortlist processing: verified
- Failed-source / last-successful-result preservation: implemented and tested
- Daily execution: macOS LaunchAgent at 08:00 local time
- Telegram delivery: LIVE_VERIFIED
- LinkedIn password/cookies/session reuse/direct scraping: NOT USED
- Automatic submission without owner approval: not enabled
- Approval-gated submission: supported — Operator prepares, pauses before submit, requests Telegram approval, after APPROVE completes and verifies
- L-3 source gap: current sources (Arbeitnow, RemoteOK, Jobicy, Remotive) are remote-work boards; Qatar/GCC on-site accounting roles structurally absent; credential-free Gulf feeds tested and rejected; premium provider credentials required to close gap
- CPC project: NOT TOUCHED
