# Implementation Plan: An expired approval, a repeated delivery and a timeout after a write, across a real restart

**Branch**: `006-restart-failures` | **Date**: 2026-10-25 | **Spec**: [spec.md](spec.md), approved by the maintainer at commit `15a4b9e`

## Summary

Three end-to-end tests on the A1 harness (`tests/e2e/_service.py`), one per
failure: A2 and A3 run the ordinary service; A4 runs it through a test
launcher with a persistent fake billing platform and a one-shot fault, both
in `tests/`. No runtime code changes. The approval-and-execution contract and
`docs/operations.md` gain the three guarantees and their limits.

## Technical Context

As specs/005: Python 3.13, FastAPI under uvicorn, SQLite, httpx with
`trust_env=False`, pytest; Windows and Linux CI.

## Constitution Check

No probabilistic path (mock engine); human approval before every write is
what A2 exercises; a contract guarantee proven by tests, no behaviour change,
so no evaluation run; no credentials beyond the tests' made-up ones.

## Design

### 1. The harness, extended (`tests/e2e/_service.py`)

`ServiceProcess(database, log, *, launcher=None, extra_env=None)`:
`launcher=None` starts `uvicorn app.main:app` as today; a launcher module
path starts `python -m <launcher> --port <port>` instead. `extra_env` adds
variables to the environment built from scratch (A2's deadline, A4's ledger
and fault). Everything else -- readiness, graceful and hard stops, reaping --
is unchanged and shared.

### 2. A2 (`tests/e2e/test_restart_expired_approval.py`)

`RESOLVE_APPROVAL_TTL_HOURS = 20 / 3600` (20 seconds), enough that the first
assertions cannot race it, short enough for CI.

1. Process 1: investigate the synthetic case; `GET` the action:
   `pending_approval`, `expires_at` kept, its `proposal` kept.
2. Stop it (graceful). Wait, polling every 0.2 s under a 60 s monotonic
   bound, until the wall clock is past the recorded `expires_at` plus one
   second.
3. Process 2 on the same database: `GET` -> `expired`, `expires_at`
   unchanged, no `provider_action_id`.
4. `POST /approve` as the approver with the kept proposal -> **409**;
   `POST /{id}/review` with `decision=approve` and the proposal, as the
   approver -> **409**; `POST /execute` with the service key -> **409**;
   `GET` -> still `expired`, no reference.

### 3. A3 (`tests/e2e/test_restart_repeated_delivery.py`)

1. Process 1: investigate -> `run_id` R1, `action_id` A; `GET` A ->
   `idempotency_key` K, `expires_at` E.
2. Restart (graceful). Process 2: investigate the same case -> `run_id` R2 !=
   R1, `action_id` A; `GET` A -> K and E unchanged, `run_id` R1.
3. One stored action for K: read-only SQL on the database file
   (`file:...?mode=ro`), `SELECT count(*) FROM pending_actions WHERE
   idempotency_key = ?` -> 1.
4. Approve, execute twice -> one reference; status read -> `executed`, that
   reference, no attention mark, a successful status, observed.

### 4. A4's infrastructure (`tests/e2e/_persistent_fake.py`, `tests/e2e/_launcher.py`)

**`PersistentFake(FakeBillingProvider)`**, `name` unchanged (`fake`):

- Ledger: a JSON file at a path given to it, `{"writes": [<ProviderAction as
  JSON>...], "fault_spent": bool}`; a lock file beside it
  (`evals.state.try_lock`), held for every read-modify-write.
- On construction and before every platform call that reads or writes
  corrections (`create_credit_note`, `create_refund`, `find_action`,
  `plan_write`), under the lock: reload the ledger and rebuild
  `_actions` (key -> action), `_credited` and `_refunded` (sums derived from
  the writes, never stored apart).
- A write: under the lock, reload, call the parent; if the parent made a new
  write (its key was absent), append it, and if the fault is armed and not
  spent, set `fault_spent`; write the ledger atomically (temporary file,
  flush, `fsync`, replace); release; then, if the fault was spent by this
  call, raise `TimeoutError` -- the write is committed, the answer lost.
  A replay of a known key returns the stored write without touching the
  fault.
- An unreadable or inconsistent ledger raises at construction and at
  reload: refused, never reset.
- The fault is armed by the launcher only (an argument, not an HTTP
  surface), and only ever loses one answer.

**`tests/e2e/_launcher.py`**: reads `E2E_FAKE_LEDGER` and `E2E_FAULT`
(`timeout_after_write_once` or unset) from its own environment; builds one
`PersistentFake`; replaces `app.api.deps.get_provider` and
`app.api.health.get_provider` with a function returning it -- so
`get_engine`, `get_approvals`, `get_investigator` and the health route, which
call `get_provider()` by name, all get it -- and sets
`app.dependency_overrides[original]` for any route that depends on it, where
`original` is the function saved **before** the replacement (a route declared
`Depends(get_provider)` holds the original callable); all of it before the
application serves or any dependency is cached; then `uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
workers=1, access_log=False)).run()` in the main thread, so the harness's
graceful stop (`SIGTERM` / `CTRL_BREAK_EVENT`) and uvicorn's shutdown line
behave as in A1.

**Its own tests (`tests/unit/test_persistent_fake.py`)**, no process: a write
persisted and reloaded by a second instance on the same ledger; a replay of
the key returning the same write, not a second; the cap on what an invoice
gives back holding across instances; the fault losing exactly one answer
after the ledger holds the write, and spent for a second instance; a
corrupt ledger refused and left as it was.

### 5. A4 (`tests/e2e/test_restart_timeout_after_write.py`)

1. Process 1 through the launcher, `E2E_FAULT=timeout_after_write_once`:
   investigate, approve, `POST /execute` -> **504**; `GET` ->
   `unknown_outcome`; the ledger, read directly by the test -> exactly one
   write, its reference W.
2. Hard kill; exit proven.
3. Process 2 through the launcher, same database, same ledger (the fault
   already spent): `POST /execute` -> 200, `executed`, reference W; the
   ledger -> still exactly one write; `POST /execute` again -> W.

### 6. Documents

- `docs/operations.md`, "After a restart": an expired action is final; a
  repeated delivery returns the action already waiting -- **and so does the
  same dispute investigated again after expiry**: the queue finds the
  correction by its economic key whatever its state, so investigating again
  returns the expired action, not a new request for approval. Reinvestigation
  alone is therefore not a way to recover an expired correction: a person
  reassesses it. Said as a known limitation (reported to the maintainer as a
  defect to open on its own), never presented as a recovery procedure, and
  no new key is invented here; an unknown outcome is settled by the next execution,
  which asks the platform before writing anything.
- `docs/contract/approval-and-execution.md`: the three guarantees beside A1's;
  the three tests under "Covered by"; under "Not guaranteed today", the
  expired correction that reinvestigation does not reopen, stage B,
  stage C, an abandoned claim mid-flight (`STALE_CLAIM`, five minutes), the
  milestone's other conditions.

## Tests

As above; A4's checkpoint: the persistent fake and its unit tests are
reviewed by Codex before the A4 scenario is written.

## Project Structure

```text
tests/e2e/_service.py                             # launcher and extra env
tests/e2e/test_restart_expired_approval.py        # A2
tests/e2e/test_restart_repeated_delivery.py       # A3
tests/e2e/_persistent_fake.py                     # A4: the platform that outlives the service
tests/e2e/_launcher.py                            # A4: the service on that platform
tests/unit/test_persistent_fake.py                # A4: the double proven on its own
tests/e2e/test_restart_timeout_after_write.py     # A4
docs/operations.md
docs/contract/approval-and-execution.md
```

## Complexity Tracking

None.
