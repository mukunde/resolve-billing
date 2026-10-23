# Billing platform

What any billing platform behind the `BillingProvider` port must do, and what
the rest of the system may rely on. Written on 2026-10-22 from the existing
records; the first real adapter -- Stripe, in test mode, read-only
(specs/001-stripe-read-adapter) -- folded in on 2026-10-23, after its contract
held on a seeded Stripe test account; its writes (specs/002-stripe-writes)
the same day. Reasons are in ADR-001 (the port), ADR-007 (unknown senders),
ADR-008 (writes, idempotency, the platform as the authority on amounts) and
taxonomy S6, S7.

Two implementations exist: the **fake**, on fixtures, the default and what
CI, the demo and the evaluation run on; and **Stripe**, selected with
`BILLING_PROVIDER=stripe` and a restricted test key, which reads only unless
`STRIPE_WRITES=enabled`.

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
  Stripe without `STRIPE_WRITES=enabled` -- is never asked: execution is
  refused with 409 before anything is read, reconciled or claimed, and the
  action keeps its state (ADR-008). Its `plan_write` and writes raise
  `PlatformCannotWrite` without any call.
- **A write is planned, then sent.** `plan_write(invoice_id, action_type)`
  reads the invoice and answers how a write of that type lands on it: what
  is due lowered (`reduce_due`, an open invoice with nothing paid, a credit
  note only), the customer's balance credited (`customer_balance`, a paid
  invoice) or money paid back (`refund`, a paid invoice). Any other state --
  partly paid, paid out of band, draft, void, uncollectible -- is
  `ProviderRejected("invoice_state")`. The execution service keeps the
  answer and passes it to every send.
- **Two writes, both keyed, each checkable.** `create_credit_note` and
  `create_refund` take the amount, the currency, an idempotency key derived
  from what the action is, the action's identifier, the allocation planned
  and a deadline (`send_by`). The adapter checks that the invoice still takes
  that allocation -- refusing (`invoice_state`) rather than writing another
  -- then, at the point of sending, that the deadline has not passed
  (`NotSent`, nothing sent). A write returns a `ProviderAction`: the
  platform's reference, the invoice, the type, the amount and currency, test
  mode, the allocation, the key and action identifier it holds, and the
  platform's status (`succeeded`, `pending`, `failed`) observed at an
  instant. What the answer does not say -- the allocation, the status -- is
  `None`, never a guess.
- **A refusal is the platform's answer, nothing else.** `ProviderRejected` is
  raised only when the platform's answer proves nothing was created; any
  other failure after the request left -- no answer, an answer that cannot
  be read, an idempotency conflict, a time budget spent -- is
  `ProviderOutcomeUnknown`, never `ProviderUnavailable`.
- **Every write under a key, found again.** `find_action(key, invoice_id)`
  answers every write the platform holds under the key on the invoice:
  none, one or more. A list cut short is `ProviderUnavailable`, never an
  absence. It is a read, answered whether or not the platform can write.
- **A window, declared by the platform.** `idempotency_window` is how long
  after a first attempt the platform still answers a key with what it made:
  every send, and every absence taken as proof, falls within it (Stripe and
  the fake: 23 hours).
- **The platform decides amounts and limits.** What an invoice can still give
  back is the platform's to enforce, and the system keeps no ledger. Before
  any write, the system applies its own guards: the correction is computed
  by code, capped at the invoice and in its currency, approved by a person
  for what was shown, and checked again against the invoice read at
  execution (ADR-008).
- **A refund draws on a settled payment.** An invoice with nothing collected
  to refund is a refusal, never a credit note instead.

## Stripe, writing

With `STRIPE_WRITES=enabled` (that exact value, read in the wiring alone) and
the restricted test key given the write permissions it needs:

- **One kind of write: a credit note.** A credit note on an open invoice with
  nothing paid lowers what is due (`amount` only); on a paid invoice it
  credits the customer's balance (`credit_amount`). A refund is a credit note
  whose whole amount is refunded to the invoice's payment (`refund_amount`):
  never a standalone refund, so one cap -- Stripe's on what an invoice's
  credit notes and refunds give back -- and one place to find a write.
- **Nothing of the case reaches Stripe.** The metadata holds the key, the
  action's identifier and its type, nothing else; `email_type` is `none`, so
  Stripe mails nobody; no `memo`, no `reason`.
- **One attempt, one request.** A write is sent with the action's key as
  Stripe's idempotency key and none of the SDK's own retries: the SDK would
  retry a 409 or a 5xx by itself, and a refusal of a later request would hide
  an earlier one that landed.
- **A closed list of refusals.** An invalid request other than an
  idempotency error (400, 404), an authentication or permission refusal
  (401, 403), and the adapter's own refusals before sending. Everything
  else is an unknown outcome. Categories only, never Stripe's text.
- **Found again by listing.** `find_action` lists the invoice's credit notes
  to the end, their refunds expanded, and keeps those whose metadata holds
  the key; never Stripe's search. It needs no writer.
- **Statuses.** `succeeded` when the credit note is issued and, for a refund,
  its refund succeeded; `pending` while the refund is in progress or awaits
  action; `failed` when it failed or was cancelled, or the credit note is
  void. A refund Stripe did not expand cannot be read.

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

**Writing** (specs/002-stripe-writes, 2026-10-23, the maintainer's run on a
fresh write generation, same versions): the seven write properties held --
a key sent twice made one credit note; it was found again by listing the
invoice's credit notes; a credit note credited the balance on a paid
invoice and lowered what was due on an open one; a refunding credit note
refunded the payment; a refund beyond what a fully credited invoice could
give back was refused, nothing written; a refund on an invoice with nothing
paid was refused before sending; and the platform without writes refused
them. End to end, the duplicate dispute investigated, approved and executed
twice made one credit note for the approved amount, and a refund executed
through the service one refunding credit note (SC-002). The one write
permission the restricted key needed was **Credit Notes: Write**: its first
run, without it, was refused with 403 (`permission`) and nothing was made;
reading credit notes needed no permission beyond the reads listed above.
No difference from the fake needed approving.

## Not guaranteed today

- That Stripe keeps idempotency keys for at least a day: its documentation
  says so; the contract run measures a key answered twice within the run,
  not a day later.
- A refund's later fate: the contract saw it succeeded or pending right
  after the write; a person reads it again for what came after.
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
`tests/unit/test_stripe_writes.py`, `tests/unit/test_provider_wiring.py`.
Both, against the contract: `tests/contract/` -- the fake in CI, Stripe
locally on a seeded account, with the read-only key, and with
`STRIPE_WRITES=enabled` on a fresh write run for the write properties
(`test_billing_platform_writes.py`).
