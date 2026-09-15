# Live WhatsApp UAT evidence — 2026-09-15

## Exact action

- Proposal: `ea934693-3b80-45f7-8b6b-1dcd68c61a17`
- Approval: `a8421126-aa1d-4477-9347-49ac76481207`
- Destination: `Jamal BD Number`
- Message: `ZAMAL AI AGENT LIVE TEST`
- Attachment: none

No replacement proposal or duplicate Telegram approval notice was created.
The exact `APPROVE <proposal-id>` command was received from the configured,
authorized Telegram chat as update `285088093`.

## Root cause and repair

The Control Center startup did not start a Telegram receiver. The Telegram text
parser did not recognize channel-proposal approvals, and channel proposals were
held only in process memory. The first live executor attempt then revealed that
WhatsApp Business exposes the selected contact as exact header text rather than
the expected header `title` attribute. Its send succeeded, but the outgoing
message container no longer had the expected `.message-out` class, so the
immediate verifier reported a false failure.

The repair:

- persists the original proposal and approval binding in the private Operator
  SQLite store;
- starts and stops the Telegram long-poll receiver with the Control Center and
  creates its SQLite connection on the receiver thread;
- accepts the exact bound Telegram command and inline approval callback only
  from the existing authorized-chat gate;
- atomically claims proposal execution and persists the approval update;
- selects only the exact WhatsApp contact title and checks exact header-text
  equality, without returning contact lists or surrounding conversation text;
- verifies exact approved-message equality and outbound-side positioning;
- permits recovery only from proven pre-compose failures;
- reconciles a send-click verification miss only after a fresh visible check
  proves the exact target and exactly one outbound match, without resending.

## Live evidence

- WhatsApp target header exact match: true.
- Exact approved message visible: true.
- Exact message positioned on outbound side: true.
- Exact outbound match count: `1`.
- Proposal status after reconciliation: `COMPLETED`.
- Reconciliation performed another send: false.
- Retry after completion: `already_executed`.
- Restarted Control Center restored the completed proposal and duplicate guard.
- Telegram receiver HTTPS long-poll connection: active.
- No chat bodies, previews, unrelated messages, bot token or private chat ID
  were printed or recorded in this evidence.

## Verification

- Focused Telegram/approval/WhatsApp tests: 198 passed.
- Receiver-thread focused tests: 52 passed.
- Full regression: 1463 passed, zero failures, in 171.776 seconds.
