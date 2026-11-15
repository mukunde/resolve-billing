# Bank evidence

What any bank behind the `BankEvidence` port must do, what the check on its
reads concludes, and what the rest of the system may rely on. Written on
2026-11-15 with spec 016 (specs/016-bank-evidence), the change that creates
the capability. Reasons are in constitution III (1.1.0), ADR-014 (a bank as
evidence) and ADR-009 ("A bank as a source of evidence").

Two implementations exist: the **fake**, on named worlds in
`fixtures/bank.json`, what CI and the evaluation run on; and **Revolut
Business**, on its Sandbox only, selected with `BANK_EVIDENCE=revolut`. With
no bank selected -- the default -- the investigation runs exactly as before,
and every run records that the check did not run (`not_run`, `no_bank`).

## What the system relies on

- **Read-only, by construction.** The port has one operation,
  `read(window_start, window_end, margin_days, now)`, and no write. The
  Revolut adapter issues GET requests only, to the pinned Sandbox host
  (`sandbox-b2b.revolut.com`), over HTTPS, and refuses redirects. Every
  request is signed with the configured Ed25519 key, as Production requires.
- **Declared accounts only.** The accounts read come from configuration
  (`REVOLUT_SANDBOX_ACCOUNT_ID`), never from a dispute. Each read asks the
  bank for its accounts first: a declared account it does not show is not
  read, and the read is incomplete (`account_not_visible`).
- **Observations, never payments.** A read returns `BankCredit`s and
  `BankReturn`s, their own types: never a `Payment`, never fed to a billing
  figure or to the duplicate-payment rule. The billing platform stays the
  authority on amounts.
- **What a credit carries.** Account, transaction id, type, state, the
  instant it was observed, amount in minor units with its currency (Revolut's
  JSON numbers read as decimals, never floats), creation and completion
  instants, origin (`external`, `internal`, `unknown` -- the mapper reports
  it, the rule decides what it means), whether a billed amount or currency
  differs (FX), whether a fee was taken, and its reference. The reference is
  held in memory for the check and never serialised; counterparty names,
  account numbers, descriptions, merchant and card fields are not kept at
  all.
- **Returns.** A transaction that names a credit read
  (`related_transaction_id`), or whose type is a return (`topup_return`,
  `refund`), is read as a return, whatever its sign or reference, with its
  own creation and completion instants.
- **Retrieval is not coverage.**
  - The read pages by creation instant, from the window's start minus the
    margin to the window's end. A full page whose last instant is shared, or
    whose boundary a second, one-millisecond request does not prove clean,
    makes the read incomplete (`page_boundary_tie`). A request budget or a
    time budget spent makes it incomplete (`budget_spent`).
  - Coverage -- that no credit created before the read's lower bound could
    complete inside the window -- is established only when the configuration
    declares the worlds controlled (`REVOLUT_CONTROLLED_WORLDS=enabled`, the
    seeded Sandbox run alone) **and** nothing read was created before the
    lower bound. The fake establishes it for a world marked `controlled` in
    which nothing it leaves out completed inside the read or lacks a
    completion instant. Elsewhere coverage is not established, and the check
    answers *not evaluable*.
- **Failures by category.** A read that fails raises `BankUnavailable` with
  a fixed category (`malformed_answer` and the transport's), never the
  bank's text. The investigation records *not evaluable*
  (`bank_unavailable`) and goes on.
- **Configuration fails visibly, by name.** An unknown `BANK_EVIDENCE` value
  refuses the start. When Revolut is selected, so do a missing variable, an
  unreadable signing key, or a non-empty seed key (`REVOLUT_SEED_KEY`) in the
  service's environment -- each by its category, never a value.

## The check

`bank_credit_check(invoice, read, invoices, now)` (`app/domain/rules.py`),
pure, runs on the disputed invoice when the platform says it is unpaid.

- **When it runs.** Immediately before the engine is called, after every
  short-circuit (`investigation-and-rules.md`). It does not run -- and the
  record says `not_run` with why -- when no bank is configured (`no_bank`),
  no invoice is designated (`no_invoice`), the invoice is paid on the
  platform (`paid_on_platform`), or the run stopped earlier
  (`short_circuit`).
- **The window.** A credit belongs to it by its completion instant, in UTC,
  from the invoice's issue to the investigation's frozen instant, both
  bounds included. A reverted credit with no completion instant, created
  before the window's end, is kept. The margin is 7 days, a convention until
  the bank states a maximum creation-to-completion delay.
- **What is taken into account.** A credit of an allowed type (`topup`,
  `transfer` -- a convention until Revolut answers), completed or reverted,
  on a declared account, in the window, whose reference cites this invoice
  among the customer's loaded invoices -- by printed number or by id, whole
  tokens, case-folded (`app/domain/references.py`). An invoice not loaded
  cannot be cited, so a match is a reference-based candidate, never an
  established allocation. A credit with no reference is never a candidate.
- **What qualifies.** Completed, in the invoice's currency, for its exact
  total, from an external origin, with no FX and no fee, citing no other
  loaded invoice, and no return naming it up to the window's end.
- **Outcomes, in this order of precedence:**

  | Outcome | When | Finding |
  |---|---|---|
  | *not evaluable* | an account not visible; the read incomplete, or not asked for the whole window; no declared account in the invoice's currency; coverage not established; the bank unavailable | `NOT_EVALUABLE`, evidence `{"check": "bank", ...}` |
  | *to reconcile* | any referenced credit that does not qualify -- reversed, partial, over, another currency, FX, fee, several invoices cited, unknown or internal origin, a related return -- or several that do | `BANK_CREDIT_TO_RECONCILE` |
  | *observed* | exactly one qualifying credit, and no exception | `BANK_CREDIT_OBSERVED` |
  | *nothing* | no referenced credit in a complete, covered read | none -- never evidence that the invoice is unpaid |

- **What a finding sets off.** Either finding, and a bank-derived
  `NOT_EVALUABLE`, blocks every correction on the invoice, makes the verdict
  human review, and allows no automatic reply. A blocking finding
  (injection, impersonation, identity conflict) keeps its precedence in the
  policy. The classification is the engine's: the mock keeps a billing
  finding's -- a duplicate invoice stays a duplicate, its correction blocked
  -- and another engine gives its own; whatever it is, the bank finding
  forces human review and blocks every correction.
- **The record.** Every run writes a `BankCheck`. A check that does not run
  records `not_run` and why. A check that runs records its outcome and
  reason, the checked invoice, window, margin and the limitations said every
  time (allocation unverified beyond the loaded invoices; coverage limited to
  the declared accounts from the invoice's issue; a later reversal not
  followed). When a read came back, it also records the declared accounts
  with their currency and visibility, completeness and coverage; a failed
  read records `not_evaluable` with `bank_unavailable`, and none of these.
  Only *observed* and *to reconcile* record the referenced credits and the
  returns naming them, with their allowlisted fields and reasons, and the
  outcome's reasons; *nothing* and *not evaluable* hold no transaction. The
  record rejects any field outside its schema and any outcome, reason or
  limitation outside its closed sets; its allowlisted string fields keep
  their declared types. What each channel shows of it is in
  `disclosure-and-replies.md`.

## The Revolut Sandbox

- **The seed** (`scripts/revolut_seed.py`) makes the Sandbox worlds with the
  Sandbox's full-access key, from `.env.seed` alone: only
  `POST /sandbox/topup`, only on the declared account, to the pinned origin,
  no redirects; an intent is noted before each top-up and a rerun never sends
  one twice; it never prints the key or a response body.
- **The read-back** (`scripts/revolut_readback.py`) reads them through the
  service's composition, read-only, and prints structure only.
- **What the Sandbox showed** (SC-002, 2026-11-15): the four seeded top-ups
  (the total citing the invoice, a part, one reverted, one citing another
  number) read back complete and covered, with the expected types, states
  and amounts -- and with **no reference and no counterparty** (origin
  unknown). None of those four is a candidate through the current adapter;
  *observed* and *to reconcile* are demonstrated on fixtures only. Whether
  the Sandbox carries the reference elsewhere is not yet looked at: a
  read-only probe awaits the maintainer's approval.

## Not guaranteed today

- **No Production claim.** The type allowlist, the margin, the reversal
  model and the coverage rule rest on conventions until Revolut answers six
  questions (ADR-014). Production would need those answers, a coverage rule,
  and the merchant's own read-only key.
- **A reference is a payer's free text.** It can cite the wrong invoice; the
  check cannot see invoices that are not loaded; a later reversal is not
  followed.
- **One bank, one account.** A second bank is a new adapter behind the same
  port; several declared accounts are read, but only one is configured today.

## Covered by

`tests/unit/test_bank_foundation.py`, `tests/unit/test_bank_check.py`,
`tests/unit/test_revolut_reader.py`, `tests/unit/test_revolut_seed.py`,
`tests/unit/test_revolut_readback.py`, `tests/unit/test_eval_bank_assertions.py`,
`tests/integration/test_bank_evidence.py`; the bank cases of the development set
(taxonomy B14, B31); on the Revolut Sandbox, the SC-002 seed and read-back.
