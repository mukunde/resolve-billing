---
description: "Tasks for a pending approval surviving a real service restart"
---

# Tasks: A pending approval survives a real service restart

**Input**: [spec.md](spec.md) (approved at `4482543`), [plan.md](plan.md)

**Tests**: this feature is a test; it is written first and shown to pass on
the existing code (no runtime change). **Checkpoint after each phase**:
`ruff check`, `pytest`, the mock baseline and the coverage check pass;
committed locally. **[maintainer]** marks his tasks.

## Phase 1: The harness

- [x] T001 `tests/e2e/_service.py`: `ServiceProcess` -- environment built from
  scratch (no inherited `.env`, no real key), loopback port, health polling
  with a deadline, liveness and the expected answer, a failed start that
  cleans up itself, graceful stop (POSIX `SIGTERM`, Windows
  `CTRL_BREAK_EVENT` with a new process group) proven by uvicorn's shutdown
  line within its deadline -- never satisfied by a kill -- hard kill,
  reaping with escalation, bounded output on failure; the test's HTTP
  client with `trust_env=False` and explicit timeouts.
- [x] T002 The `e2e` marker in `pyproject.toml`; the skip when `uvicorn` is
  not importable.

## Phase 2: The scenario

- [x] T010 `tests/e2e/test_restart_pending_approval.py`, parametrised over
  graceful stop and hard kill: plan section 2, steps 1-8, the two process
  identifiers different, the first process's exit proven before the second
  starts.
- [x] T011 By hand, once: a wrong port, a process that cannot start, an
  assertion failing mid-flow -- no process left behind (`tasklist` /
  `ps`), the output reported.

## Phase 3: Documents

- [x] T020 `docs/operations.md`: "After a restart" (find by identifier,
  read-only SQL with the store's real column names, what is safe).
- [x] T021 `docs/contract/approval-and-execution.md`: the guarantee, the
  test under "Covered by", the limits under "Not guaranteed today".

## Phase 4: Review and delivery

- [ ] T030 Codex reviews the diff until nothing is left.
- [ ] T031 **[maintainer]** Runs `pytest -m e2e` on his machine (SC-001).
- [ ] T032 **[maintainer]** Final-diff approval by its commit (sensitive:
  a contract guarantee); push and pull request -- after the Gemini full run
  of 2026-10-25.
