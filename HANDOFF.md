# Zamal AI Operator — Current Handoff

## Status
Requested expansion focused verification: GREEN

- Focused expansion: 37/37 PASS
- Full regression attempt: 799 tests; 10 failures; 91 errors in sanitized cloud runtime
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
- Gemini: LIVE_VERIFIED
- OpenAI production API: DORMANT / inactive by choice
- Codex: usable when quota available
- Claude: adapter exists; live authenticated state not currently verified
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

## Current Expansion Work
Built/verified code paths include:
- provider routing
- public job discovery
- business research
- GitHub read boundary
- Gmail/Calendar connector boundary
- capability status reporting

The 2026-09-11 sanitized cloud verification record is `outbox/2026-09-11/capability-expansion/verification.json`.
Gmail and Calendar remain implemented but awaiting n8n-owned OAuth credentials and harmless live reads.
GitHub public reads, Job Automation, and Business Research remain verified within their stated public/fixture scopes.

Do not assume external account actions are LIVE unless separately authenticated and verified.

## Next Work Order
1. Finalize machine-readable capability status.
2. Securely verify Gmail integration through existing authorized connector/n8n-owned OAuth.
3. Verify Calendar integration.
4. Verify GitHub read integration.
5. Verify Job Automation.
6. Verify Business Research.
7. LinkedIn only if an authorized supported integration exists.

## Continuation Rule For Any Agent
Inspect current files first.
Do not restart or rewrite completed work.
Do not add unrelated features.
Preserve all GREEN tests.
Run focused tests for affected area.
Run full regression before declaring GREEN.
Record evidence.
