---
description: "Tasks for Stripe as a billing platform, writing"
---

# Tasks: Stripe as a billing platform, writing

**Input**: [spec.md](spec.md) (approved at `c51e175`), [plan.md](plan.md)

**Tests**: required. Every test is written before the code it covers and
shown to fail on it.

**Organisation**: by phase, then by user story. `[P]` means parallelisable
(different files, no dependency). Each task names its files. A checkpoint ends
each phase: `ruff check`, `pytest`, the mock baseline and the coverage check
pass, and the work is committed locally. Tasks marked **[maintainer]** are
the maintainer's: they need the key or the test account.

## Phase 1: The contract as it stands

- [x] T001 `docs/contract/approval-and-execution.md`: the capability as it is
  today, from ADR-004 and ADR-008 -- states and transitions, the claim, the
  three outcomes, reconciliation, the guards, expiry and the fingerprint, a
  platform that cannot write, what is not guaranteed, and the tests that
  cover it. No new behaviour.
- [x] T002 `docs/contract/README.md`: the capability listed as covered.

**Checkpoint**: documents only; committed on its own, before any change.

## Phase 2: Foundational -- the port and the store (blocks every story)

- [x] T010 Test first, `tests/unit/test_fake_provider.py`: `plan_write`
  answers `reduce_due` for a credit note on an open invoice,
  `customer_balance` on a paid one, `refund` for a refund on a paid one, and
  refuses a refund on an open one; the writes take `action_id`, the
  allocation and `send_by`, refuse an allocation the invoice no longer allows
  and send nothing past `send_by` (`NotSent`); `find_action(key,
  invoice_id)` answers a list -- empty for an unused key or another invoice,
  one after a write; a `ProviderAction` carries the invoice, the amount and
  currency, `test_mode`, the allocation, the action identifier and when it
  was observed; the fake declares `idempotency_window` of 23 hours.
- [x] T011 `app/providers/base.py` (`plan_write`, the signatures, the
  `ProviderAction` fields, `idempotency_window`, `NotSent`,
  `ProviderOutcomeUnknown`) and
  `app/providers/fake/adapter.py`; the fake's test helpers: a lost answer, a
  second write under one key, a write unlike the action, a status that
  changes.
- [x] T012 Test first, then callers follow, in meaning and not only in
  signature: `reconcile()` reads the list -- empty → `FAILED` as today, one →
  `EXECUTED` as today, several → stays unknown (new; tested here);
  `execute()` passes the action's identifier and invoice; the read-only
  Stripe adapter's signatures; `tests/contract/test_billing_platform.py`
  (property 10); `tests/unit/test_execute_action.py` doubles. Every
  existing test green.
- [x] T013 Test first, `tests/unit/test_execute_action.py` (store): the new
  columns, added to a database file made before them; on that old file,
  actions in `EXECUTING`, `UNKNOWN_OUTCOME` or `FAILED` marked
  `window_unknown`, `APPROVED` and earlier untouched; `first_attempt_at` set
  by the first claim only, never by a later one; the allocation stored once;
  `uncertain_since` set once; `annotate` changes no state and refuses when
  the state moved; `FAILED → EXECUTED` refused without the platform's
  reference, like every `EXECUTED`.
- [x] T014 `app/infrastructure/actions.py`: the columns, the migration,
  `annotate`, `NeedsAPerson`; `PendingAction` carries the new fields.

**Checkpoint**: the mock baseline matched; nothing behaves differently.

## Phase 3: User Story 1 -- an approved correction is made once (P1)

### The execution service

- [x] T020 Test first, `tests/unit/test_execute_action.py`: a direct answer
  alike → `EXECUTED` with its reference, status and instant; a status but
  success → `EXECUTED`, annotated `not_successful`, said in `resolution`,
  never written again; an answer unlike the action (each field: invoice,
  type, amount, currency, test mode, an allocation other than the one
  stored -- `reduce_due` planned, `customer_balance` found) or with a field
  missing →
  `UNKNOWN_OUTCOME`, annotated, `NeedsAPerson`, and the next `execute()`
  writes nothing; the allocation fixed by `plan_write` after the first claim
  and reused: two sends of one action make the same request, across a
  restart, and an invoice paid between the plan and a retry is refused,
  never written with another allocation; a `plan_write` refusal → `FAILED`,
  nothing sent; a failed read there → unknown; nothing
  sent for an action not approved, expired, changed since approval, above
  the invoice or in another currency (kept from today, extended to Stripe's
  refusal path).
- [x] T021 `app/application/execute_action.py`: the check (`_unlike`),
  `plan_write` and the stored allocation, the outcomes of a send, the clock.

### The Stripe adapter, writing

- [x] T022 [P] Test first, `tests/unit/test_stripe_writes.py`, stub client:
  a credit note on an open invoice with nothing paid (`amount` only) and on
  a paid one (`credit_amount`); a refund on a paid one (`refund_amount`);
  the metadata exactly the key, the action identifier and the type;
  `email_type` `none`; no `memo`, no `reason`; `expand` exactly
  `["refunds.refund"]`; Stripe's idempotency key the action's;
  `max_network_retries` 0 on the write and reads unchanged; `plan_write`
  refusing a partly paid, draft, void or uncollectible invoice, and a
  refund on an open one; the write refusing an allocation the invoice no
  longer allows, before any write, and a failed read there an unknown
  outcome; `send_by` passed during the invoice read → nothing sent; an
  answer whose refund is not expanded → unreadable; every error class to its outcome (the closed list of
  refusals; an idempotency error, a 409, a rate limit, a connection error, a
  5xx and the time budget to `ProviderOutcomeUnknown`); no Stripe text in
  any message or log; no mutating call but `credit_notes.create`.
- [x] T023 [P] Test first, `tests/unit/test_stripe_mapping.py`:
  `provider_action` from credit note payloads (version `2026-07-29.dahlia`,
  in `tests/unit/stripe_payloads/`): each allocation, each status of
  FR-015 (issued, void; a refund pending, requiring action, succeeded,
  failed, cancelled), `livemode`, a field missing → unreadable.
- [x] T024 `app/providers/stripe/client.py` (`StripeWriter`,
  `_write_outcome`), `mapping.py` (`allocation`, `allows`,
  `credit_note_params`, `provider_action`), `adapter.py` (`plan_write`, the
  writes, `can_write`, `idempotency_window`).
- [x] T025 Test first, `tests/unit/test_provider_wiring.py`: `can_write` only
  with `STRIPE_WRITES=enabled` exactly (absent, `true`, `Enabled`, empty →
  read-only); the variable read in `deps` only; `stripe` still imported only
  where allowed. Then `app/api/deps.py`.

### The API

- [x] T026 Test first, `tests/integration/test_approval_flow.py`: `ActionOut`
  carries the status, its instant, `attention` and `first_attempt_at`;
  `NeedsAPerson` answers 409 with its category, never 504; `POST
  /actions/{id}/status` by an approver records a new observation and sends
  nothing, with writes switched off too; on an action with an incident --
  `UNKNOWN_OUTCOME`, and `EXECUTING` or `FAILED` marked `window_unknown` by
  the migration, each tested -- exactly one write alike makes it `EXECUTED`
  (with no allocation stored: one the type allows), and nothing found,
  several or unlike leave its state, annotated, with no new write ever
  permitted; refused (409) for any other state and (401/403) without an
  approver; the 504 message. Then `app/api/approvals.py` and
  `ApprovalService.refresh_status`.

**Checkpoint**: US1 on the stub; the mock baseline matched.

## Phase 4: User Story 2 -- a lost answer is settled by asking (P2)

- [x] T030 Test first, `tests/unit/test_execute_action.py`, with a clock:
  reconciliation finding one write alike → `EXECUTED` with its status;
  unlike, or two → stays unknown, annotated, nothing written; an incident
  then a reconciliation finding nothing → stays unknown, never `FAILED`,
  never sent again; none within the window → `FAILED`, retried under the
  same key; none past it → stays unknown, `window_passed`; no send at or
  past the window, from `FAILED` or `UNKNOWN_OUTCOME`, and after a restart
  (a new store on the same file); a read that starts before the limit and
  ends after it sends nothing (`NotSent`), and the action is never sent
  again; an action marked `window_unknown` by the migration is never sent;
  a write found for an action with no allocation stored is never settled
  automatically;
  a first answer lost then an idempotency conflict → unknown, never
  `FAILED`; a first write landed, its answer lost, then the retry refused
  (403) → reconciled, `EXECUTED`; refused after an unknown outcome with
  nothing found within the window → `FAILED`; an abandoned claim, within
  and past the window; a failed read leaves the state as it was;
  reconciliation never calls a write.
- [x] T031 `app/application/execute_action.py`: the window check,
  `uncertain_since`, refusals after an unknown outcome (FR-006), the
  reconciliation table.
- [x] T032 Test first, `tests/unit/test_stripe_writes.py`: `find_action`
  lists the invoice's credit notes over several pages with `expand` exactly
  `["data.refunds.refund"]`, keeps those with the key in their metadata,
  answers two when there are two, maps a refund left unexpanded as
  unreadable, raises `ProviderUnavailable` on a page cut short, never calls
  search, and works on an adapter built without a writer (read-only); the
  contract's property 10 then reads that it answers rather than raising.
  Then `app/providers/stripe/adapter.py`.

**Checkpoint**: US2 on the stub and the fake.

## Phase 5: User Story 3 -- Stripe refuses what an invoice cannot give back (P3)

- [ ] T040 Test first, `tests/unit/test_execute_action.py`: Stripe's refusal
  of a write beyond the invoice (`invalid_request`) → `FAILED` with its
  category; an invoice in a state the slice does not handle → refused before
  sending, `FAILED`, nothing sent. Then whatever the earlier phases left.

**Checkpoint**: the three stories on the stub and the fake.

## Phase 6: Write worlds and the contract

- [ ] T050 Test first, `tests/unit/test_stripe_seed.py` (stub): `--write-run`
  opens a new generation each time; the manifest keeps one bounded entry,
  `w_last`, however many generations (sixty in the test); each generation
  found by its own objects' metadata; one invoice per
  purpose, paid in full or open with nothing paid, tagged; a fresh duplicate
  world under the generation's own name and keys; keys per step; the lock;
  refusals reported; nothing reused from an earlier generation.
- [ ] T051 `scripts/stripe_seed.py`, `scripts/stripe_worlds.py`.
- [ ] T052 Test first, then `tests/contract/test_billing_platform_writes.py`:
  the seven write properties, on the fake in CI and on Stripe when the key
  and `STRIPE_WRITES=enabled` are set; each scenario checks at its start
  that its invoices carry no credit note and stops, asking for a new write
  run, if one does -- its own later writes (property 1's second call,
  property 5's write beyond the cap) are intended.
- [ ] T053 `tests/contract/test_stripe_execution.py` (SC-002): the fresh
  duplicate world investigated, its proposal approved as shown, executed
  twice -- one credit note for the approved amount; a refund executed through
  the service on a paid write invoice.
- [ ] T054 **[maintainer]** On the test account: give the restricted key the
  write permissions a credit note and a refunding credit note need, starting
  from credit notes; the permissions found recorded in `docs/operations.md`
  and `.env.example`.
- [ ] T055 **[maintainer]** Run the seed with `--write-run`, then the
  contract tests with the writing key (`-rs`, so every skip is listed with
  its reason). SC-001 holds only with all seven properties run and passed
  on Stripe: a skip is not demonstrated. Each difference from the fake is
  listed in the contract and approved, or fixed.

**Checkpoint**: SC-001 and SC-002 on the test account.

## Phase 7: Documents, review, delivery

- [ ] T060 [P] `docs/contract/approval-and-execution.md`: the change folded
  in; `docs/contract/billing-platform.md`: the port changes, Stripe writing,
  the write properties as measured.
- [ ] T061 [P] `docs/adr/008-execution-state-machine.md`: a dated amendment
  (`find_action` by invoice, the window, refusals after an unknown outcome,
  the answer checked, attention, the status read, Stripe's caps measured);
  `docs/adr/001-provider-agnostic-billing.md`: the port changes.
- [ ] T062 [P] `docs/operations.md` (switching writes on, the key's
  permissions, reading a status again, what an action needing a person
  means), `.env.example`, `README.md`.
- [ ] T063 Gates: `ruff check`, `pytest`, the mock baseline and the coverage
  check; `scripts/make_architecture_svg.py` if the diagram changes.
- [ ] T064 Codex reviews the final diff on a read-only checkout; findings
  fixed, reviewed again until nothing is left.
- [ ] T065 **[maintainer]** Approval of the final diff by its commit; then
  the push and the pull request, with "Behaviour changed": *Billing
  platform* and *Approval and execution*, the requirements affected.

## Dependencies

Phase 1 before everything. Phase 2 blocks Phases 3 to 6. T022 and T023 are
independent of T020-T021. T032 needs T024. Phase 6 needs Phases 3 to 5;
T055 needs T054. Phase 7 last; T060-T062 can start once Phase 5 is done,
and are completed with T055's results.
