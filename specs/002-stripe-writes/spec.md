# Feature Specification: Stripe as a billing platform, writing

**Feature Branch**: `002-stripe-writes`

**Created**: 2026-10-23 · **Revised**: 2026-10-23, after reviews r131 and r132

**Status**: Approved by the maintainer at commit `c51e175`

**Input**: The maintainer's decisions of 2026-10-23 (seven framing choices,
recorded below), after the read-only slice (specs/001-stripe-read-adapter)
was merged: the two writes on Stripe in test mode, a refund made through a
credit note, `find_action` by the invoice, writing switched on explicitly,
refusals told from unknown outcomes, proof on fresh seeded worlds, and a
final-diff approval.

**Behaviour changed**: capability *Billing platform*
([contract](../../docs/contract/billing-platform.md)) -- Stripe can make the
two writes when configured to; the port's `find_action` takes the invoice and
reports what it found, not only that it found something. Capability
*Approval and execution* (ADR-008 amended): what the platform says about a
write once made is recorded and, short of success, put to a person; an
absence the platform can no longer vouch for no longer reads as "not
written"; a write found unlike the one approved is an incident. Rules,
engines and policies do not change.

**Sensitive change**: yes. It moves money -- the money core (constitution II,
IV) -- and changes what an approved action does on a real platform. The pull
request needs the maintainer's explicit approval of the final diff.

## Clarifications

### Session 2026-10-23

- Q: Which writes, where? → A: The two writes the domain has, a credit note
  and a refund, on Stripe in **test mode only**; a live key stays refused.
  Partial amounts, other currencies and invoice numbers stay out of scope.
- Q: How is a refund made? → A: **Through a credit note that refunds**
  (`refund_amount`), never a standalone refund. One instrument for both
  writes: one cap -- Stripe's on what an invoice's credit notes and refunds
  can give back, which ADR-008 relies on instead of a ledger of ours -- and
  one place where a write carries its key and is found again. The cap is
  verified on the test account, not assumed.
- Q: How is a write found again? → A: Every write carries the action's
  idempotency key as Stripe's idempotency key **and** in the credit note's
  metadata. `find_action` lists **the invoice's** credit notes and matches
  the metadata. The port's `find_action` therefore takes the invoice: an
  amendment of ADR-008.
- Q: When does Stripe write? → A: **Only when switched on**:
  `can_write` is false unless the configuration says so
  (`STRIPE_WRITES=enabled`). The restricted key is given the writes the two
  writes need -- which ones is established on the test account and recorded.
  Reading only stays the normal mode.
- Q: What is a refusal? → A: Only an answer from Stripe that proves nothing
  was created -- a closed list -- is `ProviderRejected`. Anything else after
  the request was sent is an unknown outcome, reconciled -- never an outage,
  which for a write would risk a second credit note.
- Q: How is it proved? → A: In CI on the stub client; on the test account by
  contract tests with the writing key: idempotent replay, the shared cap,
  `find_action` after the fact, reconciliation. A write is permanent, so the
  seed builds **fresh worlds to write on** for each contract run.
- Q: Level of review? → A: Sensitive change: final-diff approval.
- Q: (review r131) What follows from "only Stripe's no is a refusal"? → A:
  (1) A write that landed is never written again, whatever became of it: a
  refund still pending, failed or cancelled, a credit note voided -- the
  action is `EXECUTED` with the platform's status recorded, and anything but
  success is put to a person. (2) An absence is conclusive only while Stripe
  still answers the key with what it made; past that window it leaves the
  outcome unknown, for a person. (3) A write found under the key but unlike
  the one approved -- another invoice, type, amount or currency, or two of
  them -- is an incident for a person, never "the first one found"; the
  platform's own answer to a write is checked the same way before the action
  is `EXECUTED`. (4) No
  mail to the customer from Stripe: replies go through the system's reply
  policy (ADR-011). (5) Only an invoice in a state the slice handles is
  written on; anything else is refused before sending.
- Q: (review r132) How is a retry kept safe? → A: The instant of the first
  attempt is recorded before it is sent, and never reset. Every send -- the
  first, a retry, a retry after a restart -- happens only within 23 hours of
  it; past that, nothing is sent automatically for that action. A refusal of
  a later attempt never settles an earlier one whose outcome was unknown: the
  action stays to reconcile. The status recorded after a write is an
  observation, dated; a person can have it read again, which writes nothing.

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
   to the customer's balance, no mail sent, and the action is `EXECUTED` with
   its reference.
2. **Given** an approved refund on a paid invoice, **When** it is executed,
   **Then** Stripe holds one credit note that refunds that amount to the
   payment, and the action is `EXECUTED` with the refund's status.
3. **Given** an executed action, **When** it is executed again, **Then**
   nothing more is written.
4. **Given** writing is not switched on, **When** an approved action is
   executed on Stripe, **Then** it is refused before anything is claimed
   (409), as in the read-only slice; called directly, the adapter's writes
   refuse without a call.
5. **Given** an action not approved, expired, whose invoice changed since
   approval, or for more than the invoice or in another currency, **When**
   execution is asked, **Then** nothing is sent to Stripe.

---

### User Story 2 - A lost answer is settled by asking Stripe (Priority: P2)

The request reaches Stripe but its answer is lost. The action is left with an
unknown outcome; reconciling asks Stripe what it holds under the key, on the
invoice, and settles the action without writing blind -- or, when Stripe can
no longer vouch for an absence, leaves it to a person.

**Independent Test**: on the stub, the write lands and its answer is lost;
the next `execute()` finds the credit note, checks it against the action and
marks the action `EXECUTED`; on the test account, a credit note made under a
key is found by `find_action` long after.

**Acceptance Scenarios**:

1. **Given** a write whose answer was lost, **When** the action is
   reconciled, **Then** the credit note is found on the invoice, checked
   against the action, and the action is `EXECUTED` with its reference.
2. **Given** a write that never reached Stripe, reconciled while Stripe still
   remembers the key, **When** nothing is found, **Then** the action is
   `FAILED`, and the retry is sent under the same key.
3. **Given** the same, reconciled after Stripe stopped remembering keys,
   **When** nothing is found, **Then** the outcome stays unknown and the
   action says it needs a person; nothing is written.
4. **Given** a credit note under the key that differs from the action, or
   two of them, **When** reconciled, **Then** the outcome stays unknown and
   the action says it needs a person.
5. **Given** a first answer lost and a retry that Stripe answers with an
   idempotency conflict, **When** reconciled, **Then** the outcome is
   unknown, never `FAILED`.

---

### User Story 3 - Stripe refuses what an invoice cannot give back (Priority: P3)

A correction beyond what the invoice can still give back -- credit notes and
refunds together -- is refused by Stripe, and the action fails with the reason
by category, nothing written.

**Acceptance Scenarios**:

1. **Given** a 49.00 invoice already given back in full by a credit note,
   **When** a refund of 49.00 is executed, **Then** Stripe refuses, and the
   action is `FAILED`.
2. **Given** an invoice in a state the slice does not write on (partly paid,
   draft, void, uncollectible), **When** a write is executed, **Then** it is
   refused before anything is sent.

### Edge Cases

- A credit note on an open invoice with nothing paid (it lowers what is due)
  and on a paid one (it credits the customer's balance).
- What is due changing between approval and execution.
- A refund pending, failed or cancelled after the credit note was made; a
  credit note voided afterwards.
- Stripe's idempotency key reused with other parameters, or in conflict with
  a request still in progress.
- A key Stripe has forgotten.
- Rate limiting and the SDK's retries during a write; a time budget spent
  mid-write.
- The restricted key without a permission a write needs.

## Requirements *(mandatory)*

### Functional Requirements

**The port and the execution** (ADR-001, ADR-008 amended)

- **FR-001**: `find_action(idempotency_key, invoice_id)` MUST take the
  invoice the action was made on and answer every write made under that key
  on that invoice: none, one, or more. The two writes MUST take the action's
  identifier too. A write and `find_action` MUST return what the checks of
  FR-004 read -- the invoice, the type, the amount and currency, test mode,
  the allocation, the platform's status and when it was observed. The fake
  follows; the action store already holds the invoice.
- **FR-002**: Stripe's `can_write` MUST be false unless the configuration
  says `STRIPE_WRITES=enabled`, read in the wiring only. With it false,
  everything behaves as in the read-only slice.
- **FR-003**: A write the platform made -- answered directly or found by
  reconciliation, and checked as FR-004 says -- MUST make the action
  `EXECUTED` with its reference, whatever its status: the platform's status
  is recorded as an observation, with the instant it was made, and any status
  but success (a refund pending, failed, cancelled or awaiting action; a
  credit note voided) is said in the action's resolution and its API answer,
  for a person to follow up. It is never treated as an absence, and nothing
  is written again for it. A person can have the status read again: a read,
  which writes nothing and records a new dated observation.
- **FR-004**: A write -- the platform's direct answer, or one found under
  the key -- MUST be checked against the action -- invoice, type, amount,
  currency, test mode, and how it was allocated -- before the action is
  `EXECUTED`. Two writes, one unlike the action, or an answer incomplete or
  unreadable, are an incident: the outcome stays unknown, nothing is written
  again, and the action says it needs a person.
- **FR-005**: The instant of an action's first attempt MUST be recorded
  before it is sent, and never reset. Every send -- the first, a retry, one
  after a restart -- MUST happen within 23 hours of it (a margin under the
  day Stripe keeps keys), under the action's key and with exactly the same
  parameters; past it, nothing is sent automatically for that action. An
  absence settles an unknown outcome as `FAILED` only within that window;
  past it, the outcome stays unknown and the action says it needs a person.
- **FR-006**: A refusal MUST settle an action as `FAILED` only when no earlier
  attempt of it had an unknown outcome. Otherwise -- a retry refused after a
  first answer was lost, a precondition no longer true -- the earlier attempt
  may have landed: the action is reconciled, and stays unknown if
  reconciliation cannot conclude.

**The writes on Stripe**

- **FR-010**: A credit note (the domain's `credit_note`) MUST be one Stripe
  credit note on the invoice for the approved amount: on an open invoice with
  nothing paid, it lowers what is due; on a fully paid invoice, its amount is
  credited to the customer's balance (`credit_amount`). Never recorded as
  paid out of band.
- **FR-011**: A refund (the domain's `refund`) MUST be one Stripe credit note
  on a fully paid invoice whose whole amount is refunded to the invoice's
  payment (`refund_amount`). An approved refund never becomes a lower debt or
  a balance credit; the adapter never makes a standalone refund.
- **FR-012**: The adapter MUST write only on an invoice in a state the slice
  handles -- open with nothing paid (credit note only) or fully paid -- and
  refuse any other (partly paid, draft, void, uncollectible) before sending,
  as `ProviderRejected`. A read that fails there leaves the outcome as unknown
  as any failure after the claim. What is due changing in between is Stripe's
  to refuse.
- **FR-013**: Every write MUST carry the action's idempotency key as Stripe's
  idempotency key and, in the credit note's metadata, the key, the action's
  identifier and its type -- nothing else. `email_type` is `none`; `memo` is
  absent. Nothing of the customer's message, nor any text a model wrote,
  reaches Stripe.
- **FR-014**: `find_action` MUST read every credit note of the invoice, to the
  end -- a list cut short is `ProviderUnavailable`, never an absence -- and
  return those whose metadata holds the key, with what reconciliation checks
  (FR-004). It never uses Stripe's search to prove an absence.
- **FR-015**: The `ProviderAction` returned MUST carry the credit note's
  identifier, the action's type, the key, and a status: `succeeded` when the
  credit note is issued and, for a refund, its refund succeeded; `pending`
  while the refund is in progress or awaits action; `failed` when the refund
  failed or was cancelled, or the credit note is void -- each an observation
  with its instant, not the current state.
- **FR-016**: The adapter MUST make no other write: no standalone refund, no
  invoice change, no void, no customer change.

**Failure and safety**

- **FR-020**: On a write, `ProviderRejected` MUST be raised only for Stripe
  answers that prove nothing was created, a closed list: an invalid request
  other than an idempotency error (HTTP 400 or 404), an authentication or
  permission refusal (401, 403), and the adapter's own refusal before sending
  (FR-012). Everything else after the request was sent -- an idempotency
  conflict or mismatch, a connection error, a timeout, a server error,
  retries exhausted, the time budget spent -- MUST leave the outcome unknown,
  never `ProviderUnavailable` and never a refusal. The SDK's own retries keep
  the key and the parameters. Categories, never Stripe's text.
- **FR-021**: The key rules of the read-only slice hold: a restricted test
  key (`rk_test_`) for the service, live keys refused, the seed's secret key
  apart (`.env.seed`). The writes the restricted key needs are established on
  the test account and listed in the operations manual; no key or Stripe text
  is logged.
- **FR-022**: The system's own guard of ADR-008 -- never more than the
  invoice's total, in its currency, read from the platform before the claim --
  stays, ahead of every write.

**Tests**

- **FR-030**: CI tests on the stub client, without network or SDK: the
  parameters of each write (amounts, allocation, `email_type`, metadata, no
  `memo`), the idempotency key; the closed list of refusals against every
  other error; `find_action` over several pages, a page cut short, two
  matches and a mismatch; a direct answer incomplete or unlike the action;
  each platform status after a write, and a status read again; reconciliation
  inside and past the window; no send past the window, a restart included; a
  first answer lost then an idempotency conflict; a first write landed, its
  answer lost, then a retry refused; no write for an action not approved,
  expired, changed, or for more than the invoice or another currency;
  `can_write` and its configuration; no other mutating call.
- **FR-031**: Contract tests, locally with the writing key, on fresh seeded
  worlds: the write properties below, and one end-to-end execution per write
  type through the execution service.
- **FR-032**: The seed script MUST build, on demand, fresh worlds to write on
  -- a fully paid and an open, unpaid invoice per world -- under a new
  generation each time, so a contract run never writes on an invoice a
  previous run wrote on.

### Contract properties checked for writes (closed list)

1. A write made twice under one key makes one write, and answers it both
   times.
2. `find_action` finds a write by its key on its invoice, after the fact, with
   what reconciliation checks, and answers none for a key never used.
3. A credit note on a paid invoice credits the customer's balance; one on an
   open, unpaid invoice lowers what is due. No mail is sent.
4. A refund through a credit note refunds the invoice's payment.
5. Credit notes and refunds together never exceed the invoice: beyond it,
   the platform refuses and nothing is written.
6. A refund on an invoice with nothing collected is refused before sending.
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
- **SC-005**: No reconciliation ever writes; nothing is sent past the
  window; no `FAILED` rests on an absence the platform can no longer vouch
  for, nor on a refusal that follows an unknown outcome.

## Assumptions

- Measured by the contract run on the pinned version (`2026-07-29.dahlia`,
  SDK 15.4.0): the credit notes API takes `amount`, `credit_amount` and
  `refund_amount`; a refunding credit note creates the refund itself
  (`refunds` only links refunds that exist); Stripe caps what an invoice's
  credit notes and refunds can give back; a key answered again within a run
  returns the first write.
- Taken from Stripe's documentation, not measured: that it keeps idempotency
  keys for at least a day. The 23-hour window rests on it.
- The maintainer gives the restricted key its writes; no agent sees a key.
- Out of scope, each for its own slice: partial amounts; other currencies;
  partly paid invoices; voiding a credit note; payments out of band; live
  mode; Pennylane.
