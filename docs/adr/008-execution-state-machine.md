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

PENDING_APPROVAL, APPROVED, FAILED --deadline passed or invoice changed--> EXPIRED
```

`EXPIRED` is described below, under "An approval holds for what was
shown, and not forever".

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
  correction** (D6). It is derived from provider, invoice, type and amount.
  Two legitimate disputes on one invoice for the same amount collide, and
  nothing capped the corrections an invoice could accumulate: a 99 EUR
  credit note, then a 99 EUR refund, on a 99 EUR invoice, would have paid
  back 198 EUR. The cap is settled below. Tying the key to the originating
  dispute is not: a replayed dispute must keep its identity, and the model
  has no stable dispute identifier yet (an email's `Message-ID`, for
  instance). That belongs with the correction intent (chantier 4).
- **There is no scheduler.** An abandoned claim is recovered at the next
  `execute()` call, not on a timer.

## The platform is authoritative on amounts and limits

Added with D6, 2026-10-08.

**ResolveBilling does not keep a ledger.** What an invoice can still give
back depends on the credit notes, refunds and payments the billing platform
holds. Recomputing it here would duplicate the platform's accounting, and
would be wrong the first time someone corrects an invoice outside this
system. So the platform decides:

- **A write beyond what an invoice can still give back is the platform's to
  refuse.** It arrives here as `ProviderRejected`, and the action becomes
  `FAILED` with the platform's reason. The fake models that rule: credit
  notes and refunds together never exceed the invoice, and a refund never
  exceeds the settled payments left to refund.
- **ResolveBilling keeps one guard of its own, for defence in depth.** An
  action is never executed for more than the invoice's total, or in another
  currency, and the check reads that total from the platform. This is not a
  ledger. It stops a proposal bug from relying on the platform alone.
- **The same holds for amounts to come** (D5, chantier 4). The rules
  *detect* an anomaly, and the platform *computes* the correction: its
  credit note, its tax, its numbering. ResolveBilling proposes, has a human
  approve, and keeps the trace.

**To verify, not assume:** whether Stripe enforces the same caps, and in
particular how it treats a credit note and a refund on one paid invoice. A
contract test against its sandbox (S7) establishes it. Until then, the cap
is a property of the fake, stated as such.

## An approval holds for what was shown, and not forever

Added with D13, 2026-10-09.

A request for approval stayed decidable indefinitely, and the decision
named only the action. If the invoice changed between the page being
shown and the click -- paid, voided, corrected in the platform's console --
or between the click and the execution, the approval applied to facts
nobody had looked at.

- **A request has a deadline**: 72 hours by default
  (`RESOLVE_APPROVAL_TTL_HOURS`), stored with the action. Past it, the
  action becomes `EXPIRED` the next time anyone tries to decide or opens
  the page; approving and rejecting are both refused. `EXPIRED` is the
  record that nobody decided in time, not a refusal. Checked on access,
  with no sweeper: a request nobody opens again does no harm by staying
  pending. The target integration replaces this with a timer in the
  durable workflow (ADR-010).
- **The decision names what was shown.** The page, and `GET
  /actions/{id}`, carry a fingerprint of the proposal -- instrument,
  invoice, amount -- and of the invoice as the platform holds it: status,
  total, lines. An approval sends it back. If it no longer matches, the
  action expires instead of being approved, and the dispute is to be
  investigated again on today's facts.
- **Execution checks it again.** The fingerprint approved is stored; before
  the claim, `execute()` recomputes it from the platform. A change since
  the approval expires the action, and nothing is written. An approval
  recorded before fingerprints existed carries none, and is held to the
  other guards only.

The fingerprint is read from the platform, never from a copy kept here, for
the reason given above: the platform is the authority on the invoice.
What it does not cover is the ledger of corrections already made on the
invoice -- the platform's own caps settle that, at the write.

**Re-evaluation condition:** execution moving off the request path into a
worker, or a volume at which waiting for the next `execute()` to recover an
abandoned claim is too slow. At that point, reconciliation becomes a sweep
over every `UNKNOWN_OUTCOME` and abandoned `EXECUTING`, run on a schedule,
instead of a step in `execute()`. `find_action` is keyed on the idempotency
key, so it already serves refunds and credit notes alike.
