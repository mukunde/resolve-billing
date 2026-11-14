# Implementation Plan: A bank's evidence that a payment arrived

**Branch**: `016-bank-evidence` | **Date**: 2026-11-14 | **Spec**: [spec.md](spec.md)
(approved at `a0d4307`; conventions approved at `c3e0baa`) | **Framing**: [framing.md](framing.md)

**Status**: Draft, for the maintainer's approval

## Summary

A third port, read-only, gives the merchant's completed and reverted bank credits (and the
returns related to them) over a window; a deterministic check on the disputed invoice turns
them into one of two findings that block corrections and route to a person; the engine sees
the finding's code only. Revolut Business is the first adapter, on its Sandbox; a fake serves
tests and evaluation. In order:

1. **The foundation** (one commit, here): the domain types and the port.
2. **Side by side**, each in its own session and worktree branched from the foundation:
   (a) the adapters -- the fake, the Revolut reader, the seed; (b) the check and its wiring --
   the rule, the investigation, the policies, the engine payload, the pages, the evaluation
   cases. Disjoint files; both build on the foundation only.
3. **Here**: the constitution, the ADR, the contracts, the Sandbox run (SC-002), the gates,
   the whole diff reviewed.

## Constitution check

- I: the check is code, before any model; the engine is still consulted for the
  classification, the finding having settled the routing.
- II: either finding blocks every correction on the invoice; nothing writes.
- III: the domain and application name no bank; Revolut's vocabulary stays in its mapper.
  The article names two ports today: **amended** in this pull request to name the third
  (MINOR version bump, dated rationale), with ADR-014.
- IV: amounts in minor units; Revolut's JSON numbers parsed as decimals, never floats.
- V: no tool, no model call on bank data; the engine's payload carries the finding's code.
- VII: bank text untrusted; payer names, account numbers, descriptions, raw references and
  labels read by the mapper, used by the check, then dropped; the audit holds a closed
  allowlist; ADR-009's inventory and a first *disclosure and replies* contract.
- VIII: new development cases with adjudicated labels; the baseline moved case by case only
  where bank evidence applies; a live run only with its cost shown first.

## 1. The foundation

- `app/domain/bank.py`: `BankCredit` (account id, transaction id, type, state, observed at,
  amount `Money`, completed at or None, created at, reference text or None -- kept in memory
  for the check, never serialised --, origin `external` / `internal` / `unknown`, fee and
  FX flags), `BankReturn` (id, type, signed amount, currency, related credit id or None),
  `BankRead` (credits, returns, window, margin, complete: bool, coverage_established: bool,
  reasons for incompleteness from a closed set).
- `app/providers/bank_base.py`: the port `BankEvidence` -- `name`, `async read(accounts,
  window, margin, now) -> BankRead`; declared accounts come from configuration, never from a
  dispute. A `BankUnavailable` exception for a read that failed (the check answers *not
  evaluable*, never an outage of the investigation).
- `app/domain/enums.py`: `RuleCode.BANK_CREDIT_OBSERVED`, `RuleCode.BANK_CREDIT_TO_RECONCILE`;
  the existing `NOT_EVALUABLE` reused for an incomplete read or unestablished coverage, its
  evidence naming the bank check.
- Nothing else changes; every suite passes. After it, section 2a owns `app/providers/bank/`,
  `app/providers/fake/bank*`, `scripts/revolut_seed.py`, `app/api/deps.py`'s bank branch and
  their tests; section 2b owns `app/domain/rules.py`'s bank check, `app/application/`,
  `app/policies/`, `app/decision/anonymise.py`, `app/api/run_view.py`, `app/api/review.py`,
  the templates, `evals/` and their tests.

## 2a. The adapters

- **Fake** (`app/providers/fake/bank.py`): reads `bank.json` fixtures (credits, returns, an
  "incomplete" switch per account, a timestamp-tie case) for tests and evaluation.
- **Revolut reader** (`app/providers/bank/revolut.py`, `mapping.py`, `client.py`):
  - GET only, pinned Sandbox host, redirects refused; every request signed (Ed25519,
    timestamp + method + path + query + body, Base64); the declared account checked on every
    read through `/accounts`;
  - paging `/transactions?account=…&from=…&to=…&count=…` by `created_at` from the window's
    start minus the margin, exclusive `to` moved to the last item's `created_at`; a full page
    whose last `created_at` is shared by more than one item, or a boundary not proven clean,
    makes the read incomplete; 60 requests per minute per business respected, within a time
    budget, else incomplete;
  - mapping: single-leg credits to the declared account with a positive amount; `type` on
    the allowlist (`topup`; `transfer` with an `external` counterparty); origin from the
    counterparty (absent → unknown, `self`/`revolut` → internal); FX when `bill_currency`
    differs; fee when `fee` is non-zero; returns: a `related_transaction_id` pointing at a
    credit read, or a type `topup_return` / `refund`; amounts parsed with `parse_float=Decimal`;
    `description`, counterparty ids, merchant and card fields never kept;
  - coverage: established only for the Sandbox (configuration says so, by construction of
    the worlds); on any other host, `coverage_established` is false until Revolut states a
    maximum delay.
- **Seed** (`scripts/revolut_seed.py`): only `POST /sandbox/topup`, only the declared
  account, pinned host, no redirects, the seed key from `.env.seed`, never printed; one-shot
  writes with intents as the Pennylane seed; worlds: a top-up citing an invoice number for
  its total, one for part of it, one `reverted`, one citing another number.
- **Wiring** (`app/api/deps.py`): `BANK_EVIDENCE=revolut` with `REVOLUT_API_KEY`,
  `REVOLUT_SIGNING_KEY_PATH`, `REVOLUT_SANDBOX_ACCOUNT_ID`; unset → no bank evidence, the
  investigation unchanged.

## 2b. The check and its wiring

- **The rule** (`app/domain/rules.py`, `bank_credit_check(invoice, read, cited, now)`): pure,
  no I/O. `cited(text) -> set[invoice_id]` is the billing provider's own
  `invoice_references(text, invoices)` over the customer's loaded invoices (spec 014's
  recogniser on Pennylane, ids on the fake and Stripe), so the bank never names a platform.
  Outcomes in precedence: incomplete read or coverage not established → `NOT_EVALUABLE`; a
  referenced exception (reverted, a related return, partial, over, another currency, FX, fee,
  several invoices cited, unknown or internal origin, several qualifying credits) →
  `BANK_CREDIT_TO_RECONCILE`; exactly one qualifying credit → `BANK_CREDIT_OBSERVED`; else
  nothing. Window: `completed_at` from the invoice's issue to the frozen instant, inclusive;
  a reverted credit with no `completed_at` created before the window's end is kept.
- **Evidence**: exactly the audit allowlist -- per credit: internal account id, transaction
  id, amount, currency, completion or creation instant (labelled), state and observed-at;
  per return: id, type, signed amount, currency, related credit id; reasons from the closed
  set; window, margin, complete, coverage; the limitations (payer not verified, a reference
  may be mistyped, allocation unverified beyond the loaded invoices, other accounts and means
  not covered, a later reversal not followed). Never reference text, description or
  counterparty.
- **Investigation** (`app/application/investigate_dispute.py`): an optional `bank` port in
  the constructor; when set and the disputed invoice is unpaid, read the declared accounts
  and run the check after the existing rules, before the engine; `BankUnavailable` →
  `NOT_EVALUABLE`, the investigation continues.
- **Policies**: both bank findings join the set that blocks corrections
  (`app/policies/correction.py`) and force human review with no automatic reply
  (`app/policies/action_policy.py`), the way `SUSPICIOUS_USAGE_DELTA` does; existing
  blocking findings keep their precedence.
- **Engine payload** (`app/decision/anonymise.py`): a bank finding is sent as its code only,
  its evidence dropped; a test asserts no bank field reaches any engine's payload.
- **Pages**: the run page and the review page show the bank evidence's allowlisted fields,
  reasons and limitations, authenticated as today.
- **Evaluation** (`evals/`): `bank.json` fixtures; cases per SC-003 (labels to adjudicate
  by the maintainer before the baseline moves); the runner gives each case the fake bank;
  the taxonomy row (B14 with bank evidence).

## 3. Docs, constitution, Sandbox run

- Constitution III amended (MINOR, dated, the reason); ADR-014 "A bank as evidence, behind a
  read-only port"; ADR-009's disclosure inventory; contracts: a first *bank evidence*, a
  first *investigation and rules*, a first *disclosure and replies*, amendments to *decision
  engine*, *approval and execution*, *evaluation*; `docs/contract/README.md`'s coverage list;
  operations and `.env.example`.
- The Sandbox run (SC-002): seed the four top-ups, read them back through the real adapter,
  signed, and record the outcomes with what the Sandbox could not show.

## Tests (without network) -- by requirement

| Requirement | Tests |
|---|---|
| FR-001 | the port's types; returns read; a read's completeness and coverage carried |
| FR-002 | GET only; host pinned; redirects refused; declared account checked; signature built exactly as documented (fixed vector); decimals; a page-boundary tie → incomplete; rate budget → incomplete |
| FR-003 | each outcome and its precedence; each exception reason; the window bounds; a reverted credit without `completed_at` |
| FR-003a/b | the margin; coverage not established off the Sandbox → `NOT_EVALUABLE`; established on the fake by construction |
| FR-004 | corrections blocked; human review; no automatic reply; a duplicate invoice keeps its precedence; no bank evidence configured → unchanged |
| FR-005 | the engine payload holds the code only; the run row, run page and review page hold the allowlist; logs and exceptions carry no bank text |
| FR-006 | the seed's restrictions, one by one |
| FR-007 | the docs present (reviewed) |
| SC-003 | the new cases; the baseline unchanged elsewhere |

## Risks

- Revolut's answers may change the type allowlist, the reversal model or the coverage rule:
  each is one constant or one branch, said in the contract.
- The fake must not encode what the Sandbox contradicts: the Sandbox run is the check.
- The 60 requests per minute are per business, shared with anything else using the key.
