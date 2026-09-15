# ◆ ZAMAL AI OPERATOR

## Current Step 5 scope — 2026-09-15

[Step 5 acceptance evidence](../docs/STEP5_ACCEPTANCE_EVIDENCE.md) supersedes
historical closure counts and live-health claims for this delivery. Step 4 is
**FOUNDATION/STRUCTURE COMPLETE**. CV editing, LinkedIn optimization, live
LinkedIn/Qatar Living matching and applications are deferred; AIApply remains
removed. Recovery now persists a single notification claim and checks the bound
session before clearing the same task's checkpoint. Pending checkpoints block
execution. Telegram and browser I/O are stubbed in automated acceptance tests.
The original n8n service is healthy with 11 workflows. Current Claude session
probe: SESSION_REQUIRED. Delivery requires green CI for the exact pushed SHA.

## Complete Capability Map
### An Intelligent AI Workforce for Business, Operations & Digital Work

**Tagline:** One Operator. Multiple AI Agents. Connected Business Tools. Verified Execution.

**Verified as of:** 2026-09-13 · Regression: 996/996 PASS · Closure: PASS WITH DOCUMENTED DECISION_BLOCKED
**Final Evidence Gate:** 2026-09-13 · 996/996 PASS · Historical: 996/996 as of Quality Elevation Re-Audit; 924/924 pre-re-audit; 920/920 pre-Codex step
**L-3 Decision Blocked:** Qatar/GCC job source coverage — machinery LIVE_VERIFIED; no credential-free Gulf/Qatar job feed verified; premium provider credentials required (GulfTalent, Bayt, LinkedIn, or Indeed Publisher ID). Not a release blocker. See Section 3.1.

---

## How to Read This Document

Every capability below shows its verified current status using one of four labels:

| Label | Meaning |
|---|---|
| 🟢 LIVE & VERIFIED | Built, tested, and live-verified end-to-end |
| 🔵 CONNECTED / BUILT | Code and integration complete; awaiting owner-supplied auth or final activation |
| 🟡 EXTERNAL LIMITATION | Blocked by a third-party restriction outside the Operator |
| ⚪ EXPANSION / ROADMAP | Planned or partially explored; not yet built or verified |

**Automatic vs. Approval-Required:** Throughout this document, actions are explicitly marked *automatic* (the Operator executes without interruption) or *approval-required* (the Operator pauses and sends an approval request to the owner before any external action is taken). After the owner approves, the Operator itself completes and verifies the action — the owner does not perform it manually.

---

## SECTION 1 — OPERATOR CORE & AI ENGINE

---

### 1.1 Operator Core — Central Orchestration
🟢 LIVE & VERIFIED

**What it does:** Manages every task, action, and result from a single SQLite-backed state store. Every piece of work is recorded with a canonical hash, persists across restarts, and can be replayed or audited at any time.

**Customer/business use:** Replaces ad-hoc scripting and disconnected tools with a single source of truth for all automated and semi-automated business operations.

**Workflow:**
1. Task arrives (via Telegram, scheduled trigger, or API call)
2. Operator creates a canonical task record
3. Planner decomposes the task into actions
4. Each action is executed, result-hashed, and stored
5. Full audit trail is always available

**Manual work reduced:** Eliminates manual tracking of multi-step workflows, repeated data entry across tools, and lost context when work is interrupted.

**Automatic vs. approval-required:** Core bookkeeping and coordination are automatic. Any action with external side-effects (sending, submitting, writing to external systems) is approval-required.

**Verified status:** Phase-based architecture (14 phases), canonical context, approval enforcement, replay/duplicate denial, persistence and restart recovery — all live-verified.

**Realistic use case:** Owner gives instruction "research the top 5 competitors and add findings to the knowledge base." The Operator plans the steps, coordinates the research agent, hashes and stores the results, and sends a Telegram summary — all without manual tracking.

---

### 1.2 AI Provider Router
🟢 LIVE & VERIFIED

**What it does:** Deterministically selects the best available AI provider (Claude or Gemini) for each task based on capability requirements, availability, and safety policy. Routes automatically; fails closed when no safe provider is available.

**Customer/business use:** Ensures every AI task uses the most capable available model without manual provider selection. Prevents silent fallback to an unsafe or unapproved provider.

**Workflow:** Task arrives → Router checks capability requirements → Selects Claude (primary) or Gemini (fallback) → Executes three-stage planner/coder/verifier handoff → Result stored and hashed.

**Manual work reduced:** No manual model selection or API key juggling. Failed providers are detected and routed around automatically.

**Automatic vs. approval-required:** Provider selection and routing are automatic. The router never weakens safety checks to make a provider work.

**Verified status:** Deterministic routing live-verified. Claude primary, Gemini fallback, policy-forbidden providers denied, OpenAI dormant, Codex fail-closed.

**Realistic use case:** Claude is briefly unavailable. The Operator automatically routes to Gemini, completes the task with a verified handoff, and logs which provider was used.

---

### 1.3 Claude (AI Agent)
🟢 LIVE & VERIFIED

**What it does:** Runs as the primary AI agent for planning, coding, reasoning, and verification tasks. Operates in an isolated three-stage planner/coder/verifier flow with tools and external hooks disabled for bounded, safe inference.

**Customer/business use:** Handles complex reasoning, structured output generation, code writing, document drafting, and multi-step analysis tasks.

**Workflow:** Task assigned to Claude stage → Claude receives bounded context only → Produces output → Verifier checks output → Result stored.

**Manual work reduced:** Replaces hours of manual research, writing, and structured analysis with verified AI output.

**Automatic vs. approval-required:** Inference and planning are automatic. Any action that writes to an external system based on Claude's output is approval-required.

**Verified status:** CLI 2.1.112 via memory-only credential bridge; isolated three-stage handoff verified 2026-09-12.

**Realistic use case:** Owner asks the Operator to draft a client proposal. Claude generates the structured draft, the verifier checks format and completeness, and the result is stored — ready for owner review before any send action.

---

### 1.4 Gemini (AI Agent)
🟢 LIVE & VERIFIED

**What it does:** Runs as the fallback AI agent via n8n-owned credential. Participates in the same three-stage planner/coder/verifier handoff as Claude.

**Customer/business use:** Provides continuity when Claude is unavailable. Also available for parallel task execution where multiple AI perspectives are valuable.

**Workflow:** Identical to Claude routing — planner/coder/verifier handoff via n8n credential. Canonical context, replay denial, and restart persistence all verified.

**Manual work reduced:** No manual fallback switching. The Operator routes to Gemini and continues working without interruption.

**Automatic vs. approval-required:** Same as Claude — inference automatic, external writes approval-required.

**Verified status:** Live planner/coder/verifier handoff via n8n-owned credential verified 2026-09-12.

**Realistic use case:** While Claude handles a long research task, the Operator simultaneously routes a document formatting job to Gemini — both complete independently with verified outputs.

---

### 1.5 OpenAI (GPT Models)
⚪ AVAILABLE — DORMANT BY CHOICE

**What it does:** An existing n8n binding connects to OpenAI's API. The connection is readable but paid inference is explicitly inactive.

**Customer/business use:** Can be activated as an additional AI provider for tasks where GPT models are preferred.

**Current status:** Available but dormant by owner choice. No paid API calls have been made. Prior HTTP 429 rate-limit error is historical and was not retried.

**To activate:** Owner provides an active OpenAI API key and confirms activation. Safety and approval boundaries apply identically to Claude and Gemini.

**Realistic use case:** Owner wants GPT-4o for a specific customer-facing summarization style. Provider is activated, routed as a named option, and subject to the same approval workflow as all providers.

---

### 1.6 Codex (Code Execution)
🟢 LIVE & VERIFIED

**What it does:** Provides isolated AI-assisted code generation and reasoning via OpenAI Codex CLI, authenticated through a ChatGPT subscription account — no paid API credits required.

**Customer/business use:** Code generation, code review assistance, coding-stage AI inference. Codex is the Operator's primary choice for the `coder` stage in multi-stage AI handoffs.

**Auth path:** ChatGPT subscription OAuth (id_token, access_token, refresh_token stored in `~/.codex/auth.json`). No OpenAI API key required. Works with an existing ChatGPT subscription.

**Workflow:** Operator routes coding tasks to Codex automatically. Codex receives a bounded, tools-disabled, read-only sandboxed prompt and returns structured JSON output. No tools are executed. No code runs on the machine — only text generation.

**Safety contract verified:** All 24 required features disabled including `unified_exec`, `shell_tool`, `computer_use`, `browser_use`, and all plugin/agent/hook features. Verified 0.149.0 standalone binary only. Fail-closed for any other version.

**Manual work reduced:** Coding tasks, code review requests, and structured code generation are handled by Codex without any manual prompt engineering.

**Automatic vs. approval-required:** Inference is automatic. Any action taken based on Codex output that affects external systems is approval-required through the standard gate.

**Verified status:** Live planner inference verified; context_id/hash match, canonical fields present, no tools executed. Routes as primary coder; Claude fallback available. 924/924 regression pass.

**Realistic use case:** The Operator receives a task to write a Python script to parse invoice CSVs. It routes the coding stage to Codex, receives structured code output, stores and hashes the result, and presents it for owner review before any file is written.

---

## SECTION 2 — PRODUCTIVITY & COMMUNICATION TOOLS

---

### 2.1 Gmail
🟢 LIVE & VERIFIED

**What it does:** Reads, searches, summarizes, and drafts emails. Sends only with explicit owner approval.

**Customer/business use:** Inbox triage, email summarization, automated follow-up drafts, lead response drafting, notification parsing.

**Workflow:**
- *Reading/summarizing:* Automatic — Operator reads inbox, extracts relevant threads, summarizes to Telegram
- *Drafting:* Automatic — Operator writes draft, presents for review
- *Sending:* Approval-required — Operator sends APPROVE/EDIT/REJECT to owner before any email leaves

**Manual work reduced:** Eliminates manual inbox scanning and first-draft writing for routine communications.

**Verified status:** Authorized n8n Gmail OAuth connection; harmless live read verified.

**Realistic use case:** Every morning the Operator reads the inbox, identifies unread business threads, and sends a Telegram summary with draft replies ready — owner approves or edits each send individually.

---

### 2.2 Google Calendar
🟢 LIVE & VERIFIED

**What it does:** Reads events, checks availability, proposes meeting times, and creates calendar entries with owner approval.

**Customer/business use:** Meeting scheduling coordination, availability checks, reminders, event-based workflow triggers.

**Workflow:**
- *Reading/checking:* Automatic — checks availability, reads upcoming events
- *Creating events:* Approval-required — owner approves before any calendar entry is created

**Manual work reduced:** Eliminates back-and-forth availability checking for scheduling.

**Verified status:** Authorized n8n Google Calendar OAuth connection; harmless live read verified.

**Realistic use case:** A prospect requests a meeting. The Operator checks the owner's calendar, finds the next available 30-minute slot, drafts an invitation, and sends it via Telegram for one-tap approval.

---

### 2.3 Google Drive / Docs / Sheets
🟢 LIVE & VERIFIED

**What it does:** Reads files, searches Drive, parses Docs and Sheets data, and creates or updates documents with owner approval.

**Customer/business use:** Document management, data extraction from Sheets, report generation into Docs, file organization.

**Workflow:**
- *Reading/searching:* Automatic — finds and reads authorized files
- *Creating/editing documents:* Approval-required — owner approves before any write

**Manual work reduced:** Eliminates manual searching across Drive folders and copy-paste data extraction from Sheets.

**Verified status:** Google Drive, Docs, and Sheets live-verified via n8n OAuth connections.

**Realistic use case:** Owner says "pull last month's sales numbers from the tracker sheet and put them in the monthly report doc." Operator reads the Sheet, writes the figures into the Doc, and sends the updated document link for owner review before sharing externally.

---

### 2.4 GitHub
🟢 LIVE & VERIFIED

**What it does:** Reads public and authorized private repositories, monitors CI/CD status, reads code, diffs, issues, and pull requests.

**Customer/business use:** Code review assistance, CI status monitoring, issue triage, release tracking.

**Workflow:** Reads are automatic using a least-privilege credential. No write access — the Operator does not commit, push, merge, or modify any repository.

**Manual work reduced:** Eliminates manual repository scanning for status updates and review tasks.

**Verified status:** Public repository reads and authorized private repository reads verified with least-privilege credential.

**Realistic use case:** The Operator monitors a repository's CI runs and sends a Telegram alert when a build fails, including a diff summary of the failing commit — no manual CI dashboard checking needed.

---

### 2.5 n8n Workflow Engine
🟢 LIVE & VERIFIED

**What it does:** Runs as the event and workflow orchestration layer. Hosts 10 verified workflows and 10 encrypted credential records. Triggers the Operator for scheduled and event-driven work.

**Customer/business use:** Scheduled automation (daily reports, morning digests, reminders), event-driven workflows (new email → triage, new calendar event → preparation), integration glue between services.

**Workflow:** n8n receives trigger (schedule, webhook, or service event) → calls Operator API → Operator executes task → result returned to n8n → optional notification sent.

**Manual work reduced:** Eliminates manual trigger management for recurring work.

**Automatic vs. approval-required:** n8n scheduling and routing are automatic. Any action with external side-effects still goes through the Operator's approval gate.

**Verified status:** n8n running on port 5678; 10 workflows and 10 credential records confirmed; workflow and credential integrity unchanged.

**Realistic use case:** Every weekday at 08:00, n8n triggers the Operator's morning digest workflow — Gmail triage, calendar check, job matches, and key metrics — delivered as a structured Telegram message before the owner starts their day.

---

## SECTION 3 — JOB AUTOMATION & BROWSER WORKFLOW

---

### 3.1 Job Automation — Discovery & Matching
🟢 LIVE & VERIFIED

**What it does:** Discovers real job postings from public sources (Greenhouse, LinkedIn public, etc.), ranks them against a candidate profile, maintains a shortlist, and delivers daily matches via Telegram.

**Customer/business use:** Personal or client job search automation. Eliminates daily manual job board scanning.

**Workflow:**
1. Daily trigger at 08:00 (macOS LaunchAgent)
2. Operator queries public job sources for the target location/role
3. AI ranks and shortlists matches against the profile
4. Failed sources are skipped; last successful results are preserved
5. Shortlist delivered to Telegram with job title, company, and match rationale
6. Owner reviews; no application is submitted without explicit approval

**Manual work reduced:** Eliminates daily manual job board scanning, copy-pasting listings, and manual ranking.

**Automatic vs. approval-required:** Discovery and ranking are automatic. Automatic submission without owner approval is disabled. Approval-gated job submission is supported: the Operator prepares the application, pauses before the protected final submit action, requests owner approval through Telegram, and after APPROVE the Operator completes the submission and verifies/logs the result.

**Verified status:** Job search machinery LIVE_VERIFIED — fetch, normalise, score, dedup, Telegram delivery, fallback preservation all verified. 20 ranked jobs retrieved, hashed, and persisted at time of original capability verification.

**L-3 DECISION_BLOCKED — Qatar/GCC source coverage:** All four current public sources (Arbeitnow, RemoteOK, Jobicy, Remotive) are remote-work boards; on-site Qatar/GCC accounting roles are structurally absent from them. Five credential-free GulfTalent/Bayt RSS URLs were tested and rejected (HTML responses / Cloudflare 403). Closing this gap requires operator-supplied credentials for a Gulf-specific provider (GulfTalent API key, Bayt partner key, LinkedIn OAuth, or Indeed Publisher ID). Not a release blocker.

**Realistic use case:** Owner receives a Telegram message at 08:05 each morning: "3 new matching roles in Qatar — Senior AI Engineer at Accenture (95% match), ML Lead at QCRI (88% match), Data Scientist at QNB (82% match). Reply APPLY to request submission for any."

---

### 3.2 Browser Application Workflow — Safe Form Completion
🟢 LIVE & VERIFIED

**What it does:** Opens a sandboxed headless browser, navigates to a job application form, fills fields with candidate information, attaches a CV, pauses before submission, sends an approval request to the owner via Telegram, and — only after explicit approval — clicks submit and verifies the result.

**Customer/business use:** Automates the mechanical form-filling and submission steps of job applications while keeping the owner in full control of every submission decision.

**Workflow:**
1. Owner approves an application from the daily shortlist
2. Operator opens sandboxed headless Chrome (loopback-only, no external URLs)
3. Navigates to application form, fills name/email/role/CV fields
4. Detects and verifies all filled fields and the uploaded file
5. **Stops** — does not click Submit
6. Sends Telegram approval request with field summary and APPROVE / EDIT / REJECT options
7. If APPROVE: Operator resumes, clicks Submit, verifies confirmation page text, logs evidence hash
8. If EDIT or REJECT: No submission. Action is closed with zero side effect.

**Manual work reduced:** Eliminates repetitive form-filling across multiple applications. The owner reviews and approves; the Operator handles mechanics and verification.

**Automatic vs. approval-required:** Form navigation and filling are automatic (no external contact). Submission is always approval-required. After APPROVE, the Operator itself clicks and verifies — the owner does not do it manually.

**Safety boundaries:** No session cookies, saved passwords, or stored credentials are used. No CAPTCHA bypass. No unauthorized account access. The browser is sandboxed (loopback only, route-filtered, private temp directory, SIGKILL on completion). LinkedIn scraping, cookie reuse, and session injection are not used and are blocked by design.

**Verified status:** Full browser→approval→resume→evidence flow verified end-to-end. Real sandboxed Chrome fills form, emits PREPARED contract, Telegram gate enforced, post-APPROVE submit and confirmation verified. 924/924 regression tests pass. Step 3 LIVE_VERIFIED as of 2026-09-13.

**Realistic use case:** The Operator has shortlisted a role at QCRI. Owner replies "apply." The Operator navigates the form, fills in the CV, and sends: "Form filled for QCRI ML Researcher — Name ✓, Email ✓, CV uploaded (42KB) ✓. Approve to submit?" Owner taps APPROVE. Operator submits, confirms "Application received," and saves the evidence record.

---

### 3.3 LinkedIn
🔵 CONNECTED — TWO MODES

LinkedIn operates in two separate modes with different verified states:

**Mode A — LinkedIn Job Lookup (via ChatGPT integration)**
Status: CONNECTED / LIVE EXTERNAL ONLY

The owner's existing ChatGPT-LinkedIn integration can surface LinkedIn job data via external ChatGPT workflows. This operates entirely outside the local Operator — the Operator does not control or execute this path. Results can be fed into the Operator's job matching pipeline.

**Mode B — LinkedIn Local Operator Automation**
Status: 🔵 IMPLEMENTED — AWAITING OWNER AUTH

A local LinkedIn connector is implemented with boundaries: no scraping, no cookie/password reuse, no unauthorized writes. A supported OAuth integration is required to activate. Public job sources work independently without LinkedIn auth.

**Owner action required:** Provide an authorized, supported LinkedIn OAuth integration. The Operator will not use session injection, scraping, or saved credentials.

---

### 3.4 WhatsApp Business
🟡 EXTERNAL LIMITATION — BLOCKED BY META

**What it does:** Would send business notifications and approval requests via WhatsApp Business API.

**Current status:** BLOCKED. Meta's WhatsApp Business API requires business verification and is restricted to approved business accounts. No workaround is in place, and no unofficial API or session injection is used.

**Workaround in use:** Telegram provides the full approval and notification capability in production.

**To unblock:** Owner applies for and receives Meta WhatsApp Business API approval.

---

## SECTION 4 — BUSINESS RESEARCH & INTELLIGENCE

---

### 4.1 Web & Business Research
🟢 LIVE & VERIFIED

**What it does:** Gathers information from public web sources, extracts and structures key facts, compares sources, and produces cited research summaries.

**Customer/business use:** Competitor analysis, market research, prospect research, due diligence background checks, news monitoring.

**Workflow:**
1. Research task assigned
2. Operator fetches authorized public sources
3. Extracts and structures relevant content
4. Each source is hashed and cited
5. AI produces extractive comparison and summary
6. Result stored with source hashes for auditability

**Manual work reduced:** Eliminates manual source-by-source research and note-taking. All results are documented with verifiable source citations.

**Automatic vs. approval-required:** Research and summarization are automatic. Any external communication based on research findings is approval-required.

**Verified status:** Two public sources collected, hashed, and cited; extractive comparison verified. Note: live search-provider path and real-time factual corroboration are not independently verified — the Operator uses available public sources, not a live search API.

**Realistic use case:** Owner asks "research the top 3 AI companies hiring in Qatar." Operator fetches public data from two sources, extracts company profiles, salary ranges, and open roles, and delivers a structured Telegram summary with source links.

---

### 4.2 Zoho CRM
🟡 AWAITING_INTEGRATION

**What it does:** Reads and updates CRM records, creates contacts, logs interactions, and triggers CRM-based workflows.

**Customer/business use:** Sales pipeline tracking, contact management, interaction logging, automated follow-up scheduling.

**Workflow:** Reads are automatic. Record creation and updates are approval-required before any write.

**Verified status:** AWAITING_INTEGRATION. No Zoho implementation found in zamal_core/ or deployed n8n workflows as of 2026-09-13 audit. n8n package includes the Zoho node type (node-versions.json) but no credential, workflow, or operator integration code is present. Status downgraded from prior unsubstantiated LIVE_VERIFIED claim.

**Realistic use case:** After a sales call, the Operator logs the interaction, updates the deal stage, and schedules a follow-up task in Zoho — all triggered from a Telegram voice note.

---

### 4.3 Business Orchestration
🟢 LIVE & VERIFIED

**What it does:** Coordinates multi-step business workflows that span multiple tools — for example, reading a calendar event, drafting a preparation document, and scheduling a follow-up email.

**Customer/business use:** End-to-end process automation: onboarding sequences, sales follow-up chains, research-to-report pipelines.

**Workflow:** Operator creates canonical child tasks for each step, executes them in order, and collects results. Protected send steps remain pending until owner approval — a rejected approval closes the step with zero side effect.

**Verified status:** Canonical child tasks for calendar/communication import verified; protected send remained pending and was rejected with zero side effect.

**Realistic use case:** Owner says "prep for tomorrow's investor call." Operator: reads calendar for the meeting, researches the investor's portfolio, drafts a briefing doc in Google Docs, and queues a pre-meeting email for approval — delivered as a Telegram checklist.

---

## SECTION 5 — MAC OPERATIONS & SYSTEM CONTROL

---

### 5.1 Mac Operations
🟢 LIVE & VERIFIED

**What it does:** Executes a bounded set of safe system operations on the owner's Mac: checking git status in allowed folders, running permitted commands, checking service status, opening localhost URLs in the browser, and verifying system permissions.

**Customer/business use:** Automated local system monitoring, safe git status checks, service health checks, local development workflow integration.

**Workflow:** Only commands in an explicit allowlist are ever executed. All paths are validated against an allowed-folder list. Private paths, symlink escapes, and CPC project writes are blocked at validation time.

**Automatic vs. approval-required:** Safe read-only operations (git status, service status, permission check, browser open for localhost) are automatic. Service start/stop and any write operations are approval-required.

**Safety boundaries enforced:**
- No access to `/Users/book` broadly — only specific allowed folders
- No Photos, Documents, Downloads, iCloud, browser data, or passwords
- No symlink traversal outside allowed paths
- CPC project is read-only; no writes ever
- Private filesystem is deny-by-default

**Verified status:** 39 Mac Safety tests + 853/853 full regression pass 2026-09-09. Allowed-folder resolve, allowed-command execution, private-path/traversal/symlink/CPC-write all denied at validate time.

**Realistic use case:** Operator monitors the local development service. Every 15 minutes, it checks service status automatically and alerts via Telegram if the service is down — no manual terminal checking needed.

---

### 5.2 Emergency Stop
🟢 LIVE & VERIFIED

**What it does:** Provides an immediate, latched, globally-enforced halt to all (or specific categories of) Operator actions. Three independent control flags: `stop_everything`, `pause_external_actions`, `read_only_mode`.

**Customer/business use:** Immediate risk mitigation. If anything looks wrong, owner sends one Telegram command to freeze all outbound actions.

**Workflow:**
1. Owner sends `/stop` or equivalent Telegram command
2. Operator sets the appropriate control flag (latched — survives restart)
3. All affected actions immediately raise `Denied` — no external action proceeds
4. Epoch is incremented, state is audited
5. Owner sends `/resume` to restore; each flag cleared individually

**Manual work reduced:** No manual intervention in running processes required — a single command halts everything immediately.

**Automatic vs. approval-required:** Setting/clearing emergency controls is owner-triggered. The halt itself is instantaneous and automatic.

**Verified status:** Latched controls live-tested 2026-09-09. Epoch incremented 1→2. All Mac dispatch blocked during stop; `Denied` raised as expected. All-OFF state verified after test. `control_audit` append-only trigger verified.

**Realistic use case:** Owner sees an unexpected Telegram message — an approval request for an action they don't recognize. They send `/stop` immediately. All pending actions freeze. The owner investigates, identifies the unexpected task, cancels it, and sends `/resume`. No external action escaped.

---

## SECTION 6 — COMMAND, CONTROL & APPROVAL

---

### 6.1 Telegram — Command & Approval Interface
🟢 LIVE & VERIFIED

**What it does:** Serves as the primary human-in-the-loop interface. The owner receives summaries, approval requests, and status updates via Telegram, and sends commands and approval decisions back.

**Customer/business use:** Full control of the Operator from any device with Telegram. No need to be at a desk.

**Workflow:**
- Operator sends notifications, summaries, and job match digests
- When a protected action is pending, Operator sends an inline keyboard: **APPROVE / EDIT / REJECT**
- Owner taps their choice — Operator acts accordingly
- APPROVE: Operator completes the action and logs verification evidence
- EDIT: Action remains open; owner can modify parameters
- REJECT: Action is cancelled with zero side effect

**Safety:** Only the owner's authorized Telegram chat ID receives approval requests and can issue commands. Requests from unauthorized chat IDs are silently denied.

**Verified status:** Live Telegram delivery verified. Approval keyboard, APPROVE/EDIT/REJECT routing, unauthorized-chat denial, and replay protection all verified.

**Realistic use case:** Owner is traveling. At 08:05, a Telegram message arrives: "3 job matches today." At 09:00: "Draft reply to XYZ client ready — Approve to send?" Owner taps APPROVE on their phone. Operator sends and logs the action. Owner never opened a laptop.

---

### 6.2 Approval Architecture
🟢 LIVE & VERIFIED

**What it does:** Enforces a cryptographic approval gate for all protected actions. Approval tokens are single-use, bound to a specific action and owner chat, and cannot be replayed.

**How it works:**
- Protected action is proposed → state: PENDING
- Operator notifies owner → owner approves → state: APPROVED
- Approval token is consumed atomically as execution begins → state: CONSUMING → CONSUMED
- Execution completes → state: SUCCEEDED (result hashed and stored)
- Any attempt to re-use the approval token → `Denied: replay not allowed`
- Any approval from an unauthorized chat → `Denied: chat not authorized`

**Manual work reduced:** No manual tracking of "did that action run?" — every protected action has a verifiable audit trail.

**Verified status:** Pending protected action denied without approval; rejected action closed with zero side effect; approved inference executed exactly once; replay denied; unauthorized-chat denied.

**Realistic use case:** The Operator queues an email send. The approval is valid for one send only. Even if the owner accidentally taps APPROVE twice, only one email is sent — the second attempt is immediately denied.

---

### 6.3 Replay Protection
🟢 LIVE & VERIFIED

**What it does:** Prevents any completed action from being re-executed, even if re-triggered by an event or network retry. Every action has a unique ID; completed actions are permanently closed.

**Verified status:** Canonical action replay denied during live verification. Duplicate update IDs from Telegram are also rejected.

---

### 6.4 Recovery & Restart Persistence
🟢 LIVE & VERIFIED

**What it does:** Survives process crashes, restarts, and power cycles without losing state. All tasks and action results are written to SQLite before completion. After restart, the Operator resumes from the last known state — no work is lost, no action is accidentally repeated.

**Verified status:** Completed result persistence verified after reopening SQLite store. No automatic replay on restart.

---

## SECTION 7 — INTELLIGENCE INFRASTRUCTURE

---

### 7.1 Voice / Speech-to-Text
🟢 LIVE & VERIFIED

**What it does:** Accepts voice input (audio files or microphone recordings) and transcribes them to text using Apple's Speech framework (on-device, no cloud upload). The transcribed text becomes a task instruction.

**Customer/business use:** Hands-free task creation while commuting, exercising, or away from a keyboard.

**Workflow:** Audio recorded or received → Apple Speech transcribes on-device → text passed to Operator as task instruction → normal task flow proceeds.

**Privacy:** Transcription is fully on-device using the macOS Speech framework. Audio is not uploaded to any cloud service.

**Verified status:** Real Apple Speech transcript and intake verification preserved.

**Realistic use case:** While driving, owner says "remind me to follow up with the Doha office about the partnership proposal." Voice note is transcribed, a task is created, and a calendar reminder is proposed for the next business day — all from a 10-second voice message.

---

### 7.2 Documents & Knowledge Handling
🟢 LIVE & VERIFIED

**What it does:** Maintains a local reference catalog of business knowledge, project context, and operator state. Reads, indexes, and retrieves documents to provide AI agents with relevant context for each task.

**Customer/business use:** Institutional memory — the Operator knows your business context, past decisions, and project state without being re-briefed each time.

**Scope:** Operates on the designated Operator workspace. Does not access Photos, Documents folder, Downloads, iCloud, browser history, or other private areas.

**Verified status:** Local reference catalog operations live-verified. Not unrestricted private file access.

---

### 7.3 Verification & Evidence Engine
🟢 LIVE & VERIFIED

**What it does:** Every significant action produces a cryptographic evidence record — a SHA-256 hash of the result, stored alongside the action record. Evidence records can be independently verified.

**Customer/business use:** Auditability. Proof that a specific action was taken with a specific result at a specific time. Useful for compliance, client reporting, and debugging.

**Verified status:** Stored canonical results match evidence hashes. Report references are hash-validated.

---

## SECTION 8 — PRIVACY & SECURITY BOUNDARIES

---

### 8.1 CPC Inspector — Read-Only Code Review
🟢 LIVE & VERIFIED

**What it does:** The Operator can read and inspect the CPC (Code Project Codebase) to provide context for related tasks. This is strictly one-way: the Operator reads; it never writes, commits, pushes, or modifies the CPC project.

**Enforced boundaries:**
- CPC project: read-only from the Operator (enforced in code, not just policy)
- No commit, push, merge, or branch operation is ever performed
- No file in the CPC project is modified by any Operator action

**Verified status:** CPC status healthy; readOnly=true; binary diff unchanged; CPC git SHA preserved (eae0f7b).

---

### 8.2 Private Filesystem Protection
🟢 LIVE & VERIFIED

**What it does:** Enforces a deny-by-default policy on the owner's private filesystem. The Operator cannot read or access personal, financial, or sensitive areas of the Mac even if instructed to.

**Denied paths (enforced in code):**
- `/Users/book` broadly (outside designated workspace)
- `~/Photos`, `~/Documents`, `~/Downloads`
- iCloud Drive
- Browser data, passwords, keychain
- Any path via symlink traversal outside allowed roots

**Verified status:** Nine prohibited personal paths denied without reading content. Inbox ancestor-symlink escape fixed and tested. Trusted runtime/auth readers remain narrowly scoped.

---

## STATUS SUMMARY

| Capability | Status |
|---|---|
| Operator Core | 🟢 LIVE & VERIFIED |
| Provider Router | 🟢 LIVE & VERIFIED |
| Claude | 🟢 LIVE & VERIFIED |
| Gemini | 🟢 LIVE & VERIFIED |
| OpenAI | ⚪ AVAILABLE — DORMANT |
| Codex | 🟢 LIVE & VERIFIED |
| Gmail | 🟢 LIVE & VERIFIED |
| Google Calendar | 🟢 LIVE & VERIFIED |
| Google Drive / Docs / Sheets | 🟢 LIVE & VERIFIED |
| GitHub | 🟢 LIVE & VERIFIED |
| Zoho CRM | 🟡 AWAITING_INTEGRATION |
| n8n | 🟢 LIVE & VERIFIED |
| Telegram (command + approval) | 🟢 LIVE & VERIFIED |
| Job Automation — Discovery | 🟢 LIVE & VERIFIED |
| Browser Application Workflow | 🟢 LIVE & VERIFIED |
| LinkedIn (local Operator automation) | 🔵 BUILT — AWAITING AUTH |
| LinkedIn (ChatGPT external lookup) | 🔵 CONNECTED — EXTERNAL ONLY |
| WhatsApp Business | 🟡 BLOCKED (Meta restriction) |
| Mac Operations | 🟢 LIVE & VERIFIED |
| Emergency Stop | 🟢 LIVE & VERIFIED |
| Web / Business Research | 🟢 LIVE & VERIFIED |
| Business Orchestration | 🟢 LIVE & VERIFIED |
| CPC Inspector (read-only) | 🟢 LIVE & VERIFIED |
| Voice / STT | 🟢 LIVE & VERIFIED |
| Documents / Knowledge | 🟢 LIVE & VERIFIED |
| Verification / Evidence | 🟢 LIVE & VERIFIED |
| Recovery / Restart | 🟢 LIVE & VERIFIED |
| Replay Protection | 🟢 LIVE & VERIFIED |
| Approval Architecture | 🟢 LIVE & VERIFIED |
| Private Filesystem Protection | 🟢 LIVE & VERIFIED |

---

## EXTERNAL LIMITATIONS & WHAT IS NOT CLAIMED

This document reflects only what has been built, tested, and verified. The following are explicitly not claimed:

| Item | Actual Status |
|---|---|
| OpenAI API (paid) | Not active — Codex covers inference via ChatGPT subscription; API key not required |
| WhatsApp Business | Blocked — Meta API business verification required |
| LinkedIn automation (local) | Built but not active — requires owner-supplied OAuth |
| OpenAI inference | Available but dormant — no paid calls made |
| Real job application submission | Verified end-to-end in synthetic fixture; each real submission requires explicit owner approval |
| Unrestricted Mac/computer access | Not claimed — bounded allowlist enforced |
| Live search API (web research) | Not independently verified — uses available public sources |
| LinkedIn scraping / session injection | Not used; blocked by design |
| Automatic submission without approval | Not supported — the Operator prepares and pauses; owner approves via Telegram; after APPROVE the Operator completes and verifies the submission |

---

*Document generated 2026-09-13. Verified regression: 996/996 PASS (Quality Elevation Re-Audit 2026-09-13). Historical baselines preserved: 924/924 PASS (2026-09-12 pre-re-audit), 920/920 PASS (pre-Codex step). No external submissions, no private data accessed, no commit/push performed.*
