# Email Rules

Email automation may, within authorized scope:
- Read and summarize emails.
- Classify important emails.
- Extract action items and relevant context.
- Draft complete replies/messages.
- Add authorized attachments.
- Save the completed message as a Gmail Draft without approval.

Never send an email without Jamal approval.

Preferred flow:
READ/CONTEXT → DRAFT → VERIFY RECIPIENT/SUBJECT/BODY/ATTACHMENTS → SAVE DRAFT → TELEGRAM APPROVAL → SEND → VERIFY SENT → LOG EVIDENCE.

Before sending, present the concrete final action for:
Approve / Edit / Reject

After APPROVE, the Operator must send the email itself, verify Sent status, record evidence/status, and report completion. It should not hand the remaining send step back to Jamal unless an external system requires unavoidable human interaction.

Read-only or private data restrictions in `knowledge/AUTONOMY_APPROVAL_POLICY.md` still apply. Email content must not be forwarded/shared merely because it was readable.
