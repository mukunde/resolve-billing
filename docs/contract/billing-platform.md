# Billing platform

What any billing platform behind the `BillingProvider` port must do, and what
the rest of the system may rely on. Written on 2026-10-22 from the existing
records; the first real adapter -- Stripe, in test mode, read-only
(specs/001-stripe-read-adapter) -- folded in on 2026-10-23, after its contract
held on a seeded Stripe test account. Reasons are in ADR-001 (the port),
ADR-007 (unknown senders), ADR-008 (writes, idempotency, the platform as the
authority on amounts) and taxonomy S6, S7.

Two implementations exist: the **fake**, on fixtures, the default and what
CI, the demo and the evaluation run on; and **Stripe**, read-only, selected
with `BILLING_PROVIDER=stripe` and a restricted read-only test key.

## What the system relies on

- **One vocabulary.** A platform's payloads are mapped onto the domain
  objects -- `Customer`, `Subscription`, `Invoice`, `InvoiceLine`, `Payment`,
  `Usage`, `Money` -- and no logic outside the adapter depends on a vendor's
  fields, statuses or identifier format. The platform's identifiers do travel
  as opaque values: in the domain objects, the audit and internal surfaces.
  Amounts become integer minor units through the one rounding function
  (ADR-005).
- **Reads tell "not there" from "could not read" from "cannot represent"
  from "not allowed".**
  - `find_customer_by_email` and `find_invoice` answer `None` for something
    that does not exist; `get_customer`, `get_subscription` and `get_invoice`
    raise the port's `NotFound`. An object an invoice names and the platform
    cannot find (a price, a meter) makes the invoice unreadable, never absent.
  - A read that cannot complete -- timeout, failure, outage, a list cut short,
    a time budget spent -- raises `ProviderUnavailable` once the adapter's own
    retries are spent. The investigation then decides nothing and answers
    503; an empty list would read as "this customer has no invoices" (S7).
  - Data the platform holds and the domain cannot represent raises
    `ProviderUnsupported`, with a reason from a closed set. Nothing is decided
    on it: no investigation result, no proposal, no run row; every API surface
    that reads the platform answers 422 with
    `{"detail": {"category": "provider_unsupported", "reason": ...}}`, and the
    MCP tools a
    tool error with the reason -- never the platform's data.
  - A configuration or permission error -- a wrong or refused key, the SDK's
    console logging turned on -- fails visibly, by category, and is never
    read as an empty answer.
- **Whose an invoice is, and nothing more.** `invoice_owner(invoice_id)`
  answers the customer an invoice belongs to, or `None` when there is no such
  invoice. Only the owner is extracted: nothing of the content is mapped, so
  what another account's invoice bills can never surface, not even as a
  refusal. An owner the platform cannot name is `missing_field`, never an
  absence. The investigation asks only this for a reference outside the
  customer's window and for one an unknown sender cites.
- **The sender is found by address.** `find_customer_by_email` trims and
  lower-cases the address it is given and compares it with each customer's
  address lower-cased, returning the first match in a fixed order (D9). The
  fake compares the stored address untrimmed, in the fixtures' order; Stripe
  trims it too, in the order of creation then identifier, over every
  customer, test-clock customers included, and never uses its exact filter
  or its search to prove an absence. A customer without an address
  never matches. `list_customers` returns every customer, for the
  impersonation check (ADR-007); a customer without an address cannot be
  represented, so that list refuses (`missing_field`).
- **Invoices in a window.** `list_invoices(customer, since)` returns the
  customer's invoices issued at or after `since` -- the investigation passes
  180 days before its date; an invoice outside the window is not mapped, so
  an old one the domain cannot represent does not refuse the investigation.
  `find_invoice` looks one up directly, whatever its age (S7).
- **Subscriptions.** `list_subscriptions(customer)` returns every
  subscription of the customer, cancelled ones included; `get_subscription`
  reads one. Each has one current period, a status from the closed set, a
  plan, once cancelled the date it **ended**, and -- when the platform says
  them -- its cadence (`billing_interval`, `billing_interval_count`) and its
  billing anchor as the platform holds it now. An unknown cadence is `None`,
  never guessed; the fake's subscriptions that say none are monthly from
  their period's start, its convention.
- **Usage for a period.** `get_usage(subscription_id, period)` returns the
  record for exactly that period -- metric, quantity, period -- or `None`
  (unavailable, never zero; a record that says zero is zero). The
  investigation asks for the one real period the target invoice's metered
  lines share, and for nothing otherwise (ADR-001, 2026-10-22).
- **Payments per customer.** `list_payments` returns each payment with the
  invoice it settles, its amount and its state (pending, succeeded, failed,
  refunded). A succeeded payment carries what was collected; a pending or
  failed one what was asked, never read as money received.
- **References as the platform writes them.** Optionally,
  `invoice_references(text)` and `invoice_reference_spans(text)` find the
  invoice references a message cites, normalised to the platform's own
  identifiers (S6, D10) -- on Stripe, `in_` identifiers only. Both are
  synchronous: they read the text only. A platform without them has no
  cited-reference check, and the masking then recognises its identifiers only
  as the context holds them.

## What the domain cannot represent (`ProviderUnsupported`)

The closed set of reasons, each refused rather than approximated:
`mixed_item_periods`, `multiple_prices`, `subscription_status`,
`cancellation_without_end`, `invoice_tax`, `invoice_discount`,
`invoice_credit`, `negative_line`, `fractional_unit_price`, `partial_refund`,
`multi_invoice_payment`, `unplaced_payment`, `unrepresentable_period`,
`missing_field`. A new reason is a decision, with its tests.

## Writes

- **A platform says whether it can write** (`can_write`). One that cannot --
  Stripe, in this slice -- is never asked: execution is refused with 409
  before anything is read, reconciled or claimed, and the action keeps its
  state (ADR-008). Its writes and `find_action` raise `PlatformCannotWrite`
  without any call.
- **Two writes, both keyed.** `create_credit_note` and `create_refund` take an
  idempotency key derived from what the action is (ADR-008).
- **A refusal is the platform's answer, nothing else.** `ProviderRejected` is
  raised only when the platform said no and nothing was created; any other
  failure leaves the outcome unknown, and `find_action(key)` is how it is
  resolved -- never a blind retry.
- **The platform decides amounts and limits.** What an invoice can still give
  back is the platform's to enforce, and the system keeps no ledger. Before
  any write, the system applies its own guards: the correction is computed
  by code, capped at the invoice and in its currency, approved by a person
  for what was shown, and checked again against the invoice read at
  execution (ADR-008).
- **A refund draws on a settled payment.** An invoice with nothing collected
  to refund is a refusal, never a credit note instead.

## Stripe, as measured

On a test account built by `scripts/stripe_seed.py` (2026-10-23, API version
`2026-07-29.dahlia`, SDK 15.4.0): the ten properties of the spec held, every
seeded unsupported world was refused with its reason, and the five supported
worlds -- duplicate, proration, billed after cancellation, failed payment,
metered usage -- gave the same findings, classification, proposal (amount
and currency included), verdict and approval as their fake counterparts
(SC-001). No difference from the fake needed approving: the one the run
found -- the proration check measuring a line of another period against the
current one -- was a defect of the rule, fixed (taxonomy B11).

Two reasons have no seeded world, each approved by the maintainer and
covered by mapping tests: `unrepresentable_period` (the construction tried
had its period aligned by Stripe) and `multi_invoice_payment` (both
attachments tried were refused). `cancellation_without_end`,
`unplaced_payment` and `missing_field` are mapping tests only too.

## Not guaranteed today

- That a real platform enforces the caps the fake models (ADR-008, "to verify,
  not assume"): the Stripe slice is read-only.
- Authentication of the sender: an address is a lookup, not proof (L10).
- Freshness: the fake is static; Stripe's meter summaries are eventually
  consistent, and a usage record read right after an event may lag it.
- A currency the domain does not know is read with its default two-decimal
  exponent (ADR-005's table); the seeded worlds are in euros.
- Performance: the address lookup scans every customer on each dispute; a
  cached index is for a later slice.

## Covered by

The fake: `tests/unit/test_fake_provider.py`, `tests/unit/test_extra_fixtures.py`,
`tests/unit/test_execute_action.py`, `tests/integration/test_unusable_evidence.py`,
`tests/integration/test_unsupported_platform_data.py`,
`tests/integration/test_foreign_invoice.py`. Stripe, without network:
`tests/unit/test_stripe_mapping.py`, `tests/unit/test_stripe_adapter.py`,
`tests/unit/test_provider_wiring.py`. Both, against the contract:
`tests/contract/` -- the fake in CI, Stripe locally with the read-only key
on a seeded account.
