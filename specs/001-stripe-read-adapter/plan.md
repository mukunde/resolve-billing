# Implementation Plan: Stripe as a billing platform, read-only

**Branch**: `001-stripe-read-adapter` | **Date**: 2026-10-22 | **Spec**: [spec.md](spec.md), approved by the maintainer at commit `8046270`

**Input**: Feature specification from `specs/001-stripe-read-adapter/spec.md`

## Summary

A second implementation of the `BillingProvider` port, reading a Stripe test
account, selected by configuration. The port gains what every platform needs
(`NotFound`, `ProviderUnsupported`, `can_write`, `PlatformCannotWrite`); every
API surface that reads the platform answers unsupported data with a 422 and a
category; the execution service refuses writes on a platform that cannot make
them (409) before reading or claiming anything. Mapping is pure functions on
Stripe payloads, tested in CI without network or SDK; a seed script builds
test worlds on several test clocks; contract tests compare the fake and Stripe
on the spec's ten properties.

## Technical Context

**Language/Version**: Python 3.12 (CI), 3.13 locally.

**Primary Dependencies**: the official `stripe` SDK pinned to **15.4.0**, as a
new optional extra `stripe`, with the **API version it targets,
`2026-07-29.dahlia`**, pinned explicitly in the client (the spec's "2025
generation" is the Basil changes; Dahlia keeps them). Payloads in tests are
written to that version's API reference; the contract run on a real account
is what confirms them. Async calls are the SDK's `*_async` methods, called
with `params` and `options` as the SDK defines them; test stubs reproduce
those signatures.

**Storage**: none new. No run row for an unsupported read (spec FR-002).

**Testing**: pytest. Mapping tests (CI, no SDK, no network); adapter, seed and
port tests with stub clients (CI); contract tests against the seeded account
(local, skipped without a key, written early and run by the maintainer).

**Target Platform**: the existing service; Stripe in test mode only.

**Project Type**: web service (FastAPI) with a domain core.

**Performance Goals**: none (spec, Assumptions). A time budget per adapter
call bounds the worst case.

**Constraints**: read-only; test mode only; no Stripe name outside the
adapter, the wiring, the seed script and Stripe tests; no key and no
platform text in any output.

**Scale/Scope**: a handful of seeded worlds; accounts of tens of customers.

## Constitution Check

| Principle | How this plan meets it |
|---|---|
| I. Deterministic before probabilistic | Untouched: mapping is deterministic, rules unchanged. |
| II. Evidence, then human approval, before money | No write can reach Stripe: no mutating call in the adapter, a read-only key, execution refusing before any read or claim. Unsupported data decides nothing. |
| III. Provider-agnostic boundaries | Stripe behind the port only; the port additions are vendor-neutral. |
| IV. Money in minor units | Stripe amounts are minor units; mapped through `Money`. A currency the domain does not know gets its default two-decimal exponent (ADR-005's table): the seed uses EUR only, and an unknown currency is noted as a limit. |
| V. A model acts within bounds | Untouched. |
| VI. Auditable; the platform is authoritative | Completed investigations record as before; an unsupported read records nothing it did not do. |
| VII. Untrusted input, trust boundaries | The sender lookup changes implementation (full scan, normalised) and keeps its contract; contract property 3. |
| VIII. Measured | The mock baseline must stay matched; the contract tests and SC-001 measure the new platform; no evaluation run on Stripe. |
| Sensitive areas | Trust boundaries (VII) and the evidence behind proposals (II): **final-diff approval** before merge. |

No violation; no complexity to justify.

## Design

### 1. Port additions (`app/providers/base.py`; ADR-001 and ADR-008 amended)

- **`NotFound(BillingDomainError)`** for an unknown identifier on
  `get_customer`, `get_subscription`, `get_invoice`; moved from the fake, which
  imports it (the old import path keeps working).
- **`ProviderUnsupported(Exception)`** with a `reason` from a closed set:
  `mixed_item_periods`, `multiple_prices`, `subscription_status`,
  `cancellation_without_end`, `invoice_tax`, `invoice_discount`,
  `negative_line`, `fractional_unit_price`, `partial_refund`,
  `multi_invoice_payment`, `unplaced_payment`, `unrepresentable_period`,
  `missing_field`. Its message is the category, never the platform's data.
- **`can_write: bool`** on the protocol: the fake `True`, Stripe `False`.
- **`PlatformCannotWrite(Exception)`**: raised by a platform that cannot
  write if a write or `find_action` is called anyway. Unreachable through the
  execution service (it checks `can_write` first); a test pins that it never
  becomes an unknown outcome.

### 2. Every API surface that reads the platform

- One FastAPI exception handler for `ProviderUnsupported`: **422**,
  `{"category": "provider_unsupported", "reason": "<category>"}`, logged by
  category. It covers the investigation, the approval endpoints and the
  review page, whose `proposal_fingerprint` reads the invoice. It is distinct
  from the S11 handler, which formats `RequestValidationError` only; both are
  tested for their shape and for carrying no platform data.
- The investigation logs and traces it by category
  (`error.type = provider_unsupported`) and lets it through: no decision, no
  proposal, no queued action, no run row.
- No state changes on any surface: an approval page that cannot read the
  invoice shows nothing to approve and records nothing.
- The MCP server's read tools turn it into a tool error with the category.

### 3. Execution (`app/application/execute_action.py`)

- `WritesNotSupported(IllegalTransition)` in `app/infrastructure/actions.py`.
- `execute()` raises it **first**, before reconciling, reading the invoice or
  claiming, when `provider.can_write` is false; the action keeps its state.
  `reconcile()` raises it likewise, so `find_action` is never asked.
- As an `IllegalTransition` it reaches the API as **409**, ahead of the
  catch-all that would call it an unknown outcome (504).

### 4. The Stripe adapter (`app/providers/stripe/`)

| Module | Role |
|---|---|
| `keys.py` | Accepts `rk_test_…` only; refuses `rk_live_`, `sk_live_`, `sk_test_`, before any call. |
| `client.py` | The only module importing `stripe`. `StripeClient` with API version `2026-07-29.dahlia`, bounded network retries; async helpers that read every page, nested lists included, within a per-call time budget; error mapping; read methods only. |
| `mapping.py` | Pure functions from Stripe payloads (plain dicts) to domain objects; raise `ProviderUnsupported`. No SDK import. |
| `adapter.py` | `StripeBillingProvider`: the port; `can_write = False`; writes and `find_action` raise `PlatformCannotWrite` without any call. |

**Errors**: connection errors, timeouts, rate limits, 5xx →
`ProviderUnavailable` after bounded retries; `resource_missing` → `NotFound`
(`None` for `find_*`); authentication and permission errors →
`ProviderConfigurationError` (adapter-local, with a category), which fails the
request; a time budget exceeded → `ProviderUnavailable`.

**Nulls**: a field the mapping needs and Stripe returns null
(`customer.email`, `customer.currency`, a price, a period) is
`ProviderUnsupported("missing_field")`, never a blind `.upper()` or `.strip()`;
an optional one (`customer.name`, a description) stays `None`.

**Mappings** (spec FR-010 to FR-017):

- *Customer*: `id`; `email` as written (compared normalised); `currency`
  upper-cased; `name` as display name.
- *Address lookup and `list_customers`*: customers listed without a clock,
  then the test clocks listed and their customers listed per clock (the
  customers list takes a `test_clock` filter and otherwise leaves them out);
  deduplicated by id; sorted by (`created`, `id`); first whose trimmed,
  lower-cased address equals the one sought.
- *Subscription*: items read in full. Supported: one recurring price, or
  several items sharing one price and one period; else `multiple_prices` or
  `mixed_item_periods`. Period from the items' `current_period_start` and
  `current_period_end`. Status table per spec FR-012; `canceled` uses
  `ended_at` (missing → `cancellation_without_end`); a scheduled cancellation
  keeps the status, no date. Plan: the price's `lookup_key`, else its id.
- *Invoice*: `id`; status 1:1; issued at `status_transitions.finalized_at`, a
  draft at `created`; `total` and line `amount` in minor units; line `period`.
  Refused, by category, from the version's structured fields: any non-zero
  entry in the invoice's `total_taxes` or a line's `taxes` (exclusive or
  inclusive) → `invoice_tax`; any non-zero `discount_amounts` on a line or
  `total_discount_amounts` on the invoice → `invoice_discount`; a negative
  line `amount` → `negative_line`; a line price whose
  `pricing.unit_amount_decimal` has a non-zero fractional part (`"1000"` and
  `"1000.000"` are whole) → `fractional_unit_price`.
- *Price and meter*: when a line or an item carries only a price id, the
  price is read (cached for the call); a metered price's meter likewise; the
  metric is the meter's `event_name`, the same on the line and in the usage.
- *Payment*: invoice payments listed per invoice with
  `expand=["data.payment.payment_intent.latest_charge"]`; refunds read from
  that charge (`amount_refunded`, `refunded`), no deeper expansion. Succeeded:
  the charge succeeded, amount = `amount_paid`; refunded: `refunded` true,
  amount = the amount originally collected; failed / pending: from the
  intent's status, amount = `amount_requested`, never counted as received.
  `0 < amount_refunded < amount` → `partial_refund`. A payment intent found on
  invoice payments of more than one invoice of the customer →
  `multi_invoice_payment`. A payment of another type, or without an intent →
  `unplaced_payment`.
- *Usage*: the subscription's metered items. Exactly one, and its meter used
  by no other item of the customer's subscriptions (checked over every item
  read) → the meter's event summaries for `customer` and the meter id, with
  `start_time` (inclusive) and `end_time` (exclusive) set to the item's
  current period; both must fall on whole minutes, else
  `unrepresentable_period`; quantities summed. No summary, or a shared meter
  → `None`; a summary of zero → zero.
- *References*: `in_` followed by at least 14 alphanumeric characters, as a
  span with the identifier itself.

**Windows and directness**: `list_invoices` reads the customer's invoices in
full (test-clock customers included) and keeps those issued at or after
`since`; `find_invoice` reads by id.

### 5. Wiring and configuration

- `BILLING_PROVIDER=fake|stripe` (default `fake`) in `app/api/deps.py`;
  `STRIPE_API_KEY` (restricted, read-only, test) read there and nowhere else.
- `STRIPE_SEED_KEY` (`sk_test_…`) read only by the seed script.
- `.env.example`, `docs/operations.md`: creating the restricted key with read
  permissions only (customers, subscriptions, invoices, invoice payments,
  payment intents, charges, prices, meters, meter event summaries, test
  clocks), and the seed key.
- `pyproject.toml`: extra `stripe = ["stripe==15.4.0"]`.

### 6. Seed script (`scripts/stripe_seed.py`)

- Refuses anything but `sk_test_`. Idempotent: every object carries metadata
  `rb_world=<name>`; a run lists what exists and completes what is missing, a
  half-created world included.
- **One test clock per world** (a clock holds at most three customers; one
  per world also keeps worlds from moving each other). Each world records in
  its clock's name and its customer's metadata the **target invoice** and the
  **investigation instant** -- right after the target invoice is finalised,
  before any renewal can add an invoice the test did not expect.
- Supported worlds, each modelled on a development situation:
  - *duplicate*: a monthly subscription from 2026-09-01; at 2026-09-02 a
    one-off invoice for the same period and amount; the clock advanced past
    finalisation; target = the one-off invoice;
  - *proration*: an invoice item for half of September at the full monthly
    price (positive line only); target = that invoice;
  - *billed after cancellation*: a subscription ended at 2026-08-31; an
    invoice for September issued afterwards; target = it;
  - *failed payment*: a customer with Stripe's declining test card; the
    September invoice's payment fails; target = it;
  - *usage*: a metered price on a meter; the clock frozen inside the period;
    meter events sent with timestamps inside that period and inside Stripe's
    accepted window for meter events relative to the clock's time; the clock
    then advanced past the period's end and the invoice finalised; target =
    that invoice, investigated on that period. The exact window is checked
    against Stripe's documentation when T031 is written; if historical events
    cannot be accepted on a clock, the usage world is dropped from SC-001,
    usage stays proven by the mapping and adapter tests, and the contract
    says so.
- Unsupported worlds: **one per `ProviderUnsupported` reason the seed can
  produce in test mode** (mixed item periods, multiple prices, an
  unsupported status, tax, discount, negative line, fractional unit price,
  partial refund, a payment across invoices where the API allows it); a
  reason the seed cannot produce is covered by mapping tests only, and listed.
- Waits, within a bound, for each clock to be `ready`, the target invoices
  finalised and the meter summaries present; never writes a zero in place of
  a delay.

### 7. Tests

| Level | Where | Runs |
|---|---|---|
| Mapping | `tests/unit/test_stripe_mapping.py`, payloads in `tests/unit/stripe_payloads/` (version `2026-07-29.dahlia`) | CI, no SDK, no network |
| Adapter, stub client | `tests/unit/test_stripe_adapter.py` | CI: key checks, pagination to the end, time budget, error mapping, test-clock customers, nulls, no mutating call (the stub fails on any write method), SDK call signatures |
| Seed, stub client | `tests/unit/test_stripe_seed.py` | CI: key check, idempotence, half-created worlds, one clock per world, bounded waits |
| Port and surfaces | `tests/unit/test_execute_action.py`, `tests/integration/test_unsupported_platform_data.py` | CI: 409 before any read or claim, `find_action` never called; 422 with category on investigation, approval read and review page, no state change, no run row; the S11 422 shape unchanged; MCP tool error |
| Wiring | `tests/unit/test_provider_wiring.py` | CI: `BILLING_PROVIDER` selects; the key read in `deps` only; `stripe` imported only where allowed (static check) |
| Contract | `tests/contract/test_billing_platform.py` | fake in CI; Stripe locally with `STRIPE_API_KEY`, else skipped with the reason |
| Investigation on Stripe | `tests/contract/test_stripe_investigation.py` | local: each world's target invoice at its instant, against its fake counterpart (SC-001) |

## Project Structure

### Documentation (this feature)

```text
specs/001-stripe-read-adapter/
├── spec.md
├── plan.md      # this file
└── tasks.md
```

### Source Code

```text
app/providers/base.py            # NotFound, ProviderUnsupported, can_write, PlatformCannotWrite
app/providers/fake/adapter.py    # imports NotFound; can_write = True
app/providers/stripe/            # keys.py, client.py, mapping.py, adapter.py
app/infrastructure/actions.py    # WritesNotSupported
app/application/execute_action.py
app/application/investigate_dispute.py
app/main.py                      # the ProviderUnsupported handler
app/api/deps.py                  # BILLING_PROVIDER, STRIPE_API_KEY
app/mcp/server.py                # tool error with the category
scripts/stripe_seed.py
tests/unit/ tests/integration/ tests/contract/
docs/contract/billing-platform.md   # the change folded in
docs/adr/001-*.md, docs/adr/008-*.md # amendments
README.md, docs/operations.md, .env.example, pyproject.toml, .gitignore
```

**Structure Decision**: the existing single-service layout; one adapter
package beside `fake/`, one new test directory `tests/contract/`.

## Research notes (2026-10-22)

- Billing periods moved from the subscription to its items in API version
  2025-03-31 (Basil); Dahlia keeps them there.
- Invoices lost `charge`, `payment_intent` and `paid` in Basil; payments are
  read through Invoice Payments.
- Legacy usage records were removed in Basil; billing meters are the only
  usage-based billing, aggregated asynchronously; summaries take a customer,
  a meter and minute-aligned start and end times.
- The customers list filter by email is exact and case-sensitive; customer
  search is eventually consistent and unavailable in some regions. The list
  leaves out test-clock customers unless filtered by clock.
- A test clock holds at most three customers.
- Restricted keys (`rk_…`) carry configurable permissions that cannot be read
  from the key.
- stripe-python 15.4.0 targets API version `2026-07-29.dahlia`.

Sources: Stripe changelog and API reference (subscriptions, invoice payments,
invoice line items, usage-based billing and meter event summaries, customers
list and search, test clocks, expansion, API keys) and the stripe-python
repository, consulted on 2026-10-22.

## Complexity Tracking

None.
