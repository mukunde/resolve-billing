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
- **Tax, as the platform posts it** (specs/013-invoice-tax). A line's
  `amount` is excluding tax; the tax posted on it is in `taxes`, each entry
  an amount and its rate (a percentage, never multiplied by anything here),
  with the platform's taxability reason when it states one. An invoice's
  amount excluding tax and its tax by rate are derived from its lines; its
  `total` is including tax. On an invoice carrying tax, the mapper reconciles
  what the platform posts -- every line and tax amount in the invoice's
  currency, the amount excluding tax, the tax by rate, the total -- and
  refuses `tax_mismatch` when they disagree (a tax no line carries, such as
  a taxed shipping cost, included); prices that include tax are
  `tax_inclusive`; a tax amount without a readable rate is `missing_field`.
  An invoice whose tax is zero everywhere is untaxed: its zero entries are
  dropped and it is read exactly as before tax existed. On Stripe a taxed
  invoice's rates are read from its tax rate objects, once per rate; an
  untaxed invoice reads none.
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
`cancellation_without_end`, `invoice_discount`,
`invoice_credit`, `negative_line`, `fractional_unit_price`, `partial_refund`,
`multi_invoice_payment`, `unplaced_payment`, `unrepresentable_period`,
`missing_field`, `tax_mismatch`, `tax_inclusive`. A new reason is a decision,
with its tests. `invoice_tax` was retired by spec 013: tax is read, and what
does not reconcile is `tax_mismatch`.

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
- **A taxed invoice, line by line** (specs/013-invoice-tax). On an invoice
  with tax, the credit note names every line of the invoice, whole, instead
  of an amount, so that Stripe reverses each line with its tax; every line is
  read to the end, and before sending the lines' amounts with their tax must
  make the approved amount including tax, or nothing is sent
  (`invoice_state`); a fractional quantity is refused too (a credit note line
  takes an integer). `credit_amount` and `refund_amount` are the total
  including tax. An untaxed invoice's request is unchanged, byte for byte.
  The credit note's `amount`, including tax, is checked against the approved
  amount.
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
run, without it, was refused with 403 (`permission`) and nothing was made.
With the reads alone, listing an invoice's credit notes answered when it had
none; credit notes that exist were read with Credit Notes: Write granted,
which allows reading them. Reading existing ones without it is not shown.
No difference from the fake needed approving.

**Tax** (specs/013-invoice-tax, 2026-11-12, run by an agent at the maintainer's
request, same API version and SDK, the restricted key given **Tax Rates: Read**): the
seed built the taxed worlds on the account -- the taxed duplicate, the taxed invoice
after cancellation, prices including tax (refused `tax_inclusive`) and a taxed shipping
cost (refused `tax_mismatch`). The read contract passed (78 passed, 28 skipped, all
fake-only or write tests): the two taxed worlds gave the findings, classification,
proposal, verdict and approval of their fake counterparts, and each refused shape its
reason. On a fresh write generation the write contract passed (25 passed, 1 skipped,
fake-only), the taxed properties included: a credit note on a paid taxed invoice,
written line by line for the total including tax and found again by `find_action`; one
on an open taxed invoice lowering what is due by that total; a taxed
refunding credit note refunding that total; a refund beyond what a credited taxed
invoice could give back refused, nothing written. Two defects of the seed were found and
fixed on the way: a price named a product the seed did not make, and the taxed
cancellation world's clock started on its month instead of a month before (its first
build was deleted and rebuilt). No difference from the fake needed approving.

## Pennylane, reading (specs/014-pennylane-read)

`BILLING_PROVIDER=pennylane`, a company token read-only on Customers, Customer invoices
and Billing subscriptions, and `PENNYLANE_SANDBOX_COMPANY_ID`: every read first checks,
through the identity endpoint, that the token's company is that sandbox, and refuses
otherwise (`sandbox`), the verdict kept. It writes nothing: `can_write` is false, every
write refused before any call, `find_action` empty. Its identifiers are typed
(`pl_cus_`, `pl_inv_`, `pl_sub_`, `pl_pay_` and Pennylane's integer), so ids of different
kinds never meet.

What Pennylane's documentation leaves open is mapped by the maintainer's conventions
(coordination decision 46), each recorded where it is applied, or refused:

- **Dates**: a date is its midnight in UTC (changed on 2026-11-12 from Paris's midnight,
  which broke the checks' UTC calendar across summer time); an invoice at the window's
  edge may fall one day in or out. A line's `imputation_dates` run from `start_date` to
  the midnight after `end_date`, included; a line without them has no period.
- **Invoices**: `paid` → paid, `upcoming` and `late` → open, `draft` → draft,
  `cancelled` with no credit note → void (convention); every other status refused
  (`invoice_status`); quotes, proformas and orders left out; credit notes told by their
  link to the invoice they credit, never by status, and an invoice with any credit note
  against it, whatever its date, refused (`invoice_credit`); an invoice without lines
  refused (`missing_field`).
- **Tax**: `FR_200`, `FR_100`, `FR_55`, `FR_21` read as 20, 10, 5.5, 2.1 %; `exempt` as no
  tax charged; every other code refused (`tax_code`); a discount, a negative line, a unit
  price finer than a cent refused; posted figures reconciled as spec 013 does.
- **Payments**: `confirmed` and `paid_out` → succeeded, `refunded` → refunded (read as
  full), the failures listed → failed, the states in progress (`found` and `not_found`
  included) → pending; `charged_back` and any other refused (`payment_status`). The
  maintainer kept, on 2026-11-12, the assumption this makes: a collection still
  confirmed or paid out was not refunded. Its effect: a refund Pennylane records elsewhere
  -- a partial one especially -- would not be seen, and a refund could be proposed on
  money already given back; the approver sees it first, and a refund made by credit note
  is caught, the invoice being refused. A payment seen under two of the customer's
  invoices is refused (`multi_invoice_payment`); one shared with another customer's
  invoice cannot be detected from one customer.
- **Subscriptions**: `in_progress` → active; `finished` → cancelled, the service ending
  at `finish` included; `stopped` → cancelled, the service running until the day before
  `next_occurrence`; others refused (`subscription_status`). The current period runs
  from `prev_occurrence` to `next_occurrence` and must be exactly one cycle of the rule by
  the checks' own calendar, else refused (`missing_field`). An invoice of a cancelled
  subscription is read only if every line has a period running forwards.
- **Customers**: found by any of their addresses, trimmed and case-folded, in id order;
  a customer with two addresses refused (`multiple_addresses`), the domain holding one.
- **References**: customers cite an invoice's number. The disputed invoice is resolved
  once, among the invoices the investigation loaded: a typed id as it is, a number to its
  invoice; an unresolved number is an unknown reference and is never sent to the API.
  Numbers of loaded invoices are recognised in a message -- whole, case-folded, bounded by
  a character that is neither a letter nor a digit, the longest first -- checked against
  the disputed invoice and masked before any engine; a number outside them is not
  recognised.

Reads per investigation: the customers, the customer's invoices and credit notes, each
invoice's lines and payments, the subscriptions: several dozen requests, paced under 25
per 5 seconds per token within the process, retried within a 20-second budget, every list
read to its end or the read refused as unavailable. Two processes on one token are not
paced together.

**As measured** (2026-11-12, run by an agent at the maintainer's request, on the
declared sandbox, the seed with its own read-write token, the contract with the
read-only one). SC-002 was amended by the maintainer on 2026-11-12: the documentation
offers no way to attach a hand-made invoice to a subscription, so the duplicate and
after-cancellation comparisons with the fake are made only where the sandbox lets their
worlds be built.

- The seed first stopped on Pennylane's own refusal: no invoice or subscription before
  the company's invoice numbering is set. An agent set it, at the maintainer's request, in the sandbox's
  settings (prefix `F-(annee)-(mois)-`, from 1).
- Built: the taxed invoice, the paid invoice, the subscription (made, then stopped),
  the credited invoice, the customer with two addresses, the invoice with a discount, the
  fractional unit price. Excluded by the maintainer after the seed reported them
  (`NOT_SEEDED`, each with why): the mixed VAT rate, which Pennylane refuses on a
  customer invoice; the partial payment and the charge-back, which the documented API
  cannot make; the taxed duplicate and the taxed invoice after cancellation, which need
  a subscription's invoices the API does not let a seed control. The comparison of these
  two with their fake counterparts is therefore not measured on Pennylane.
- The contract: 9 passed, 6 skipped -- the five exclusions, and the paid invoice's
  payments, as Pennylane listed **no payment** for an invoice marked paid. The taxed
  invoice was read field by field as written, its number equal to Pennylane's own
  field; each refused world gave its reason (`invoice_credit`, `multiple_addresses`,
  `invoice_discount`, `fractional_unit_price`); the owner, the number's resolution and
  the absence of writes held.
- Observed, not assumed: `mode` is answered as a plain string though sent as an object;
  `recurring_rule` is answered with `rule_type` and `day_of_month` as a list. Once
  stopped, the subscription kept `next_occurrence` (a month after its start) and no
  `finish` -- the shape the stopped convention reads. In `awaiting_validation` mode it
  generated one invoice at once, left as a draft without a number. An invoice past its
  deadline reads `late`. The state before the stop was not recorded: an earlier run, cut
  short by a defect of the seed (since fixed), had already stopped it.

## Pennylane, writing (specs/015-pennylane-credit-notes)

Credit notes only, on the declared sandbox only, and only when `PENNYLANE_WRITES` is
exactly `enabled`, with a token of their own (`PENNYLANE_WRITE_TOKEN`,
`customer_invoices:all`) checked against the same company; otherwise `writes` is empty
and Pennylane writes nothing. **Off as delivered**: Pennylane's written answers on an
atomic cap and on finalising without sending come first (decision 47). Pennylane has
no refund write: a refund proposed on it is a person's (see the execution contract).

- **How it lands** (`plan_write`): an open invoice (`upcoming`, `late`) → `reduce_due`;
  a paid one → `customer_balance`, an amount owed to the customer that a person pays
  back; any other state refused (`invoice_state`).
- **The write, in one claim**: no other credit note of ours left half-way for the
  customer; the customer and, later, the draft hold nothing that could carry a document
  out (no SIREN, no VAT number, no eligible electronic billing address, only addresses
  under the reserved test domain); a **draft** dated the day, in the invoice's currency,
  `external_reference` `rb:` + the action's key, its lines from the approved breakdown
  (a taxed invoice reversed line by line, an untaxed one in a single line); the draft
  read back and compared exactly with what was approved; **linked**; checked again;
  **finalised**; read back. Never an email or e-invoicing request.
- **Nothing written**, `FAILED`, in two cases only: a draft that differs, or a link
  refused for the cap -- the draft deleted and seen gone. Anything else after the
  creation is `partial_write`, a person's.
- **Found again** (`find_action`) by `external_reference`: a credit note linked,
  finalised and alike is the write, with the breakdown it credits; a draft or an
  unlinked one is reported as partial, never as a success.
- The only identifier Pennylane holds is the key; `test_mode` is true for the declared
  sandbox alone.

**Seen on the sandbox** (2026-11-12, observations, not guarantees): a draft credit note
can be linked, then finalised; a draft's lines take no `imputation_dates`; a second
create with an `external_reference` already used is refused (422); a finalised credit
note reads `cancelled`, negative amounts, numbered in the invoices' sequence; an
invoice credited through the API reads `partially_cancelled` (one credited from the
interface `partially_paid`), a paid one stays `paid`; the cap is refused at linking
(400 "The credit notes amount is bigger than the invoice amount"), alone and summed,
from the API and across the interface and the API; a document dated before the latest
finalised invoice cannot be finalised; the interface would not finalise for a company
customer without SIREN and VAT number, the API did; a day after, no credit note carried
an electronic billing address or an e-invoicing state.

**Prerequisites** (decision 47): the atomic cap -- not met (sequential refusals only);
finalising without sending or queueing -- not met (no written answer yet). The write
contract (`tests/contract/test_pennylane_writes.py`, on a fresh seed generation) is
recorded here once run.

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
Tax (spec 013): `tests/unit/test_invoice_tax.py`, `tests/unit/test_tax_rules.py`,
`tests/unit/test_tax_payload.py`, `tests/unit/test_untaxed_bytes.py`,
`tests/integration/test_tax_breakdown.py`, and the taxed tests of the Stripe
files above.
Pennylane writes, without network: `tests/unit/test_pennylane_writes.py`,
`tests/unit/test_pennylane_seed.py`; on its sandbox, `tests/contract/test_pennylane_writes.py`.
Pennylane, without network: `tests/unit/test_pennylane_client.py`,
`tests/unit/test_pennylane_mapping.py`, `tests/unit/test_pennylane_adapter.py`,
`tests/unit/test_pennylane_review.py`, `tests/unit/test_pennylane_review2.py`,
`tests/unit/test_pennylane_seed.py`; on its sandbox,
`tests/contract/test_pennylane_contract.py`.
Both, against the contract: `tests/contract/` -- the fake in CI, Stripe
locally on a seeded account, with the read-only key, and with
`STRIPE_WRITES=enabled` on a fresh write run for the write properties
(`test_billing_platform_writes.py`).
