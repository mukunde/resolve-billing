# Feature Specification: A pending approval survives a real service restart

**Feature Branch**: `005-restart-pending-approval`

**Created**: 2026-10-25 · **Revised**: 2026-10-25, after review r182

**Status**: Draft -- for the maintainer's approval

**Input**: The maintainer chose, on 2026-10-25, the workstream "the four
milestone failures" (README, "Roadmap": the first milestone is declared
reached only with evidence of four failures handled -- a restart during an
approval, an expired approval, a repeated delivery, a timeout after a
write). The ranking and this first slice were worked out with Codex (r180,
r181) and the framing approved by the maintainer the same day.

**Behaviour changed**: no runtime behaviour. The capability *Approval and
execution* gains a stated guarantee: a pending action survives a restart of
the service -- what the code already does, written into its contract and now
proven. **The evidence changes**: a new end-to-end test drives the service
as a separate operating-system process, stops it and starts a new one on the
same database; `docs/operations.md` gains how an operator finds and resumes
an action after a restart.

**Sensitive change**: yes, by the constitution's rule on contracts: a new
normative guarantee in a capability's contract, even with no runtime change.
The maintainer approves the final diff by its commit.

## Context

The workstream is staged; each stage is its own specification:

- **A. The service boundary, a real process restart**, mock engine and fake
  billing platform: (A1) a pending approval survives a restart -- *this
  specification*; (A2) an approval expires across a restart; (A3) the same
  dispute delivered twice, before and after a restart, queues one action and
  one write; (A4) the response lost after a committed write, then a restart,
  reconciled to exactly one write.
- **B.** The same through the orchestrator (local n8n and Mailpit).
- **C.** A confirmation on Stripe in test mode.

Reaching the milestone also takes the support-tool integration, monitoring
and evidence on a real platform: outside this workstream.

What exists: `tests/unit/test_execute_action.py` recovers a lost response
"across a restart" by reopening the SQLite store in the same process, the
same provider object kept in memory; `tests/integration/test_approval_flow.py`
drives approval and execution through the API in one process. Neither
stops the service as an operating system stops it.

## Clarifications

### Session 2026-10-25

- Q: Does the fake billing platform need to keep its history across the
  restart? → A: Not for A1: the restart happens before any write, so the
  fake can start empty in the new process. An on-disk ledger for the fake,
  and a deterministic way to lose a response after a committed write, belong
  to A4, where they are needed, as sensitive verification infrastructure
  (reviewed in r181).
- Q: How is "one write on the platform" observed, the platform living inside
  the service's process? → A: Through the service's own reads of the
  platform: the replayed execution returns the same reference, and a status
  read (`POST /actions/{id}/status`, which asks the platform for every write
  under the action's key) returns the action executed under that same
  reference, with no attention mark (`several_writes` or any other), the
  platform's status a success and its observation recorded. That is
  evidence of **one matching write under this action's key**, not an
  independent count of every write on the platform; the test never reaches
  into the process, and the limit is said.
- Q: How is "nothing written before approval" observed? → A: It cannot be
  counted directly: a status read refuses a pending action, and the restart
  discards the first process's fake. The evidence available: the action
  pending, with no platform reference, and an authenticated execution
  attempted before approval refused without moving the action. Said so.
- Q: Which dispute? → A: The synthetic one the integration tests already
  use: customer `alice@acme.fr`, invoice `inv_002`, which the mock engine
  turns into a pending credit note of EUR 99.
- Q: A route to list actions for the operator? → A: Not in this slice: it
  raises authorisation and disclosure questions of its own. Recovery is
  documented by run or action identifier, and by read-only SQL on the
  service's database.
- Q: Graceful or abrupt stop? → A: Both: a portable graceful stop and a
  hard kill, each followed by a restart. Neither proves recovery from a crash
  inside a database transaction; said so.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - An approval waiting when the service restarts (Priority: P1)

A dispute has been investigated and its credit note waits for a person. The
service is stopped -- cleanly, or killed -- and started again. The person
approves; the orchestrator executes; the credit note is written once.

**Independent Test**: the new end-to-end test, in CI, on the developer's
machine and on Linux, in seconds, with nothing beyond localhost.

**Acceptance Scenarios**:

1. **Given** a pending credit note of EUR 99 on `inv_002`, created through
   the authenticated API, **When** the service process is stopped and a new
   one starts on the same database and configuration, **Then** the action is
   still pending, with the same identifier and review fingerprint and no
   platform reference, and an authenticated execution attempted before
   approval is refused without moving it.
2. **Given** that restarted service, **When** an approval arrives without an
   approver's credentials, **Then** it is refused and the action stays
   pending.
3. **Given** that restarted service, **When** an authenticated approver
   approves and the orchestrator executes, then executes again, **Then** the
   action is executed once: both answers carry the same platform reference,
   the approver recorded is the authenticated one, and a status read returns
   it executed under that reference with no attention mark, a successful
   platform status and the observation recorded.
4. **Given** the same flow, **When** the service is killed instead of
   stopped cleanly, **Then** the outcome is the same.

### User Story 2 - The operator knows how to resume (Priority: P2)

After a restart, the operator finds the actions waiting or uncertain and
knows what is safe to do.

**Acceptance Scenarios**:

1. **Given** `docs/operations.md`, **When** the operator follows it after a
   restart, **Then** they find an action by its run or action identifier,
   or list the waiting and uncertain ones with a read-only SQL query, and
   know that finding an action neither resumes the orchestrator's workflow
   nor executes anything.

### Edge Cases

- The service does not start: the test fails within a bounded time, with the
  process's last output, and leaves no process behind.
- The first process has not exited: the test does not start the second one;
  it fails, naming why.
- A port taken by another program: the test does not race for it silently.

## Requirements *(mandatory)*

- **FR-001**: A test MUST start the service as a separate operating-system
  process -- the current Python interpreter, one worker, no reload, bound to
  the loopback address -- with its own temporary database, the mock engine,
  the fake billing platform and the service and approver credentials, and
  wait for its health route within a bounded time, checking that the
  process is still alive.
- **FR-002**: The test MUST stop that process and confirm it exited before
  starting a replacement on the same database and configuration -- once by
  a portable graceful stop, once by a hard kill -- and MUST always reap
  every process it started, escalating after a deadline.
- **FR-003**: After the restart, the test MUST show, through the HTTP API
  only: the action still pending with the same identifier and fingerprint
  and no platform reference; an authenticated execution before approval
  refused, the action unmoved; an unauthenticated approval refused; an
  authenticated approval recorded under the authenticated approver; an
  execution and a replayed execution returning the same platform reference;
  a status read returning the action executed under that reference, with no
  attention mark, a successful platform status and the observation
  recorded. The documents call this one matching write under the action's
  key, not a count of every platform write.
- **FR-004**: The test MUST be deterministic -- bounded polling, no fixed
  sleeps -- run in CI with no network beyond localhost, and, on failure,
  report the processes' bounded output.
- **FR-005**: `docs/operations.md` MUST say how an operator, after a
  restart, finds an action by run or action identifier, lists waiting and
  uncertain actions with read-only SQL (SQLite opened read-only), and what
  is safe: finding an action resumes nothing and executes nothing.
- **FR-006**: The approval-and-execution contract MUST state that a pending
  action survives a restart of the service (it lives in the database, not
  in the process), if it does not already, and name the new test under
  "Covered by".
- **FR-007**: The specification and the documents MUST say what this slice
  proves and what it does not: the local service boundary; not the
  milestone, not exactly-once on a real platform, not recovery from a crash
  inside a database transaction, not the orchestrator's resumption.

## Success Criteria *(mandatory)*

- **SC-001**: The new test passes in CI and on the maintainer's machine,
  in seconds, for both the graceful stop and the hard kill.
- **SC-002**: CI stays green; the mock baseline matched; no runtime code
  changes. A runtime change found necessary during the work stops it for a
  review of scope with the maintainer before it is made.

## Assumptions

- The service's state that matters to an approval -- the action, its
  fingerprint, the run -- lives in its SQLite database (`AUDIT_DB`); the
  fake billing platform's in-memory state is empty and identical at each
  start, which is enough while no write precedes the restart.
- `uvicorn` starts the application as `app.main:app`, as the README's
  "Run it" does.
- Nothing is merged before the maintainer's Gemini full run of 2026-10-25.
