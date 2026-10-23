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
implementation of the port, selectable by configuration, and four additions
to the port (an unknown-identifier error, an unsupported-data error, a
declaration that a platform cannot write, and whose an invoice is). Engines
and policies do not change; one rule changes what it takes -- the
impersonation check receives whose the cited invoice is, not the invoice;
the execution service gains one refusal.

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
- Q: (2026-10-22, at plan approval) Usage is billed in arrears on Stripe: the
  item's current period is never the invoiced one. → A: Option (a): the port
  reads usage for a period, `get_usage(subscription_id, period)`, the period
  shared by the target invoice's metered lines; distinct or unreal periods
  ask for nothing. The fake answers only a record covering that period;
  findings and engine context are compared on every development case before
  and after, and any change is submitted to the maintainer. Amends FR-016
  and ADR-001.
- Q: (2026-10-22, at implementation review r118) Three decisions of the
  maintainer. → A: (1) An invoice that may be another customer's is read for
  its owner only: the port gains `invoice_owner` (FR-004), and the
  impersonation check takes the owner, so nothing of another account's
  invoice -- not even a refusal on its content -- can surface. (2) A pre-tax
  credit that is not a discount (a credit balance applied) is refused with a
  new reason, `invoice_credit` (FR-013). (3) Two departures from the plan's
  null handling are approved: a customer's currency Stripe holds none of maps
  to none; a customer without an address is never a match in the lookup and
  is `missing_field` when it must be mapped (FR-010).
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
  but with data the domain cannot represent. It is distinct from
  `ProviderUnavailable` (try later) and from a configuration error. The
  investigation lets it through: no decision, no proposal, nothing queued for
  approval, no run row (the constitution's VI asks one of completed
  investigations, and none is invented). The API answers **422** with the
  stable category `provider_unsupported` and a short reason category (for
  instance `mixed_item_periods`, `partial_refund`), never the platform's data;
  the failure is logged by category; nothing retries it.
- **FR-003**: A platform MUST declare whether it can write. The execution
  service MUST refuse, before claiming the action, to execute on a platform
  that cannot; nothing is sent and no reconciliation is attempted.
- **FR-004** *(added 2026-10-22, review r118)*: The port MUST name
  `invoice_owner(invoice_id)`: the customer an invoice belongs to, or `None`
  when there is no such invoice. Only the owner is extracted: nothing of the
  invoice's content is mapped and nothing further is read, so what it bills
  never makes it refuse; an owner the platform cannot name is
  `ProviderUnsupported("missing_field")`, never an absence -- the owner's
  identity is the one thing it needs. The investigation uses it wherever only
  ownership matters -- a reference outside the customer's window, a reference
  cited by an unknown sender -- and the impersonation check takes the owner,
  never the invoice.

**Reading** (selected by configuration; the fake stays the default)

- **FR-010** *(amended 2026-10-22, review r118)*: Customers: identifier,
  address, currency (upper-case), name. A currency Stripe holds none of (a
  customer never billed) maps to none. A customer without an address cannot
  be represented: the lookup never matches it, and mapping it -- listing
  every customer for the impersonation check -- is `ProviderUnsupported`.
- **FR-011**: The address lookup MUST scan every customer page by page --
  including customers attached to test clocks, which Stripe's list leaves out
  unless asked, each customer counted once -- in a fixed order (creation, then
  identifier), comparing trimmed, lower-cased addresses, and return the first
  match; it MUST NOT rely on Stripe's exact filter or its search. Every list
  the adapter reads includes test-clock objects the same way.
- **FR-012**: Subscriptions: supported with exactly one recurring price item,
  or several items sharing one period and one price; otherwise
  `ProviderUnsupported`. The period comes from the items. Status: active,
  trialing, past_due, paused map to themselves; canceled maps to cancelled
  with the date it **ended** (`ended_at`, never the date the cancellation was
  requested), and a canceled subscription without an end date is
  `ProviderUnsupported`; a cancellation scheduled for later keeps the current
  status and no cancellation date; incomplete, incomplete_expired and unpaid
  are `ProviderUnsupported`. The plan is the price's lookup key, else its
  identifier. *(Added 2026-10-23, option B+)* The subscription also carries
  its cadence -- the price's interval and interval count -- and its original
  billing anchor, each `None` when Stripe does not give it, never guessed.
- **FR-013**: Invoices: identifier `in_…`; status draft, open, paid, void,
  uncollectible map to themselves; issued when finalised (a draft: when
  created); total and lines in integer minor units with their periods.
  An invoice carrying tax (exclusive or inclusive), a discount (on a line or
  on the invoice, pre-tax ones included), a pre-tax credit that is not a
  discount (`invoice_credit`, added 2026-10-22), a negative line or a
  fractional unit price is `ProviderUnsupported` in this slice; each marker
  is detected from Stripe's structured fields and tested.
- **FR-014**: `list_invoices(customer, since)` returns invoices issued at or
  after `since`; `find_invoice` finds one whatever its age.
- **FR-015**: Payments: one per invoice payment attached to one invoice. A
  succeeded payment carries the amount actually collected (`amount_paid`),
  never the amount requested; a pending or failed one carries the amount
  requested, with its state, and is never read as money received. Succeeded
  only when the underlying collection succeeded, failed when it failed,
  pending while it is in progress, refunded only when fully refunded. A
  partial refund, a payment allocated across invoices, or a payment the
  domain cannot place is `ProviderUnsupported`.
- **FR-016** *(amended 2026-10-22, option (a))*: The port reads usage for a
  period: `get_usage(subscription_id, period)`. The investigation asks for the
  period all the target invoice's metered lines share, when it is real;
  otherwise it asks for nothing and the lines are unchecked. On Stripe: for a
  subscription with exactly one metered item whose meter no other item of the
  customer uses, the meter's summary over **that period**; no summary, or a
  shared meter, is `None` (unavailable), never zero; a summary that exists and
  says zero is zero. The fake answers its record only when it covers that
  period exactly.
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
- **FR-021**: The adapter MUST be given a restricted test key (`rk_test_…`) and
  refuse, before any call, a live key or a standard secret key. A key's
  permissions cannot be read from the key itself: the maintainer creates it
  with read permissions only, and the adapter contains no call to a mutating
  endpoint, whatever the key allows. No write is ever attempted to probe
  permissions. The seed script MUST use the secret test key (`sk_test_…`) and
  refuse a live one.
- **FR-022**: The Stripe API version MUST be pinned in the adapter.
- **FR-023**: Stripe is named only in the adapter package, the wiring
  (`deps`), the seed script and Stripe-specific tests. The domain, the
  application and the policies stay independent of Stripe; the changes they
  get are the four port additions (FR-001 to FR-004), the impersonation
  check taking the owner, the investigation and API handling of
  `ProviderUnsupported`, and the execution service's refusal.
  The SDK is an optional extra.
- **FR-024**: Keys come from the environment and are never logged, printed,
  traced or written to the audit.

**Tests**

- **FR-030**: Mapping tests run in CI without network and without the SDK, on
  hand-written Stripe payloads covering every mapping and every
  `ProviderUnsupported` case above.
- **FR-031**: Contract tests run the same assertions against the fake and,
  locally with the read-only key, against the seeded account, at the seed's
  investigation date. They start only once every test clock is ready, every
  seeded invoice is finalised, and every expected meter summary is available,
  within a bounded wait; fixing the investigation's date does not move
  Stripe's clocks.
- **FR-032**: The seed script creates only worlds inside this slice's scope
  (no tax, discount, negative line, fractional price, partial refund, mixed
  periods), plus one world per `ProviderUnsupported` case for the contract
  tests; it waits, within a bound, for clocks to be ready and usage to be
  aggregated, and never replaces a delay by a zero.

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
