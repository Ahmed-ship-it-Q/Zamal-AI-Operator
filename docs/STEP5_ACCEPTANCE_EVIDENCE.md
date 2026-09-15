# Step 5 acceptance evidence — 2026-09-15

## Scope and delivery contract

Step 4 is **FOUNDATION/STRUCTURE COMPLETE**. Steps 1–4 are preserved.
CV editing, LinkedIn profile optimization, LinkedIn/Qatar Living live CV matching,
job discovery and applications are deferred. AIApply remains removed.
No real test messages, posts, applications, payments or external commitments were
performed. Git commit and ordinary push are explicitly authorized for delivery.

Baseline: `main`, `776de696a2c7ef0cc150088ab9eea783be5b94a7`.
[Baseline CI #26](https://github.com/Ahmed-ship-it-Q/Zamal-AI-Operator/actions/runs/34949943044)
was independently verified as completed/success for that exact SHA.

The final release gate is the **CI push run whose head SHA equals the delivered
commit**, plus a clean working tree. The delivery receipt supplies its immutable
run URL, full SHA and conclusion. Find the commit's run through
[GitHub Actions](https://github.com/Ahmed-ship-it-Q/Zamal-AI-Operator/actions/workflows/ci.yml).
A passing baseline or local tests alone cannot satisfy this gate. The current
commit cannot embed its own SHA; GitHub's SHA-bound run is the CI evidence source.

## Recovery defects and bounded end-to-end proof

The initial reproduction used real handlers and a disposable SQLite store:
identical saves dispatched two notifications, and resume reported success without
checking the session. Both defects were corrected without resetting existing data.

- Checkpoints persist a deterministic task/reason/next-action/session binding and
  notification claim. Optimistic revisions make concurrent saves converge on one
  claim. Repeated saves and reopening SQLite do not dispatch another notice.
- The session requirement cannot be replaced while a checkpoint is pending.
  The UI requires selecting the session, or explicitly selecting a local task.
  Authentication-related reasons without a target remain fail-closed.
- Resume performs a fresh, bounded, metadata-only challenge/login probe for the
  recorded session. Missing sessions, CAPTCHA, OTP, probe errors and unknown
  targets return SESSION_REQUIRED/ACTION_REQUIRED and preserve the checkpoint.
- A pending checkpoint blocks Gate execution even when an action is approved.
  Emergency stop blocks resume; stop-and-clear during the probe remains latched.
  Resume clears the checkpoint on the same task and does not dispatch an action.
  Existing approval, policy and at-most-once action gates still apply afterwards.
- Real loopback HTTP endpoints and real SQLite exercise save twice, blocked
  resume, simulated human session recovery and same-task successful resume.
  Telegram delivery and browser responses are stubbed. Exactly one consolidated
  ACTION_REQUIRED notice reaches the delivery stub; no real message is sent.

Delivery is **at most once per pending checkpoint**, not guaranteed exactly once
at Telegram. A crash after persisting the notification claim but before delivery
can lose a notice. No automatic resend risks duplicating a real notification.
A legacy note-only checkpoint cannot be treated as verified session recovery;
resave it with the required session. Repeated resume cannot replay execution.

## Gate evidence

| Gate | Evidence and boundary |
|---|---|
| Control Center lifecycle | Real local HTTP fixtures: task creation/detail, proposals, rejection, emergency set/clear, checkpoint save/resume |
| Approval and replay | Existing Gate and Telegram callback tests; new checkpoint execution-block and one-dispatch recovery test |
| Recovery | `tests/test_step5_recovery.py`: concurrent/restart deduplication, HTTP recovery, failed probes, emergency epochs, unchanged task ID and no replay |
| Private reads | Exact target/scope, expiration/consumption, default deny, broad extraction denial and read/send separation tests |
| AI routing | Provider fallback/privacy tests; health metadata is not inference verification; UI now distinguishes HEALTHY/CONFIGURED from LIVE_VERIFIED |
| Current browser limitation | A fresh Claude metadata-only probe returned SESSION_REQUIRED; no login or authentication bypass was attempted |
| n8n runtime | `/healthz` and `/healthz/readiness` returned HTTP 200; listening process had the authorized original database open |
| n8n preservation | Read-only SQLite connection with `query_only=ON`: 11 workflows, one account before and after regression. No credential, account identity or workflow content was queried; no database copy, migration or write was performed |
| Startup | Existing command continues to use the authorized original database location and Node 22; only an identifying comment was redacted |
| Project isolation | CPC tested through fixtures; actual CPC project was not accessed or changed. Production task database is excluded from the regression harness |
| Artifact | Root `:memory:` was confirmed empty by read-only file-type inspection and preserved; only exact `/:memory:` is ignored |

## Test and privacy evidence

- Earlier completed privacy/secret-filter/router checks: 91 passed (46 + 28 + 17).
- Focused Control Center/recovery/Step 4 checks: 191 passed.
- Final full regression: **1454 passed, zero failures**, in 200.487 seconds. Canonical command: `python3 scripts/run_tests.py`.
- CI uses the same runner. It isolates default SQLite storage and the special
  `:memory:` fixture path; stubs Telegram delivery, browser execution and live
  service probes; removes inherited credential environment values; rejects
  external socket connections and known production loopback service ports.
- Repository PII/structured-secret scan reports only synthetic test-fixture
  addresses/credential patterns and dependency-maintainer metadata after removal
  of identifying account text from documentation and the startup comment.
  Fixture matches were attributed to named sanitizer/detection tests, without
  printing their values. No real credential was used or added.
- Changed-file secret/PII scan covered all 16 changed files; the sole generic
  match was an existing variable reference, not a credential literal. No secret
  or PII values were found in the changes. Whitespace verification passed. Pattern scans are heuristic and do not certify arbitrary data.

## Current capability boundaries

Historical LIVE_VERIFIED records retain their dated scope; they do not establish
current browser sessions, provider inference, external delivery or unrestricted
private-message access. This acceptance validates local orchestration/recovery,
privacy controls and the existing n8n service. It does not enable job applications,
CV access, LinkedIn optimization, AIApply or unrestricted project writes.
