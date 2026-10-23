# Implementation Plan: The seeded account checked once per contract session

**Branch**: `003-contract-readiness-once` | **Date**: 2026-10-23 | **Spec**: [spec.md](spec.md), approved by the maintainer at commit `0e1ec7f`

## Summary

The Stripe half of the contract tests keeps, per pytest process, what the
readiness check found -- or why it failed -- and what the write generation
is, in two caches of immutable data. Each test still builds its own provider
and reads Stripe for its property; the write scenarios keep their
per-scenario freshness check. No product code changes.

## Technical Context

Python 3.12 (CI), 3.13 locally; pytest with `pytest-asyncio` 1.4,
`asyncio_mode = "auto"`, each test on its own event loop (unchanged). Files:
`tests/contract/conftest.py`, `tests/contract/test_stripe_execution.py`,
`tests/contract/test_readiness.py`, `docs/operations.md`.

## Constitution Check

| Principle | How this plan meets it |
|---|---|
| II, VIII (evidence) | The contract's properties are checked as before, each on a fresh provider; only the account's preconditions are read once, and the documents say so. |
| Sensitive areas | A change to the verification set-up: final-diff approval by the maintainer (spec). |

No violation; nothing in `app/` changes.

## Design

### 1. `Once`, a check made once per process (`tests/contract/conftest.py`)

```python
@dataclass(frozen=True)
class Diagnosis:
    reason: str  # the contract's own text, never Stripe's

class Once:
    """Runs a check at most once per process and keeps what it found."""
    async def __call__(self, check: Callable[[], Awaitable[T]]) -> T: ...
```

- First call: runs `check()`. A value is kept and returned. A
  `pytest.fail` (the contract's own diagnosis: not ready within the bound, a
  world missing, a generation incomplete) is kept as a `Diagnosis` with its
  message, then re-raised. Anything else -- `pytest.skip`, a platform error,
  `KeyboardInterrupt` -- is not kept and propagates: the next call checks
  again.
- Later calls: the kept value, or `pytest.fail(diagnosis.reason)` at once.
- What `check` returns is immutable: frozen dataclasses, tuples,
  `MappingProxyType` over a dict copied once.

Two module-level instances: `READINESS` and `WRITE_GENERATION`. Tests swap
them for fresh ones with `monkeypatch.setattr`.

### 2. Readiness (`stripe_platform`)

- `Readiness` (frozen): `seeded` (a read-only mapping of world name to the
  existing frozen `Seeded`) and `usage` (subscription id, period).
- `stripe_platform()` keeps its order: no key → skip; no SDK → skip; then
  builds **its own** `StripeBillingProvider`, and asks
  `await READINESS(lambda: _readiness(provider))`. `_readiness` is today's
  `_ready` plus the usage world's subscription and period, now read inside
  the check (today `stripe_platform` reads them after it).
- The `Platform` it returns is built from the kept data and this test's
  provider; `unreachable` stays a lambda building a new provider.
- `_ready` and `_look` are unchanged.

### 3. The write generation (`stripe_writing`)

- `WriteGeneration` (frozen): `number`, `invoices` (read-only mapping of
  purpose to invoice id), `duplicate` (a frozen `Duplicate`: email, target
  invoice, investigation instant).
- `stripe_writing()`: key and `STRIPE_WRITES` → else skip; SDK → else skip;
  builds its own provider (writer included) and read-only provider; asks
  `await WRITE_GENERATION(lambda: _generation(provider.reader))`.
  `_generation` is today's discovery: no write run → `pytest.skip` (not
  kept); an incomplete generation or missing purposes → `pytest.fail` (kept).
- Pinned: the first generation found stays for the process; a write run
  opened later is not seen until the next session.
- `untouched` and `credit_notes` are built per test on this test's reader:
  the freshness check stays per scenario.
- `Writing.duplicate` becomes the `Duplicate`;
  `tests/contract/test_stripe_execution.py` reads `duplicate.email`,
  `.target_invoice`, `.investigate_at`.

### 4. Documents

- `docs/operations.md`, "The contract on Stripe": preconditions checked
  once at the start of a session, not promised to stay true; no seed run and
  no change to the worlds in use during a session; the write contract run
  sequentially, one session per write generation; with pytest-xdist, once
  per worker. The duration of a run, measured by the maintainer (SC-001).
- The conftest docstring says the same.

## Tests (FR-007), in `tests/contract/test_readiness.py`, CI

Each test with fresh `READINESS` and `WRITE_GENERATION`:

| Case | How |
|---|---|
| A success shared | `Once` around a counting check: two calls, one check, the same value. |
| A failure shared | `_ready` on `Hung` with a short bound: the first call fails after the wait; a second fails at once with the same reason, the check not run again. |
| A world missing shared | `_look` on an account lacking a world: its diagnosis kept and repeated. |
| A skip not kept | a check raising `pytest.skip`: the next call runs it again. |
| An interruption not kept | a check raising `KeyboardInterrupt`: propagates; the next call runs it again. |
| No Stripe text kept | a diagnosis's reason equals the contract's message; a check raising a Stripe-shaped error with a sentinel keeps nothing. |
| Immutable | the kept `seeded` and `invoices` refuse assignment; a `Seeded` refuses attribute change. |
| Distinct loops, distinct providers | a `pytester` sub-run, isolated from the outer session, of two async tests marked `loop_scope="function"` sharing that sub-run's cache; each calls `stripe_platform()` with a key set and `connect` and `_ready` stubbed, and records its loop's and provider's identity: two loops, two providers, one readiness check, equal data. |
| The fake untouched | `fake_platform()` and `fake_writing()` leave both caches empty. |
| Two caches apart | filling `WRITE_GENERATION` leaves `READINESS` empty, and the reverse. |
| Pinned | a second discovery after a new generation is noted returns the first. |
| Freshness per scenario | two `stripe_writing()` calls sharing one discovery; each `untouched(invoice)` called: one discovery, two real credit-note reads; a credit note added between them makes the second answer False. |

## Project Structure

```text
specs/003-contract-readiness-once/   spec.md, plan.md, tasks.md
tests/contract/conftest.py
tests/contract/test_readiness.py
tests/contract/test_stripe_execution.py
docs/operations.md
```

## Complexity Tracking

None.
