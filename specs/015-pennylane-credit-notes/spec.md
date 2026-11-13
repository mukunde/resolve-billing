# Feature Specification: Pennylane credit notes

**Feature Branch**: `015-pennylane-credit-notes`

**Created**: 2026-11-12

**Status**: Approved by the maintainer on 2026-11-12, at `915cd57`

**Input**: Roadmap item 3, order A (coordination decision 45): after Pennylane reading
(spec 014, merged), Pennylane credit notes. The framing was reviewed by Codex (r374-r379)
and its eleven recommendations approved by the maintainer on 2026-11-12 (coordination
decision 47). Sandbox experiments ran the same day (script reviewed by Codex r380-r382,
run at the maintainer's request); their results are below and fix what the framing left
open.

**Behaviour changed**: capabilities *Approval and execution*
([contract](../../docs/contract/approval-and-execution.md)) and *Billing platform*
([contract](../../docs/contract/billing-platform.md)) -- a platform declares which types
it writes; a platform may declare no idempotency window, under which a creation is never
resent and an absence never proves anything; a new attention, `partial_write`; a
per-customer blocker in the action store; the approved tax breakdown reaches the write;
Pennylane writes credit notes on the declared sandbox. Stripe and the fake write as
before.

**Sensitive change**: yes. It is the money-moving core (constitution II) and amends ADR-008.
The pull request needs the maintainer's explicit approval of the final diff.

## Clarifications

### Session 2026-11-12 (decision 47, the maintainer)

- Q: Which writes? → A: Credit notes only, on the declared sandbox, with a write token of
  their own behind `PENNYLANE_WRITES=enabled`. Production is out of scope.
- Q: A refund proposed on Pennylane, which has no refund write? → A: It stays a proposal a
  person carries out outside ResolveBilling (a bank transfer). The approval page says so;
  execution refuses it before reading or claiming anything.
- Q: A credit note on a paid invoice? → A: Allowed, read as an amount owed to the customer
  (`customer_balance`), the money returned by a person.
- Q: A document left half-way? → A: Final for everything automatic, to a person; nothing
  resent, nothing resumed. The customer is blocked for further writes until a person
  resolves it.
- Q: A creation whose answer was lost? → A: Never resent; an absence is never proof.
- Q: A request already sent cannot be cancelled on Pennylane's side? → A: The residual risk
  is accepted on the sandbox; the person is told and reads the document again after a
  delay.
- Q: Before writes are enabled? → A: Pennylane's written answer that the cumulative cap is
  enforced atomically, and that finalising sends or queues nothing. Without both, the code
  ships with writes off.

### What the sandbox showed (2026-11-12, observations, not guarantees)

- A **draft** credit note can be linked to an invoice; it is then finalised. A draft's
  lines take no `imputation_dates` (Pennylane's draft schema refuses them).
- A second create with an `external_reference` already used -- by a finalised document or
  a draft -- is refused, 422 "External reference has already been taken". Not shown
  atomic; no rule rests on it.
- A finalised credit note reads `status: cancelled`, `draft: false`, its amounts negative
  (`currency_amount` -60.0), numbered in the invoices' own sequence. An open invoice it
  credits becomes `partially_cancelled`, `remaining_amount_with_tax` lowered by the credit;
  a paid one stays `paid`.
- The cap, sequentially: linking a credit note beyond the invoice's total is refused, 400
  "The credit notes amount is bigger than the invoice amount", and so is a second credit
  note whose sum with the first exceeds it; the refused draft stays a draft. Refused at
  linking, not at creation. Across the interface and the API too: after a credit note of
  72.00 made in Pennylane's interface (drafted there, finalised through the API after
  decision 47's check), the API's 72.00 more was refused at linking the same way.
  Atomicity under competing links is not shown.
- Pennylane's interface would not finalise a document for a company customer without a
  SIREN and a VAT number; the API finalised them. An invoice credited from the interface
  read `partially_paid`, one credited through the API `partially_cancelled` -- both refused
  by spec 014's reads (`invoice_credit`).
- On four draft lines (20 %, 5.5 %, exempt, a 0.05 price) Pennylane's tax per line equalled
  this code's (the line's amount excluding tax times its rate, rounded to the cent). None
  was an exact half-cent tie, so Pennylane's rounding rule is not inferred; the draft
  comparison (FR-004) is what guards it.
- A document dated before the latest finalised invoice cannot be finalised (422): credit
  notes are dated the day they are made.
- After finalising: `electronic_billing_address` and `e_invoicing` null on every credit
  note, and still null a day later (a field observation, not proof that nothing was
  sent).
- From the write contract's runs (2026-11-13): a draft credit note reads status
  `credit_note` and an empty number; a paid invoice credited whole reads `cancelled` and
  `paid: true`; the write contract passed on the fourth generation (recorded in the
  billing-platform contract).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An approved credit note is written on Pennylane (Priority: P1)

An approver approves a credit note proposed on a Pennylane invoice; execution writes it on
the sandbox as one credit note, linked to the invoice, finalised, for exactly the amount
and tax approved.

**Why this priority**: It is the slice.

**Independent Test**: The adapter against a stubbed transport, without network; then the
write contract on the sandbox.

**Acceptance Scenarios**:

1. **Given** an approved credit note on an open taxed invoice, **When** it is executed,
   **Then** one credit note exists on Pennylane, linked, finalised, its amount excluding
   tax, tax by rate and total equal to the approved breakdown, carrying the action's key as
   its `external_reference`; the action is `EXECUTED` with its reference, allocation
   `reduce_due`.
2. **Given** the same on a paid invoice, **Then** the same, allocation `customer_balance`.
3. **Given** a draft whose figures differ from the approved breakdown, **Then** the draft is
   deleted, a read shows it gone, and the action is `FAILED` with Pennylane's figures in its
   reason; nothing numbered exists.
4. **Given** a link of a confirmed draft that Pennylane refuses for the cap (400, its cap
   message), **Then** the same: draft deleted, shown gone, `FAILED` with Pennylane's reason.
   Any other refusal of the link goes to a person (US 2).

### User Story 2 - A write that may have landed goes to a person (Priority: P1)

**Why this priority**: Pennylane documents no idempotency; a resend can make a second
credit note.

**Independent Test**: Unit tests with a transport that loses answers, refuses at each
step, answers late, and a stale claim whose worker returns.

**Acceptance Scenarios**:

1. **Given** a creation whose answer is lost, **When** the action is reconciled, **Then**
   one credit note linked, finalised and alike found makes it `EXECUTED`; a draft or an
   unlinked document makes it `UNKNOWN_OUTCOME` with `partial_write`; several or unlike
   are incidents; none found leaves it to a person -- never `FAILED`, never resent.
2. **Given** a lost or refused answer at linking (other than the cap on a draft), at
   finalising, or at deleting, **Then** `UNKNOWN_OUTCOME`, `partial_write`, the document's
   id and stage in the resolution; nothing automatic follows.
3. **Given** an action of a customer that is `EXECUTING`, `UNKNOWN_OUTCOME` or
   `partial_write`, **When** another credit note of that customer is executed, **Then** it
   is refused before anything is sent.
4. **Given** a worker past its step deadline or no longer holding its claim, **Then** it
   sends nothing more.

### User Story 3 - A refund on Pennylane is a person's (Priority: P1)

**Acceptance Scenarios**:

1. **Given** a refund proposed on Pennylane, **When** the approval page and
   `GET /actions/{id}` are read, **Then** they say it cannot be executed on this platform
   and is a person's to carry out; **When** it is executed, **Then** 409 before anything
   is read or claimed, the action keeping its state.

### Edge Cases

- Writes not enabled, or a token whose company is not the declared sandbox: the platform
  writes nothing (`writes` empty), as a read-only platform today.
- An unlinked or unfinalised credit note carrying our reference prefix for the customer:
  refused before creating (an incident), whatever the store says.
- An untaxed invoice: one credit line at the invoice's no-tax code, its amount the approved
  amount; the comparison has no tax on either side.
- A taxed invoice: credited whole (spec 013: partial corrections on a taxed invoice stay
  withheld), each original line reversed at its rate, quantity negative.
- The key (64 hexadecimal characters, prefixed) longer than `external_reference` allows:
  refused before sending, nothing written (its limit is to be read in the contract run).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A billing platform MUST declare the set of types it writes (`writes`): Stripe
  `credit_note` and `refund`, the fake both, Pennylane `credit_note` when writes are enabled
  and none otherwise. `can_write` stays as "writes anything". An action of a type the
  platform does not write MUST be refused by `execute()` and `reconcile()` before anything
  is read, reconciled or claimed (409, the action keeping its state), and the approval page
  and `GET /actions/{id}` MUST say it is a person's to carry out outside ResolveBilling.
- **FR-002**: Pennylane MUST write only when `PENNYLANE_WRITES=enabled`, with a token of its
  own (`PENNYLANE_WRITE_TOKEN`, `customer_invoices:all`) loaded for writes alone, and only
  to the company `PENNYLANE_SANDBOX_COMPANY_ID` names, checked as reads are.
- **FR-003**: `plan_write` on Pennylane MUST answer `reduce_due` for an open invoice
  (`upcoming`, `late`), `customer_balance` for a paid one, and refuse every other state
  (`invoice_state`).
- **FR-004**: `create_credit_note` on Pennylane MUST, within the claim, in this order:
  check the store blocker (FR-009); refuse when the customer has a credit note carrying our
  prefix that is not linked and finalised; refuse, nothing sent, unless the customer has no
  SIREN, no VAT number, no eligible electronic billing address and every address under the
  reserved test domain (decision 47, point 10); create a draft dated the day, in the invoice's
  currency, `external_reference` derived from the key, its lines from the approved breakdown
  (FR-010); read it back and compare it exactly with the approved breakdown; link it; read
  the customer and the draft again -- the same customer conditions, and no electronic
  billing address or e-invoicing state on the draft -- and finalise it only then (otherwise
  FR-006, the draft not finalised); read it back. The credit note's labels MUST carry
  nothing of the dispute's message.
- **FR-005**: Exactly two cases MAY end as "nothing written": a draft whose figures differ
  from the approved breakdown, and a confirmed draft whose link Pennylane refuses for the
  cap (400 with its cap message, the draft read back still a draft and unlinked). In both,
  the draft MUST be deleted, and the action MUST be `FAILED` only after a read shows it
  gone; a deletion refused, lost or not shown MUST follow FR-006. Every other refusal,
  including any other refusal of the link, follows FR-006.
- **FR-006**: Any refusal, lost or unreadable answer after the creation, outside FR-005, MUST
  make the action `UNKNOWN_OUTCOME` with attention `partial_write`, the document's id and
  stage in its resolution, final for everything automatic. The resolution MUST say that a
  request may still be in flight.
- **FR-007**: A platform MAY declare no idempotency window (`None`): the first send happens;
  a creation is never resent; an absence found by `find_action` is never proof, the action
  being left to a person instead of `FAILED`. Pennylane declares none. Stripe's 23 hours and
  its behaviour are unchanged.
- **FR-008**: `find_action` on Pennylane MUST list the documents carrying the key's
  `external_reference` and return a `ProviderAction` only for a credit note linked to the
  invoice, finalised and alike; a draft or unlinked document is `partial_write`; several or
  unlike are incidents as today.
- **FR-009**: The action store MUST refuse executing a credit note for a customer that has
  another credit-note action `EXECUTING`, `UNKNOWN_OUTCOME` or marked `partial_write`,
  until that action is `EXECUTED` (a person's status read finding exactly one alike, as
  today) or a person records its resolution.
- **FR-009a**: An authenticated approver MAY record the resolution of a `partial_write`
  action (`POST /actions/{id}/resolution`, by POST only, the identity the authenticated
  one): a free-text account of what they did on Pennylane, and the observation of a status
  read made at least ten minutes after the attempt's last request, both stored with the
  instant. It releases the blocker; the action stays final for everything automatic and is
  never executed again. The page and the API say, before it is recorded, that a request
  may still have been in flight.
- **FR-010**: The write port MUST pass the approved `TaxBreakdown` (None on an untaxed
  invoice). A platform that builds a credit note from new lines (Pennylane) MUST report on
  its `ProviderAction` the breakdown observed, as the positive amounts of the credit, a
  breakdown whose every tax is zero normalised to None; execution and reconciliation MUST
  compare it exactly with the approved one before `EXECUTED`. Platforms that report none
  (Stripe, which reverses the invoice's own lines, and the fake) and refunds are compared
  as today.
- **FR-011**: Within an execution attempt, before each request, the Pennylane adapter MUST
  check a step deadline (two minutes after the claim, each request's timeout counted) and
  that the store still holds the action `EXECUTING` under its claim; past either it MUST
  stop (FR-006 if a document exists). Reconciliation and a person's status read, which
  only read, are not bound by the claim.
- **FR-012**: The adapter MUST never call Pennylane's email or e-invoicing endpoints.
- **FR-013**: `ProviderAction.test_mode` MUST be true only for the declared sandbox; the
  only identifier Pennylane holds is the key; the check of "the key and identifier the
  platform holds" becomes the key, the store holding one action per key.
- **FR-014**: ADR-008, the execution contract and the billing-platform contract MUST be
  amended in the same pull request: the per-type capability, no window, `partial_write`,
  the blocker, the step deadline, and the residual risk.

### Key Entities

- **Write capability**: the set of types a platform writes.
- **Partial write**: a document made and not completed; its id and stage; a person's.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Tests without network cover every acceptance scenario and FR-001 to FR-013,
  including a lost answer at each step, a refusal at each step, a late answer after a read
  showed nothing, a stale claim, the blocker, and the fake and Stripe unchanged.
- **SC-002**: A write contract on the sandbox, writes enabled for that run only (decision
  47's condition for finalising: customers without SIREN, VAT number or electronic billing
  address, reserved-domain addresses, checked before each finalisation): a credit note on
  an open taxed invoice and on a paid one, found again by `find_action`; a credit beyond
  the total refused at linking, nothing numbered left; a refund refused before claiming;
  a whole-invoice credit on an invoice with mixed rates, an exempt line and rounding edges,
  linked, finalised and found again by `find_action` with the approved figures exactly;
  an untaxed credit. Recorded in the contract.
- **SC-003**: The investigation does not change: the mock baseline matches case by case,
  the floor holds, coverage is up to date.
- **SC-004**: The delivered configuration leaves Pennylane writes off; the contract states
  which prerequisite (FR clarifications) is met and which is not.

## Assumptions

- The approved correction on a taxed invoice is its whole total (spec 013); the credit note
  reverses every line.
- Pennylane's answers to the two written questions may arrive after merging; enabling
  writes is a configuration step, recorded when taken.
- The interface case of the cumulative cap and the next-day e-invoicing observation are
  recorded in the contract when made.
