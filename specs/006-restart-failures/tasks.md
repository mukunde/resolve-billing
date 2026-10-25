---
description: "Tasks for an expired approval, a repeated delivery and a timeout after a write, across a real restart"
---

# Tasks: An expired approval, a repeated delivery and a timeout after a write, across a real restart

**Input**: [spec.md](spec.md) (approved at `15a4b9e`), [plan.md](plan.md)

**Tests**: this feature is tests; each is written and shown to pass on the
existing code (no runtime change). **Checkpoint after each phase**:
`ruff check`, `pytest`, the mock baseline and the coverage check pass;
committed locally. **[maintainer]** marks his tasks.

## Phase 1: The harness

- [x] T001 `tests/e2e/_service.py`: a `launcher` option and `extra_env`,
  the A1 behaviour unchanged (A1's tests still pass).

## Phase 2: A2 and A3

- [x] T010 `tests/e2e/test_restart_expired_approval.py` (plan section 2).
- [x] T011 `tests/e2e/test_restart_repeated_delivery.py` (plan section 3).

## Phase 3: A4's infrastructure -- checkpoint

- [ ] T020 `tests/e2e/_persistent_fake.py` and its tests,
  `tests/unit/test_persistent_fake.py` (plan section 4).
- [ ] T021 `tests/e2e/_launcher.py`; a check that the launched service uses
  the persistent fake (a write lands in the ledger).
- [ ] T022 Codex reviews the persistent fake, the launcher and their tests
  before A4's scenario is written.

## Phase 4: A4

- [ ] T030 `tests/e2e/test_restart_timeout_after_write.py` (plan section 5).

## Phase 5: Documents

- [ ] T040 `docs/operations.md` "After a restart" and the
  approval-and-execution contract (plan section 6).

## Phase 6: Review and delivery

- [ ] T050 Codex reviews the diff until nothing is left.
- [ ] T051 **[maintainer]** `pytest -m e2e` on his machine (SC-001).
- [ ] T052 **[maintainer]** Final-diff approval by its commit (sensitive);
  push and pull request.
