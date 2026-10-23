# Approval and execution

What happens to a proposed financial action from the moment it is queued to
the moment the billing platform holds the write, and what the rest of the
system may rely on. Written on 2026-10-23 from the existing records, as they
stand, before the first change that touches the capability
(specs/002-stripe-writes) is folded in. Reasons are in ADR-004 (human
approval), ADR-008 (the execution state machine, the platform as the
authority on amounts, approvals bound to what was shown) and ADR-009
(identities).

## What the system relies on

- **Money moves only through approval, then execution.** Nothing but the
  execution service calls a write on the billing platform. The decision
  engine recommends, the action policy authorises, a person approves, and
  execution writes exactly the instrument approved: a credit note as a
  credit note, a refund as a refund. A type with no write on the platform is
  refused before anything is claimed, never replaced by the nearest one.
- **One action per correction.** An action is queued with an idempotency key
  derived from what it is -- platform, invoice, type, amount and currency --
  so the same proposal queued twice is one action, and the key is the one
  the platform receives with the write.
- **Approval and execution are two steps.** Approving records who said yes,
  to what; executing rings the platform. A platform outage never costs a
  second human decision.
- **Who may do what.** Approving and rejecting are an authenticated
  approver's, and the identity recorded is the authenticated one, never a
  name sent in the request (ADR-009). Executing is the orchestrator's, as an
  authenticated service. A route whose kind of caller is not configured
  refuses. The review page takes decisions by POST only; a GET moves
  nothing.
- **An approval holds for what was shown, and not forever.**
  - A request for approval has a deadline, 72 hours by default
    (`RESOLVE_APPROVAL_TTL_HOURS`); past it, the action becomes `EXPIRED`
    the next time anyone decides or opens it, checked on access.
  - The page and `GET /actions/{id}` carry a fingerprint of the proposal
    (instrument, invoice, amount) and of the invoice as the platform holds
    it (status, total, lines). An approval sends it back; if it no longer
    matches, the action expires instead of being approved.
  - Execution recomputes it from the platform before the claim; a change
    since the approval expires the action and nothing is written. An
    approval recorded before fingerprints existed carries none and is held
    to the other guards only.
- **One guard of the system's own, before the claim.** An action is never
  executed for more than the invoice's total or in another currency, read
  from the platform. Beyond that, what an invoice can still give back is
  the platform's to refuse; no ledger is kept here.

## States

```text
PENDING_APPROVAL --approve--> APPROVED --claim--> EXECUTING --> EXECUTED
       |                          ^                   |-> FAILED ---- retry --> (claim)
       |--reject--> REJECTED      |                   \-> UNKNOWN_OUTCOME
       \--deadline--> EXPIRED     |                          | reconcile
                                  |               found: EXECUTED · absent: FAILED

PENDING_APPROVAL, APPROVED, FAILED --deadline passed or invoice changed--> EXPIRED
```

- **Every transition names the state it expects.** The store's `UPDATE`
  carries the state it read; if the action moved in between, nothing changes
  and the transition is refused (409). That holds across processes.
- **`EXECUTED` takes proof.** The store refuses it without the platform's
  reference for the write.
- **Executing starts with a claim.** `APPROVED`, or `FAILED` for a retry,
  moves to `EXECUTING`; only the caller that makes that move rings the
  platform, and a concurrent one is refused (409).

## Three outcomes

| the platform… | state | next |
|---|---|---|
| returned the write | `EXECUTED` | done, with its reference |
| refused (`ProviderRejected`) | `FAILED` | nothing written; a retry claims again |
| did not answer, or answered unreadably | `UNKNOWN_OUTCOME` | reconciled before anything else |

- **An unknown outcome is settled by asking, never by writing blind.**
  Reconciliation asks the platform what it holds under the idempotency key
  (`find_action`): found, the action is `EXECUTED` with that reference;
  absent, it is `FAILED`, which the next `execute()` retries under the same
  key. The `resolution` column says what was asked and what was answered.
- **An abandoned claim is an unknown outcome.** An action left in
  `EXECUTING` for more than 5 minutes is reconciled by the next `execute()`;
  one still in flight is refused.
- **`execute()` reconciles on its own**: calling it again is the retry. The
  API answers 502 when the platform refused, 504 when the outcome is unknown,
  409 for an action not in a state to execute.
- **A platform that cannot write is never asked.** `execute()` and
  `reconcile()` refuse with 409 before anything is read, reconciled or
  claimed, and the action keeps its state. A read that fails before the
  claim leaves the action as it was: unsupported data 422, an outage 503.

## Not guaranteed today

- That Stripe enforces the caps the fake models: the only platform that
  writes is the fake.
- Reconciliation is only as good as the platform's memory of the key: a key
  it forgot reads as an absence.
- The idempotency key identifies a technical write, not an economic
  correction: two legitimate disputes on one invoice for the same amount
  collide (ADR-008, D6).
- There is no scheduler: an abandoned claim waits for the next `execute()`.

## Covered by

`tests/unit/test_execute_action.py`, `tests/unit/test_action_policy.py`,
`tests/integration/test_approval_flow.py`,
`tests/integration/test_approval_binding.py`,
`tests/integration/test_review_page.py`,
`tests/integration/test_resume_callback.py`, `tests/integration/test_auth.py`,
`tests/integration/test_unsupported_platform_data.py`.
