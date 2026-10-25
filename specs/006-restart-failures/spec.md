# Feature Specification: An expired approval, a repeated delivery and a timeout after a write, across a real restart

**Feature Branch**: `006-restart-failures`

**Created**: 2026-10-25

**Status**: Draft -- for the maintainer's approval

**Input**: The maintainer asked, on 2026-10-25, to finish the workstream "the
four milestone failures" before framing step 4 with Claude. A1 (a pending
approval survives a restart) is merged (specs/005, PR 24). The rest of
stage A -- A2, A3, A4 -- was framed with Codex (r193) and the framing
approved by the maintainer the same day.

**Behaviour changed**: no runtime behaviour. The capability *Approval and
execution* gains three proven guarantees, written into its contract: an
approval's deadline survives a restart and expires it; the same correction
delivered again after a restart is the same action; a write whose answer was
lost is found again after a restart, never written twice. **The evidence
changes**: three end-to-end tests on the A1 harness, and, for A4, a test
launcher and a persistent fake billing platform that live in `tests/`.

**Sensitive change**: yes -- new normative guarantees in a capability's
contract, and test infrastructure on which financial guarantees rest (a
persistent fake platform, a deliberate fault). The maintainer approves the
final diff by its commit.

## Context

Stage A proves the four failures at the HTTP service boundary, with the
service run as a real operating-system process, stopped and started again.
A1 is done. This specification covers the other three; stage B (through the
orchestrator: n8n and Mailpit) and stage C (on Stripe in test mode) remain,
and so do the milestone's other conditions (the support-tool integration,
monitoring, evidence on a real platform). Said plainly in the documents.

## Clarifications

### Session 2026-10-25

- Q: One specification for three failures? → A: Yes: they share the harness
  and the recovery contract. A4, the riskiest, is an implementation
  checkpoint of its own: A2 and A3 first; then the persistent fake and its
  fault, reviewed, before A4's scenario is built (r193).
- Q: A2's wait? → A: The test follows the deadline the action records
  (`expires_at`), not an assumed delay: it queues with a short deadline,
  checks the pending state, stops the service, waits with a bounded monotonic
  timeout until that recorded instant has passed, then restarts. The
  deadline is long enough that the first assertions cannot race it on a
  loaded machine.
- Q: A3, what is "the same"? → A: The investigation makes a new run each
  time; the queue finds the correction already pending and returns that
  action, whose run stays the first. So: two distinct runs, one action --
  same identifier, key and deadline -- and one stored action. This is
  deduplication of the correction, not of every delivery.
- Q: A4, how does the platform outlive the service, and how is the answer
  lost? → A: In `tests/` only: a launcher started by the harness instead of
  `uvicorn app.main:app` builds the application and overrides the
  service's factories -- `get_approvals` and `get_investigator`, which call
  `get_provider` directly -- so that investigating, approving, executing,
  reading a status and the review page all use one test-owned fake. That
  fake persists its writes to a ledger file of its own (never the service's
  database) and, once, after committing a write and the fault's spending
  durably together, raises `TimeoutError` instead of answering.
- Q: A crash mid-flight, leaving the action `executing`? → A: Not in this
  slice: an abandoned claim is reconciled after a hard-coded five minutes
  (`STALE_CLAIM`); testing it needs a wait or a test-only clock. Said so.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A2: an approval expires across a restart (Priority: P1)

A credit note waits for a person; the service stops; its deadline passes
while it is down; the service starts again. Nobody can approve or execute it
any more.

**Independent Test**: an end-to-end test on the A1 harness, with a short
approval deadline (`RESOLVE_APPROVAL_TTL_HOURS`).

**Acceptance Scenarios**:

1. **Given** a pending credit note with its recorded deadline, **When** the
   service is stopped, the deadline passes, and a new process starts on the
   same database, **Then** reading the action shows it `expired`, with the
   same deadline as recorded.
2. **Given** that expired action, **When** an authenticated approver approves
   it through the API, and through the review page's form (a POST), and the
   orchestrator executes it, **Then** each is refused and the action stays
   expired, with no platform reference.

### User Story 2 - A3: the same correction delivered again after a restart (Priority: P1)

The same dispute arrives once, the service restarts, and it arrives again.
One action waits for a person; once approved and executed, one write.

**Acceptance Scenarios**:

1. **Given** the synthetic dispute investigated on the first process,
   **When** it is investigated again on the replacement process, **Then**
   the two investigations have distinct run identifiers and return the same
   action -- identifier, idempotency key and deadline -- and one action is
   stored for that key.
2. **Given** that one action, **When** it is approved and executed twice,
   **Then** both executions return the same platform reference, and a status
   read shows one matching write under the action's key, no attention mark.

### User Story 3 - A4: a timeout after a write, then a restart (Priority: P1)

The platform writes the credit note, but its answer never reaches the
service; the service stops; a new one starts. The write is found, never
made twice.

**Acceptance Scenarios**:

1. **Given** an approved credit note and a platform that commits the write
   then loses its answer once, **When** the orchestrator executes, **Then**
   the answer is 504, the action is `unknown_outcome`, and the platform's
   own ledger already holds one write.
2. **Given** that state, **When** the service is killed and a new process
   starts on the same database and the same platform ledger, and the
   orchestrator executes again, **Then** the action is `executed` under the
   reference the ledger holds, the ledger still holds exactly one write, and
   a replayed execution returns the same reference.

### Edge Cases

- The persistent fake's ledger cannot be read, or is inconsistent: refused,
  never reset.
- The fault is spent once and stays spent across the restart.
- Any test failure leaves no process behind (the A1 harness's guarantee).

## Requirements *(mandatory)*

- **FR-001**: A2's test MUST start the service with a short approval
  deadline, queue the synthetic credit note, keep its recorded `expires_at`,
  stop the service, wait under a bounded monotonic timeout until that
  instant has passed, restart on the same database, and show through HTTP:
  the action `expired`, the deadline unchanged, an authenticated API
  approval refused, an authenticated review-page POST refused, an execution
  refused, the action still expired with no platform reference.
- **FR-002**: A3's test MUST investigate the same synthetic dispute on two
  processes, separated by a restart, and show: distinct run identifiers;
  the same action identifier, idempotency key and deadline; one stored
  action for that key; after approval, two executions with one reference;
  a status read with one matching write and no attention mark.
- **FR-003**: A4 MUST run the service through a test launcher in `tests/`
  that overrides `get_approvals` and `get_investigator` -- and any other
  factory reaching the provider -- to use one test-owned persistent fake,
  with the normal action store, audit sink and mock engine; the launcher
  serves with uvicorn programmatically, one worker, loopback, its graceful
  and hard stops as A1's.
- **FR-004**: The persistent fake MUST live in `tests/`, keep its ledger in a
  file of its own, persist the complete platform writes (key, action
  identifier, references, allocation, amounts, currency, status, instants)
  and derive what an invoice has given back from them, commit each write and
  the fault's spending durably together, reload them on start, and refuse --
  never reset -- a ledger it cannot read. Its idempotency and its cap on what
  an invoice gives back MUST be tested on their own, so the recovery test
  does not rest on an unverified double.
- **FR-005**: A4's test MUST show: the first execution answered 504 and the
  action `unknown_outcome`, with one write already in the ledger read
  directly; after a hard kill and a restart on the same database and ledger,
  an execution reconciling to `executed` under that write's reference, the
  ledger still holding exactly one write, and a replay returning the same
  reference.
- **FR-006**: `docs/operations.md` "After a restart" MUST say what an operator
  sees and does for an expired action, a repeated delivery and an unknown
  outcome (reconciled by the next execution, never written blind); the
  approval-and-execution contract MUST state the three guarantees, list the
  new tests under "Covered by", and its limits.
- **FR-007**: No runtime code changes; one found necessary stops the work for
  a review of scope with the maintainer. The documents MUST say what stage A
  proves and what remains: stage B, stage C, an abandoned claim mid-flight,
  the milestone's other conditions.

## Success Criteria *(mandatory)*

- **SC-001**: The three new tests pass in CI and on the maintainer's machine,
  each in seconds.
- **SC-002**: The persistent fake's own tests pass; CI green; the mock
  baseline matched.

## Assumptions

- `RESOLVE_APPROVAL_TTL_HOURS` is read as a number of hours, fractions
  allowed, at each queueing, as `app/infrastructure/actions.py` does today.
- Reconciliation of an `unknown_outcome` happens on the next execution, as
  the contract says, with no wait.
- The A1 harness (`tests/e2e/_service.py`) is extended, not replaced.
