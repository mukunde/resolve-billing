---
description: "Tasks for the seeded account checked once per contract session"
---

# Tasks: The seeded account checked once per contract session

**Input**: [spec.md](spec.md) (approved at `0e1ec7f`), [plan.md](plan.md)

**Tests**: required, written before the code and shown to fail on it.

**Checkpoint after each phase**: `ruff check`, `pytest`, the mock baseline
and the coverage check pass; committed locally.

## Phase 1: `Once`

- [ ] T001 Test first, `tests/contract/test_readiness.py`: a success shared;
  a `pytest.fail` kept as its reason and repeated at once; a skip, a platform
  error and `KeyboardInterrupt` not kept and checked again; no Stripe text
  kept.
- [ ] T002 `tests/contract/conftest.py`: `Diagnosis`, `Once`, the two
  instances.

## Phase 2: Readiness once (US1)

- [ ] T010 Test first, `tests/contract/test_readiness.py`: a failure on
  `Hung` shared without a second wait; a world missing shared; the kept data
  immutable; a platform error not kept, checked again; a `pytester`
  sub-run of two async tests (`loop_scope="function"`) sharing that
  sub-run's cache and calling `stripe_platform()` (key set, `connect` and
  `_ready` stubbed): two loops, two providers, one check; the fake
  untouched.
- [ ] T011 `tests/contract/conftest.py`: `Readiness`, `_readiness`,
  `stripe_platform()` on `READINESS`.

## Phase 3: The write generation once (US2)

- [ ] T020 Test first, `tests/contract/test_readiness.py`: the generation
  found once and pinned; no write run skipped and not kept; an incomplete
  generation kept as its diagnosis; the two caches apart; the freshness check
  still made per scenario: two `stripe_writing()` calls, one discovery, each
  `untouched` a real read, and a credit note added in between turns the
  second False.
- [ ] T021 `tests/contract/conftest.py`: `WriteGeneration`, `Duplicate`,
  `_generation`, `stripe_writing()` on `WRITE_GENERATION`;
  `tests/contract/test_stripe_execution.py` on `Duplicate`.

## Phase 4: Documents, run, review, delivery

- [ ] T030 `docs/operations.md` and the conftest docstring (plan section 4).
- [ ] T031 **[maintainer]** A full contract run on the seeded account, with
  writes on a fresh write run; its duration reported beside the nine minutes
  of 2026-10-23 (SC-001), and recorded in `docs/operations.md`.
- [ ] T032 Codex reviews the final diff; findings fixed until nothing is
  left.
- [ ] T033 **[maintainer]** Approval of the final diff by its commit; then
  the push and the pull request, declaring "behaviour unchanged in the
  product; verification set-up changed".
