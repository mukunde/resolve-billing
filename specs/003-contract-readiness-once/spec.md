# Feature Specification: The seeded account checked once per contract session

**Feature Branch**: `003-contract-readiness-once`

**Created**: 2026-10-23 · **Revised**: 2026-10-23, after review r143

**Status**: Draft -- for the maintainer's approval

**Input**: The maintainer's choice of 2026-10-23 (step 3 of the order set
after specs/002-stripe-writes): contract tests check the seeded Stripe
account once per session instead of before every test. Framing reviewed by
Codex (r142) and approved by the maintainer with its six adjustments.

**Behaviour changed**: none in the product -- no capability's contract
changes. **The verification set-up changes**: the Stripe half of the
contract tests checks the account's preconditions once at the start of a
session, where it checked them before every test. `docs/operations.md`
("The contract on Stripe") is updated accordingly.

**Sensitive change**: yes, by its effect on the evidence: it changes how
fresh the preconditions behind the contract's results are (constitution,
"Changes that need the maintainer's approval of the final diff"). The pull
request needs the maintainer's explicit approval of the final diff.

## Context

About thirty-four Stripe contract tests each call `stripe_platform()`,
whose `_ready()` reads every test clock, its customers, each of the
fourteen worlds' target invoice and the usage world's meter summary --
some thirty-five requests -- before the test itself. The maintainer's last
full run took nine minutes, mostly that. The preconditions found at the
start are reused for the rest of the session, with no promise that they stay
true: someone, or Stripe itself, may change the account in between.

## Clarifications

### Session 2026-10-23

- Q: What is kept between tests? → A: The readiness **result** only -- the
  seeded worlds (customer, target invoice, investigation instant) and the
  usage world's period: identifiers, dates and periods, in structures no
  test can change. Never a provider, an SDK client, a coroutine, a task, or
  a raw exception that could hold a client, a key or Stripe's text. Each
  test still builds its own provider and reads Stripe for real.
- Q: What if the account is not ready? → A: The outcome is kept too, as a
  controlled diagnosis: the first Stripe test fails after the bounded wait,
  and the later ones fail at once with the same reason. A missing key or
  SDK still skips; an interruption propagates and is not kept.
- Q: And the write contract? → A: A cache of its own: the latest write
  generation's identity -- its invoices by purpose, its duplicate customer --
  found once per session and pinned: a generation opened during the session
  is never picked up. The freshness check (no credit note on a scenario's
  invoice) stays per scenario. Write tests run alone never wait for the
  clocked worlds.
- Q: What does it promise? → A: Preconditions checked once at the start of
  the session, not that they stay true: during a session, nobody runs the
  seed or changes the worlds in use.
- Q: In parallel? → A: A session's cache belongs to its process: with
  pytest-xdist, readiness is checked once per worker. The write contract
  runs sequentially, and two sessions never use the same write generation
  at once; both stated, not enforced by code.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A contract session checks the account once (Priority: P1)

The maintainer runs the contract on the seeded account. The first Stripe
test waits, within its bound, for the account to be ready; every later test
starts at once from what that check found.

**Independent Test**: in CI, with `_look` replaced by a counter: several
platform fixtures in one session make one check; their worlds are equal and
independent copies.

**Acceptance Scenarios**:

1. **Given** a ready account, **When** a session runs every Stripe contract
   test, **Then** the account's preconditions are read once, and each test
   still reads Stripe for its own property.
2. **Given** an account not ready within the bound, **When** the session
   runs, **Then** the first Stripe test fails with the reason after the
   wait, and every later one fails at once with that same reason, without
   waiting again.
3. **Given** no key, or no SDK, **When** the session runs, **Then** the
   Stripe tests are skipped with the reason, as today, and nothing is kept
   that would change the next session.

### User Story 2 - Write tests find their generation once (Priority: P2)

**Acceptance Scenarios**:

1. **Given** a write generation, **When** the write tests run, **Then** it
   is found once, pinned for the session, and each scenario still checks its
   invoice carries no credit note.
2. **Given** a write run opened during the session, **When** a later test
   runs, **Then** it still uses the generation found first.
3. **Given** only the write tests are run, **When** they start, **Then**
   they do not wait for the clocked worlds.

### Edge Cases

- A test that changes what it was given: the next test sees the original.
- The fake platform: never touches either cache.
- An interruption (KeyboardInterrupt) during the first check: propagates,
  and the next session checks again.

## Requirements *(mandatory)*

- **FR-001**: The readiness of the seeded account MUST be checked at most
  once per pytest process, by the first Stripe test that needs it, within
  the existing bound.
- **FR-002**: What is kept MUST be loop-independent, immutable data:
  identifiers, dates, periods. No provider, client, coroutine, task or raw
  exception.
- **FR-003**: A failed check MUST be kept as its reason (the text the
  contract already writes, with no Stripe text), and later tests MUST fail
  with it without waiting. Skips stay skips; interruptions propagate.
- **FR-004**: The write generation MUST be found once per process and
  pinned, in a cache apart from readiness; the per-scenario freshness check
  MUST stay.
- **FR-005**: Every Stripe test MUST still build its own provider and read
  Stripe for the property it checks.
- **FR-006**: `docs/operations.md` MUST say: preconditions checked once at
  the start of a session; no seed and no change to the worlds in use during
  one; the write contract run sequentially, one session per generation;
  with pytest-xdist, once per worker.
- **FR-007**: Tests in CI, with a fresh cache in each: a success shared;
  a failure (not ready within the bound) shared; a world missing shared as
  its diagnosis; a skip not kept; an interruption propagated, nothing kept,
  and the next check made again; the fake independent of both caches; a
  copy changed by one test unseen by the next; two tests on distinct event
  loops sharing the data with distinct providers; the write generation
  pinned and independent of readiness.

## Success Criteria *(mandatory)*

- **SC-001**: On the maintainer's account, a full contract run passes as
  before, with the account's preconditions read once; its duration is
  reported beside the nine minutes of 2026-10-23.
- **SC-002**: CI stays green and the mock baseline matched.

## Assumptions

- Nobody changes the seeded account during a contract session: the seed is
  run before, never during.
- `pytest-asyncio` 1.4 with `asyncio_mode = "auto"`, each test on its own
  event loop: unchanged.
