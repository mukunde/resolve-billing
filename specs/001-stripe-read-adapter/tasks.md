---
description: "Tasks for Stripe as a billing platform, read-only"
---

# Tasks: Stripe as a billing platform, read-only

**Input**: [spec.md](spec.md) (approved at `8046270`), [plan.md](plan.md)

**Tests**: required (constitution: tests derive from the expectations, and
every test is written before the code it covers and shown to fail on it).

**Organisation**: by phase, then by user story. `[P]` means parallelisable
(different files, no dependency). Each task names its files. A checkpoint ends
each phase: `ruff check`, `pytest`, the mock baseline and the coverage check
pass, and the work is committed locally.

## Phase 1: Setup

- [ ] T001 Add the optional extra `stripe = ["stripe>=15.4,<16"]` in
  `pyproject.toml`; install it locally; the free CI job does not install it.
- [ ] T002 [P] Add `BILLING_PROVIDER`, `STRIPE_API_KEY` (restricted, read-only,
  test) and `STRIPE_SEED_KEY` (secret, test, seed script only) to
  `.env.example`, with how to create the restricted key with read permissions
  only.
- [ ] T003 [P] Create `app/providers/stripe/__init__.py` and
  `tests/contract/__init__.py`; register a `contract` marker in
  `pyproject.toml`.

**Checkpoint**: nothing behaves differently; all checks green.

## Phase 2: Foundational -- the port additions (blocks every story)

- [ ] T010 Test first, `tests/unit/test_fake_provider.py`: `NotFound` imported
  from `app.providers.base`; the fake raises it; the old import path still
  works; the fake declares `can_write = True`.
- [ ] T011 `app/providers/base.py`: `NotFound`, `ProviderUnsupported` (closed
  set of reason categories, message = the category), `PlatformCannotWrite`,
  `can_write` on the protocol; `app/providers/fake/adapter.py` imports
  `NotFound` and declares `can_write = True`.
- [ ] T012 Test first, `tests/unit/test_execute_action.py`: on a provider with
  `can_write = False`, `execute()` raises `WritesNotSupported` before any read,
  reconciliation or claim; the action keeps its state; `find_action` is never
  called; `reconcile()` refuses the same way.
- [ ] T013 `app/infrastructure/actions.py`: `WritesNotSupported(IllegalTransition)`;
  `app/application/execute_action.py`: the check first in `execute()` and in
  `reconcile()`.
- [ ] T014 Test first, `tests/integration/test_unsupported_platform_data.py`:
  through the API, a provider raising `ProviderUnsupported("mixed_item_periods")`
  gives 422 with `{"category": "provider_unsupported", "reason":
  "mixed_item_periods"}`, no run row, nothing queued; executing an approved
  action on a read-only provider gives 409, never 504.
- [ ] T015 `app/application/investigate_dispute.py` (log and trace by
  category, let through) and `app/api/disputes.py` (422).
- [ ] T016 [P] `docs/adr/001-provider-agnostic-billing.md` and
  `docs/adr/008-execution-state-machine.md`: amendments for the three port
  additions and read-only platforms.

**Checkpoint**: the port carries the new contract; the fake and every existing
test unchanged in behaviour.

## Phase 3: User Story 1 -- investigate on Stripe data (P1) 🎯

### Mapping, tests first (CI, no SDK)

- [ ] T020 [P] Payloads in `tests/unit/stripe_payloads/`: hand-written Stripe
  objects for every supported shape and every `ProviderUnsupported` category
  (customers, subscriptions incl. scheduled and ended cancellations, invoices
  incl. tax inclusive and exclusive, line and invoice discounts, negative
  line, decimal unit price, invoice payments incl. failed, pending, partial and
  full refunds, multi-invoice, meter summaries incl. zero and absent).
- [ ] T021 `tests/unit/test_stripe_mapping.py`: one test per mapping rule of
  plan §4, against those payloads; shown to fail (module absent).
- [ ] T022 `app/providers/stripe/mapping.py`: the pure mapping functions, no
  SDK import, until T021 passes.

### Client and adapter, tests first (CI, stub client)

- [ ] T023 `tests/unit/test_stripe_adapter.py` with a stub SDK client: key
  checks (only `rk_test_` accepted, before any call); every page read, nested
  lines and items included; test-clock customers listed and deduplicated; the
  fixed order of the address lookup; the time budget turns into
  `ProviderUnavailable`; error mapping (connection, timeout, 429, 5xx,
  `resource_missing`, authentication, permission) with categories and no
  provider text; the stub fails on any mutating method, and none is called;
  writes and `find_action` raise `PlatformCannotWrite` without a call;
  references `in_…` only.
- [ ] T024 `app/providers/stripe/keys.py`, `client.py` (the only `stripe`
  import, pinned API version, async list helpers, error mapping),
  `adapter.py` (`StripeBillingProvider`), until T023 passes.
- [ ] T025 `app/api/deps.py`: `BILLING_PROVIDER` selects the provider;
  `STRIPE_API_KEY` read there only; a test that `stripe` is imported by
  nothing outside `app/providers/stripe/`, `scripts/stripe_seed.py` and Stripe
  tests (static check of imports).

**Checkpoint**: CI green without the SDK; the adapter proven on stubs.

## Phase 4: User Story 2 -- seed a test account (P2)

- [ ] T030 Test first, `tests/unit/test_stripe_seed.py` with a stub client:
  refuses anything but `sk_test_`; plans only missing objects (a half-created
  world completed, nothing twice); stays within the slice's scope plus the
  unsupported worlds; waits within a bound and never writes a zero.
- [ ] T031 `scripts/stripe_seed.py`: the worlds of plan §6 on one test clock
  advanced to 2026-10-15; prints what it did and the investigation date.
- [ ] T032 Run by the maintainer, locally, with the secret test key: the seed,
  twice. (No agent runs it: it needs the key.)

## Phase 5: User Story 3 -- contract tests (P3)

- [ ] T040 `tests/contract/test_billing_platform.py`: the ten properties of
  the spec, parametrised over the fake (CI) and Stripe (local, skipped without
  `STRIPE_API_KEY`, with the reason); waits for the seeded account to be ready
  (clock, invoices, summaries) within a bound.
- [ ] T041 `tests/contract/test_stripe_investigation.py`: each supported world
  investigated with the mock engine at the seed's date, compared with its fake
  counterpart (findings, classification, proposed action) -- SC-001; the
  unsupported worlds give 422 categories.
- [ ] T042 Run by the maintainer, locally, with the read-only key: both
  files; every difference either fixed or written in the contract for
  approval.

## Phase 6: Documentation and close

- [ ] T050 `docs/contract/billing-platform.md`: the change folded in -- the
  three port additions, Stripe as a second implementation, the approved
  differences, "Covered by" updated.
- [ ] T051 [P] `README.md` ("Where it stands", "Limits"), `docs/operations.md`
  (configuring Stripe, the two keys, the seed), the bug-free path of the free
  CI job described as before.
- [ ] T052 Final checks (`ruff`, `pytest`, mock baseline, coverage check);
  Codex review of the whole diff; the maintainer approves the final diff by
  its commit (sensitive change); push and pull request.

## Dependencies

- Phase 2 blocks Phases 3 to 5. In Phase 3, T020 to T022 (mapping) and T023 to
  T024 (client) can proceed in parallel once T011 is in; T025 needs T024.
- Phase 4 needs T022 (the seed reuses nothing of the adapter but its scope
  rules). Phase 5 needs Phases 3 and 4, and T032.
- T032 and T042 need the maintainer and his keys; everything else runs
  without a key.
