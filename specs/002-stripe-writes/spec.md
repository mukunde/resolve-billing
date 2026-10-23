# Feature Specification: Stripe as a billing platform, writing

**Feature Branch**: `002-stripe-writes`

**Created**: 2026-10-23

**Status**: Draft -- for the maintainer's approval

**Input**: The maintainer's decisions of 2026-10-23 (seven framing choices,
recorded below), after the read-only slice (specs/001-stripe-read-adapter)
was merged: the two writes on Stripe in test mode, a refund made through a
credit note, `find_action` by the invoice, writing switched on explicitly,
refusals told from unknown outcomes, proof on fresh seeded worlds, and a
final-diff approval.

**Behaviour changed**: capability *Billing platform*
([contract](../../docs/contract/billing-platform.md)) -- Stripe can make the
two writes when configured to; the port's `find_action` takes the invoice.
Rules, engines, policies and the approval flow do not change.

**Sensitive change**: yes. It moves money -- the money core (constitution II,
IV) -- and changes what an approved action does on a real platform. The pull
request needs the maintainer's explicit approval of the final diff.

## Clarifications

### Session 2026-10-23

- Q: Which writes, where? → A: The two writes the domain has, a credit note
  and a refund, on Stripe in **test mode only**; a live key stays refused.
  Partial amounts, other currencies and invoice numbers stay out of scope.
- Q: How is a refund made? → A: **Through a credit note that refunds**
  (`refund_amount`), not a standalone refund. Stripe caps an invoice's credit
  notes; a standalone refund is outside that cap. Made through the credit
  note, credit notes and refunds share Stripe's cap, which ADR-008 relies on
  instead of a ledger of ours. To be verified on the test account, not
  assumed.
- Q: How is a write found again? → A: Every write carries the action's
  idempotency key as Stripe's idempotency key **and** in the credit note's
  metadata. `find_action` lists **the invoice's** credit notes and matches
  the metadata, which still works once Stripe has forgotten the key (it keeps
  keys for at least a day). The port's `find_action` therefore takes the
  invoice: an amendment of ADR-008.
- Q: When does Stripe write? → A: **Only when switched on**:
  `can_write` is false unless the configuration says so
  (`STRIPE_WRITES=enabled`). The restricted key gains the one write it needs
  (credit notes); reading only stays the normal mode.
- Q: What is a refusal? → A: Only Stripe's answer saying no -- an amount too
  large, an invoice that cannot take it -- is `ProviderRejected`: nothing was
  created. A connection error, a timeout, a server error, an exhausted retry
  or a spent time budget **after the request was sent** is an unknown
  outcome, reconciled -- never an outage, which for a write would risk a
  second credit note.
- Q: How is it proved? → A: In CI on the stub client; on the test account by
  contract tests with the writing key: idempotent replay, the shared cap,
  `find_action` after the fact, reconciliation. A write is permanent, so the
  seed builds **fresh worlds to write on** for each contract run.
- Q: Level of review? → A: Sensitive change: final-diff approval.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An approved correction is made on Stripe, once (Priority: P1)

A person approves a credit note proposed for a duplicate invoice on a Stripe
test account. Execution makes one credit note on Stripe for the approved
amount and records its reference; executing again makes nothing more.

**Why this priority**: the first time an approved action moves money on a
real platform.

**Independent Test**: on a fresh seeded duplicate world, investigate, approve
the proposal, execute twice; Stripe holds one credit note, the action is
`EXECUTED` with its `cn_` reference.

**Acceptance Scenarios**:

1. **Given** an approved credit note on a paid invoice, **When** it is
   executed, **Then** Stripe holds one credit note for that amount, credited
   to the customer's balance, and the action is `EXECUTED` with its reference.
2. **Given** an approved refund on a paid invoice, **When** it is executed,
   **Then** Stripe holds one credit note that refunds that amount to the
   payment, and the action is `EXECUTED`.
3. **Given** an executed action, **When** it is executed again, **Then**
   nothing more is written.
4. **Given** writing is not switched on, **When** an approved action is
   executed on Stripe, **Then** it is refused before anything is claimed
   (409), as in the read-only slice.

---

### User Story 2 - A lost answer is settled by asking Stripe (Priority: P2)

The request reaches Stripe but its answer is lost. The action is left with an
unknown outcome; the next execution asks Stripe what it holds under the key,
on the invoice, and settles the action without writing blind.

**Independent Test**: on the stub, the write succeeds and its answer is lost;
the next `execute()` finds the credit note by its metadata and marks the
action `EXECUTED`; on the test account, a credit note made under a key is
found by `find_action` long after.

**Acceptance Scenarios**:

1. **Given** a write whose answer was lost, **When** the action is
   reconciled, **Then** the credit note is found on the invoice and the action
   is `EXECUTED` with its reference.
2. **Given** a write that never reached Stripe, **When** the action is
   reconciled, **Then** nothing is found and the action is `FAILED`, ready for
   a retry.

---

### User Story 3 - Stripe refuses what an invoice cannot give back (Priority: P3)

A correction beyond what the invoice can still give back -- credit notes and
refunds together -- is refused by Stripe, and the action fails with the reason
by category, nothing written.

**Acceptance Scenarios**:

1. **Given** a 49.00 invoice already given back in full by a credit note,
   **When** a refund of 49.00 is executed, **Then** Stripe refuses, and the
   action is `FAILED`.
2. **Given** an unpaid invoice, **When** a refund is executed, **Then** Stripe
   refuses, and nothing is written.

### Edge Cases

- A credit note on an open invoice (it lowers what is due) and on a paid one
  (it credits the customer's balance).
- A refund on an invoice paid by a payment already partly refunded elsewhere.
- Stripe's idempotency key reused with other parameters (refused).
- A key Stripe has forgotten; a credit note voided after it was made.
- Rate limiting and retries during a write; a time budget spent mid-write.
- The restricted key without the credit-note permission.

## Requirements *(mandatory)*

### Functional Requirements

**The port** (ADR-001, ADR-008 amended)

- **FR-001**: `find_action(idempotency_key, invoice_id)` MUST take the
  invoice the action was made on, and answer the write made under that key on
  that invoice, or `None`. The fake and the execution service follow; the
  action store already holds the invoice.
- **FR-002**: Stripe's `can_write` MUST be false unless the configuration
  says `STRIPE_WRITES=enabled`, read in the wiring only. With it false,
  everything behaves as in the read-only slice.

**The writes on Stripe**

- **FR-010**: A credit note (the domain's `credit_note`) MUST be one Stripe
  credit note on the invoice for the approved amount: on an open invoice it
  lowers what is due; on a paid invoice its amount is credited to the
  customer's balance (`credit_amount`), never recorded as paid out of band.
- **FR-011**: A refund (the domain's `refund`) MUST be one Stripe credit note
  on the paid invoice whose amount is refunded to the invoice's payment
  (`refund_amount`). The adapter never makes a standalone refund.
- **FR-012**: Every write MUST carry the action's idempotency key as Stripe's
  idempotency key and in the credit note's metadata, with the action's type.
  Sent again with the same key, it returns the first credit note; nothing is
  made twice.
- **FR-013**: `find_action` MUST read every credit note of the invoice, to
  the end, and return the one whose metadata holds the key; it never uses
  Stripe's search to prove an absence.
- **FR-014**: The `ProviderAction` returned MUST carry the credit note's
  identifier, the action's type, the key, and a status: `succeeded` when the
  credit note is issued and, for a refund, its refund succeeded; `pending`
  while the refund is in progress.
- **FR-015**: The adapter MUST make no other write: no standalone refund, no
  invoice change, no void.

**Failure and safety**

- **FR-020**: On a write, only Stripe's answer refusing it -- an invalid
  request, a refused amount, an idempotency key reused with other
  parameters, a missing permission -- MUST be `ProviderRejected`, by category,
  never with Stripe's text. Anything else after the request was sent -- a
  connection error, a timeout, a server error, retries exhausted, the time
  budget spent -- MUST be raised as an unknown outcome, never as
  `ProviderUnavailable` and never as a refusal.
- **FR-021**: The key rules of the read-only slice hold: a restricted test
  key (`rk_test_`) for the service, live keys refused, the seed's secret key
  apart (`.env.seed`). The restricted key MUST be given the credit-note write
  permission (and only the writes Stripe requires for a refunding credit
  note, listed in the operations manual); no key or Stripe text is logged.
- **FR-022**: The system's own guard of ADR-008 -- never more than the
  invoice's total, in its currency, read from the platform at execution --
  stays, ahead of every write.

**Tests**

- **FR-030**: CI tests on the stub client, without network or SDK: the
  parameters of each write, the idempotency key and metadata, `find_action`
  over several pages, the error mapping of writes (refusal vs unknown
  outcome), `can_write` and its configuration, no other mutating call.
- **FR-031**: Contract tests, locally with the writing key, on fresh seeded
  worlds: the write properties below, and one end-to-end execution per
  write type through the execution service.
- **FR-032**: The seed script MUST build, on demand, fresh worlds to write on
  -- a paid and an open invoice per world -- under a new generation each
  time, so a contract run never writes on an invoice a previous run wrote
  on.

### Contract properties checked for writes (closed list)

1. A write made twice under one key makes one write, and answers it both
   times.
2. `find_action` finds a write by its key on its invoice, after the fact, and
   answers `None` for a key never used.
3. A credit note on a paid invoice credits the customer's balance; one on an
   open invoice lowers what is due.
4. A refund through a credit note refunds the invoice's payment.
5. Credit notes and refunds together never exceed the invoice: beyond it,
   the platform refuses and nothing is written.
6. A refund on an invoice with nothing collected is refused.
7. A platform that cannot write says so, and nothing is written (property 10
   of the read-only slice, unchanged).

### Key Entities

- **Write world**: a seeded customer with invoices made to be written on,
  fresh for each contract run.

## Success Criteria *(mandatory)*

- **SC-001**: The seven write properties hold on the test account, or each
  difference is listed in the contract and approved.
- **SC-002**: End to end on Stripe: a dispute investigated, its proposal
  approved and executed, makes exactly one credit note for the approved
  amount; executing again makes none.
- **SC-003**: The free CI job runs with no key and no network, and the mock
  baseline is matched.
- **SC-004**: With `STRIPE_WRITES` not set to `enabled`, nothing is ever
  written to Stripe.

## Assumptions

- Stripe's credit notes API at version `2026-07-29.dahlia` (SDK 15.4.0)
  takes `amount`, `credit_amount` and `refund_amount`, and caps an invoice's
  credit notes at what it can still give back; a refunding credit note
  creates the refund itself. The contract run confirms each.
- The maintainer creates the writing permission on the restricted key; no
  agent sees a key.
- Out of scope, each for its own slice: partial amounts; other currencies;
  voiding a credit note; payments out of band; live mode; Pennylane.
