# Zamal AI Operator — Current Handoff

## Status
Requested expansion focused verification: GREEN

- Focused expansion: 37/37 PASS
- Full regression attempt: 799 tests; 10 failures; 91 errors in sanitized cloud runtime
- CPC project HEAD: eae0f7b
- CPC baseline diff hash unchanged
- CPC baseline status hash unchanged
- No CPC write/edit from Operator
- No paid OpenAI API usage

## Core Rules
- Operator is the central brain and source of truth.
- n8n is workflow/event layer only.
- The owner gives intent and approvals; the Operator handles planning, routing, routine execution, recovery, verification, and completion.
- The owner should not need to manually relay prompts between approved agents or perform routine browser/computer steps when an authorized capability exists.
- Routine safe internal/preparation work may be automatic across approved AI providers, browser/computer-use, GitHub read, Gmail draft, Calendar read/planning, Drive/Docs/Sheets, n8n, and allowed Mac/project capabilities.
- Protected external/irreversible actions require explicit approval at the final boundary. Preferred remote approval channel is authenticated Telegram with action-bound APPROVE / EDIT / REJECT.
- After APPROVE, the Operator completes the approved final action itself, verifies it, records evidence, and reports completion.
- Email may be fully prepared and saved as Draft without approval; Send requires approval.
- Job applications may be discovered, ranked, tailored, browser-filled, and prepared to final review automatically; final Submit requires approval, after which the Operator submits and records evidence.
- Private filesystem/data is DENY BY DEFAULT. Read-only scopes may be inspected but never edited/deleted/moved/renamed/forwarded/uploaded/shared.
- Deletion is DENY BY DEFAULT; prefer reversible archive/trash and require fresh approval immediately before any real deletion.
- Do not access /Users/book broadly, Photos, Documents, Downloads, iCloud, browser password stores/data, passwords, private keys, `.ssh`, secrets/tokens, banking/payment credentials, unrelated private files, or sensitive identity documents unless narrowly authorized for a specific task.
- Approval cannot bypass emergency stop, denied capabilities, provider safety checks, or project-specific restrictions.
- CPC project is read-only from Operator.
- Never weaken safety checks just to make a provider work.
- Never claim success without verification evidence.

Canonical autonomy/approval policy: `knowledge/AUTONOMY_APPROVAL_POLICY.md`.
Execution method: `AI_EXECUTION_STANDARD.md`.

## Provider State
- Gemini: LIVE_VERIFIED
- OpenAI production API: DORMANT / inactive by choice
- Codex: provider/runtime availability may vary; use only when preflight passes
- Claude: adapter exists; live runtime must pass current preflight before use
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
Do not assume an external account action is LIVE unless separately authenticated and verified.

## Current Work Order
1. Provider check/activation and router fallback verification.
2. Mac safety operations + Emergency Stop.
3. External integrations and approval-gated job/browser automation.
4. Project status & closure records.
5. Master capability documentation.

## Continuation Rule For Any Agent
Before substantial work, read and follow `AI_EXECUTION_STANDARD.md` and `knowledge/AUTONOMY_APPROVAL_POLICY.md`.
Use: PREFLIGHT → INSPECT → EXECUTE → TEST → FIX → VERIFY → CHECKPOINT.
Choose the matching task formula, keep context/token usage minimal, and read only task-relevant files.
Inspect current files first.
Do not restart or rewrite completed work.
Do not add unrelated features.
Preserve all GREEN tests.
Run focused tests for affected area.
Run full regression before declaring GREEN.
Record evidence.
