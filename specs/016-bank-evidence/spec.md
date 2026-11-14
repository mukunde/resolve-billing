# Feature Specification: A bank's evidence that a payment arrived

**Feature Branch**: `016-bank-evidence`

**Created**: 2026-11-14

**Status**: Draft, for the maintainer's approval

**Input**: Roadmap item 3, the last of its integrations (coordination decisions 4, 45 and
48). The framing was reviewed by Codex (r399-r405) and decided by the maintainer on
2026-11-13 (decision 48): observe a receipt for a person, read from the merchant's bank,
never conclude about the invoice. The bank changed the same day from Qonto, whose developer
portal admits only paying, verified customers, to **Revolut Business**, whose Sandbox is free
and open; the slice is a demonstration on that Sandbox and on fixtures, not a claim about
Production, until Revolut answers the questions sent on 2026-11-14. Access: a Sandbox
account and a dedicated EUR account (`RB worlds`) made by the maintainer, a read key
(Accounts and Transactions, Read) with its Ed25519 signing key, and the Sandbox's
full-access key for the seed alone (the API Console offered no simulations-only product).

**Behaviour changed**: new capability *Bank evidence*; capability *Investigation and rules*
(a new check, its precedence, corrections blocked); *Decision engine* (what its payload
holds); *Approval and execution* (corrections blocked, the review page's evidence);
*Evaluation* (new cases, the baseline moved); *Disclosure and replies* (a new source and its
allowlists). Investigations without bank evidence configured behave as before.

**Sensitive change**: yes. It adds a third port and amends constitution III; it touches
what reaches a model (V, VII) and what blocks a correction (II). The pull request needs the
maintainer's explicit approval of the final diff.

## Clarifications

### Session 2026-11-13 (decision 48, the maintainer)

- Q: The objective? → A: Observe a receipt for a person; nothing is concluded about the
  invoice. Establishing settlement (reconciliation) is a later slice.
- Q: The source? → A: The merchant's bank, read through a third port; Revolut Business.
- Q: Allocation? → A: A credit citing this invoice's number and no other loaded invoice is a
  reference-based candidate, its allocation stated as unverified beyond the loaded
  invoices.
- Q: Scope? → A: Partial, combined and over-payments, fees and other currencies, when the
  credit cites the invoice, are "to reconcile" (a person's); payments before the invoice's
  issue are outside coverage, not looked for.
- Q: Third-party data? → A: The engine receives the finding's code only; the audit keeps a
  closed allowlist; payer names, account numbers, raw labels, descriptions and references
  go nowhere.
- Q: The constitution? → A: Article III amended to name a third port, with its ADR.
- Q: Production claims? → A: None until Revolut answers; the incoming-transfer type
  allowlist and the lookback margin are conventions the maintainer approves.

### Session 2026-11-14 (the maintainer)

- Q: Are invoice numbers unique across the merchant, or only per customer? → A: Unique
  across the merchant -- French invoicing law requires a unique, chronological numbering
  per company, and both platforms integrated follow it (Pennylane one sequence per company;
  Stripe's per-customer numbering carries a per-customer prefix). The finding may therefore
  say that the cited number designates this invoice alone; it still says that the payer is
  not verified and that a reference may be mistyped. If a merchant numbered per customer,
  the finding would mislead a person, never move money.

### Open, for the maintainer
- **The type allowlist** (convention until Revolut answers): `topup`, and `transfer` with an
  `external` counterparty.
- **The lookback margin** (convention until Revolut answers): how long before the window a
  credit's creation is read (proposed: 7 days).

## What the documentation establishes, and what the probe will settle

From developer.revolut.com (2026-11-13/14): transactions listed by `created_at`, `from`
inclusive and `to` exclusive, at most 1 000 per request, no documented tie-breaker; states
`created`, `pending`, `completed`, `declined`, `failed`, `reverted`; `completed_at` required
when completed; `reference` and the leg's counterparty optional; each leg with `amount` (a
JSON number), `currency`, optional `fee`, `bill_amount`, `bill_currency`, and a
`description` that can name a third party; `related_transaction_id` documented for refunds
and cashback; 60 requests per minute per business; Sandbox top-ups (`POST /sandbox/topup`)
with a reference and a final state; signing optional in Sandbox, valid when sent.

Not documented, asked of Revolut: the type and reference of an incoming SEPA transfer, the
shape of a reversal of a completed credit, timestamp ties, the longest creation-to-
completion delay, history depth. The read-only probe (reviewed before it runs) settles what
the Sandbox shows: the declared account's visibility and currency, the fields and values a
transaction actually carries, whether signed requests are accepted.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A claimed payment with a matching bank credit goes to a person with the evidence (Priority: P1)

A customer says they paid by transfer; the invoice is unpaid on the platform; the bank
shows one completed credit, from an outside payer, in the invoice's currency, for its total,
citing its number and no other.

**Independent Test**: The rule and the fake without network; then the Revolut adapter on the
Sandbox, a top-up seeded with the reference.

**Acceptance Scenarios**:

1. **Given** that credit, **When** the dispute is investigated, **Then** the finding *bank
   credit observed* is recorded, no correction is proposed, the verdict is human review with
   no automatic reply, and the engine's payload holds the finding's code and nothing of the
   bank's data.
2. **Given** the same, **When** a person opens the run page or the review page, **Then**
   they see the credit's allowlisted fields, the matching reasons and the limitations.

### User Story 2 - Doubtful evidence goes to a person, uncertain reads conclude nothing (Priority: P1)

**Acceptance Scenarios**:

1. **Given** a credit citing the invoice that is reversed, partial, over, in another
   currency, with FX or a fee, citing several invoices, or a second qualifying credit, or a
   return related to a candidate, **Then** *bank credit to reconcile*, even beside a
   qualifying credit.
2. **Given** a read that failed, was cut short, hit an unresolved timestamp tie at a page
   boundary, or whose coverage is not established, **Then** *not evaluable*.
3. **Given** a complete read with no credit, **Then** no finding: the case behaves as today
   (`claimed_payment_without_evidence` → `insufficient_information`), the coverage limits
   recorded.

### User Story 3 - Nothing else moves (Priority: P1)

**Acceptance Scenarios**:

1. **Given** a payment already recorded on the billing platform and the same money at the
   bank, **Then** no duplicate payment is found: bank observations are never `Payment`s.
2. **Given** a duplicate invoice and a bank credit, **Then** the duplicate keeps its
   precedence; the bank finding only blocks corrections.
3. **Given** no bank evidence configured, **Then** every investigation behaves exactly as
   before (the mock baseline matches case by case).

### Edge Cases

- A credit with no counterparty, or a `self` or `revolut` one: unknown or internal origin,
  never a candidate.
- A credit with no reference: never a candidate.
- A reverted credit with no `completed_at`, created before the window's end: kept, to
  reconcile.
- The declared account not visible with the key, or in another currency than an invoice:
  the check is *not evaluable* for that invoice.
- Amounts as JSON numbers: parsed as decimals, never floats.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A third port, `BankEvidence`, MUST return, for declared accounts and a window,
  the completed and reverted credits and the returns related to them (by
  `related_transaction_id`, or of a return type), as their own domain type, never `Payment`;
  with whether the read was complete and whether coverage is established.
- **FR-002**: The Revolut adapter MUST issue GET requests only, to the pinned Sandbox host,
  refuse redirects, check the declared account on every read, sign every request with the
  Ed25519 key as Production requires, and parse amounts as decimals. A timestamp tie at a
  full page's boundary MUST make the read incomplete.
- **FR-003**: The check MUST run before any model (I), on the disputed invoice when it is
  unpaid on the platform, with the criteria of "What counts", "Reference" and "Time" in the
  framing (decision 48), and the outcomes in their order of precedence: not evaluable; to
  reconcile; observed; nothing.
- **FR-004**: Either finding MUST block every correction on the invoice and route to human
  review with no automatic reply; existing findings keep their precedence; the engine is
  still consulted for the classification.
- **FR-005**: The engine's payload MUST hold the finding's code only. The run row, the run
  page and the review page MUST hold exactly the audit allowlist of the framing, the
  limitations said every time; counterparty fields, descriptions, raw references and labels
  MUST appear nowhere -- not in logs, traces, exceptions or API answers.
- **FR-006**: The seed MUST make top-up credits only, on the declared account only, with the
  seed key from `.env.seed`; the service and the probe MUST never load that file.
- **FR-007**: Constitution III, a new ADR for the port, ADR-009's disclosure inventory, and
  the contracts named in "Behaviour changed" MUST be written or amended in the same pull
  request.

### Key Entities

- **Bank credit observation**: account (internal id), transaction id, type, state observed
  and its instant, amount and currency, completion or creation instant (labelled), whether
  a reference matched and which invoice numbers it cites (only those loaded), origin class
  (external, internal, unknown).
- **Bank return observation**: id, type, signed amount, currency, the credit it relates to.
- **Bank evidence finding**: *observed* or *to reconcile*, its reasons from a closed set, the
  window, margin, completeness and coverage, the limitations.

## Success Criteria *(mandatory)*

- **SC-001**: Tests without network cover every acceptance scenario and edge case, the
  payload and disclosure allowlists, and the adapter's GET-only, host, redirect, signing,
  pagination-tie and decimal behaviour.
- **SC-002**: On the Sandbox: credits seeded as top-ups (a matching one, a partial one, a
  reverted one, one with another reference) are read back through the real adapter, signed,
  and give their findings. Recorded in the bank-evidence contract, with what the Sandbox
  cannot show.
- **SC-003**: New development cases (labels adjudicated by the maintainer) and the taxonomy
  row; the existing cases' baseline unchanged where no bank evidence applies, the moved
  ones moved case by case; a live engine run only with its cost shown first.

## Assumptions

- Invoice numbers are unique across the merchant (clarified 2026-11-14); the credit stays a
  reference-based candidate, the payer unverified.
- Revolut's answers may change the type allowlist, the reversal model and the coverage rule;
  each is a conventional default until then.
