# ADR-005 — Money as integer minor units

**Status:** accepted · 2026-09-20 · revised 2026-09-21

## Context

`0.1 + 0.2` is not `0.3` in binary floating point. On one invoice that is a
rounding curiosity; summed over a month of proration it is a reconciliation
meeting.

Two other mistakes are as common and quieter. Treating every currency as
two-decimal reads `JPY 1000` as ten yen. And adding an amount in euros to an
amount in dollars produces a number that means nothing but looks like money.

## Decision

An amount is an integer count of minor units plus a currency code. Never a
float, anywhere.

**One conversion function.** `Money.from_decimal` is the only path from a
decimal into minor units, with one documented rounding mode: half-up, which
rounds away from zero and is therefore symmetric in sign — a 99.005 credit and
a 99.005 charge round to the same magnitude.

**Floats are rejected at runtime**, not merely discouraged. Passing one to
`from_decimal` or `multiply` raises a `TypeError` naming the reason. A rule
that only lives in a document is a rule that will be broken.

**A per-currency exponent table.** Two decimals is the default; `JPY`, `KRW`,
`VND`, `CLP` and `ISK` are zero-decimal, `KWD`, `BHD`, `JOD`, `OMR` and `TND`
are three.

**Cross-currency arithmetic and ordering raise.** "Is 100 EUR more than 100
USD" has no answer without a rate and a date, and inventing one inside a
dispute invents money.

`Decimal` remains legal for quantities and for intermediate proration maths,
but it crosses back into `Money` through `from_decimal`.

## Consequences

Every adapter converts at its boundary. The fixtures hold decimal strings in
major units precisely so that conversion is exercised on every load.

**Equality is a deliberate exception.** Ordering across currencies raises;
equality returns `False`. Raising on `==` would break sets, dict keys and `in`
checks — which the reply-draft guard depends on — and unlike ordering,
equality across currencies has an answer: they are not the same amount. The
distinction is documented in the module and covered by two adjacent tests.

The criterion is sharp: **raise wherever an implicit conversion would fabricate
an amount or a decision.** Addition and ordering can; equality cannot.

`Money` is immutable, so an amount can travel from a decision through an
action into an audit row without any of them mutating it.

Cost is money too. `cost_micros` is an integer count of millionths of a
dollar, for the same reason: a per-run cost of $0.0007 held as a float and
summed over an evaluation set drifts.

And a distinction that came out of the same reasoning: an unknown rate records
`NULL`, not `0`. Zero is a claim — it asserts the call was free — and a
self-hosted model with no per-call price is a different fact from a model
whose rate nobody added. One is a result worth publishing; the other is a
defect.

## Amendment, 2026-10-19 -- comparisons across currencies

Where a rule must compare amounts in different currencies -- an invoice's
lines, its payments, another live invoice for the same period -- it does not
conclude. `CURRENCY_MISMATCH` says so, no amount is proposed, and a person
reviews. Several currencies on one account are not an anomaly (taxonomy S10).
