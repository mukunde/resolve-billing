---
description: "Tasks for Stripe as a billing platform, read-only"
---

# Tasks: Stripe as a billing platform, read-only

**Input**: [spec.md](spec.md) (approved at `8046270`), [plan.md](plan.md)

**Tests**: required. Every test is written before the code it covers and
shown to fail on it.

**Organisation**: by phase, then by user story. `[P]` means parallelisable
(different files, no dependency). Each task names its files. A checkpoint ends
each phase: `ruff check`, `pytest`, the mock baseline and the coverage check
pass, and the work is committed locally.

## Phase 1: Setup

- [ ] T001 `pyproject.toml`: optional extra `stripe = ["stripe==15.4.0"]` and a
  `contract` pytest marker; install the extra locally; the free CI job does not
  install it.
- [ ] T002 [P] `.env.example`: `BILLING_PROVIDER`, `STRIPE_API_KEY`
  (restricted, read-only, test) and `STRIPE_SEED_KEY` (secret, test, seed
  only), with the permissions the restricted key needs.
- [ ] T003 [P] Create `app/providers/stripe/__init__.py` and
  `tests/contract/__init__.py`.
- [ ] T004 [P] `.gitignore`: the working-notes rule anchored to the root
  (`/PLAN.md`), so Spec Kit's `plan.md` is not ignored on case-insensitive
  file systems.

**Checkpoint**: nothing behaves differently; all checks green.

## Phase 2: Foundational -- the port and every surface (blocks every story)

- [ ] T010 Test first, `tests/unit/test_fake_provider.py`: `NotFound` imported
  from `app.providers.base`; the fake raises it; the old import path works;
  the fake declares `can_write = True`.
- [ ] T011 `app/providers/base.py`: `NotFound`, `ProviderUnsupported` (closed
  reasons, message = the category), `PlatformCannotWrite`, `can_write`;
  `app/providers/fake/adapter.py` imports `NotFound`, declares `can_write`.
- [ ] T012 Test first, `tests/unit/test_execute_action.py`: on a provider with
  `can_write = False`, `execute()` raises `WritesNotSupported` before any read,
  reconciliation or claim, and the action keeps its state; `reconcile()`
  likewise; `find_action` never called; a provider raising
  `PlatformCannotWrite` from a write is never turned into an unknown outcome.
- [ ] T013 `app/infrastructure/actions.py` (`WritesNotSupported`),
  `app/application/execute_action.py` (the check first in `execute()` and
  `reconcile()`).
- [ ] T014 Test first, `tests/integration/test_unsupported_platform_data.py`:
  a provider raising `ProviderUnsupported("mixed_item_periods")` gives 422
  `{"category": "provider_unsupported", "reason": "mixed_item_periods"}` on the
  investigation, `GET /actions/{id}`, approve, the review page, and execute on
  a writable provider (never 504); no run row, nothing queued, no transition
  caused by the read (time-based expiry unchanged); no platform text in any
  response or log; the S11 validation 422 keeps its own shape; executing on a
  read-only provider gives 409; the MCP read tools return a tool error with
  the category.
- [ ] T015 `app/main.py` (the handler), `app/api/approvals.py` (re-raise ahead
  of the execute catch-all), `app/application/investigate_dispute.py` (log and
  trace by category, let through), `app/mcp/server.py` (tool error).
- [ ] T016 [P] `docs/adr/001-provider-agnostic-billing.md`,
  `docs/adr/008-execution-state-machine.md`: amendments for the port
  additions and read-only platforms.

**Checkpoint**: the port carries the new contract; existing behaviour
unchanged.

## Phase 3: User Story 1 -- investigate on Stripe data (P1) 🎯

### Contract tests, written now, run later

- [ ] T020 `tests/contract/test_billing_platform.py`: the ten properties of the
  spec, parametrised over the fake (runs in CI now) and Stripe (skipped without
  `STRIPE_API_KEY`, with the reason); waits for the seeded account within a
  bound.
- [ ] T021 `tests/contract/test_stripe_investigation.py`: each supported
  world's target invoice at its investigation instant, with the mock engine,
  against its fake counterpart (SC-001); each unsupported world gives its 422
  category. Skipped without a key.

### Mapping, tests first (CI, no SDK)

- [ ] T022 [P] `tests/unit/stripe_payloads/`: hand-written objects for API
  version `2026-07-29.dahlia`, for every supported shape and every
  `ProviderUnsupported` reason (incl. nulls, whole and fractional
  `unit_amount_decimal`, inclusive and exclusive tax, line and invoice
  discounts, scheduled and ended cancellations, failed, pending, partially and
  fully refunded payments, a payment across invoices, meter summaries present,
  zero and absent, a non-minute period).
- [ ] T023 `tests/unit/test_stripe_mapping.py`: one test per rule of plan §4;
  shown to fail (module absent).
- [ ] T024 `app/providers/stripe/mapping.py` until T023 passes.

### Client and adapter, tests first (CI, stub client)

- [ ] T025 `tests/unit/test_stripe_adapter.py`, stub client with the SDK's own
  `*_async(params=…, options=…)` signatures: key checks before any call; every
  page and nested list read; test clocks listed and their customers merged,
  deduplicated, in the fixed order; price and meter reads cached per call;
  the invoice-payment expansion; the time budget; error mapping with
  categories and no platform text; no mutating method called (the stub fails
  on any); writes and `find_action` raise `PlatformCannotWrite` without a
  call; references `in_…` only.
- [ ] T026 `app/providers/stripe/keys.py`, `client.py`, `adapter.py` until T025
  passes.
- [ ] T027 Test first, `tests/unit/test_provider_wiring.py`: `BILLING_PROVIDER`
  selects the provider (default fake); `STRIPE_API_KEY` read in `deps` only;
  `stripe` imported only in `app/providers/stripe/client.py`,
  `scripts/stripe_seed.py` and Stripe tests (static check of imports); then
  `app/api/deps.py`.

**Checkpoint**: CI green without the SDK; the adapter proven on stubs; the
contract tests pass on the fake.

## Phase 4: User Story 2 -- seed a test account (P2)

- [ ] T030 Test first, `tests/unit/test_stripe_seed.py`, stub client: refuses
  anything but `sk_test_`; one clock per world; plans only what is missing (a
  half-created world completed, nothing twice); records each world's target
  invoice and investigation instant; stays within scope plus the unsupported
  worlds it can produce; waits within a bound, never writes a zero.
- [ ] T031 `scripts/stripe_seed.py`: the worlds of plan §6, every invoice on
  its world's subscription, preconditions read back and checked; a world
  that cannot be built or whose facts do not hold is reported, not dropped.
  The usage world waits for the maintainer's decision (plan, "Open decision").
- [ ] T032 **Maintainer**: create the account's two keys; run the seed twice,
  locally. No agent runs it.

## Phase 5: User Story 3 -- prove the contract on Stripe (P3)

- [ ] T040 **Maintainer**: run `tests/contract/` locally with the read-only
  key.
- [ ] T041 Fix what the run shows (mapping or payload assumptions), with a
  failing test first; a difference that is not a defect is written in the
  contract for the maintainer's approval.

## Phase 6: Documentation and close

- [ ] T050 `docs/contract/billing-platform.md`: the change folded in -- port
  additions, Stripe as a second implementation, approved differences, the
  limits (unknown currencies' default exponent, usage world if dropped),
  "Covered by" updated.
- [ ] T051 [P] `README.md` ("Where it stands", "Limits"), `docs/operations.md`
  (configuring Stripe, the two keys and their permissions, the seed).
- [ ] T052 Final checks; Codex review of the whole diff; the maintainer
  approves the final diff by its commit (sensitive change); push and pull
  request.

## Dependencies

- Phase 1, then Phase 2 (blocks the rest). T001 and T002 both touch
  configuration but different files; T001 alone edits `pyproject.toml`.
- Phase 3: T020 and T021 first (they define the target); T022 to T024 and T025
  to T026 in parallel once T011 is in; T027 after T026.
- Phase 4 after T024 (the seed respects the mapping's scope). Phase 5 after
  Phases 3 and 4 and T032.
- T032 and T040 need the maintainer and his keys; nothing else needs a key.
