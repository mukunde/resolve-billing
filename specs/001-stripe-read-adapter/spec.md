# Feature Specification: Stripe as a billing platform, read-only

**Feature Branch**: `001-stripe-read-adapter`

**Created**: 2026-10-22

**Status**: Draft

**Input**: Maintainer decision of 2026-10-22: the first real billing
platform, Stripe in test mode, read-only first; test data created by a
script; contract tests run locally; the official SDK as an optional
dependency; a new capability contract, "Billing platform".

**Behaviour changed**: capability *Billing platform*
([contract](../../docs/contract/billing-platform.md)) -- a second
implementation of the port, selectable by configuration. Investigation,
rules, engines and policies do not change.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Investigate a dispute against Stripe data (Priority: P1)

An operator points ResolveBilling at a Stripe account in test mode. A dispute
is investigated on that account's customers, subscriptions, invoices,
payments and usage, exactly as it is on the fake platform: the same rules
fire on the same situations, and the same decision follows.

**Why this priority**: it is the first time the system meets a real platform's
data instead of fixtures written for it. Every later integration -- writes,
Pennylane, a support tool -- assumes this works.

**Independent Test**: seed a Stripe test account with a known situation (a
duplicated invoice), investigate a dispute on it with the mock engine, and
get the same findings and decision as for the equivalent fake world.

**Acceptance Scenarios**:

1. **Given** a Stripe test account holding a customer billed twice for the
   same period, **When** that customer disputes the second invoice, **Then**
   the investigation finds `DUPLICATE_INVOICE` and proposes a credit note for
   human approval -- as for the fake world it was modelled on.
2. **Given** an address no Stripe customer holds, **When** it sends a dispute,
   **Then** the sender is unknown and nothing about any account is read
   further.
3. **Given** Stripe cannot be reached, **When** a dispute is investigated,
   **Then** nothing is decided and the API answers 503 (S7).

---

### User Story 2 - Seed a Stripe test account with known situations (Priority: P2)

The maintainer runs one script against an empty Stripe test account and gets
a small set of worlds that reproduce situations of the development set --
duplicate, proration, billed after cancellation, failed payment, metered
usage -- with backdated subscriptions so the periods match.

**Why this priority**: without known data, User Story 1 cannot be shown, and
the contract tests have nothing to compare.

**Independent Test**: run the script twice on the same account: the second
run changes nothing and says so.

**Acceptance Scenarios**:

1. **Given** an empty test account and a test-mode key, **When** the script
   runs, **Then** each world exists and the script prints what it created.
2. **Given** the worlds already exist, **When** it runs again, **Then** it
   creates nothing.
3. **Given** a live-mode key, **When** the script starts, **Then** it refuses
   before any call.

---

### User Story 3 - Prove the adapter keeps the contract (Priority: P3)

One set of contract tests runs against the fake and against Stripe and checks
the same properties: not-found against could-not-read, the address lookup,
the invoice window, usage, payments, references.

**Why this priority**: tests on the fake prove a contract, not an adapter
(README, "Limits"); this is the first test that proves an adapter.

**Independent Test**: run the contract tests locally with a test key; without
one they are skipped and say why.

**Acceptance Scenarios**:

1. **Given** the seeded account, **When** the contract tests run against
   Stripe, **Then** every property that holds for the fake holds for Stripe,
   or the difference is named in the contract.
2. **Given** no test key, **When** the test suite runs (as in CI), **Then**
   the Stripe tests are skipped, and every other test runs.

### Edge Cases

- A subscription whose items have different billing periods (Stripe moved
  periods from the subscription to its items in 2025): the domain has one
  period per subscription.
- A customer address that differs from the dispute's only by case or spaces
  (Stripe's address filter is exact).
- An invoice paid in several partial payments (Stripe's invoice payments).
- A refunded or partly refunded payment.
- Usage recorded on a meter whose events are still being aggregated.
- An invoice referenced by its number ("ACME-0003") rather than its
  identifier.
- Rate limiting, a timeout, an outage; a revoked or live key.

## Requirements *(mandatory)*

### Functional Requirements

**Reading**

- **FR-001**: The system MUST offer Stripe as a billing platform behind the
  existing port, selected by configuration; the fake stays the default.
- **FR-002**: The adapter MUST map Stripe customers, subscriptions, invoices
  and their lines, invoice payments and metered usage onto the domain
  objects, with amounts as integer minor units in upper-case ISO currency
  codes, and statuses onto the domain's closed sets.
- **FR-003**: A subscription's period MUST come from its items; when items
  disagree, the subscription MUST NOT be given a period it does not have
  [NEEDS CLARIFICATION: report it so the checks say `NOT_EVALUABLE` (D14),
  or reject the subscription as unreadable?].
- **FR-004**: The address lookup MUST keep the contract -- case and spaces do
  not matter, the first match wins -- whatever Stripe's own filter does.
- **FR-005**: `list_invoices` MUST return the invoices issued at or after
  `since`; `find_invoice` MUST find one whatever its age.
- **FR-006**: `get_usage` MUST return, for a metered subscription item, the
  quantity its meter aggregated over the item's current period, or `None`.
- **FR-007**: `list_payments` MUST return one payment per invoice payment,
  with its amount and a state among pending, succeeded, failed, refunded.
- **FR-008**: The adapter MUST recognise the invoice references Stripe uses in
  a message -- its identifiers and its invoice numbers -- and map a number to
  its invoice (S6, D10).
- **FR-009**: The port MUST name the error for an unknown identifier, so that
  every platform raises the same one (the gap the contract records).

**Failure and safety**

- **FR-010**: A read Stripe cannot complete (connection, timeout, rate limit,
  5xx) MUST raise `ProviderUnavailable` once the SDK's own retries are spent;
  an authentication or permission refusal MUST fail visibly as a
  configuration error, never as an empty answer.
- **FR-011**: In this slice the adapter MUST refuse every write -- credit
  note, refund -- with `ProviderRejected` before calling Stripe, saying writes
  are not enabled; `find_action` answers `None`. Nothing is ever created on
  the platform.
- **FR-012**: The adapter, the seed script and the contract tests MUST refuse
  a live-mode key before any call.
- **FR-013**: The Stripe API version MUST be pinned in the adapter, so a
  change on Stripe's side cannot silently change a mapping.
- **FR-014**: Nothing outside the adapter MUST name Stripe or import its SDK
  (ADR-001); the SDK is an optional dependency.
- **FR-015**: The key MUST come from the environment and never be logged,
  printed, traced or written to the audit.

**Data and tests**

- **FR-016**: A seed script MUST create the test worlds idempotently and
  refuse a live key (User Story 2).
- **FR-017**: Contract tests MUST run the same assertions against the fake and
  against Stripe, the Stripe run being skipped without a test key (User
  Story 3).

### Key Entities

- **Stripe test account**: the maintainer's own; holds only seeded data.
- **Test world**: a customer and everything billed to it, reproducing one
  development situation; identified by metadata the seed script sets.
- **Contract test**: a property of the port, checked against every platform.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For each seeded world, an investigation with the mock engine on
  Stripe gives the same findings, classification and proposed action as on
  the fake world it reproduces.
- **SC-002**: Every contract property holds for both platforms, or its
  difference is written in the contract.
- **SC-003**: The free CI job runs unchanged: no key, no network, the same
  tests, the mock baseline matched.
- **SC-004**: No write reaches Stripe: the account holds only what the seed
  script created.

## Assumptions

- Stripe test mode is free and the maintainer creates the test account and
  its key himself; the key is never shown to an agent.
- The current API generation (2025 onwards) is the one targeted: periods on
  subscription items, invoice payments, billing meters for usage (legacy usage
  records are gone).
- Evaluation cases stay on the fake: their identifiers are the fake's. Running
  the evaluation set on Stripe is out of scope.
- Writes (credit notes, refunds, `find_action`, the caps ADR-008 asks to
  verify) are the next slice, with their own spec: they touch the
  money-moving core.
- Performance is not a goal here: `list_customers` may page the account; a
  cached index comes with a real deployment.
