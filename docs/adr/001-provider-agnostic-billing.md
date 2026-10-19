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
