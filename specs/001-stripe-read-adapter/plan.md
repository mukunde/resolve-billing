# Implementation Plan: Stripe as a billing platform, read-only

**Branch**: `001-stripe-read-adapter` | **Date**: 2026-10-22 | **Spec**: [spec.md](spec.md), approved by the maintainer at commit `8046270`

**Input**: Feature specification from `specs/001-stripe-read-adapter/spec.md`

## Summary

A second implementation of the `BillingProvider` port, reading a Stripe test
account, selected by configuration. The port gains what every platform needs
(`NotFound`, `ProviderUnsupported`, `can_write`, `PlatformCannotWrite`, and
`invoice_owner`); every
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
| I. Deterministic before probabilistic | Untouched: mapping is deterministic; one rule, the impersonation check, takes the cited invoice's owner instead of the invoice (2026-10-22, review r118). |
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
  `missing_field`, and `invoice_credit` (added 2026-10-22, review r118). Its
  message is the category, never the platform's data.
- **`invoice_owner(invoice_id) -> str | None`** (added 2026-10-22, review
  r118): whose an invoice is. Only the owner is extracted -- the content is
  never mapped, the lines never completed -- so what the invoice bills never
  makes it refuse; `None` means no such invoice, and an owner Stripe does not
  name (`customer` null or empty) is `missing_field`, never an absence. The
  investigation uses it for a reference outside the window
  and for one an unknown sender cites; `suspected_impersonation` takes an
  `InvoiceOwner` (invoice id, customer id) instead of the invoice.
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
- **No state change caused by the read.** `approve()` and `read_action` first
  apply the existing time-based expiry (`expire_if_overdue`: a deadline that
  passed expires the action whatever the platform says, as today), then read
  the fingerprint; if that read raises, nothing further is transitioned. The
  execute endpoint's catch-all (`except Exception` → 504, unknown outcome)
  gets an `except ProviderUnsupported: raise` ahead of it, so the handler
  answers 422 and the action keeps its state; a read before the claim never
  leaves an unknown outcome. For Stripe, execution is refused even earlier
  (§3). Tests pin each path: approve, read action, review page, execute on a
  writable provider whose invoice read is unsupported.
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
(`customer.email`, a price, a period) is
`ProviderUnsupported("missing_field")`, never a blind `.upper()` or `.strip()`.
Two departures, approved by the maintainer on 2026-10-22 (review r118):
`customer.currency` null maps to none -- the domain's field is optional, and
Stripe leaves it null until a customer is first billed, so refusing would
fail every address scan on an account holding one such customer; and a
customer without an address is never a match in the lookup (compared on the
raw payload, only the match is mapped), while listing every customer -- the
impersonation check, for an unknown sender -- refuses with `missing_field`.
`customer.name` maps to the optional display name and stays `None`. A line's
`description`, required by the domain (`InvoiceLine.description: str`), maps
to the empty string when Stripe has none -- it is informative only, read by
the engine, never by a rule -- with a test on both.

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
  `current_period_end`; cadence from the price's `recurring.interval` and
  `interval_count`, anchor from `billing_cycle_anchor` (each `None` when
  absent). Status table per spec FR-012; `canceled` uses
  `ended_at` (missing → `cancellation_without_end`); a scheduled cancellation
  keeps the status, no date. Plan: the price's `lookup_key`, else its id.
- *Invoice*: `id`; status 1:1; issued at `status_transitions.finalized_at`, a
  draft at `created`; `total` and line `amount` in minor units; line `period`.
  Refused, by category, from the version's structured fields: any non-zero
  entry in the invoice's `total_taxes` or a line's `taxes` (exclusive or
  inclusive) → `invoice_tax`; any non-zero `discount_amounts` on a line or
  `total_discount_amounts` on the invoice → `invoice_discount`; a negative
  line `amount` → `negative_line`; a non-zero `pretax_credit_amounts` on a
  line or `total_pretax_credit_amounts` on the invoice → `invoice_discount`
  when its type is `discount`, `invoice_credit` otherwise; a line price whose
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
  `start_time` (inclusive) and `end_time` (exclusive) set to the period asked
  (option (a), decided below); both must fall on whole minutes, else
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

- Refuses anything but `sk_test_`, before the SDK is loaded. Idempotent:
  every object it makes carries metadata `rb_world=<name>` and
  `rb_step=<world>/<step>`, each step looks for its object before making one,
  and every creation carries an idempotency key of its world, generation and
  step (so two runs at once, or a retried request, make it once, and a new
  generation reuses no key). An invoice Stripe makes itself -- a first
  invoice, a renewal, a final or an arrears one -- is taken as its step only
  if no step took it yet and, except the final invoice of a cancellation
  (whose billing reason is not checked), Stripe made it for that step's
  billing reason;
  the choice is kept on the subscription (`rb_taken_<step>`, or `none` when
  Stripe made none) and on the invoice, so a later run never looks at
  whatever is latest by then.
  A card already on the customer is reused. Shared objects (products,
  prices, the coupon, the tax rate) carry fixed identifiers or lookup keys
  and the tag `shared/<kind>/<key>`; the meter, which has no metadata, is
  found by its event name and checked for its configuration. A run lists
  what exists and completes what is missing, a half-created world included;
  nothing is made twice.
- **One run at a time**: an exclusive lock on a file in the temporary
  directory, held for the run and freed if the process dies; a second run is
  refused. It holds between runs of one environment only -- Windows and WSL
  do not lock each other out -- so the seed is always run from the same one.
- **A manifest makes a clock the seed's**: a customer of its own, without a
  clock, whose metadata names each world's generation (`g_<world>`: its
  number, month and opening time) and clock (`c_<world>`). Exactly one
  manifest, readable and consistent -- a clock named with no generation, or
  a generation that cannot be read -- or every world is reported before
  anything is made; a clock whose name is not its generation's is found while
  its world is processed, and that world alone is reported. A clock the
  manifest does not name is never used, whatever it is called. A generation
  an earlier run opened and stopped before noting its clock re-sends the
  clock's creation with its key: within the day Stripe remembers keys, that
  returns the clock it made, if it made one; past it, a clock bearing that
  generation's name is reported for the maintainer to settle, never adopted.
- **Only what the seed made is touched.** A clock holding a customer the
  seed did not make, or a shared object that exists untagged or on other
  terms (a price's amount, interval and count, usage type, meter, scheme or
  quantity transform; the coupon's percentage and duration; the tax rate;
  the meter's configuration) is a collision: reported, and left alone.
- **A world's one meter event** carries a fixed identifier and the month's
  event day as its timestamp. Its state is kept on the customer (`sending`
  before the request, `sent` after): a run that finds `sending` sends
  nothing -- usage already aggregated settles it, and otherwise the
  maintainer is asked to check the meter -- since Stripe deduplicates by
  identifier for a day at least, not forever.
- Lists are read to their end, and one cut short is an error, never a
  shorter answer.
- **Dates, decided by the maintainer on 2026-10-23**: every world is built
  on **the month that ends just before the seed runs**, not on fixed dates.
  With `last` the day before the run: the month starts on the same day of
  the previous month (at most the 28th, so month arithmetic stays exact),
  midnight UTC, and ends one month later -- at the latest on `last`. A world
  that needs history starts one month earlier. So no clock is ever ahead of
  the real time a world needs, and meter events stay inside Stripe's accepted
  window (35 days back, 5 minutes ahead), whichever clock it judges them by.
  A world keeps the month of its generation, noted in the manifest, and a
  later run -- another day, another month -- resumes it on those dates. A new
  generation is opened by deleting the world's clock (Stripe deletes a clock
  30 days after its creation anyway): the next run builds it again on that
  run's month, under the next number, reusing no object and no key. Its clock
  is named `rb:<world>@<first day of its month>/g<number>`. The investigation
  instant each world
  records is what the contract tests use; the fake counterparts keep their
  own dates.
- **Amounts are the fake counterpart's**, in EUR, because SC-001 compares the
  whole proposal: 99.00 for the duplicate and the proration target, 49.00 for
  the cancellation and failed-payment targets, 47 blocks at 0.50 (23.50) for
  the usage target.
- **One test clock per world** (a clock holds at most three customers; one
  per world also keeps worlds from moving each other), named as above.
  Each world records in its customer's metadata the **target invoice** and
  the **investigation instant** -- right after the target invoice is
  finalised, before any renewal can add an invoice the test did not expect --
  and only once its preconditions hold.
- **Every invoice of a world belongs to the world's subscription**: invoices
  are created with the `subscription` parameter, their items carrying the
  subscription and an explicit period, so the rules see what they compare
  (same subscription, same or later period, same total).
- **Preconditions checked before a world is recorded**: the seed reads the
  world back through the adapter's own mapping and checks the facts the
  world exists for (two live invoices on one subscription with equal totals
  and overlapping periods; a line covering half a period at the full price;
  an invoice period starting after the subscription ended; a failed payment;
  usage over the target line's period equal to its quantity; the target's
  total equal to the counterpart's and its state the counterpart's -- paid,
  or open; for an unsupported world, its reason). A world already recorded
  is checked again, without writing, on every run: one that no longer holds
  is reported, not called complete. A
  world Stripe does not let the seed build, or whose facts do not hold, is
  **reported to the maintainer, never dropped silently**: removing a world
  from SC-001 needs his approval. A Stripe refusal is reported by its type,
  code, parameter and request id (to find it in the Dashboard's logs), not by
  its message; a request Stripe did not answer is reported as an outcome to
  reconcile, which the next run does by looking for what the step made.
- Supported worlds, each modelled on a development situation (S the month's
  start, E its end, P one month before S):
  - *duplicate* (`duplicate_fr`): an Enterprise subscription at 99.00 from S,
    its first invoice paid; at S + 1 day a one-off item of 99.00 for [S, E)
    on that subscription, invoiced, finalised and paid; target = that invoice;
  - *proration* (`proration`): a Pro subscription at 49.00 from S; at the
    middle of the month the price moves to Enterprise without proration, and
    an item of 99.00 -- the full monthly price -- for [middle, E) is invoiced
    and finalised, left open; target = that invoice;
  - *billed after cancellation* (`billed_after_cancellation`): a Pro
    subscription from P; at S - 1 day an item of 49.00 for [S, E) on it, then
    the subscription cancelled at once with a final invoice, which takes the
    pending item; finalised and paid; target = it;
  - *failed payment* (`failed_payment_valid_invoice`): a Pro subscription from
    P paid with a test card that works; before S the default card becomes
    Stripe's `pm_card_chargeCustomerFail`; the clock passes S + 1 hour, the
    renewal invoice for [S, E) is finalised and its payment fails; target = it;
  - *usage* (`legitimate_overage`): a Pro subscription and a metered one (0.50
    per block, on the meter `rb_api_call_blocks`) from S; at E - 1 day a
    meter event of 47, sent with the clock's time; the seed waits for the
    summary to say 47, then the clock passes E + 1 hour and the arrears
    invoice for [S, E) is finalised and paid; target = it.
- Unsupported worlds: **one per `ProviderUnsupported` reason the seed can
  produce in test mode**, each a subscription from S whose first invoice is
  the target unless said: mixed item periods (a monthly and a yearly price,
  flexible billing mode), multiple prices (two monthly prices), an
  unsupported status (a declining card, `default_incomplete`), tax (a 20%
  exclusive rate), discount (a 10% coupon), negative line (an invoice with a
  49.00 item and a -10.00 one), fractional unit price (a price of 4900.5
  cents), partial refund (10.00 refunded on a paid invoice), a credit balance applied before tax (a billing credit grant on a
  metered subscription; target = the arrears invoice). A reason the seed
  cannot produce is covered by mapping tests only, and listed
  (`scripts/stripe_worlds.py`). A period off whole minutes was planned too,
  on a clock started 30 seconds past a minute; the first run (2026-10-23)
  showed Stripe aligning that construction's period, and the maintainer
  approved moving that reason to the list the seed does not produce. So
  for a payment across invoices: Stripe attached one payment to a
  subscription invoice charged automatically only for the invoice's exact
  total, and to a one-off invoice sent to the customer refused a payment
  larger than the invoice (`amount_too_large`); the maintainer approved moving
  it too.
- Waits, within a bound, for each clock to be `ready`, the target invoices
  finalised and the meter summaries present; never writes a zero in place of
  a delay. The bound holds for the whole wait, reads included, and a wait
  past it reports the world.
- **Found in Phase 5, as expected, and fixed (2026-10-23, the maintainer's
  option B+)**: the proration check compared a line with the subscription's
  *current* period. Where a world's target lies in another
  period of a different length -- the arrears invoice once the subscription
  has renewed, the cancelled subscription's last period -- it can see a
  mismatch the fake never shows when that line's period is shorter than the
  current one and the gap passes the tolerance, depending on the run's month.
  SC-001 reported it for `billed_after_cancellation` and `usage`. The rule now
  measures only lines inside the current period. A line outside covers whole
  periods, and is no proration, only when both its bounds fall on the
  subscription's schedule: its original anchor stepped by its cadence, months
  clamped to their last day. The subscription carries both (`billing_interval`,
  `billing_interval_count`, `billing_anchor`), read on Stripe from the price's
  recurrence and the subscription's `billing_cycle_anchor`, and in the fake's
  fixtures. Any other line outside -- or any line outside when the cadence is
  unknown -- is said not evaluable (taxonomy B11). A first version (option B)
  took the anchor's day and hour alone for proof, and review r125 showed it
  wrong for a yearly subscription and an anchor on the 31st.

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

## Decided 2026-10-22: usage and the current period -- option (a)

Stripe bills metered usage **in arrears**: the invoice finalised at the end of
a period carries the usage of the period that just ended, while the
subscription item's *current* period is already the next one. The port's
`get_usage(subscription_id)` has no period, and the approved spec (FR-016)
reads the item's current period. So on Stripe, a metered invoice is always
compared with the wrong period: the investigation finds `USAGE_UNAVAILABLE`
(D14) and sends "the invoice is correct" to a person. Safe, but SC-001 cannot
hold for a usage world.

- **(a) Recommended -- amend the port**: `get_usage(subscription_id, period)`.
  - *Which period*: the target invoice's metered lines must all share one
    real period (start before end); the investigation then asks for that
    period. Lines with distinct periods, or no real period, ask for nothing:
    usage stays `None`, the lines are said unchecked (`USAGE_UNAVAILABLE`),
    exactly as today for a metric billed on several lines. No line is chosen
    arbitrarily.
  - *The fake* returns its record only when it covers that period exactly.
    Findings and engine context can both change: with two metrics on
    distinct periods, a line the current record checks today becomes
    unchecked; a record on another period disappears from the context. So
    both are **compared, not assumed**: the mock baseline matched case by
    case; findings and `BillingContext.usage` compared on every development
    case before and after; counter-tests for a record on another period and
    for two metrics on distinct periods. **Any change is submitted to the
    maintainer** before anything merges.
  - *Stripe* reads the meter's summary over that period.
  - Amends the approved spec (FR-016) and ADR-001.
- **(b) Keep the port**: on Stripe a usage dispute never concludes; the usage
  world's expected result in SC-001 becomes `USAGE_UNAVAILABLE`, a deviation
  from its fake counterpart that the maintainer approves.

**The maintainer chose (a) on 2026-10-22**, amending FR-016 and ADR-001.

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
