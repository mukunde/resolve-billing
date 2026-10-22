# Billing platform

What any billing platform behind the `BillingProvider` port must do, and what
the rest of the system may rely on. Written on 2026-10-22 from the existing
records, before the first real adapter (Stripe, read-only) is folded in.
Reasons are in ADR-001 (the port), ADR-007 (unknown senders), ADR-008
(writes, idempotency, the platform as the authority on amounts) and taxonomy
S6, S7.

## What the system relies on

- **One vocabulary.** A platform's payloads are mapped onto the domain
  objects -- `Customer`, `Subscription`, `Invoice`, `InvoiceLine`, `Payment`,
  `Usage`, `Money` -- and nothing outside the adapter sees a vendor's field,
  status or identifier format. Amounts become integer minor units through
  the one rounding function (ADR-005).
- **Reads tell "not there" from "could not read".**
  - `find_customer_by_email` and `find_invoice` answer `None` for something
    that does not exist.
  - `get_customer`, `get_subscription` and `get_invoice` raise for an unknown
    identifier. *Today the error type is the fake's own `NotFound`; the port
    does not name one.*
  - A read that cannot complete -- timeout, failure, outage -- raises
    `ProviderUnavailable` once the adapter's own retries are spent. The
    investigation then decides nothing and answers 503; an empty list would
    read as "this customer has no invoices" (S7).
- **The sender is found by address.** `find_customer_by_email` matches an
  address regardless of case and surrounding spaces, and returns the first
  customer that matches (D9). `list_customers` returns every customer, for
  the impersonation check (ADR-007); a real adapter may serve it from a cached
  index.
- **Invoices in a window.** `list_invoices(customer, since)` returns the
  customer's invoices issued at or after `since` -- the investigation passes
  180 days before its date. `find_invoice` looks one up directly, whatever its
  age (S7).
- **Usage per subscription.** `get_usage` returns one record per
  subscription -- metric, quantity, period -- or `None`.
- **Payments per customer.** `list_payments` returns each payment with the
  invoice it settles, its amount and its state (pending, succeeded, failed,
  refunded).
- **References as the platform writes them.** Optionally,
  `invoice_references(text)` and `invoice_reference_spans(text)` find the
  invoice references a message cites, normalised to the platform's own
  identifiers (S6, D10). A platform without them simply has no
  cited-reference check, and its references pass the masking only when
  written exactly as the context holds them.

## Writes

- **Two writes, both keyed.** `create_credit_note` and `create_refund` take an
  idempotency key derived from what the action is (ADR-008).
- **A refusal is the platform's answer, nothing else.** `ProviderRejected` is
  raised only when the platform said no and nothing was created; any other
  failure leaves the outcome unknown, and `find_action(key)` is how it is
  resolved -- never a blind retry.
- **The platform decides amounts and limits.** What an invoice can still give
  back is the platform's to enforce; the system keeps one guard of its own (no
  write above the invoice total or in another currency) and no ledger.
- **A refund draws on a settled payment.** An invoice with nothing collected
  to refund is a refusal, never a credit note instead.

## Not guaranteed today

- That a real platform enforces the caps the fake models (ADR-008, "to verify,
  not assume"); no real adapter exists yet.
- Authentication of the sender: an address is a lookup, not proof (L10).
- Freshness: the fake is static; a real platform's reads may lag its writes.

## Covered by

`tests/unit/test_fake_provider.py`, `tests/unit/test_extra_fixtures.py`,
`tests/unit/test_execute_action.py`, `tests/integration/test_unusable_evidence.py`
-- against the fake only.
