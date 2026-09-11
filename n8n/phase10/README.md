# Phase 10 reusable integration modules

All five definitions are imported and published in the existing local n8n database:

| Module | Published workflow ID |
| --- | --- |
| Intake | ZamalP10Intake01 |
| Approval presentation | ZamalP10Approval01 |
| Error | ZamalP10Error01 |
| Response | ZamalP10Response01 |
| Logging | ZamalP10Logging01 |

The source templates retain `active: false` for safe import/review. Runtime
publication is verified separately: installed n8n 2.33.0 requires a published
version for database subworkflow calls. These are callable subworkflows, without
public webhook, schedule or automatic execution triggers. Existing Command Center
configuration is preserved.

Intake uses the approved encrypted HTTP Header Auth credential reference
`Zamal Operator Gateway` to call `http://127.0.0.1:8788/events`. Credential material
is absent from definitions. `scripts/run_gateway.py` loads the existing private
owner-controlled `state/gateway/auth.env`; it never creates credentials.
Provisioning approval `9c74d7ef-7a51-486a-a7e9-26db73c0bd08` is already EXECUTED.
Do not reprovision or replay it.

Approval presentation accepts canonical task/action/approval IDs from the Operator.
It does not authorize execution. Decisions remain in the existing Phase 9 trusted
local control plane. Error/response/logging modules whitelist result fields;
there is no parallel n8n approval/task store or remote decision endpoint.

Real n8n execution of `ZamalP10Verify01` invoked all five published modules:
protected proposal → approval presentation → response → logging, duplicate intake,
text intake and error formatting. The protected proposal remains PENDING and must
not be executed as part of verification. Duplicate input reuses that proposal.
Evidence: `state/gateway/binding-verification-result.json` and
`state/gateway/live-verification.json`. The manual verification workflow is inactive.

Versioned workflow exports remove credentials, parameters, static and pinned data.
They are hashed audit snapshots, not restore-ready backups. Recovery snapshots are
private under `state/backups/`; restoration requires separate review/authorization.
