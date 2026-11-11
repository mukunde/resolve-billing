# Feature Specification: Pennylane as a billing platform, read-only

**Feature Branch**: `014-pennylane-read`

**Created**: 2026-11-11

**Status**: Draft -- for the maintainer's approval

**Input**: Roadmap item 3, order A (coordination decision 45): after tax in the domain
(spec 013, merged), Pennylane as a second real billing platform, reading first. The
framing was reviewed by Codex (r349-r353) and decided by the maintainer on 2026-11-10 and
2026-11-11 (coordination decision 46): what the domain accepts as unknown stays unknown;
what it requires is refused until its meaning is established -- by Pennylane's
documentation, by Pennylane's written answer, or by a convention the maintainer approves
with its effect. Access: a Pennylane sandbox opened by the maintainer on 2026-11-10 and a
company token, read-only on Customers, Customer invoices and Billing subscriptions, in
`.env` as `PENNYLANE_API_TOKEN`; no agent has seen it. A read-only probe (2026-11-11)
found the sandbox empty of customers, invoices and subscriptions.

**Behaviour changed**: capability *Billing platform*
([contract](../../docs/contract/billing-platform.md)) -- a third implementation of the
port, selectable by configuration; four new reasons in the closed set; the invoice
reference hooks receive the invoices the investigation loaded; an invoice may carry its
number. The fake and Stripe behave, serialise and send to the engine exactly as before
(spec 013's snapshot holds them).

**Sensitive change**: yes. It adds a source of the evidence financial proposals rest on
(constitution II), and it touches what a message's invoice references resolve to and what
the masking hides before a model (VII). The pull request needs the maintainer's explicit
approval of the final diff.

## Clarifications

### Session 2026-11-11 (decision 46, the maintainer)

- Q: Fields whose meaning is not established? → A: Where the domain accepts "unknown"
  (a line without a period, a payment without a date), mapped as unknown; where it
  requires a value, refused until established. A value the seed writes and reads back
  establishes no meaning.
- Q: A date without a time? → A: Convention: the date's midnight in Europe/Paris,
  converted to UTC. Effect: an invoice at the 180-day window's edge may fall one day in or
  out.
- Q: A line's period? → A: Convention: `imputation_dates.end_date` is inclusive; the
  domain's period runs from `start_date` at midnight (Paris) to the midnight after
  `end_date`, excluded. A line without `imputation_dates` has no period;
  `performance_period` (German VAT) is not read.
- Q: When does a subscription's service end? → A: Convention: `finished` → at `finish`,
  that day included; `stopped` → `stopped_at` prevents the next renewal, the service runs
  until the day before `next_occurrence`, included (`next_occurrence` empty → refused).
- Q: Payment statuses and `cancelled`? → A: Conventions, pending Pennylane's technical
  support (asked on 2026-11-11), to be adjusted on its answer: `confirmed`, `paid_out` →
  succeeded; `refunded` → refunded (read as full); `failed`, `aborted`, `error`,
  `cancelled`, `customer_approval_denied` → failed; `initiated`, `pending`, `emitted`,
  `prepared`, `submitted`, `pending_submission`, `pending_customer_approval`,
  `resubmission_requested`, `draft`, `found`, `not_found` → pending; `charged_back` and
  any status not listed → refused. An invoice `cancelled` with no credit note → void.
- Q: Invoice references? → A: Option (a): numbers resolved in this slice, within the
  customer's invoices already loaded; a number outside them is not resolved.
- Q: A customer with several addresses? → A: Refused (`multiple_addresses`).
- Q: The seed? → A: A second token, read and write on the three scopes, for the seed
  alone, created by the maintainer at the contract run; seeded documents stay in the
  sandbox.
- Q: [NEEDS CLARIFICATION: Q1] A subscription's current period, cadence and anchor? The
  domain requires a current period. Recommended, in the line of the conventions above:
  the current period runs from `prev_occurrence` to `next_occurrence` (midnights, Paris);
  the cadence is `recurring_rule` (`monthly` → month, `yearly` → year, `weekly` → week,
  `interval` as the count); the anchor is `start`. A subscription missing any of these is
  refused (`missing_field`). The alternative: cadence and anchor left unknown (proration
  then never concludes on Pennylane), the current period still needed.
- Q: [NEEDS CLARIFICATION: Q2] Which Pennylane company may the adapter read? Recommended:
  only the sandbox the maintainer declares (`PENNYLANE_SANDBOX_COMPANY_ID`): the adapter
  checks, before its first read, that the token's company is that one, and refuses
  otherwise -- as the Stripe adapter refuses a live key. Reading a real company is a later
  decision, with its data terms. The alternative: any company the token reaches.
- Q: [NEEDS CLARIFICATION: Q3] The dispute's invoice identifier? The support tool knows an
  invoice's number, the port Pennylane's integer id. Recommended: on Pennylane,
  `dispute.invoice_id` may be either; a number is resolved within the customer's loaded
  invoices like a cited reference, and one that resolves to nothing is an unknown
  reference (`UNKNOWN_INVOICE`), as today. The alternative: the integer id only, the
  intake resolving numbers itself.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A dispute is investigated on Pennylane data (Priority: P1)

An operator points ResolveBilling at the Pennylane sandbox. A dispute from a known customer
on a supported situation is investigated on that data as on the fake: the same findings,
the same decision, an amount including tax. A situation the domain does not represent, or
whose meaning is not established, is refused with its reason, and nothing is decided.

**Why this priority**: It is the slice.

**Independent Test**: Mapping and adapter tests without network; then the contract on the
seeded sandbox, against the fake counterparts.

**Acceptance Scenarios**:

1. **Given** the seeded taxed duplicate (two invoices of one subscription, same imputation
   period, 100.00 + 20 % each), **When** the second is disputed, **Then** the findings,
   classification, proposal (120.00 EUR with its breakdown), verdict and approval equal
   those of `taxed_duplicate_fr` on the fake.
2. **Given** an invoice with a credit note against it, **When** it is read, **Then** it is
   refused with `invoice_credit`.
3. **Given** a customer with two addresses, **When** a dispute names one of them, **Then**
   it is refused with `multiple_addresses`.
4. **Given** an invoice whose only payment is `charged_back`, **When** it is read, **Then**
   it is refused with `payment_status`.
5. **Given** another customer's invoice number cited in a message, **When** it is
   investigated, **Then** only whose the invoice is is read, nothing of its content.

### User Story 2 - Invoice numbers are resolved and masked (Priority: P1)

A customer cites "F20260042". The number is resolved to the invoice among the customer's
loaded invoices: the cited-reference check (taxonomy S6) compares it with the disputed
invoice, and the masking (D10) replaces it before the model sees the message.

**Why this priority**: Without it, two protections Stripe has would be lost on Pennylane.

**Independent Test**: Unit tests of the finder and of the masking with Pennylane invoices;
the fake and Stripe unchanged.

**Acceptance Scenarios**:

1. **Given** a message citing the number of another of the customer's loaded invoices,
   **When** investigated, **Then** the cited-reference conflict is found as on Stripe.
2. **Given** a message citing a loaded invoice's number, **When** sent to an engine,
   **Then** the number is replaced by its alias.
3. **Given** a number outside the loaded invoices, **When** investigated, **Then** it is not
   resolved, not masked, and the trace says a number was left unresolved -- the number
   itself not written there.

### User Story 3 - The contract is held on a seeded sandbox (Priority: P1)

A seed script builds the worlds on the sandbox with its own token, after checking that the
token's company is the declared sandbox; the contract tests run on them with the read-only
token. The maintainer runs both.

**Why this priority**: A platform's contract is shown on the platform (spec 001).

**Independent Test**: The seed's logic against a stub, without network; the contract run
is the maintainer's.

**Acceptance Scenarios**:

1. **Given** a seed token whose company is not the declared sandbox, **When** the seed
   starts, **Then** it writes nothing and says why.
2. **Given** a write whose answer was lost, **When** the seed runs again, **Then** it looks
   for what it made before writing, and stops, saying so, where it cannot tell.

### Edge Cases

- An invoice created from ledger entries and without usable lines: refused
  (`missing_field`), never an empty invoice.
- A refused invoice inside the customer's 180-day window refuses the read (an omitted
  invoice could hide a duplicate); outside it, it is not mapped.
- A quote, proforma or order in the invoice list: not an invoice, left out; named as the
  disputed invoice, refused (`invoice_status`).
- An invoice of a cancelled subscription whose lines carry no period: refused
  (`missing_field`), so the after-cancellation check never falls back on the issue instant.
- The same payment id under two invoices: refused (`multi_invoice_payment`).
- A 429 within the read's time budget: waited out as `retry-after` asks; beyond the budget,
  `ProviderUnavailable`.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `BILLING_PROVIDER=pennylane` MUST select a Pennylane adapter of the
  `BillingProvider` port, reading the Company API v2 with `PENNYLANE_API_TOKEN`, read-only
  (`can_write` false, every write refused before any call as on Stripe without writes).
  Pennylane's vocabulary stays in its mapper (ADR-001). [Q2: the company check.]
- **FR-002**: Customers: found by address -- the given address trimmed and lower-cased,
  compared with every address of every customer, in id order, read to the end; a customer
  with several addresses refused (`multiple_addresses`), one without an address
  `missing_field` where it must be mapped, no currency mapped to none; `list_customers`
  as the port asks.
- **FR-003**: Invoices: read with every line (paginated to the end) and mapped in the
  invoice's currency (`currency_*` fields, never the euro equivalents); issue instant by
  the date convention; statuses `paid` → paid, `upcoming` and `late` → open, `draft` →
  draft, `cancelled` with no credit note → void; `partially_paid`, `partially_cancelled`,
  `archived`, `incomplete` and any unlisted status refused (`invoice_status`, a new
  reason); quotes, proformas and orders left out of lists and refused when named; credit
  notes told by `credited_invoice` (never by status) and never mapped as invoices; an
  invoice with any credit note against it refused (`invoice_credit`); invoices created from
  ledger entries included; the window's `since` applied after the date conversion.
- **FR-004**: Lines: period by the line-period convention, none without
  `imputation_dates`; tax through spec 013 -- `vat_rate` by an explicit table
  (`FR_200` → 20, `FR_55` → 5.5...), `exempt` and the other no-tax codes read as no tax
  charged with the code as the reason, `mixed` and any unlisted code refused (`tax_code`, a
  new reason), posted figures reconciled by `app/providers/tax.py`; a non-zero discount
  (`invoice_discount`), a negative line (`negative_line`), a unit price finer than the
  currency's minor unit (`fractional_unit_price`) refused.
- **FR-005**: Payments: the payments of the invoices the investigation loads, statuses by
  the convention table (each entry recorded with its source: "convention, 2026-11-11", to
  become "confirmed by Pennylane, <date>" or be changed), unlisted statuses refused
  (`payment_status`, a new reason); amount and currency kept; `received_at` none; a payment
  id under several invoices refused (`multi_invoice_payment`); matched bank transactions
  never read as payments.
- **FR-006**: Subscriptions: `in_progress` → active; `stopped` and `finished` → cancelled,
  ended by the end-of-service convention; any other status refused
  (`subscription_status`); `plan_code` the subscription's `label`, or its id when it has
  none; current period, cadence and anchor: [Q1]. An invoice of a cancelled subscription
  whose lines carry no period refused (`missing_field`).
- **FR-007**: `invoice_owner` MUST read the invoice alone and extract its customer, nothing
  else, no lines; `NotFound` for gets and `None` for finds on a 404; `missing_field` for an
  existing object missing a required field; 401/403 a configuration error by category;
  429 and 5xx retried within the read's budget, then `ProviderUnavailable`.
- **FR-008**: The adapter MUST pace its requests per token, process-wide, under 25 per 5
  seconds, honouring `retry-after` within the read's time budget. Several processes on one
  token are not coordinated; the contract says so.
- **FR-009**: The domain `Invoice` MAY carry its `number`; it is serialised only when set,
  so the fake's and Stripe's invoices dump, fingerprint and reach the engine exactly as
  before. The engine payload does not carry it.
- **FR-010**: The reference hooks (`invoice_references`, `invoice_reference_spans`) MUST
  receive the customer's loaded invoices; Pennylane's find the numbers of those invoices in
  the text, normalised as the platform writes them, and answer their ids; a number not
  among them is not resolved, and the investigation's trace records that one was left
  unresolved, without the number. The fake and Stripe ignore the invoices given and answer
  as today. [Q3: the dispute's invoice identifier.]
- **FR-011**: A seed script (`scripts/pennylane_seed.py`) MUST build the worlds with its
  own token (`PENNYLANE_SEED_TOKEN`, never loaded by the adapter), after checking, failing
  closed, that the token's company is `PENNYLANE_SANDBOX_COMPANY_ID`; recovery after a lost
  answer defined per operation (customers and invoices by an `external_reference` the seed
  sets and lists; subscriptions by listing the seed customer's, stopping and reporting when
  it cannot tell); subscriptions created with no email sending and an offline payment
  method, stopped once their invoices exist. Worlds: the taxed duplicate and the taxed
  invoice after cancellation (fake counterparts from spec 013), and each refused shape the
  API lets the seed build. A world the API does not let it build is reported, and its
  exclusion goes to the maintainer (spec 001's precedent).
- **FR-012**: The contract tests (`tests/contract/`) MUST run on Pennylane when its token
  and sandbox id are present, and skip with the reason otherwise; CI runs the fake only.
- **FR-013**: The billing-platform contract, ADR-001 (a dated amendment), the README and
  `docs/operations.md` MUST say what is built, the conventions with their provenance, and
  how to run the seed and the contract.
- **FR-014**: CI MUST make no network call; the development set, the mock score and the
  baseline are unchanged; spec 013's snapshot of untaxed bytes still holds.

### Key Entities

- **A convention**: a mapping the maintainer approved where the documentation is silent,
  recorded with its date and effect, and replaced when Pennylane answers.
- **A world**: a seeded sandbox state reproducing one dispute or one refused shape.
- **The declared sandbox**: the one Pennylane company the adapter and the seed may reach.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Mapping and adapter tests cover every row of the status, payment and VAT
  tables, every new reason, the conventions' dates, pagination, pacing and errors.
- **SC-002**: On the seeded sandbox, the taxed duplicate and the taxed invoice after
  cancellation give the findings, classification, proposal, verdict and approval of their
  fake counterparts; each seeded refused shape is refused with its reason; whose an invoice
  is is answered without its content.
- **SC-003**: The fake's and Stripe's behaviour is unchanged: the mock baseline matched
  case by case, the snapshot of untaxed bytes held, the Stripe tests green.
- **SC-004**: A message citing a loaded invoice's number is masked before any engine and
  checked against the disputed invoice.

## Assumptions

- Pennylane's answer on payments and `cancelled` may change the conventions after this
  slice; the change is a table row, its test and the contract's sentence.
- Writes on Pennylane are spec 015.
- The contract run, read-only key and seed key, are the maintainer's; no agent sees either
  token.
