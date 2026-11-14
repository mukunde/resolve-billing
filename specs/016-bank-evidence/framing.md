# Framing: spec 016, a bank's evidence that a payment arrived -- Revolut Business (v6)

Main 19b6130 (spec 015 merged). Sources: coordination decisions 4 (amended 2026-10-16) and
45 ("Qonto ensuite, sur son propre cadrage : nouveau port, amendement de l'article III,
règle, cas, divulgation des données bancaires de tiers"); README roadmap item 3;
.specify/memory/constitution.md (I, II, III, V, VII, VIII); ADR-003, ADR-009;
evals/taxonomy.md B14, B15. Revised after Codex r399-r404. **v4, the bank changed**: Qonto's Developer
Portal accepts only verified Qonto customers ("Sign-up by email is not available yet"), a
paid business account; the maintainer chose Revolut Business on 2026-11-13, whose Sandbox
is free and open to non-customers. Revolut facts from its official developer
documentation (developer.revolut.com, guides and `business.yaml`), read on 2026-11-13;
"unverified" marks what the pages did not establish. Everything below the documentation
section is unchanged in substance from v3, the vendor's names replaced.

## The product question, and the objective to choose

A customer writes "I paid by bank transfer, stop the reminders"; the billing platform shows
the invoice unpaid. Today (taxonomy B14) the rules have nothing to look at:
`claimed_payment_without_evidence` is `insufficient_information`, information requested;
`payment_already_received` (a dunning complaint whose money arrived) has no development
case and no rule. The merchant's bank holds a fact the rules lack.

Two objectives, of very different weight (question 1):
- **Observe a receipt, for a person** (recommended for this slice): a deterministic check
  says whether a credit that fits the invoice was posted on the merchant's account, and the
  case goes to a person with that evidence. Nothing is concluded about the invoice.
- **Establish that the invoice is settled**: allocate a bank credit to an invoice, follow
  its later reversal, and let the decision rest on it -- reconciliation, a much larger
  slice.

## What the documentation establishes (Revolut Business)

- **Access**: a Sandbox (`sandbox-business.revolut.com`) separate from Production; sign-up
  open to anyone, mock contact details accepted and not verified, approval automatic, no
  email or SMS (helper buttons stand in). API keys are created in the API Console with the
  products and permissions chosen; Sandbox accounts also come with pre-generated
  **full-access** keys, which this slice does not use.
- **Request signing**: a key with the Accounts product (Transactions: Read) is created with
  signing keys, and in Production every request with it must carry `Revolut-Api-Timestamp`
  and `Revolut-Api-Signature`; in Sandbox signing is optional, but signing headers, when
  sent, must be valid. An unsigned Sandbox probe therefore shows connectivity, not that the
  adapter signs correctly: that is tested on its own.
- **Transactions** (`GET /transactions`, scope `transactions:read`): reverse chronological,
  at most 1 000 per request, paginated by `created_at` (`from` inclusive, `to` exclusive);
  filters `account`, `type`, `state` (repeatable). A transaction has `id`, `type` (`atm`,
  `card_payment`, `card_refund`, `card_chargeback`, `card_credit`, `charge`,
  `charge_refund`, `exchange`, `transfer`, `loan`, `fee`, `refund`, `topup`,
  `topup_return`, `tax`, `tax_refund`, `rev_payment`), `state` (`created`, `pending`,
  `completed`, `declined`, `failed`, `reverted`), `created_at`, `updated_at`,
  `completed_at` (required when completed), `reference` ("the payment reference"),
  `related_transaction_id`, and `legs` -- one leg except between the merchant's own
  accounts -- each with `amount` (a JSON number), `currency`, `account_id`, and optionally
  `fee`, `bill_amount`, `bill_currency` and a counterparty (`account_type` `self`,
  `revolut` or `external`, ids). `reference` and the counterparty are optional.
- **Simulations, Sandbox only**: `POST /sandbox/topup` (scope `simulations:write`) credits
  an account with an amount (at most 10 000), a currency, a `reference` and a `state`
  (`pending`, `completed`, `reverted`, `failed`); `POST /sandbox/transactions/{id}/{action}`
  moves a transfer to `completed`, `reverted`, `declined` or `failed`. **Every resulting
  state is final**: a credit cannot be completed and later reverted in Sandbox, so a later
  reversal is never tested there; and top-ups have no outside payer.
- **Unverified, and gates before any build**: which `type` an incoming SEPA transfer from
  an outside payer carries in Production (`transfer` with an external counterparty, or
  `topup`): the Sandbox can only make `topup` credits, so the allowlist rests on
  Revolut's answer or a convention the maintainer approves; whether `reference` carries the
  payer's remittance text verbatim; how a reversal of a completed incoming credit appears
  -- the same transaction moving to `reverted`, or a separate return (`topup_return` or
  another type, a negative leg, `related_transaction_id` pointing at the credit, the
  reference perhaps not repeated); amounts
  as JSON numbers, parsed as decimals, never floats (IV); rate limits (not found in the
  pages read); how far back transactions are kept.

## The alternative, compared fairly

| | Revolut Business | Pennylane's matched transactions |
|---|---|---|
| Posting evidence (money reached the account) | yes, from the bank | yes, from Pennylane's bank feed |
| Allocation evidence (which invoice it pays) | none: our own heuristic on the reference | yes: Pennylane's or a person's match, reliability unverified |
| Reversals visible | state `reverted`; how a reversal of an incoming credit appears is unverified | unverified |
| New vendor, port, constitution amendment | yes | no (inside the billing platform's port) |
| Sandbox feasibility | top-ups with a reference and a state; no outside payer | matching in the sandbox unverified |

Stripe has no bank feed: its payments are its own collections, already read.

## The design proposed (objective "observe a receipt")

1. **A third port, read-only by construction**:    `BankEvidence` (name to settle), beside
   `BillingProvider` and `DecisionEngine`: the credits on declared accounts over a window,
   `completed` and `reverted` both, asked for explicitly by `state`, each with its state,
   `completed_at`, `created_at` and `updated_at`; window membership is decided by
   `completed_at`, normalised to UTC. A reverted credit is placed by its `completed_at`
   when it carries one (the field is required for completed transactions, optional
   otherwise); only when it carries none and its `created_at` falls before the window's end
   is it kept as potentially relevant (to reconcile), never silently dropped. The read pages
   by `created_at` from a lower bound set before the window opens by a margin the maintainer
   approves. **Retrieval and coverage are two things**: a read can be complete for the
   range it asked for and still miss a credit created before that range and completed
   inside the window, since no maximum creation-to-completion delay is documented. Until
   Revolut states one, coverage is not established: on Production data the check answers
   *not evaluable*; in the demonstration, on the Sandbox and fixtures whose worlds create
   every credit inside the margin, coverage holds by construction and is said so.
   **Completeness of the read**: Revolut continues a page with an exclusive `to` at
   the last item's `created_at`, with no documented tie-breaker; a full page whose last
   instant is shared by more than one item, or any page boundary the read cannot prove
   clean, makes the read *not evaluable*. **Returns**: until Revolut establishes how a
   reversal of an incoming credit appears, the port also returns, for the same window and
   accounts, the transactions whose `related_transaction_id` points at a credit it returns
   or whose `type` is a return (`topup_return`, `refund`), whatever their sign or reference;
   one related to a candidate makes it *to reconcile*. A fake on fixtures and a Revolut adapter, the vendor's vocabulary only in its
   mapper. Bank observations are their own type, never `Payment`: they never feed the
   duplicate-payment rule or any billing figure. Constitution III names two ports: an
   amendment, the maintainer's, with an ADR.
2. **The check, deterministic** (I), on the disputed invoice when it is unpaid on the
   platform:
   - **What counts**: a single-leg credit (positive leg amount) to a declared account,
     `state` completed as observed now (instant recorded), a `type` on an allowlist settled
     by Revolut's answer or the maintainer's convention (gate above), a counterparty present
     and `external` -- absent, `self` or `revolut` is an unknown or internal origin and never
     counts --, a `reference` present (absent: no candidate), the leg's own `currency` equal
     to the invoice's, no `bill_amount`/`bill_currency` differing from it (no FX), a `fee`
     absent or zero, and the leg's amount, parsed as a decimal, in minor units equal to the
     invoice's total including tax; no aggregation. A credit that cites the invoice but fails
     the FX or fee condition is *to reconcile*.
   - **Reference**: the credit's reference cites this invoice's number and no other invoice
     number among the customer's loaded invoices (spec 014's recogniser, reused through a
     domain-level function, not the vendor's mapper). That makes the credit a
     *reference-based candidate*, not an established allocation: a number outside the loaded
     invoices -- another customer's, an older one -- cannot be detected in the reference.
     The limitation is recorded with the finding and shown to the reviewer (question 3).
   - **Time**: `completed_at` from the invoice's issue date (spec 014's UTC convention) to the
     investigation's frozen instant, bounds inclusive. A payment before issue is **outside
     coverage** in this slice: not looked for, and said so (question 4).
   - **Outcomes**, in this order of precedence:
     1. a read that failed, was cut short, hit an unresolved page boundary, or whose
        coverage is not established → *not evaluable*, never "nothing";
     2. any *referenced exception* -- a credit citing this invoice that is reversed, of
        another amount (partial, over), in another currency, or citing several invoices; or
        several qualifying credits → *bank credit to reconcile*, a person's, even if one
        qualifying credit sits beside it;
     3. exactly one qualifying credit and no exception → *bank credit observed*, a
        reference-based candidate;
     4. nothing found in a complete read → no finding: absence is never evidence of
        non-payment, and the coverage limits (other accounts, other means, before issue)
        are said.
3. **What the findings set off**: never a correction -- like `NOT_EVALUABLE`, either finding
   blocks every correction on the invoice (II) -- and the verdict is human review, with no
   automatic reply. Existing findings keep their precedence (a duplicate invoice, a billing
   error: the bank credit does not make an invalid invoice valid, B15). The engine is still
   consulted for the classification, as it is for `SUSPICIOUS_USAGE_DELTA`: the finding
   settles the routing, not the customer's complaint. No write anywhere.
4. **Disclosure** (VII), three allowlists:
   - *to the engine*: the finding's code only -- no amount, date, reference, label or payer;
   - *audit* (run row, authenticated run page and review page, both showing it): for each
     credit taken into account, the bank account's internal id, the transaction id, amount
     and currency, `completed_at`, state observed and the instant it was observed; the invoice
     it was checked against; outcome-specific reasons from a closed set (reference matched,
     amount equal / partial / over, currency differs, reversed, several invoices cited,
     several candidates, FX or fee present, a related return found, origin unknown or
     internal); for a return, its own id, type, signed amount, currency and the id of the
     credit it relates to; the completion instant when present, the creation instant
     otherwise, labelled as such; the window, the lookback margin, whether the read was
     complete and whether coverage is established; and the
     limitations, said every time -- allocation unverified beyond the loaded invoices,
     coverage (declared accounts, from issue on), a later reversal not followed. Never the
     raw reference, label or counterparty fields;
   - *nowhere*: counterparty name, account number, bank identifier, raw label and reference
     -- read by the mapper, used by the check, then dropped. Bank text stays untrusted (VII).
   ADR-009's channel inventory gains this source: API answers, pages, emails, logs, traces.
5. **Access and safety**: a GET-only adapter (it never issues another method); a key of
   its own created with **Transactions: Read and Accounts: Read only**, never the
   pre-generated full-access key; the Sandbox base URL pinned, redirects refused, the
   declared accounts checked on every read; no arbitrary URL; no model tool on bank data
   (V). The key and its signing private key are put in `.env` (the private key as a file
   path) by the maintainer, loaded with `python -m dotenv run`, never printed, logged or
   put in an exception; agents do not read `.env` (the standing rule, not a technical
   barrier). Credits for the worlds are made by a seed with a second key, `simulations:write`
   only, kept apart as Pennylane's seed token is. A probe that prints structure only,
   reviewed before it runs, decides the gates above.
6. **Evaluation and contracts** (VIII). What each level shows, kept apart: the Sandbox
   shows top-up credits with a chosen reference and a final state, read back through the
   real adapter; fixtures alone show a later reversal, a separate return, an outside
   payer's transfer and pagination ties; Production's incoming-transfer type, reference
   fidelity and reversal shape stay assumptions until Revolut answers. Development cases with
   labels the maintainer adjudicates -- a claim with a matching credit (to a person, the credit observed); the B14
   positive boundary, a payment already recorded on the platform, with the same credit at
   the bank (no double counting, no duplicate payment); an amount-only credit; a reference
   to two invoices; a partial payment; another currency; a reversed credit; a cut-short read;
   a duplicate invoice with a bank credit (the duplicate keeps precedence); no credit
   (`insufficient_information`, as today); and the engine payload checked for bank data.
   The taxonomy row, the mock baseline moved case by case, a live run only with its cost
   shown first. Contracts, written or amended in the same pull request (the constitution's
   "coverage is declared"): a first contract for *bank evidence*; a first contract for the
   *investigation and its rules* (the new check, its precedence, corrections blocked),
   since that capability has none yet; the *decision engine* contract (what its payload
   holds of bank evidence); *approval and execution* (corrections blocked, the review page's
   evidence); *evaluation* (the new cases and the baseline); a first contract for
   *disclosure and replies* (listed as uncovered in docs/contract/README.md), holding the
   three allowlists and every channel they touch; and ADR-009's disclosure inventory.

## Questions for the maintainer

1. **The objective**: (a) observe a receipt for a person, no conclusion about the invoice
   (recommended); (b) establish settlement -- reconciliation, a later and larger slice.
2. **The source** (decided 2026-11-13: Revolut Business): (a) the bank's posting, a new port;
   (b) Pennylane's matched transactions, an allocation already made, no new vendor; (c) both,
   later.
3. **Allocation**: a credit citing this invoice's number and no other loaded invoice is a
   reference-based candidate, its allocation stated as unverified beyond the loaded
   invoices (recommended); are invoice numbers unique across the merchant, or only per
   customer?
4. **Scope**: partial, combined and over-payments, fees and other currencies, when the
   credit cites the invoice, are "to reconcile" (a person's), never matched; payments
   before the invoice's issue are outside coverage, not looked for (recommended).
5. **Third-party data**: the engine receives the finding's code only (recommended); the
   audit keeps the allowlist above.
6. **Access**: will you create a Revolut Business Sandbox account (free, mock details), and
   in the API Console two keys -- one Transactions and Accounts Read only, one Sandbox
   simulations Write only -- putting the first with its signing key in `.env` and the second
   in `.env.seed`? May I then write a read-only probe, reviewed before it runs?
6b. **What Revolut must answer**: may I draft the question to Revolut (which `type`, counterparty
   and reference an incoming SEPA transfer carries; how its reversal appears; whether
   `created_at` is unique for pagination; the longest creation-to-completion delay), for you
   to send? Until answered, the allowlist and the lookback margin are conventions for you to
   approve, and this slice is a demonstration on the Sandbox and fixtures, not a Production
   claim -- do you accept that scope?
7. **The constitution**: approve amending article III to name a third port for bank
   evidence, with its ADR.
