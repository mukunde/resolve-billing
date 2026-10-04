# ADR-007 — An unknown sender gets a result, not an error

**Status:** accepted · 2026-10-04

## Context

A dispute arrives from an address that matches no customer. Until now the
investigation raised, and the API answered 404: the run left no audit row,
the evaluation runner could not even score such a case, and nobody looked at
what the message was asking for.

Most unknown addresses are innocent — a colleague in accounts payable, a new
employee, a personal mailbox. Some are not. Writing from an address nobody
can check, about an account one does not hold, is how invoice and bank-detail
fraud starts. The two call for opposite answers: a question for the first, a
human for the second. Treating every unknown sender as a fraud would bury the
real attempts under colleagues; treating none of them as one would let a
stranger talk about someone else's invoices.

## Decision

An unknown sender is a result. It stops the run before the billing context is
loaded: there is no customer to load it for, and the engine is never called.
The injection screen still runs first, on the raw text, and still wins.

Two deterministic rules then split the cases (ADR-003):

- `UNKNOWN_CUSTOMER` always fires. On its own, the system asks the sender to
  write from the address we know: `request_information`, authorised by the
  policy like any other question. The reply is built from the findings alone
  and names no customer — confirming that an account or an invoice exists is
  itself a leak.
- `SUSPECTED_IMPERSONATION` fires when the message reaches for a known
  customer's account. It is a blocking finding: nothing is proposed and a
  human reviews. The human contacts the customer through the known channel
  only, and decides what, if anything, the unknown sender is told.

The signals, any one of which is enough:

| Signal | Fires when |
|---|---|
| `known_invoice` | the dispute cites an invoice we hold |
| `same_domain` | the sender writes from a customer's domain or a subdomain of it |
| `lookalike_domain` | the sender's domain imitates a customer's: equal once lookalike letters are folded (`acrne.fr`), another extension (`acme.com`), the name decorated (`acme-fr.com`), or one letter off a name of six or more |
| `company_name` | the message names a customer, as a whole word, ignoring case, accents and legal form |

`same_domain` escalates on purpose. It is most often a colleague, and a
human confirming that with the known contact costs one review. Answering a
stranger about an account because their address ends the right way costs
more. Webmail domains carry no identity and are never compared.

What the rules found about the sender is its own axis on the result:
`sender` is `known`, `unknown` or `suspected_impersonation`. It is not a
`Classification`. The classification says what a dispute is about; the
sender says whether we know who is asking, and the two vary independently.
Folding the second into the first would also hand it to the engine, whose
output schema is built from the classification enum — and the engine knows
nothing about identity, which the rules settle before it is called. So the
classifications stay as they are: `insufficient_information` when the
sender is asked for the known address, `unknown` — not classified, engine
not called — when a human reviews. The API returns `sender`, the audit
stores it, and evaluation cases can expect it (`expect_sender`, `known` when
a case does not say).

The provider port gains two reads: `find_invoice`, which returns `None`
rather than raising, and `list_customers`, which the comparison needs.
`Customer` gains an optional `display_name`.

## Consequences

The 404 is gone. Every dispute with an address now produces an audited run,
and the evaluation set can carry unknown-sender cases.

The evaluation runner holds the decision whatever a case expects: a credit
note or refund proposed to a sender who is not `known` is unsafe even with
approval, and so is any action other than `none` towards a suspected
impersonator. Like every unsafe action, either fails the build.

`list_customers` is cheap on the fake and not on a real platform: paging
every Stripe customer on each dispute is not an option. A real adapter
serves it from a cached index of names and domains, refreshed out of band.
The port says so; the first real adapter has to honour it.

The lookalike check is a heuristic with named limits. It compares the label
in front of the extension, so a lookalike hidden in a subdomain of another
domain (`acme.example-mail.com`) passes, as do multi-part extensions
(`.co.uk`) and internationalised homoglyphs. So does a domain that keeps the
customer's first word and changes the rest (`acme-payments.fr` for
`acme-industries.fr`): matching on a shared first word was considered and
rejected, because first words like a city or `groupe` are shared by
unrelated companies and would flag them. Such a sender is caught only by
another signal — a known invoice, the company name, or the exact domain. The
check is tuned for recall on the plain cases, and every threshold has a test
on each side.

A company name made of common words matches more than it should. The name
check is a whole-word match, not a guess, and its false positives cost one
review each.

The MCP server's context and rules tools still require a known customer:
they are read tools for a caller who already knows whom it is asking about.
