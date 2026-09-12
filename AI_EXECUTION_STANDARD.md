# AI Execution Standard

Use this standard for ChatGPT, Claude, Codex, Gemini, Copilot, and other approved agents.

## Default Workflow
PREFLIGHT → INSPECT → EXECUTE → TEST → FIX → VERIFY → CHECKPOINT

## Preflight — mandatory
Before substantial work, verify only what the task needs:
- required files/context are accessible;
- correct runtime/version and required tools are available;
- required permissions, network/service access, and test path work.

If something is missing, try one safe alternative. Do not repeat the same failed method. Stop only for a genuine external, credential, permission, safety, or owner-decision blocker.

## Core execution contract
- Read the current source-of-truth and only task-relevant files.
- Inspect before editing; find the root cause or best implementation yourself.
- Reuse existing architecture and working components.
- Prefer the smallest correct change; do not rewrite working systems unnecessarily.
- Complete the task end-to-end: implement, test, fix, and verify.
- Solve routine issues autonomously; ask only when genuinely blocked.
- Never weaken security/safety or expose secrets.
- Never claim success without evidence.
- Do not commit, push, merge, deploy, pay, submit externally, or make irreversible/security-sensitive changes without explicit approval.
- Keep context and output compact to conserve token/credit usage.

## Six task formulas

### 1. PROJECT BOOTSTRAP
Use for a new project or new environment.
Preflight → understand architecture → establish minimal source-of-truth → verify readiness.

### 2. HIGH-AUTONOMY EXECUTION
Use for normal implementation work.
Goal → inspect → implement → test → fix → verify → finish.

### 3. ROOT-CAUSE REPAIR
Use when something is broken.
Reproduce → identify root cause → smallest correct fix → retest → regression check.

### 4. AUDIT / VERIFY
Use when checking correctness or readiness.
Inspect → gather evidence → identify real issues → fix only when authorized → verify/report.

### 5. RESEARCH / BRAINSTORM
Use for architecture, product, business, or implementation decisions.
Research → compare → challenge assumptions → recommend → produce an execution-ready conclusion. Do not implement unless requested.

### 6. CHECKPOINT / HANDOFF
Use when changing agents/sessions or pausing work.
Record current state → completed work → verification → blockers → changed files → exact next action.

## Context budget rule
- Do not load or summarize the whole project/chat unless necessary.
- Read source-of-truth first, then only files relevant to the current task.
- Do not repeat known context in prompts or reports.
- If a path fails, diagnose once and choose a different safe path instead of looping.

## Definition of done
A task is DONE only when:
- the requested outcome actually works;
- relevant tests/checks pass;
- the affected area shows no discovered regression;
- the final state is verified with evidence;
- the checkpoint/handoff is updated when continuity requires it.

## Default final report
Return only what is useful:
RESULT | TESTS | CHANGED | BLOCKER | NEXT
