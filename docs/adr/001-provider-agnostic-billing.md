# ADR-001 — Provider-agnostic billing

**Status:** accepted · 2026-09-20

## Context

The same dispute logic has to run at clients on Stripe, Zuora, OpenCell, Lago
or an in-house platform. Those platforms disagree about almost everything at
the surface: field names, status vocabularies, whether amounts arrive as
decimal strings or integers, how invoice lines carry their period.

They agree about the substance. A customer has subscriptions, a subscription
produces invoices, an invoice has lines covering a period, a payment settles
an invoice. That vocabulary is older than any of the vendors and will outlive
them.

Writing the rules against one platform's shape means rewriting them for the
next client. Writing them against a shared abstraction that leaks platform
concepts is the same thing with more indirection.

## Decision

A normalised domain — `Customer`, `Subscription`, `Invoice`, `InvoiceLine`,
`Payment`, `Usage`, `Money` — and a `BillingProvider` protocol that each
platform implements by mapping its payloads onto it.

Nothing in `app/domain`, `app/application` or `app/policies` imports a vendor.
`app/api/deps.py` is the only module that names a concrete implementation.

The fake provider's fixtures deliberately do **not** match the domain: amounts
are decimal strings in major units, statuses are called `state`, invoice
totals are called `amount`, lines are called `label`. That makes the adapter
perform a real mapping rather than a rename, and it exercises the single
rounding function of ADR-005 on every load.

Exactly one method mutates anything, and it takes an idempotency key.

## Consequences

Adding a platform is an adapter and a mapper. The rules, the engines, the
action policy and the audit do not move.

The domain carries invariants the platforms do not enforce. Datetimes must be
timezone-aware, normalised to UTC at the adapter boundary — `2026-09-01T00:00`
in Paris is a different instant from the same string in UTC, and a period that
slides by two hours changes which month an invoice belongs to.

Some platform detail does not survive the mapping. A unit price finer than the
currency's minor unit, for instance, has no home in `Money`; real platforms
carry a separate high-precision field for it. The fixtures use block pricing
rather than pretending the problem does not exist, and the limitation is
documented where the mapping happens.

No empty adapters. A directory named after a platform nobody has integrated
reads, to whoever inherits this, as a feature that exists.

## Amendment, 2026-10-19 -- a read that fails

A read the platform could not complete raises `ProviderUnavailable`, once
the adapter's own retries are spent. The investigation decides nothing on a
partial read -- an empty answer would read as "this customer has no
invoices" -- and the API answers 503; the caller can send the request again
as it is. A reference not found in the loaded window is looked up directly,
and only whether it is the customer's is kept (taxonomy S7).

## Amendment, 2026-10-22 -- what every platform says the same way

With the first real platform (Stripe, read-only; specs/001-stripe-read-adapter)
the port names three things every adapter needs, so that no caller depends on
one adapter's own errors:

- **`NotFound`**: an unknown identifier on `get_customer`, `get_subscription`,
  `get_invoice` (it was the fake's own class).
- **`ProviderUnsupported`**, with a reason from a closed set: the platform
  answered, with data the domain cannot represent (a subscription whose items
  run on different periods, an invoice with tax in this slice, a partial
  refund...). Not an outage and not a configuration error: nothing is decided
  on it, no run is recorded, and every API surface answers 422 with the
  category -- never the platform's data.
- **`can_write`**: whether the platform can make the two writes. One that
  cannot is never asked (ADR-008, same date).

Usage is read for a period, `get_usage(subscription_id, period)`: Stripe bills
metered usage in arrears, so the period the invoice covers is not the item's
current one. The investigation asks for the period the target invoice's
metered lines share; the fake answers its record only when it covers that
period exactly. Lines on distinct periods, on no real period, or no metered
line at all ask for nothing. So an invoice without metered lines no longer
brings a usage record into the engine's context: no rule compared it. On the
development set this changes the engine context of four cases (`duplicate_fr`,
`duplicate_en`, `known_customer_bulk_dispute`, `proration`) and no finding;
the maintainer accepted it on 2026-10-22.
