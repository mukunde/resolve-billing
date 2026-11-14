# Implementation Plan: A bank's evidence that a payment arrived

**Branch**: `016-bank-evidence` | **Date**: 2026-11-14 | **Spec**: [spec.md](spec.md)
(approved at `a0d4307`; conventions approved at `c3e0baa`) | **Framing**: [framing.md](framing.md)

**Status**: Draft, for the maintainer's approval (Codex r418 answered)

## Summary

A third port, read-only, gives the merchant's completed and reverted bank credits (and the
returns related to them) on declared accounts over a window; a deterministic check on the
disputed invoice turns them into one of two findings that block corrections and route to a
person; the engine sees the finding's code only; the check's record is kept whatever it
found. Revolut Business is the first adapter, on its Sandbox; a fake serves tests and
evaluation. In order:

1. **The foundation** (one commit, here): the domain types, the port, the reference
   recogniser moved to the domain, the investigator's optional bank argument and its
   configuration contract, the fixture schema. Every suite passes.
2. **Side by side**, each in its own session and worktree branched from the foundation, on
   disjoint files: (a) the adapters -- the fake reader, the Revolut reader, the seed, the
   composition in `deps.py`; (b) the check -- the rule, the investigation's use of it, the
   policies, the engine boundary, the audit record, the pages, the evaluation cases.
3. **Here**: the constitution, the ADR, the contracts, the Sandbox run (SC-002), the gates,
   the whole diff reviewed.

## Constitution check

- I: the check is code, before any model; the engine is still consulted for the
  classification, the finding having settled the routing.
- II: either finding, and a bank-derived `NOT_EVALUABLE`, blocks every correction on the
  invoice; nothing writes.
- III: the domain and application name no bank; Revolut's vocabulary stays in its mapper.
  The article names two ports today: **amended** in this pull request to name the third
  (MINOR version bump, dated rationale), with ADR-014.
- IV: amounts in minor units; Revolut's JSON numbers parsed as decimals, never floats.
- V: no tool, no model call on bank data; a code-only projection at the engine boundary.
- VII: bank text untrusted; payer names, account numbers, descriptions, raw references and
  labels read by the mapper, used by the check, then dropped; the audit holds a closed
  allowlist; ADR-009's inventory and a first *disclosure and replies* contract.
- VIII: new development cases with adjudicated labels; the baseline moved case by case only
  where bank evidence applies; a live run only with its cost shown first.

## 1. The foundation

- `app/domain/bank.py`:
  - `DeclaredAccount` (internal id, currency, visible: bool);
  - `BankCredit` (account id, transaction id, type, state, observed at, amount `Money`,
    completed at or None, created at, reference text or None -- in memory for the check,
    never serialised --, origin `external` / `internal` / `unknown`, `fx: bool` -- a
    `bill_currency` or a `bill_amount` differing from the leg's --, `fee: bool`);
  - `BankReturn` (id, type, signed amount `Money`, related credit id or None);
  - `BankRead` (accounts, credits, returns, window, margin, complete, coverage_established,
    incompleteness reasons from a closed set);
  - `BankCheck` -- the record kept whatever the outcome: window, margin, accounts (id,
    currency, visible), complete, coverage, the outcome (`observed`, `to_reconcile`,
    `not_evaluable`, `nothing`, `not_run` with why), the per-credit and per-return
    allowlisted fields and reasons, the limitations; `to_record()` and `from_record()`.
- `app/domain/references.py`: spec 014's recogniser (whole, case-folded, bounded, longest
  first) moved here from `app/providers/pennylane/mapping.py`, which imports it; a function
  `cited_invoices(text, invoices) -> set[invoice id]` over the **loaded invoices only**,
  matching each one's printed `number` and its id, so a bank reference is read the same way
  on every platform (the fake, Stripe and Pennylane hooks are not used for bank text).
- `app/providers/bank_base.py`: the port `BankEvidence` -- `name`, `async read(window,
  margin, now) -> BankRead`, the declared accounts from configuration, never from a dispute;
  `BankUnavailable` for a read that failed.
- `app/domain/enums.py`: `RuleCode.BANK_CREDIT_OBSERVED`, `RuleCode.BANK_CREDIT_TO_RECONCILE`.
- `app/application/investigate_dispute.py`: the constructor takes `bank: BankEvidence | None
  = None`, stored, unused yet; `InvestigationResult.bank_check: BankCheck | None = None`.
- `app/api/bank_config.py`: `bank_evidence() -> BankEvidence | None`, returning None until
  section 2a fills it; `deps.py` passes it to the investigator.
- Fixture schema frozen: `app/providers/fake/fixtures/bank.json` (an empty, valid file),
  documented in the module: accounts (id, currency, visible), credits and returns keyed by
  account, an `incomplete` switch and a `tie` case per account.
- Ownership after it: section 2a owns `app/providers/bank/`, `app/providers/fake/bank.py`,
  `app/api/bank_config.py`, `scripts/revolut_seed.py`, `.env.example`'s bank lines and their
  tests; section 2b owns the rule in `app/domain/rules.py`, `app/application/`,
  `app/policies/`, `app/decision/` (the engine boundary), `app/infrastructure/audit.py`,
  `app/api/run_view.py`, `app/api/review.py`, the templates, `evals/`,
  `app/providers/fake/fixtures/bank.json` and their tests.

## 2a. The adapters

- **Fake** (`app/providers/fake/bank.py`): reads `bank.json`; honours `incomplete` and `tie`.
- **Revolut reader** (`app/providers/bank/revolut.py`, `mapping.py`, `client.py`):
  - GET only, pinned Sandbox host, redirects refused; every request signed (Ed25519;
    timestamp + method + path + query exactly as sent + body; Base64), tested on a fixed
    vector; the declared accounts read from `/accounts` on every read (visibility, currency);
  - paging `/transactions?account=…&from=…&to=…&count=…` by `created_at` from the window's
    start minus the margin; `to` moved to the last item's `created_at`; a full page whose last
    `created_at` is shared by more than one item, or a boundary not proven clean, makes the
    read incomplete; 60 requests per minute respected, within a time budget, else incomplete;
  - mapping: single-leg credits to a declared account, positive amount; `type` on the
    allowlist (`topup`; `transfer` with an `external` counterparty); origin from the
    counterparty (absent → unknown; `self`, `revolut` → internal); `fx` and `fee` as above;
    returns: a `related_transaction_id` pointing at a credit read, or a type `topup_return` /
    `refund`; amounts with `parse_float=Decimal`; `description`, counterparty ids, merchant
    and card fields never kept; a malformed answer → `BankUnavailable` with a fixed category,
    never the platform's text;
  - coverage: established only on the Sandbox host (its worlds created inside the margin);
    elsewhere false until Revolut states a maximum delay.
- **Seed** (`scripts/revolut_seed.py`): only `POST /sandbox/topup`, only the declared
  account, pinned host, no redirects, the seed key from `.env.seed`, never printed; one-shot
  writes with intents as the Pennylane seed; worlds: a top-up citing an invoice number for
  its total, one for part of it, one `reverted`, one citing another number.
- **Composition** (`app/api/bank_config.py`): `BANK_EVIDENCE=revolut` with
  `REVOLUT_API_KEY`, `REVOLUT_SIGNING_KEY_PATH`, `REVOLUT_SANDBOX_ACCOUNT_ID`; unset → None.

## 2b. The check and its wiring

- **The rule** (`app/domain/rules.py`, `bank_credit_check(invoice, read, invoices, now) ->
  (RuleFinding | None, BankCheck)`), pure:
  - not evaluable when the read is incomplete, coverage is not established, a declared
    account is not visible, or none is in the invoice's currency;
  - `cited_invoices(reference, invoices)` over the customer's loaded invoices;
  - outcomes in precedence: `NOT_EVALUABLE` (evidence `{"check": "bank", …}`); a referenced
    exception (reverted; a related return; partial; over; another currency; FX; fee; several
    invoices cited; unknown or internal origin; several qualifying credits) →
    `BANK_CREDIT_TO_RECONCILE`; exactly one qualifying credit → `BANK_CREDIT_OBSERVED`; else
    no finding, the record saying `nothing`;
  - window: `completed_at` from the invoice's issue to the frozen instant, inclusive; a
    reverted credit with no `completed_at` created before the window's end is kept.
- **Where it runs** (`app/application/investigate_dispute.py`): **immediately before the
  engine is called**, after every existing short-circuit (prompt injection, automated
  response, message too long, invoice-reference conflict, the findings sent to a person).
  A run that short-circuits never reads the bank, and its record says `not_run` with the
  reason; a dispute whose invoice is paid on the platform, or with no bank configured, says
  `not_run` too. `BankUnavailable` → `NOT_EVALUABLE` (bank), the investigation continuing.
  The bank finding joins the findings for the policies and the audit.
- **The engine boundary**: a single projection, applied to what is passed to
  `DecisionEngine.decide` (every engine, the mock included): a bank finding, and a
  bank-derived `NOT_EVALUABLE`, become their code with no evidence and a fixed generic
  description; the originals stay for the policies, the audit and the pages. Deterministic
  explanations that reach API answers or notifications name the code, never bank values.
- **Policies**: the two bank findings and a bank-derived `NOT_EVALUABLE` block every
  correction (`app/policies/correction.py`) and force human review with no automatic reply
  (`app/policies/action_policy.py`, `app/policies/reply.py`); existing blocking findings keep
  their precedence.
- **The record** (`app/infrastructure/audit.py`): a column `bank_check_json` on the run row,
  the `BankCheck` allowlist, written for every run (`not_run` included); a migration adding
  it; round-trip tests. Traces carry the outcome code only.
- **Pages**: the run page shows the bank check -- outcome, allowlisted fields, reasons,
  window, completeness, coverage, limitations. Runs with a bank finding have no action (their
  corrections are blocked), so they have no review page: the run page, authenticated, is
  where a person reads the evidence, linked from the dispute's answer as today. A review page
  that exists (an action on a run whose bank check found nothing) shows the record too.
- **Evaluation** (`evals/`): `bank.json` fixtures; cases per SC-003, labels to adjudicate by
  the maintainer before the baseline moves; the runner gives each case the fake bank; the
  taxonomy row (B14 with bank evidence).

## 3. Docs, constitution, Sandbox run

- Constitution III amended (MINOR, dated, the reason); ADR-014 "A bank as evidence, behind a
  read-only port"; ADR-009's disclosure inventory; contracts: a first *bank evidence*, a
  first *investigation and rules*, a first *disclosure and replies*, amendments to *decision
  engine*, *approval and execution*, *evaluation*; `docs/contract/README.md`'s coverage
  list; operations and `.env.example`.
- The Sandbox run (SC-002): seed the four top-ups, read them back through the real adapter,
  signed, and record the outcomes with what the Sandbox could not show.

## Tests (without network) -- by requirement

| Requirement | Tests |
|---|---|
| FR-001 | the types; returns read; accounts' visibility and currency; completeness and coverage carried |
| FR-002 | GET only; host pinned; redirects refused; declared accounts checked; the signature on a fixed vector; decimals; a page-boundary tie → incomplete; the rate budget → incomplete; a malformed or failed answer → `BankUnavailable`, a fixed category |
| FR-003 | each outcome and its precedence; each exception reason (FX by billed amount and by billed currency); the window bounds; a reverted credit without `completed_at`; an invisible account and a currency mismatch with zero transactions → not evaluable; a reference citing an unloaded invoice is not a second citation |
| FR-003a/b | the margin; coverage not established off the Sandbox → `NOT_EVALUABLE`; established on the fake by construction |
| FR-004 | corrections blocked by each bank code and by a bank-derived `NOT_EVALUABLE`; human review; no automatic reply; each short-circuit runs before the bank and records `not_run`; a duplicate invoice keeps its precedence; no bank configured → unchanged |
| FR-005 | a capturing engine, the mock and the hosted request payloads hold the code only; the run row's `bank_check_json` round-trips the allowlist; the run page shows it; logs, traces and exceptions carry no bank text |
| FR-006 | the seed's restrictions, one by one |
| FR-007 | the docs present (reviewed) |
| SC-003 | the new cases; the baseline unchanged elsewhere |

## Risks

- Revolut's answers may change the type allowlist, the reversal model or the coverage rule:
  each is one constant or one branch, said in the contract.
- The fake must not encode what the Sandbox contradicts: the Sandbox run is the check.
- The 60 requests per minute are per business, shared with anything else using the key.
- Moving the recogniser touches Pennylane's mapper: its tests must pass unchanged.
