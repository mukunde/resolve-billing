# Implementation Plan: A pending approval survives a real service restart

**Branch**: `005-restart-pending-approval` | **Date**: 2026-10-25 | **Spec**: [spec.md](spec.md), approved by the maintainer at commit `4482543`

## Summary

One new end-to-end test module that starts the service as a real
operating-system process, creates a pending credit note through the
authenticated HTTP API, stops the process (gracefully, then in a second test
by a hard kill), starts a replacement on the same database, and drives the
approval and execution to one matching write -- all through HTTP. A small
process harness for it, under `tests/`. No runtime code changes. Documents:
the operator's recovery after a restart in `docs/operations.md`; the restart
guarantee in the approval-and-execution contract.

## Technical Context

Python 3.13, FastAPI served by `uvicorn` (`app.main:app`), SQLite
(`AUDIT_DB`), `httpx` (already a dependency) as the test's HTTP client,
pytest 9 with `asyncio_mode = "auto"`. Runs on Windows (the maintainer's
machine) and Linux (CI, `pytest -q` in `.github/workflows/evals.yml`).

## Constitution Check

- Deterministic before probabilistic: the mock engine; nothing probabilistic.
- Human approval before any financial mutation: exactly what the test
  exercises, across a restart.
- Evidence for the change's kind: a contract guarantee, proven by a test;
  no behaviour change, so no evaluation run is needed; the existing cases
  and the mock baseline still pass in CI.
- English, no credentials: the test's credentials are the made-up ones of
  `tests/integration/_auth.py`.

## Design

### 1. The harness (`tests/e2e/_service.py`)

A small context manager, `ServiceProcess`, used only by tests:

- **Start**: `subprocess.Popen([sys.executable, "-m", "uvicorn",
  "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--workers",
  "1", "--no-access-log"])` -- no `--reload` -- with an environment built
  from scratch, not inherited wholesale: `PATH`, `SYSTEMROOT` (Windows),
  `PYTHONPATH` to the repository, `AUDIT_DB` (a temporary file),
  `BILLING_PROVIDER=fake`, `DECISION_ENGINE=mock`, `RESOLVE_SERVICE_KEY`,
  `RESOLVE_APPROVERS`, `RESOLVE_ORCHESTRATOR_URL` (the integration tests'
  value), and nothing from the maintainer's `.env` -- so no real key can
  reach the process. Standard output and error go to a temporary file.
- **Port**: chosen by binding a socket to port 0 on 127.0.0.1, reading the
  port and closing it just before the start; if the process exits on a
  port in use, one new port is tried, then the test fails naming why (the
  race is narrow and reported, never silent).
- **Ready**: polls `GET /health` every 0.1 s up to a 20 s deadline, checking
  `poll()` each time; a process that exits, or a deadline passed, fails the
  test with the last 4 KB of its output.
- **Graceful stop**: on POSIX, `SIGTERM`; on Windows, the process is started
  with `CREATE_NEW_PROCESS_GROUP` and sent `CTRL_BREAK_EVENT`, which uvicorn
  handles as a shutdown. Then `wait(10)`.
- **Hard kill**: `kill()` (`SIGKILL` / `TerminateProcess`), then `wait(10)`.
- **Always reaped**: `__exit__` stops whatever is still running, escalating
  to `kill()` after the deadline, and closes the log; a test's failure never
  leaves a process behind.
- **Exited, proven**: before the replacement starts, the harness asserts
  the first process's `returncode` is set.

### 2. The test (`tests/e2e/test_restart_pending_approval.py`)

Parametrised over `stop` in (`graceful`, `kill`); one temporary database per
case; every request over HTTP with `httpx.Client`:

1. Process 1 starts. `POST /api/v1/disputes/investigate` with the service key
   and the synthetic case (`alice@acme.fr`, `inv_002`, the integration
   tests' message) -> an `action_id`. `GET /api/v1/actions/{id}` -> state
   `pending_approval`, credit note, 9900 EUR minor units, no
   `provider_action_id`, its `proposal` fingerprint kept.
2. Process 1 stopped by `stop`; its exit asserted.
3. Process 2 starts on the same database and environment. `GET` again ->
   same `action_id`, same `proposal`, same state, still no reference.
4. `POST /actions/{id}/execute` with the service key -> 409, and a `GET`
   shows the action unmoved.
5. `POST /actions/{id}/approve` with no credentials -> 401 (or the API's
   refusal status for an unauthenticated approver), action still pending.
6. `POST /actions/{id}/approve` as `m.dupont@acme.fr` with the kept
   `proposal` -> `approved`, `approved_by` the authenticated name.
7. `POST /actions/{id}/execute` with the service key -> `executed`, a
   `provider_action_id`; again -> `executed`, the same reference.
8. `POST /actions/{id}/status` as the approver -> `executed`, the same
   reference, `attention` null, `provider_status` a success, and
   `provider_status_at` set.

The process identifiers of the two processes are asserted different.

### 3. Running it

The module lives under `tests/e2e/` and runs in the ordinary `pytest -q`,
locally and in CI; it needs `uvicorn` (the `api` extra, installed in CI). It
is skipped, with the reason, only when `uvicorn` is not importable. A
`pytest.mark.e2e` marker is declared in `pyproject.toml` so the suite can be
run alone (`pytest -m e2e`).

### 4. Documents

- `docs/operations.md`, a section "After a restart": an action is found by
  `GET /api/v1/actions/{id}` or `GET /api/v1/runs/{run_id}`; the waiting and
  uncertain ones are listed with read-only SQL -- `sqlite3
  "file:audit.db?mode=ro" "select action_id, state, created_at from
  pending_actions where state in ('pending_approval','approved','failed',
  'unknown_outcome','executing')"` (column names checked against the store
  at implementation) -- and finding an action neither resumes the
  orchestrator's workflow nor executes anything; what to do for each state
  points to the approval-and-execution contract.
- `docs/contract/approval-and-execution.md`: a line under "What the system
  relies on" -- a pending action, its fingerprint and its deadline live in
  the service's database and survive a restart of the service; the new
  test under "Covered by"; the limits (FR-007) under "Not guaranteed today".

## Tests

The new module is the feature. Its own failure modes are checked by hand
once during implementation (a wrong port, a process that cannot start, a
failing assertion mid-flow) to see that no process is left behind; not as
permanent tests.

## Project Structure

```text
tests/e2e/__init__.py
tests/e2e/_service.py                        # the process harness
tests/e2e/test_restart_pending_approval.py   # the scenario, graceful and killed
pyproject.toml                               # the e2e marker
docs/operations.md                           # after a restart
docs/contract/approval-and-execution.md      # the guarantee, covered by, limits
```

## Complexity Tracking

None.
