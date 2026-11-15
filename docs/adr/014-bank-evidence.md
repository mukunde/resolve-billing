# ADR-014 — A bank as evidence, behind a read-only port

**Status:** accepted · 2026-11-14 · spec 016 · amends constitution III (1.1.0);
extends ADR-001 and ADR-009 · scoped to the Revolut Business Sandbox and
fixtures; the Sandbox read-back (SC-002) ran on 2026-11-15 and its check found
nothing -- a positive bank finding is demonstrated on fixtures only

## Context

A customer often disputes an unpaid invoice by saying "I paid". The billing
platform is the authority on what an invoice owes (constitution VI), but it
does not always know about a transfer the merchant received: a bank transfer
reaches the merchant's bank first, and the platform learns of it only when
someone reconciles it. Until then, the platform's "unpaid" and the customer's
"I paid" can both be true, and an automatic answer built on the platform alone
would be wrong.

The merchant's own bank can say whether money arrived. It cannot say what the
money was for. A transfer's reference is free text, typed by the payer: it can
cite the wrong invoice, several invoices, or nothing at all. Its amount can be
partial, over, in another currency or reduced by a fee. And it can be reversed
after it completed. So the bank is evidence about a receipt, never a fact
about an invoice.

The constitution allowed vendors behind two ports only: `BillingProvider` and
`DecisionEngine`. A bank is neither. Hiding it inside the billing provider
would have made a bank observation look like a platform `Payment`. It would
then have fed the duplicate-payment rule and the billing figures, and the
platform's authority on amounts would have been lost.

Revolut Business was chosen as the first bank because its Sandbox is free and
documented. Qonto's developer portal is open only to paying, verified
customers. Revolut has not yet answered six questions about Production
behaviour:

- the type of an incoming SEPA transfer;
- whether its reference carries the payer's text verbatim;
- how a reversal of a completed credit appears;
- whether `created_at` can tie at a page boundary;
- the longest delay between creation and completion;
- how far back the history goes.

Until it answers, some choices below are conventions the maintainer approved.

## Decision

**A third port, read-only by construction.** `BankEvidence` (in
`app/providers/bank_base.py`) has one operation, `read`. For the declared
accounts and a window, it returns:

- the completed and reverted credits;
- the returns related to them;
- whether the read was complete;
- whether its coverage is established.

The port has no write method, and the Revolut adapter issues GET requests only.
They go to a pinned host, with redirects refused. The accounts come from
configuration, never from a dispute. A read that fails raises
`BankUnavailable` with a fixed category, never with the bank's text.

**Its own types, never `Payment`.** `BankCredit`, `BankReturn` and `BankRead`
(in `app/domain/bank.py`) are observations. They feed no billing figure and no
duplicate-payment rule. The billing platform stays the only authority on
amounts.

**A deterministic check, before any model** (constitution I). The rule runs on
the disputed invoice when the platform says it is unpaid. It decides one of
four outcomes, in this order of precedence:

1. *not evaluable*: the read failed or was cut short, a page boundary was not
   proven clean, an account was not visible, no declared account is in the
   invoice's currency, or coverage is not established. An uncertain read is
   never "nothing".
2. *to reconcile*: a credit cites the invoice, but something is off. It may be
   reversed, have a related return, or be partial, over, in another currency,
   with FX or a fee. It may cite several invoices, or come from an unknown or
   internal origin. Several qualifying credits also count.
3. *observed*: exactly one qualifying credit and no exception. That credit is
   external, completed, of an allowed type, in the invoice's currency, for its
   exact total, and its reference cites this invoice only.
4. nothing: absence is never evidence of non-payment.

**Time.** A credit belongs to the window by its `completed_at`, in UTC, from
the invoice's issue to the investigation's frozen instant, both bounds
included. A reverted credit with no `completed_at`, created before the
window's end, is kept, and makes the outcome *to reconcile*. A payment made
before the invoice was issued is outside coverage: it is not looked for, and
the record says so.

A reference is read with the domain recogniser (`app/domain/references.py`).
It recognises only the invoices loaded for this customer. An invoice that is
not loaded cannot be recognised, so a match is a reference-based candidate,
not an established allocation, and the record says so.

**What the findings set off.** Either finding, and a bank-derived
`NOT_EVALUABLE`, does three things:

- it blocks every correction on the invoice (constitution II);
- it sends the case to a person;
- it allows no automatic reply (ADR-011).

Existing findings keep their precedence: a bank credit does not make an
invalid invoice valid. The engine is still consulted for the classification;
the finding settles the routing.

**What each channel sees** (constitution VII, ADR-009):

- *The engine* sees the finding's code only: no amount, no date, no reference,
  no payer. A single projection at `DecisionEngine.decide` applies this to
  every engine, the mock included.
- *The audit* keeps a closed allowlist, enforced by types: `BankCheck` refuses
  any field or reason outside it. The audit means the run row
  (`bank_check_json`), the run page and the review page. A record is kept for
  every run, whatever the outcome, `nothing` and `not_run` included. The
  allowlist holds:
  - the checked invoice's id, the outcome and its reason from a closed set;
  - the declared accounts' internal ids, currencies and visibility;
  - each credit's account id, transaction id, amount with its currency,
    completion instant, state and the instant it was observed;
  - each return's id, type, signed amount with its currency, related credit
    and labelled instant;
  - reasons from a closed set;
  - the window, the margin, completeness and coverage;
  - the limitations, said every time.
- *Nowhere*: payer names, account numbers, bank identifiers, descriptions, raw
  labels and references. The mapper reads them and the check uses them. After
  that they are dropped: a credit's reference is held in memory only, never
  serialised.

**Retrieval is not coverage.** Retrieval and coverage are judged separately:

- *Retrieval.* The read pages by `created_at` from a lower bound set a margin
  before the window: 7 days, by convention. A full page that ends on a shared
  instant makes the read incomplete.
- *Coverage.* A complete read can still miss a credit created before the lower
  bound and completed inside the window. Coverage is therefore established
  only in controlled worlds whose credits are all created inside the margin.
  Those are the seeded Sandbox and fixtures, checked rather than trusted.
  Elsewhere, the check answers *not evaluable* until Revolut states a maximum
  delay.

**Conventions until Revolut answers.** Two choices are conventions the
maintainer approved, to be revisited when Revolut answers:

- The credit types allowed are `topup` and `transfer`, and both need an
  `external` origin. The rule decides the origin; the mapper only reports it.
- The lookback margin is 7 days.

## Consequences

- The domain and application layers name no bank. Revolut's implementation
  stays behind the port in `app/providers/bank/`, its vocabulary translated in
  its mapper. A second bank is a new adapter behind the same port.
- An investigation without a bank configured behaves exactly as before.
  `app/api/bank_config.py` returns none, and the record says `not_run`.
- Disputes where the bank has something to say now go to a person rather than
  receiving an automatic answer. That costs review time, and it is accepted:
  an automatic answer built on bank evidence would be a claim the system
  cannot stand behind.
- No Production claim. The slice seeds incoming credits on the Revolut
  Business Sandbox through top-ups. Fixtures cover what this slice does not
  make on the Sandbox: an outside payer, a reversal of a completed credit, a
  separate return and a timestamp tie at a page boundary. Production needs:
  - Revolut's answers;
  - an established coverage rule;
  - the allowlist revisited;
  - the merchant's own key, with read scope only.
- The seed uses the Sandbox full-access key, because the API Console available
  to the maintainer offered no product limited to simulations. The seed
  confines that key's use (spec 016, FR-006):
  - it uses the pinned origin only, and refuses redirects;
  - it issues `POST /sandbox/topup` only, on the declared account only;
  - the key lives in `.env.seed`, which the service and the probe never load.
- A bank-derived `NOT_EVALUABLE` is a new way for a run to need a person. The
  evaluation cases cover each outcome, and the mock baseline moves only where
  bank evidence applies.
