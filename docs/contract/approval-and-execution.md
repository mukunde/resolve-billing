# Approval and execution

What happens to a proposed financial action from the moment it is queued to
the moment the billing platform holds the write, and what the rest of the
system may rely on. Written on 2026-10-23 from the existing records, as they
stood, before the first change that touches the capability; that change --
writes on a real platform, Stripe in test mode (specs/002-stripe-writes) --
folded in the same day. Reasons are in ADR-004 (human approval), ADR-008
(the execution state machine, the platform as the authority on amounts,
approvals bound to what was shown, writes on a real platform) and ADR-009
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
- **Who may do what.** Approving, rejecting and having a status read again
  are an authenticated approver's, and the identity recorded is the
  authenticated one, never a name sent in the request (ADR-009). Executing is
  the orchestrator's, as an authenticated service. A route whose kind of
  caller is not configured refuses. The review page takes decisions by POST
  only; a GET moves nothing.
- **A pending action survives a restart of the service.** It lives in the
  service's database with its deadline and its run, not in the process; its
  review fingerprint, computed from the stored action and the invoice as
  the platform holds it, stays equal while neither changes. After a clean
  stop or a kill, a new process decides and executes it as the old one
  would have.
- **A deadline survives a restart.** An approval whose deadline passed while
  the service was down is `expired` when read again, its deadline unchanged;
  approving it -- through the API or the review page -- and executing it are
  refused.
- **The same correction delivered again is the same action.** A second
  investigation of the same dispute, before or after a restart, is a run of
  its own and returns the action already queued under the correction's
  idempotency key: one approval, one write.
- **A lost answer is found again, never written twice.** When the platform
  wrote but its answer was lost, the action is `unknown_outcome`; after a
  restart, the next execution reconciles it from the platform to `executed`
  under the write's reference, and the platform holds one write.
- **An approval holds for what was shown, and not forever.**
  - A request for approval has a deadline, 72 hours by default
    (`RESOLVE_APPROVAL_TTL_HOURS`); past it, the action becomes `EXPIRED`
    the next time anyone decides or opens it, checked on access.
  - The page and `GET /actions/{id}` carry a fingerprint of the proposal
    (instrument, invoice, amount) and of the invoice as the platform holds
    it (status, total, lines). An approval sends it back; if it no longer
    matches, the action expires instead of being approved.
  - Execution recomputes it from the platform before every claim; a change
    since the approval expires the action and nothing is written. An
    approval recorded before fingerprints existed carries none and is held
    to the other guards only.
- **One guard of the system's own, before the claim.** An action is never
  executed for more than the invoice's total or in another currency, read
  from the platform. Beyond that, what an invoice can still give back is
  the platform's to refuse; no ledger is kept here.
- **A write is planned once.** After the first claim and before the first
  send, the platform says how the write will land on the invoice as it
  stands (`plan_write`: what is due lowered, the customer's balance
  credited, or money refunded). The answer is stored with the action, and
  every send passes exactly that: a retry never rebuilds it, and an invoice
  that no longer takes it is refused, never written otherwise.
- **Every send falls within the platform's window.** The instant of the
  first attempt is recorded in the first claim, before anything is sent, and
  never reset. A send -- the first, a retry, one after a restart -- happens
  only within the platform's idempotency window from it (23 hours on Stripe
  and on the fake), checked before the claim and again by the adapter at
  the point of sending. Past it, nothing is sent again for that action.

## States

```text
PENDING_APPROVAL --approve--> APPROVED --claim--> EXECUTING --> EXECUTED
       |                          ^                   |-> FAILED ---- retry --> (claim)
       |--reject--> REJECTED      |                   \-> UNKNOWN_OUTCOME
       \--deadline--> EXPIRED     |                          | reconcile
                                  |               found: EXECUTED · absent: FAILED

PENDING_APPROVAL, APPROVED, FAILED --deadline passed or invoice changed--> EXPIRED
FAILED --a person's status read finds the write--> EXECUTED
```

- **Every transition names the state it expects.** The store's `UPDATE`
  carries the state it read; if the action moved in between, nothing changes
  and the transition is refused (409). That holds across processes.
- **`EXECUTED` takes proof.** The store refuses it without the platform's
  reference for the write -- from any state, `FAILED` included.
- **Executing starts with a claim.** `APPROVED`, or `FAILED` for a retry,
  moves to `EXECUTING`; only the caller that makes that move rings the
  platform, and a concurrent one is refused (409).

## Outcomes

| the platform… | state | next |
|---|---|---|
| returned the write, and it is the one approved | `EXECUTED` | done, with its reference and the status observed |
| returned a write unlike the one approved, or an answer it cannot read | `UNKNOWN_OUTCOME` | left to a person |
| refused (`ProviderRejected`), with no earlier attempt unknown | `FAILED` | nothing written; a retry claims again |
| refused after an earlier attempt's outcome was unknown | reconciled at once | the refusal settles nothing on its own |
| did not answer | `UNKNOWN_OUTCOME` | reconciled before anything else |

- **The answer is checked before `EXECUTED`.** The write the platform
  returns -- directly, or found again -- must be the one approved: invoice,
  type, amount, currency, test mode, the allocation planned, and the key and
  action identifier the platform holds. Anything else is an incident.
- **What became of a write is an observation.** The platform's status (a
  refund succeeded, pending, failed; a credit note voided) is recorded with
  the instant it was observed. A write made is `EXECUTED` whatever its
  status, and never written again; any status but success is said in the
  action's resolution and its API answer, for a person to follow up.
- **An unknown outcome is settled by asking, never by writing blind.**
  Reconciliation asks the platform for every write under the key on the
  invoice (`find_action`):

  | found | within the window | past it |
  |---|---|---|
  | one, alike | `EXECUTED`, status observed | the same |
  | one unlike, one with no allocation planned, or several | left to a person | the same |
  | none, no incident | `FAILED`, retried under the same key | left to a person |
  | none, after an incident | left to a person | the same |

  It never writes. The `resolution` column says what was asked and what was
  answered. A read that fails leaves the action as it was.
- **An abandoned claim is an unknown outcome.** An action left in
  `EXECUTING` for more than 5 minutes is reconciled by the next `execute()`;
  one still in flight is refused.
- **`execute()` reconciles on its own**: calling it again is the retry. The
  API answers 502 when the platform refused, 504 when the outcome is unknown
  ("reconciled on the next execute, or put to a person"), 409 for an action
  not in a state to execute, or left to a person, with the reason.
- **A platform that cannot write is never asked to write.** `execute()` and
  `reconcile()` refuse with 409 before anything is read, reconciled or
  claimed, and the action keeps its state. A read that fails before the
  claim leaves the action as it was: unsupported data 422, an outage 503.

## When an action needs a person

An action says why in `attention`, from a closed set:

| attention | means | what is automatic afterwards |
|---|---|---|
| `window_passed` | the platform may no longer answer the key | nothing |
| `window_unknown` | when its first attempt was sent cannot be shown -- an action recorded before the columns existed, or a write found with no allocation planned | nothing |
| `several_writes` | two writes or more under one key | nothing |
| `unlike_the_action` | a write that is not the one approved | nothing |
| `unreadable_answer` | an answer that does not say what it made | nothing |
| `not_successful` | a write made, whose status is not success | the action is `EXECUTED`; to follow up |

- **An incident is final for everything automatic.** Every attention but
  `not_successful` stops the action for good: `execute()` refuses it (409)
  before reconciling, reading or claiming, and no later reconciliation that
  finds nothing makes it `FAILED`.
- **A person can have the status read again**
  (`POST /actions/{id}/status`). Nothing is sent to the platform, and it
  needs no writing: it works with writes switched off. For an `EXECUTED`
  action, the status observed is recorded with its instant, and
  `not_successful` set or cleared by it. For an action left to a person --
  `UNKNOWN_OUTCOME`, or `EXECUTING` or `FAILED` marked `window_unknown` --
  exactly one write alike makes it `EXECUTED`; nothing found, several or
  unlike leave it where it is, and never permit a new write.
- **Actions older than this change** -- in `EXECUTING`, `UNKNOWN_OUTCOME` or
  `FAILED` when the store first opens a file without the new columns -- are
  marked `window_unknown`; approved and earlier ones are untouched.

## Not guaranteed today

- That Stripe keeps idempotency keys for at least a day: taken from its
  documentation, not measured. The 23-hour window rests on it.
- Clearing an incident, other than by a person's status read that finds the
  write: a person acts on the platform, outside this system. An action past
  its window cannot be executed again; its correction is a person's.
- The idempotency key identifies a technical write, not an economic
  correction: two legitimate disputes on one invoice for the same amount
  collide (ADR-008, D6).
- There is no scheduler: an abandoned claim waits for the next `execute()`.
- The restart guarantees are shown at the service boundary, on the fake
  platform (a persistent test double for the lost answer): not recovery from
  a crash inside a database transaction, not an action left `executing` by a
  crash mid-flight (reconciled after five minutes), not the orchestrator's
  resumption, not exactly-once on a real platform.
- An expired correction is not reopened by investigating the dispute again:
  the queue returns the expired action found under its key. A person
  reassesses it.

Stripe's cap on what an invoice's credit notes and refunds give back held
on its test account (2026-10-23, write property 5); an approved correction
was made there once, end to end (SC-002).

## Covered by

`tests/unit/test_execute_action.py`, `tests/unit/test_action_store.py`,
`tests/unit/test_action_policy.py`, `tests/unit/test_stripe_writes.py`,
`tests/integration/test_approval_flow.py`,
`tests/integration/test_approval_binding.py`,
`tests/integration/test_review_page.py`,
`tests/integration/test_resume_callback.py`, `tests/integration/test_auth.py`,
`tests/integration/test_unsupported_platform_data.py`,
`tests/e2e/test_restart_pending_approval.py`,
`tests/e2e/test_restart_expired_approval.py`,
`tests/e2e/test_restart_repeated_delivery.py`,
`tests/e2e/test_restart_timeout_after_write.py`,
`tests/unit/test_persistent_fake.py`; on a Stripe test account,
`tests/contract/test_stripe_execution.py`.
