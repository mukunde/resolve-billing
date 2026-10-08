# ADR-008 — Executing a financial action: one claim, and no answer is not a no

**Status:** accepted · 2026-10-08

## Context

An approved action becomes a credit note through one call to the billing
platform. Two code reviews read that path and found it promised more than it
kept. The idempotency key was right in intent, and the execution around it
was not:

- A failed action could be marked `EXECUTED` by calling `execute()` again,
  with no write on the platform. The state table allowed `FAILED → EXECUTED`,
  and `execute()` went straight to that transition for any state other than
  `APPROVED`.
- Two executions of one approved action both read `APPROVED` and both called
  the platform. The check was made in Python on a read, and the `UPDATE` that
  followed named the action but not the state it expected.
- Every exception was recorded as `FAILED`, a timeout included. Replayed
  against the previous code, a lost response left the action `FAILED` while a
  credit note existed on the platform.

In finance, "the platform did not answer" and "the platform said no" are
different facts. Treating the first as the second means either retrying a
write that already landed, or reporting a failure that did not happen.

## Decision

**`EXECUTED` takes proof.** The store refuses `EXECUTED` without the
platform's reference for the write. The guard lives in the store, not in the
caller, so no future path can claim money moved without that reference.

**Executing starts with a claim.** `APPROVED`, or `FAILED` for a retry,
moves to `EXECUTING`, and only the caller that makes that move rings the
platform. A concurrent caller finds the action claimed and is refused (409).
Every transition's `UPDATE` names the state it expects (`WHERE action_id = ?
AND state = ?`). If the action moved between the read and the write, nothing
changes and the store raises. That covers two processes, not only two
coroutines.

**Three outcomes, not two:**

| the platform… | state | what happens next |
|---|---|---|
| returned the credit note | `EXECUTED` | done, with its reference |
| answered no (`ProviderRejected`) | `FAILED` | nothing was written; a retry claims again |
| did not answer, or answered unreadably | `UNKNOWN_OUTCOME` | reconciled before anything else |

**An unknown outcome is settled by asking, never by writing blind.** The
port gains a read, `find_action(idempotency_key)`. Reconciliation asks the
platform what it holds under the key. If the write landed, the action becomes
`EXECUTED` with that reference. If not, it becomes `FAILED`, which the next
`execute()` retries. Either way, the `resolution` column says in words what
was asked and what was answered: that column is the audit of the recovery.

**An abandoned claim is an unknown outcome.** A process can die between
ringing the platform and writing the answer down, which leaves the action in
`EXECUTING`. Past a grace period (5 minutes by default), `execute()` treats
the claim as abandoned and reconciles it. A claim still in flight is refused.
Reconciling one that is not abandoned after all is safe, since the
idempotency key makes the two writes one; the grace period only keeps the
noise down.

`execute()` reconciles on its own, so recovery needs no endpoint or operator
step: calling it again is the retry. The API answers 502 when the platform
refused the write, and 504 when the outcome is unknown, with the instruction
that the next call reconciles.

```text
PENDING_APPROVAL --approve--> APPROVED --claim--> EXECUTING --> EXECUTED
                                  ^                   |-> FAILED ----- retry --> (claim)
                                  |                   \-> UNKNOWN_OUTCOME
                                  |                          | reconcile
                                  |               found: EXECUTED · absent: FAILED
```

## Consequences

**The port changed** (ADR-001). `BillingProvider` gains `find_action`, and
adapters must raise `ProviderRejected` only when the platform's answer says
no. Any other error leaves the outcome unknown, which is the safe default: an
adapter that cannot tell is treated as not knowing. A real adapter implements
`find_action` by looking the key up, or by listing the invoice's credit notes
and matching on it. Stripe keeps idempotency keys for a limited time, so the
listing has to work without them.

**Reconciliation is only as good as the platform's memory of the key.** If
the platform forgot the key and the write did land, `find_action` returns
nothing, and the retry relies on the platform's idempotency to avoid a second
credit note. The test against the Stripe sandbox (S7) is where this is shown,
not assumed.

**What this does not settle:**
- **A refund was executed as a credit note** (D4). It is settled in a
  separate change: the port gains `create_refund`, `execute()` writes the
  instrument that was approved, and a type with no write is refused
  before the claim.
- **The idempotency key identifies a technical write, not an economic
  correction.** Two legitimate disputes on one invoice for the same amount
  collide (D6).
- **There is no scheduler.** An abandoned claim is recovered at the next
  `execute()` call, not on a timer.

These are separate decisions.

**Re-evaluation condition:** more than one kind of write (refunds included),
or execution moving off the request path into a worker. At that point,
reconciliation becomes a sweep over every `UNKNOWN_OUTCOME` and abandoned
`EXECUTING`, run on a schedule, instead of a step in `execute()`.
