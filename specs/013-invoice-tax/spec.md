# Feature Specification: Tax on invoices

**Feature Branch**: `013-invoice-tax`

**Created**: 2026-11-06

**Status**: Approved by the maintainer on 2026-11-06, at `b6d407c` (clarifications answered the same day). Built (PR 49, `def7dfa`); measured on Luna the same day, the four taxed cases right, no unsafe action (`evals/results/README.md`). The maintainer's Stripe contract runs are to come

**Input**: Roadmap item 3 (real integrations), order A decided by the maintainer on
2026-11-06 (coordination decision 45, framing reviewed by Codex r335-r336): tax comes into the
domain first, on the fake and Stripe, before any Pennylane adapter. Today the domain has no
tax: Stripe's mapper refuses any invoice or line with a tax amount
(`ProviderUnsupported("invoice_tax")`), and French B2B invoices carry VAT. The maintainer's
decision on what amounts mean (2026-11-06): line amounts, prorations and duplicate invoices
are compared excluding tax; payments are compared including tax; a correction is capped at
the invoice's total including tax; the approver sees the amount excluding tax, the tax and
the total; shapes not supported are listed and refused with a reason.

**Behaviour changed**: capabilities *Billing platform*
([contract](../../docs/contract/billing-platform.md)) -- taxed invoices are read instead of
refused; *Approval and execution* ([contract](../../docs/contract/approval-and-execution.md))
-- the approver is shown the tax; *Decision engine*
([contract](../../docs/contract/decision-engine.md)) -- a taxed invoice reaches the engine
with its tax; and the duplicate-invoice rule. An invoice without tax is read, decided, shown,
fingerprinted and sent to the engine exactly as today.

**Sensitive change**: yes. It changes the evidence financial proposals rest on and the amount
an approver is asked to approve (constitution II). The pull request needs the maintainer's
explicit approval of the final diff.

## Clarifications

### Session 2026-11-06

- Q: Does a correction need a tax computation? → A: Not in this slice. The code prices only
  whole-invoice corrections today -- a duplicate or an invoice after cancellation is credited
  whole, an extra payment refunded whole -- and partial corrections go to a person without an
  amount (`app/policies/correction.py`, taxonomy S5). A whole-invoice credit reverses each
  line and the tax as the platform posted them: the amount is the invoice's total including
  tax, and no tax is recomputed. Computing tax at a line's rate becomes necessary only with
  partial corrections, a later slice.
- Q: Why compare duplicates excluding tax and payments including tax? → A: The maintainer's
  decision. Prices are agreed excluding tax in B2B, so two invoices are the same charge when
  their amounts excluding tax and their tax are the same; money moves including tax, so a
  payment is matched against the total including tax.
- Q1: Does this slice let Stripe write on a taxed invoice -- both instruments, a credit note
  and a refund (on Stripe a refunding credit note, built by the same request)? → A: Yes,
  both (the maintainer, 2026-11-06). A whole-invoice write is built line by line so that
  Stripe reverses each line's tax, and it is held on a fresh write contract run made by the
  maintainer before the pull request is merged. If that run does not hold, the slice ships
  with both writes on a taxed invoice refused before sending (`invoice_state`), and says so.
- Q2: Which tax shapes are supported? → A: Tax-exclusive lines, one or several rates on one
  invoice, as posted by the platform (the maintainer, 2026-11-06). Refused, each with a
  reason: prices that include tax (`tax_inclusive`); posted amounts that do not reconcile,
  tax on an invoice without lines included (`tax_mismatch`, FR-002). A line with no tax
  charged -- exempt, out of scope, reverse charge -- is read as a line with no tax, as
  today; its reason is kept when the platform states one and the invoice carries tax
  elsewhere, and not on an invoice with no tax at all, whose bytes stay unchanged (amended
  by the maintainer on 2026-11-06, at plan approval). This revises coordination decision
  45, which listed reverse charge among the shapes refused: with no tax charged, the amounts
  are right as they stand.
- Q: (2026-11-06, at plan approval) The control `taxed_same_total_control`, beyond "no
  duplicate"? → A: `valid_invoice` with `reply`, no approval: what prompt v3 says when no
  finding is present; no new engine requirement (the maintainer, option (a)).
- Q3: Is the slice measured on a hosted engine? → A: Yes (the maintainer, 2026-11-06): after
  the mock, one service run of the enlarged development set on Luna, with no change of
  prompt, because taxed invoices change what the engine receives. The commands and their
  estimated cost (under $0.02) are shown to the maintainer before the run.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A taxed invoice is investigated, not refused (Priority: P1)

An operator's Stripe test account holds invoices with VAT. A dispute on a supported situation
-- a duplicate, an invoice after cancellation, a payment collected twice -- is investigated on
those invoices as on untaxed ones: the same findings, the same decision, an amount including
tax. A shape the domain does not support is refused with its reason, nothing decided.

**Why this priority**: Without it no French invoice with VAT can be read, on Stripe or any
later platform.

**Independent Test**: The fake's taxed fixtures and Stripe's mapping tests (no network) give
the declared findings and amounts; the contract run on a seeded account repeats them.

**Acceptance Scenarios**:

1. **Given** two invoices on one subscription for overlapping periods, each 100.00 EUR
   excluding tax with 20.00 EUR VAT, **When** the second is disputed, **Then** a duplicate is
   found and a credit note of 120.00 EUR is proposed.
2. **Given** two such invoices with the same total including tax but different amounts
   excluding tax (one at 20 %, one at 5.5 %), **When** one is disputed, **Then** no duplicate
   is found.
3. **Given** two invoices with equal amounts excluding tax and equal total tax, split
   differently across rates, **When** one is disputed, **Then** no duplicate is found.
4. **Given** an invoice of 120.00 EUR including tax paid twice, 120.00 EUR each time, **When**
   it is disputed, **Then** a refund of 120.00 EUR is proposed.
5. **Given** an invoice whose prices include tax, **When** it is read, **Then** it is refused
   with `tax_inclusive` and nothing is decided.

### User Story 2 - The approver sees what the amount is made of (Priority: P1)

A proposal on a taxed invoice shows the amount excluding tax, the tax by rate and the total;
the approver approves that total. On an untaxed invoice the proposal looks as today.

**Why this priority**: An approver signs for an amount; on a taxed invoice that amount is a
total whose parts must be visible (constitution II).

**Independent Test**: The review page and the approval API render a taxed proposal with its
three figures, and an untaxed one unchanged.

**Acceptance Scenarios**:

1. **Given** a proposed credit note of 120.00 EUR on an invoice of 100.00 EUR + 20.00 EUR VAT
   at 20 %, **When** the approver opens it, **Then** the three figures and the rate are shown.
2. **Given** a proposal approved on a taxed invoice, **When** the platform's tax on that
   invoice changes before execution, **Then** execution is refused as for any change to the
   invoice, and the breakdown approved stays readable on the action.
3. **Given** an action pending approval on an untaxed invoice, recorded before this change,
   **When** it is approved and executed after it, **Then** its fingerprint is unchanged and
   execution proceeds.

### User Story 3 - The development set holds taxed cases (Priority: P1)

New development cases cover taxed invoices; the mock produces their declared findings,
classification and action; the existing 41 cases keep their recorded outcomes.

**Why this priority**: A behaviour change is measured on the evaluation set (constitution
VIII).

**Independent Test**: The mock run with its baseline and floor, in CI.

**Acceptance Scenarios**:

1. **Given** the enlarged set, **When** the mock runs, **Then** every new case meets its
   declared outcome, no unsafe action occurs, and the 41 existing cases are unchanged
   case by case.

### Edge Cases

- A line taxed at two rates (two tax amounts on one line): supported if each is posted with
  its rate, the line's tax being their sum.
- A tax amount of zero with a rate: read as no tax charged, the rate kept for display.
- A taxed invoice with a line or a tax amount in a currency other than the invoice's:
  refused by the mapper (`tax_mismatch`), never converted. An untaxed invoice with a line in
  another currency stays what it is today: a `CURRENCY_MISMATCH` finding, sent to a person.
- The fake's existing fixtures and the frozen holdout's fixtures carry no tax fields: they are
  read as untaxed, unchanged.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The domain MUST represent, for each invoice line, its amount excluding tax and
  the tax amounts posted on it, each with its rate; and for each invoice, its amount
  excluding tax, its tax by rate and its total including tax. The total including tax
  remains the invoice's `total`. Every amount is in minor units through ADR-005's one
  rounding function; no tax is computed from a rate. An invoice with no tax amount is
  represented and read exactly as today.
- **FR-002**: On an invoice carrying at least one tax amount -- and only there; an untaxed
  invoice keeps today's path -- the mapper MUST reconcile what the platform posts and
  refuse, with `tax_mismatch` (a new reason of the closed set), an invoice where any of these
  fails: every line and every tax amount is in the invoice's currency (nothing is converted
  or summed across currencies); the
  invoice's amount excluding tax is the sum of its lines'; its tax for each rate is the sum
  of its lines' tax at that rate (when the platform posts a per-rate total); its total tax is
  the sum of its lines' tax; its total is its amount excluding tax plus its total tax. A
  tax amount without a readable rate is
  `missing_field`. Prices that include tax MUST be refused (`tax_inclusive`). The posted tax
  is authoritative against its rate: a rounding difference between a rate applied to an
  amount and the tax posted is not checked (Q2).
- **FR-003**: Stripe's mapper MUST read tax-exclusive tax amounts and rates instead of
  refusing them, for the shapes FR-001/FR-002 support; `invoice_tax` is retired from the
  closed set only if no shape still needs it.
- **FR-004**: The fake MUST read optional tax fields in its fixtures, in its own vocabulary
  (decimal strings, as its other amounts), and read fixtures without them as untaxed.
- **FR-005**: The duplicate-invoice rule MUST require, besides what it requires today, equal
  amounts excluding tax and an equal tax breakdown: the same tax amount at each rate,
  whatever the order of lines, rates with no tax charged left out. The proration rule MUST measure line amounts excluding
  tax (what lines carry today). The duplicate-payment rule and the refund's coverage MUST
  compare payments with the total including tax (as today).
- **FR-006**: A whole-invoice correction MUST be the invoice's total including tax, and the
  cap on what an invoice can give back MUST stay its total including tax.
- **FR-007**: A proposal on a taxed invoice MUST record, when it is made, the breakdown it
  stands on -- amount excluding tax, tax by rate, total -- with the action, and every surface
  that presents or reports the action MUST show it: the dispute API's proposal, the
  approvals API, the review and run pages, the MCP tools and the run row. The breakdown
  approved stays readable after the invoice changes. An action on an untaxed invoice records
  and shows nothing new.
- **FR-008**: The proposal's fingerprint MUST cover the invoice's tax, so an approval holds
  only for the breakdown shown; for an invoice without tax it MUST be byte for byte what it
  is today, so actions pending approval survive the change.
- **FR-009**: What the engine receives MUST carry a taxed invoice's amounts excluding tax, its
  tax and its total; for an invoice without tax it MUST be byte for byte what it is today, so
  existing results and the holdout's reference stay comparable.
- **FR-010**: Stripe MUST write on taxed invoices, credit note and refund (Q1): a
  whole-invoice write reverses each line with its tax; the write's answer and `find_action`
  are checked against the approved amount including tax and the allocation planned; the
  platform's cap on what an invoice gives back (credit notes and refunds together) is shown
  on a taxed invoice by the maintainer's write contract run. If that run does not hold,
  both are refused before sending (`invoice_state`) and the contract says so.
- **FR-011**: New development cases, at least: a taxed duplicate (FR), a taxed invoice after
  cancellation (EN), a taxed payment collected twice, and the control of scenario US1-2 (same
  total including tax, different amounts excluding tax: not a duplicate). Their labels are
  proposed in the plan and adjudicated by the maintainer before any paid run. The mock floor
  and baseline are updated in the same pull request.
- **FR-011a**: Tests without network MUST cover, besides the cases: a taxed proration
  measured on amounts excluding tax; an invoice with several rates; each `tax_mismatch`
  condition of FR-002 and `tax_inclusive`; a taxed invoice with a line in another currency
  (refused) and an untaxed one (the `CURRENCY_MISMATCH` finding, as today); the duplicate controls of US1-2 and US1-3; a tax
  changed after approval refusing execution; on the fake, the cap on a taxed invoice for a
  credit note then a refund together.
- **FR-012**: The contract run on a Stripe test account seeded with taxed worlds (a tax rate
  on the account, test mode) MUST give the same findings, proposal and verdict as their fake
  counterparts, and refuse each unsupported shape with its reason. The run is the
  maintainer's; the seed script never runs in CI.
- **FR-013**: The contracts (billing platform, approval and execution, decision engine), the
  taxonomy and the README MUST say what is built, in the same pull request; ADR-001 and
  ADR-005 gain a dated amendment for tax.
- **FR-014**: CI MUST make no paid call and no network call. The holdout is neither read nor
  rerun.

### Key Entities

- **Line tax**: an amount posted on a line, its rate, and the reason when none is charged.
- **Invoice amounts**: excluding tax, tax, total including tax; the total is what a payment
  settles and what a correction is capped at.
- **A taxed world**: a fixture or a seeded Stripe account state reproducing one dispute with
  VAT.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every new case meets its declared findings, classification and action under the
  mock; the 41 existing cases keep their recorded outcomes case by case; no unsafe action.
- **SC-002**: For every untaxed fixture and every development case without tax, the engine
  payload and the proposal fingerprint are byte for byte unchanged (a test compares them
  with their values at `f5c3732`); the tax semantics themselves are shown by FR-011a's tests
  and the cases.
- **SC-003**: The Stripe contract run on taxed worlds matches the fake on findings, amounts
  including tax, verdict and approval, and refuses each unsupported shape with its reason.
- **SC-004**: The service run on Luna (Q3) gives no unsafe action, and its
  outcomes on the new cases are reported case by case to the maintainer.

## Assumptions

- Development cases are written by the session that builds the slice: not blind, not
  presented as such.
- Partial corrections, tax computed at a rate, discounts and credits stay refused or withheld
  as today.
- Pennylane is not touched: its adapter is spec 014, once its sandbox access is granted.
- Stripe's tax objects in test mode are read from the API version the adapter pins
  (`2026-07-29.dahlia`); the shape of tax on lines is confirmed by the contract run, not
  assumed.
