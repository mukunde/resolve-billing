# Implementation Plan: Tax on invoices

**Branch**: `013-invoice-tax` | **Date**: 2026-11-06 | **Spec**: [spec.md](spec.md), approved by the maintainer at `b6d407c` | **Plan**: approved by the maintainer on 2026-11-06, at `1dcb37d` (control (a); the zero-tax reason deviation approved and folded into the spec)

## Summary

Tax comes into the domain as amounts posted on lines, each with its rate; an invoice's
amount excluding tax and its tax by rate are derived from its lines and reconciled against
what the platform posts. Untaxed invoices keep today's path and today's bytes. The fake and
Stripe read tax; the duplicate rule compares a taxed invoice excluding tax and by rate; a
proposal on a taxed invoice records and shows its breakdown; Stripe writes a taxed invoice's
credit note line by line. Four development cases, a mock run in CI, one Luna service run on
the maintainer's approval, and the maintainer's Stripe contract runs. One file, as plans
008 to 012.

## Technical Context

Python 3.12 (CI), 3.13 locally; pydantic 2.13. No new dependency. Stripe API version pinned
at `2026-07-29.dahlia`, SDK 15.4.0 (unchanged). SQLite tables gain nullable columns through
the existing `_migrate` of each repository. CI makes no network or paid call.

## Constitution Check

| Principle | How this plan meets it |
|---|---|
| I | No rule moves after a model call; the duplicate rule gains a comparison, deterministic. |
| II | Amounts stay computed by code from posted figures: a whole-invoice correction is the posted total. The approver sees what the total is made of; the fingerprint binds the approval to the tax shown. No threshold opens. |
| III | Tax is domain vocabulary (amount, rate); Stripe's fields stay in its mapper. |
| IV | Tax amounts are `Money`, through the one rounding function; rates are `Decimal` and never multiply anything here. |
| V, VII | The engine receives a taxed invoice's figures, pseudonymised as today; no new kind of effect. |
| VI | The run row records the breakdown; nothing amended in place. |
| VIII | Four cases, the mock gate, a hosted run approved before it starts, contract runs stored. |
| IX | Development cases only; the holdout is neither read nor rerun; its fixtures are untaxed and stay byte-identical. |
| Sensitive | Evidence and the amount approved change: final-diff approval. |

## Design

### 1. The domain (FR-001)

- `app/domain/models.py`: a frozen `LineTax` -- `amount: Money`, `rate: Decimal` (a
  percentage, `20` for 20 %), `reason: str | None` (the platform's taxability reason, e.g.
  reverse charge, kept as text). `InvoiceLine.taxes: tuple[LineTax, ...] = ()`; `amount`
  stays the line's amount excluding tax.
- `Invoice` gains derived, read-only properties: `taxed` (any non-zero tax amount),
  `amount_excluding_tax` (sum of line amounts, in the invoice's currency), `tax_by_rate`
  (rate -> `Money`, zero amounts left out, order-free), `tax_total`. Nothing new is stored on
  `Invoice`: the posted total stays `total`, and the mapper has checked (section 2) that it
  equals the derived amount plus the tax.
- Byte identity (FR-008, FR-009): `InvoiceLine` serialises without `taxes` when the tuple is
  empty (a wrap `model_serializer`), so `model_dump()` of an untaxed invoice is what it is at
  `f5c3732`. A golden test stores, at `f5c3732`, the fingerprint material and the engine
  payload of every development case's world and of the fake's fixtures, and asserts them
  equal after the change (SC-002).
- Zero-amount tax entries: on an invoice with no non-zero tax they are dropped by the
  mappers, as Stripe's mapper ignores them today, so the invoice stays untaxed and
  byte-identical; on a taxed invoice they are kept with their rate and reason. *Deviation
  from the approved spec, for the maintainer's explicit approval: Q2 says a line with no tax
  charged keeps "its reason ... when the platform states one"; on an untaxed invoice a
  reverse-charge reason would not be kept, because keeping it would change the bytes
  FR-008/FR-009 require unchanged. If approved, Q2's wording is amended in the spec, dated.*

### 2. Reading tax (FR-002, FR-003, FR-004)

- `app/providers/base.py`: `UNSUPPORTED_REASONS` gains `tax_mismatch` and `tax_inclusive`,
  and loses `invoice_tax`, which no shape needs any more (FR-003): a tax posted at invoice
  level with no line carrying it fails reconciliation and is `tax_mismatch` (Q2, FR-002).
  The tests that asserted `invoice_tax` assert the new reasons.
- A shared check, `app/providers/tax.py` (domain-side, no vendor): given the mapped lines and
  the platform's posted figures (total; optionally a posted amount excluding tax, posted
  tax by rate, posted total tax), refuse `tax_mismatch` when any FR-002 condition fails,
  only when the invoice carries a non-zero tax. Both mappers call it.
- The fake (`app/providers/fake/fixtures.py`): an optional `taxes` list on a line --
  `[{"rate": "20", "amount": "20.00", "reason": null}]`, decimal strings like its other
  amounts -- and an optional `"prices_include_tax": true` on an invoice, refused
  `tax_inclusive`. Fixtures without them read as today.
- Stripe (`app/providers/stripe/mapping.py`, `client.py`):
  - an invoice line's `taxes[]` (amount, `tax_behavior`, `taxability_reason`,
    `tax_rate_details.tax_rate`) becomes `LineTax`; the rate's percentage is read from the
    tax rate object, fetched and cached by id the way prices and meters are (one read per
    rate). Errors keep today's distinctions: a referenced rate Stripe answers does not exist
    (404) or a rate object without a percentage is `missing_field`; a timeout or failure is
    `ProviderUnavailable` after the client's retries; an authentication or permission
    refusal keeps its configuration category. Each path has a test;
  - `tax_behavior: inclusive` on any tax is `tax_inclusive`;
  - the invoice's `total_taxes` (per-rate), `total_excluding_tax` and `total` are passed to
    the shared check;
  - the existing refusals for discounts and credits stay.

### 3. The rules and the correction (FR-005, FR-006)

- `detect_duplicate_invoice`: when either invoice is taxed, it also requires
  `amount_excluding_tax` and `tax_by_rate` equal. When neither is, the rule is today's,
  unchanged (it does not start comparing line sums of untaxed invoices).
- `proration_mismatch`: unchanged in code -- lines carry amounts excluding tax; a taxed
  proration test shows it.
- `duplicate_payment`, `_covers`, `correction_for`: unchanged -- they compare the total
  including tax. The fake's cap (`_refuse_beyond_the_invoice`) is the total including tax,
  credit notes and refunds together, as today; a test exercises it on a taxed invoice.

### 4. The breakdown, recorded and shown (FR-007, FR-008)

- `ProposedAction` (domain) gains `tax_breakdown: TaxBreakdown | None`, set by the
  investigation when the target invoice is taxed: `excluding_tax`, `taxes` (rate, amount,
  ordered by rate), `total`. `None` otherwise.
- `pending_actions` and `agent_runs` gain a nullable `tax_breakdown` column (JSON), through
  their `_migrate`; `PendingAction` carries it.
- Surfaces, each showing the three figures and the rates when present, unchanged otherwise:
  the dispute API's `ActionOut` and the approvals API's `ActionOut` (field omitted when
  `None`, so untaxed responses are unchanged), `review.html` and `review_done.html` (an
  "HT / TVA x % / TTC" block under the amount), the run view, the MCP tools that return a
  proposal or an action.
- Before presentation and before approval, the stored breakdown is compared with the
  breakdown of the invoice read now (the review page and the approvals API already read the
  live invoice for the fingerprint): if they differ, the page shows the proposal as stale
  and the approval is refused, so a person never approves a breakdown other than the one
  shown. A test changes the tax between investigation and opening the review page.
- The fingerprint already dumps the invoice as the platform holds it now: a tax changed
  after approval changes it, and execution is refused as today (US2-2). Untaxed:
  byte-identical (section 1).

### 5. What the engine receives (FR-009)

`app/decision/anonymise.py`: for a taxed invoice only, the invoice gains
`excluding_tax_minor` and `taxes: [{rate, amount_minor}]`, and each line with tax its
`taxes`. No key is added for an untaxed invoice. The prompt and the output schema are
unchanged.

### 6. Stripe writes on a taxed invoice (FR-010)

- `credit_note_params`: on an untaxed invoice, the request is today's, byte for byte (a test
  pins it). On a taxed invoice: no `amount`; `lines` = one `invoice_line_item` per invoice
  line, its full quantity, so Stripe reverses each line with its tax;
  `credit_amount` / `refund_amount` = the total including tax, by allocation. The adapter's
  read before writing (`_invoice_written_on`) reads only the first page of lines today; on
  a taxed invoice it reads every line, paginated to the end (a list cut short is
  `ProviderUnavailable`, never a partial write). Before sending, the selected lines'
  amounts plus their tax MUST equal the approved total including tax, or the write is
  refused (`invoice_state`). A taxed invoice with a fractional line quantity is refused
  (`invoice_state`): the credit note's `quantity` is an integer. Tests: more lines than one
  page; a mismatch of the selected lines; a fractional quantity.
- `provider_action`: the credit note's `amount`, which includes tax in the pinned SDK
  (`CreditNote.amount`, as `total`), is checked against the approved amount, as today. The
  write contract run verifies this mapping on a taxed credit note; it does not decide it.
- `find_action` unchanged (listing by metadata).
- If the maintainer's write run does not hold, `plan_write` refuses a taxed invoice with
  `invoice_state`, and the contract says so (FR-010).

### 7. The cases (FR-011), labels for the maintainer

Four cases appended to `evals/cases.json`, each with its own customer, subscription,
invoices and payments in `fixtures/` (no existing world moves), `label_source:
"maintainer, 2026-11-06"` once adjudicated:

| Case | World | Message | Proposed labels |
|---|---|---|---|
| `taxed_duplicate_fr` | two invoices, same subscription and September period, 100.00 + 20.00 VAT at 20 % = 120.00 EUR each | « Facturé deux fois pour septembre ? » | `duplicate_billing`, `credit_note`, approval, findings `DUPLICATE_INVOICE`; proposal 120.00 EUR with breakdown |
| `taxed_billed_after_cancellation_en` | subscription cancelled in August, September invoice 49.00 + 9.80 = 58.80 EUR, paid | "I cancelled in August, why a September invoice?" | `billing_after_cancellation`, `refund`, approval, `INVOICE_AFTER_CANCELLATION`; 58.80 EUR |
| `taxed_duplicate_payment` | one invoice 49.00 + 9.80 = 58.80 EUR, two succeeded payments of 58.80 | « Deux prélèvements de 58,80 € pour la même facture » | `duplicate_billing`, `refund`, approval, `DUPLICATE_PAYMENT`; 58.80 EUR |
| `taxed_same_total_control` | two invoices, same subscription and period, both 120.00 EUR including tax: 100.00 + 20.00 at 20 %, and 113.74 + 6.26 at 5.5 % | « Facturé deux fois pour septembre ? » | no finding (not a duplicate); `valid_invoice`, `reply`, no approval (adjudicated: (a)) |

The control's labels beyond "no duplicate finding" are the maintainer's to adjudicate; US1-2
decides only that no duplicate is found. Two answers:
(a) `valid_invoice` with `reply`, no approval -- what prompt v3 says when no finding is
present, so no new engine requirement (recommended);
(b) `unknown` with `escalate`, approval required -- two invoices for one period that no rule
explains, a person looks; this would be a new engine requirement, outside this slice. Then, each with its test: `DEVELOPMENT_SET_SHA256`, `ENGINE_CASES` (counted from the
mock run), the mock baseline regenerated (the 41 existing cases unchanged case by case),
the CI floor raised by the new cases the mock gets right, coverage tables regenerated.
Spec 008's comparison already reads its cases at its manifest's commit. Spec 012's does
not: `evals/prompt_compare.py` reads today's case ids and requires today's set hash, so
enlarging the set would stop it reproducing its published report. It is changed to read
the case ids and the expected labels from `evals/cases.json` as it stood at the runs'
commit (`git show <commit>:evals/cases.json`), checked against the runs' hash, as spec 008's
does; a test reproduces the stored `prompt-v4-comparison.md` from the stored results.

### 8. Measurement and contract runs (SC-003, SC-004)

- **Luna service run (Q3)**: after the mock and CI are green, on the PR's commit. A new
  first call under the enlarged set (exit 3 is its normal end), then one
  `evals.service_run` on Luna, prompt v3. Estimated cost: 45 cases at about $0.0002, under
  $0.02. Commands and cost shown to the maintainer before anything is sent. Result stored in
  `evals/results/`; outcomes on the new cases reported case by case; no unsafe action
  required.
- **Stripe contract runs (the maintainer's)**: `scripts/stripe_seed.py` gains taxed worlds
  -- a 20 % tax rate (exclusive) on the test account; the duplicate, after-cancellation and
  duplicate-payment worlds taxed; a two-rate invoice; an inclusive-tax invoice (refused
  `tax_inclusive`). The read run (`tests/contract/`, read-only key) compares each taxed world
  with its fake counterpart; the write run (`STRIPE_WRITES=enabled`, a fresh generation)
  writes a taxed credit note on an open and on a paid invoice, a taxed refund, a refund
  beyond the cap (refused), and reads them back by `find_action`. A taxed invoice whose
  tax does not sit on its lines alone -- a taxed shipping cost (`shipping_cost`) -- is
  seeded and refused `tax_mismatch`. Only deliberately inconsistent posted figures, which
  Stripe does not produce, are left to mapping tests, approved as such.

### 9. Documents (FR-013)

Billing-platform contract (tax read, the new reasons, Stripe taxed writes, as measured),
approval-and-execution contract (the breakdown), decision-engine contract (the payload's
tax), taxonomy (S5's note: whole-invoice corrections include tax; partial ones still
withheld), README (the platform table and "Decided, not yet built"), ADR-001 and ADR-005
dated amendments.

## Delivery

One pull request, in this order of commits: domain and shared check with the golden test;
fake; rules and correction tests; breakdown and surfaces; engine payload; Stripe read; Stripe
write; cases, baseline and floor; documents. Then, on approval, the Luna run and the
maintainer's contract runs, their results committed to the same pull request before merge.

## Gates

`ruff check .`; `python -m pytest -q -p no:cacheprovider`; `python -m evals.runner --engine
mock --min-fully-correct <new floor> --baseline evals/baselines/mock.json`; `python -m
evals.coverage --check`. Codex reviews each commit's diff; the maintainer approves the final
diff.
