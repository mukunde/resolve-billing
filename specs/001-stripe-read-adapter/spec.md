# Feature Specification: Stripe as a billing platform, read-only

**Feature Branch**: `001-stripe-read-adapter`

**Created**: 2026-10-22 · **Revised**: 2026-10-22, after clarification

**Status**: Draft -- for the maintainer's approval

**Input**: Maintainer decisions of 2026-10-22: the first real billing
platform, Stripe in test mode, read-only first; test data created by a
script; contract tests run locally; the official SDK as an optional
dependency; a new capability contract, "Billing platform"; and the eight
clarifications recorded below.

**Behaviour changed**: capability *Billing platform*
([contract](../../docs/contract/billing-platform.md)) -- a second
implementation of the port, selectable by configuration, and three additions
to the port (an unknown-identifier error, an unsupported-data error, a
declaration that a platform cannot write). Rules, engines and policies do not
change; the execution service gains one refusal.

**Sensitive change**: yes. It touches what identifies a sender (trust
boundaries, VII) and the evidence financial proposals rest on (II). The pull
request needs the maintainer's explicit approval of the final diff.

## Clarifications

### Session 2026-10-22

- Q: How does a read-only slice handle writes? → A: The platform declares it
  cannot write. The execution service refuses before claiming the action;
  nothing reaches Stripe, and no reconciliation is attempted. `ProviderRejected`
  keeps its meaning (the platform said no); `find_action` is never asked to
  invent an absence. The port and ADR-008 say so.
- Q: A subscription whose items have different periods? → A: Unsupported:
  refused visibly, no period invented, no decision.
- Q: Payments? → A: A conservative mapping: succeeded only when the underlying
  collection succeeded; refunded only when fully refunded; anything the domain
  cannot represent, partial refunds included, refused visibly.
- Q: Usage? → A: One metered item per meter on the subscription; the meter's
  summary over the item's current period; no summary means unavailable, never
  zero. An older invoice's line then simply does not match the period, and the
  comparison does not conclude (D14).
- Q: Invoice references in a message? → A: Stripe identifiers (`in_…`) only in
  this slice. Invoice numbers come with a later slice and a port change,
  resolved within the disputing customer's invoices.
- Q: Keys? → A: Two: a restricted read-only key for the adapter, the secret
  test key for the seed script only. Both refuse a live key.
- Q: The address lookup? → A: A full, paginated scan with normalised addresses,
  in a fixed order; Stripe's exact, case-sensitive filter and its eventually
  consistent search are not used to prove an absence.
- Q: Level of review? → A: Sensitive change: final-diff approval.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Investigate a dispute against Stripe data (Priority: P1)

An operator points ResolveBilling at a Stripe test account. A dispute on a
supported situation is investigated on that account's data exactly as on the
fake platform: the same rules fire, the same decision follows. A situation
the adapter cannot represent is not investigated, and says so.

**Why this priority**: the first time the system meets a real platform's data
instead of fixtures written for it.

**Independent Test**: seed a world reproducing a duplicate invoice, investigate
it with the mock engine at the seed's date, compare with the equivalent fake
world.

**Acceptance Scenarios**:

1. **Given** a seeded customer billed twice for the same period, **When** that
   customer disputes the second invoice, **Then** the investigation finds
   `DUPLICATE_INVOICE` and proposes a credit note for human approval, as for
   the fake world it reproduces.
2. **Given** an address no Stripe customer holds, **When** it sends a dispute,
   **Then** the sender is unknown; the only further read is the customer index
   used by the impersonation check (ADR-007).
3. **Given** Stripe cannot be reached, **When** a dispute is investigated,
   **Then** nothing is decided and the API answers 503 (S7).
4. **Given** a subscription with items on different periods, or a partly
   refunded payment, **When** a dispute touches it, **Then** nothing is
   decided and the answer says the platform's data is not supported.
5. **Given** a proposed credit note on Stripe data, **When** a person approves
   it, **Then** execution is refused before any claim: the platform cannot
   write, and nothing is sent.

---

### User Story 2 - Seed a Stripe test account (Priority: P2)

The maintainer runs one script against a Stripe test account and gets a small
set of worlds reproducing development situations -- duplicate, proration,
billed after cancellation, failed payment, metered usage -- with test clocks so
periods and dates match a fixed investigation date.

**Independent Test**: run it, interrupt it, run it again: it completes the
missing worlds, recreates nothing, and says what it did.

**Acceptance Scenarios**:

1. **Given** an empty test account and the secret test key, **When** the script
   runs, **Then** each world exists, tagged in metadata, and the script prints
   what it created and the date to investigate at.
2. **Given** some worlds exist, possibly half-created, **When** it runs again,
   **Then** it completes what is missing and creates nothing twice.
3. **Given** a live key, **When** the script starts, **Then** it refuses before
   any call.

---

### User Story 3 - Prove the adapter keeps the contract (Priority: P3)

The same contract tests run against the fake and against the seeded Stripe
account, on the closed list of properties below.

**Acceptance Scenarios**:

1. **Given** the seeded account and the read-only key, **When** the contract
   tests run locally, **Then** every property holds for both platforms, except
   the approved differences listed in the contract.
2. **Given** no key, **When** the suite runs (as in CI), **Then** the Stripe
   tests are skipped, and the mapping tests run without network.

### Edge Cases

- Items on different periods; a scheduled cancellation at period end; an
  `incomplete`, `incomplete_expired` or `unpaid` subscription.
- An invoice with tax, a discount, a negative line or a fractional unit price.
- An invoice paid in several payments; a partly refunded payment.
- A meter shared by two items; a meter still aggregating.
- An address differing only by case or spaces; two customers with one address.
- More results than one page, on every list, nested ones included.
- Rate limiting, a timeout, an outage; a revoked, read-write or live key.

## Requirements *(mandatory)*

### Functional Requirements

**The port** (ADR-001 and ADR-008 amended)

- **FR-001**: The port MUST name `NotFound` for an unknown identifier on
  `get_customer`, `get_subscription`, `get_invoice`; the fake raises it too.
- **FR-002**: The port MUST name `ProviderUnsupported`: the platform answered,
  but with data the domain cannot represent. The investigation decides nothing
  and the caller is told the data is not supported -- distinct from
  `ProviderUnavailable` (try later) and from a configuration error.
- **FR-003**: A platform MUST declare whether it can write. The execution
  service MUST refuse, before claiming the action, to execute on a platform
  that cannot; nothing is sent and no reconciliation is attempted.

**Reading** (selected by configuration; the fake stays the default)

- **FR-010**: Customers: identifier, address, currency (upper-case), name.
- **FR-011**: The address lookup MUST scan every customer page by page in a
  fixed order (creation, then identifier), comparing trimmed, lower-cased
  addresses, and return the first match; it MUST NOT rely on Stripe's exact
  filter or its search.
- **FR-012**: Subscriptions: the period from the items when they all share one;
  otherwise `ProviderUnsupported`. Status: active, trialing, past_due, paused
  map to themselves; canceled maps to cancelled with the date it **ended**; a
  cancellation scheduled for later is still active with no cancellation date;
  incomplete, incomplete_expired and unpaid are `ProviderUnsupported`. The
  plan is the price's lookup key, else its identifier.
- **FR-013**: Invoices: identifier `in_…`; status draft, open, paid, void,
  uncollectible map to themselves; issued when finalised (a draft: when
  created); total and lines in integer minor units with their periods.
  Invoices with tax, discounts, negative lines or a fractional unit price are
  `ProviderUnsupported` in this slice.
- **FR-014**: `list_invoices(customer, since)` returns invoices issued at or
  after `since`; `find_invoice` finds one whatever its age.
- **FR-015**: Payments: one per invoice payment; amount collected; succeeded
  only when the underlying collection succeeded, failed when it failed,
  pending while it is in progress, refunded only when fully refunded; a
  partial refund or a payment the domain cannot place is `ProviderUnsupported`.
- **FR-016**: Usage: for a subscription with exactly one metered item whose
  meter no other item of the customer uses, the meter's summary over that
  item's current period; no summary, or a shared meter, is `None`
  (unavailable), never zero.
- **FR-017**: References: `invoice_references` and `invoice_reference_spans`
  recognise Stripe invoice identifiers (`in_…`) only.
- **FR-018**: Every list MUST be read to its end, nested lists included, within
  a time budget; a list cut short is `ProviderUnavailable`, never a partial
  answer.

**Failure and safety**

- **FR-020**: Connection errors, timeouts, rate limiting and 5xx MUST become
  `ProviderUnavailable` once the SDK's bounded retries are spent; an
  authentication or permission error MUST fail visibly as a configuration
  error, with a category, never the provider's text (as for engines).
- **FR-021**: The adapter MUST be given a restricted read-only key and refuse a
  live key or a key that can write, before any call; the seed script MUST use
  the secret test key and refuse a live one.
- **FR-022**: The Stripe API version MUST be pinned in the adapter.
- **FR-023**: Stripe is named only in the adapter package, the wiring
  (`deps`), the seed script and Stripe-specific tests; the domain, the
  application and the policies do not change. The SDK is an optional extra.
- **FR-024**: Keys come from the environment and are never logged, printed,
  traced or written to the audit.

**Tests**

- **FR-030**: Mapping tests run in CI without network and without the SDK, on
  hand-written Stripe payloads covering every mapping and every
  `ProviderUnsupported` case above.
- **FR-031**: Contract tests run the same assertions against the fake and,
  locally with the read-only key, against the seeded account, at the seed's
  investigation date.

### Contract properties checked (closed list)

1. An unknown identifier raises `NotFound`; `find_*` answers `None`.
2. A read that cannot complete raises `ProviderUnavailable`.
3. The address lookup ignores case and surrounding spaces; first match in a
   fixed order.
4. `list_invoices` honours `since`, inclusive; `find_invoice` ignores it.
5. A subscription has one period, a status from the closed set, and a
   cancellation date only once ended.
6. Amounts are integer minor units in the invoice's currency.
7. Usage is per subscription and covers one period, or is `None`.
8. Payments carry their invoice, amount and a state from the closed set.
9. Data the domain cannot represent raises `ProviderUnsupported`.
10. A platform that cannot write says so; nothing is ever written.

### Key Entities

- **Test world**: a customer and everything billed to it, reproducing one
  development situation, tagged in Stripe metadata.
- **Investigation date**: the seed's test-clock date, at which contract tests
  and User Story 1 investigate.

## Success Criteria *(mandatory)*

- **SC-001**: For each seeded world, an investigation with the mock engine on
  Stripe gives the same findings, classification and proposed action as on
  the fake world it reproduces.
- **SC-002**: The ten properties hold for both platforms, or the difference is
  listed in the contract and approved.
- **SC-003**: The free CI job runs with no key and no network, the mapping
  tests included, and the mock baseline matched.
- **SC-004**: The account holds only what the seed script created: no write
  from the adapter ever reaches it.

## Assumptions

- The maintainer creates the test account, the restricted read-only key and
  the secret test key; no agent sees a key.
- The 2025 API generation is targeted: periods on subscription items, invoice
  payments, billing meters (legacy usage records are gone).
- Evaluation cases stay on the fake; running the evaluation set on Stripe is
  out of scope.
- Out of scope, each for its own slice: writes and the caps ADR-008 asks to
  verify; invoice numbers in messages; tax, discounts and fractional prices;
  partial refunds; a cached customer index for performance.
