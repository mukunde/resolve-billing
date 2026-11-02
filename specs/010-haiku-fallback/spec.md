# Feature Specification: A fallback from `gpt-6-luna` to Claude Haiku 5.5

**Feature Branch**: `010-haiku-fallback`

**Created**: 2026-11-02

**Status**: Approved by the maintainer on 2026-11-02, at `a45c9d5`, with its threshold of 30

**Input**: Spec 009 put `gpt-6-luna` on the demonstrator's service path with no
automatic fallback, and left the question to this specification, to be decided
before the holdout reference run. The framing was worked out with Codex (r271,
r282) and decided by the maintainer on 2026-11-02:

- **build it now**, as an optional selection beside standalone Luna;
- **one attempt each**: Luna for at most 30 seconds, then Haiku for at most 60
  seconds or what remains, within the same 90 seconds;
- **the reference run measures the composite**, with the fallback on, declared
  before the run.

## Clarifications

### Session 2026-11-02

- Q: What does a fallback protect against? → A: A failure of OpenAI's service
  or of the path to it. A cut of the local network reaches both vendors, and the
  dispute then escalates as it does today. Its value is for the pilot: on the
  demonstrator, a failure already goes to a person.
- Q: Does Luna lose its retries? → A: In the composite only, on purpose. A
  passing 429 moves to Haiku instead of waiting. Standalone Luna keeps spec 009's
  bounds, unchanged, and stays selectable.
- Q: Which Haiku? → A: `claude-haiku-5-5`, the configuration spec 008 measured,
  pinned, whatever `CLAUDE_MODEL` says, with the SDK's retries off.
- Q: What about the reference run? → A: It measures the composite. With no
  outage it observes Luna's path and does not validate the fallback. If Haiku
  answers some cases, the result is kept with each case attributed, and it is
  never rerun to obtain a Luna-only score. The maintainer's attestations must
  cover both model families (FR-013).
- Q: Real data? → A: Out of scope, as in spec 009. A fallback means the real-data
  gate depends on both vendors' terms; Anthropic's default routing is global, with
  no regional commitment.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - OpenAI is down, the dispute is still investigated (Priority: P1)

With the composite selected, when Luna's attempt fails for a reason outside the
model, Haiku answers the dispute, through the same rules, policy and approval.

**Why this priority**: It is the feature.

**Independent Test**: With fake clients, each trigger ends in Haiku's decision,
and the run names Haiku as the model that answered.

**Acceptance Scenarios**:

1. **Given** a timeout, a connection failure, a 429 or a 5xx on a request actually
   sent, **When** Luna's attempt ends, **Then** one Haiku attempt follows, with the
   same masked payload and no wait for a `Retry-After`.
2. **Given** Haiku answers, **When** the decision is recorded, **Then** it goes
   through the same action policy and human approval as any engine's.
3. **Given** Haiku fails too, **When** its attempt ends, **Then** it ends as on a
   standalone engine (FR-008), and no third attempt follows.

---

### User Story 2 - A fallback never hides what must be seen (Priority: P1)

A refusal, a wrong model or an invalid answer does not move to another vendor: they
say something is wrong, and a second vendor would bury it.

**Why this priority**: Constitution, principles II and V.

**Independent Test**: With fake clients, each exclusion sends nothing to Anthropic
and ends as it does on standalone Luna.

**Acceptance Scenarios**:

1. **Given** a 4xx refusal other than a 429, a wrong model, an unusable answer, a
   cancellation or a defect, **When** Luna's attempt ends, **Then** Haiku is not
   called.
2. **Given** a failure before anything was sent (a limiter, a queue, the deadline, a
   recording failure), **When** it happens, **Then** Haiku is not called.

---

### User Story 3 - Who answered can always be read (Priority: P1)

Every run says which vendor answered and what each leg cost, and an answer from
Haiku can never read as Luna's.

**Why this priority**: The audit is the point; a mixed result without attribution
would be unusable, for the reference run first.

**Independent Test**: A run that fell back shows both attempts, one correlation id,
Haiku as the answering model, and Luna's unknown cost kept apart.

**Acceptance Scenarios**:

1. **Given** a fallback, **When** the run is read, **Then** the engine's name is the
   composite's, the model is Haiku's, and both attempts appear in order.
2. **Given** a timed-out Luna attempt, **When** Haiku answers, **Then** the total is
   shown as a lower bound.

---

### User Story 4 - The way back stays one variable (Priority: P2)

The operator selects the composite, standalone Luna, Claude or the mock by one
variable, and each behaves as it does alone.

**Independent Test**: Each selection passes the same smoke test; standalone Luna
keeps spec 009's bounds.

### Edge Cases

- Both vendors unreachable: the outage path, within the 90 seconds.
- Luna uses all its 30 seconds: Haiku has what remains, at most 60 seconds.
- A timed-out Luna request may have been processed and billed: the same payload has
  then reached both vendors. Accepted on synthetic data, and stated.
- Haiku answers from another model: discarded, and the dispute escalates, as for
  Luna.
- Anthropic's account out of credit: a refusal, as on any engine.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The service MUST offer the composite as a closed choice of its engine
  selection, under its own stable name and policy version. Standalone Luna, Claude and
  the mock MUST behave as they do today, and the default stays the mock.
- **FR-002**: Luna's leg MUST send the request spec 008 evaluated, and Haiku's the
  request spec 008 evaluated for `claude-haiku-5-5`, pinned by tests to their
  fingerprints, whatever `CLAUDE_MODEL` says, with the SDK's retries off.
- **FR-003**: The composite MUST make at most one Luna attempt, at most 30 seconds,
  then at most one Haiku attempt, at most 60 seconds or what remains, within 90
  seconds for the whole engine stage, queues, recording and waits included.
- **FR-004**: The fallback MUST trigger only after a request actually sent to OpenAI
  ended in a timeout, a connection failure, an HTTP 429 or a 5xx.
- **FR-005**: It MUST NOT trigger on any other 4xx, a wrong model, an unusable answer,
  a failure before the send, a recording failure, a budget refusal, a cancellation or a
  defect. Those end as they do on standalone Luna.
- **FR-006**: The choice to fall back MUST be made per dispute, with no memory of
  earlier failures. Each vendor's in-flight and rate limits stay shared across disputes,
  as in spec 009.
- **FR-007**: One correlation id MUST link both legs' attempts to the run. Each attempt
  MUST carry its provider and model, and the run the model that answered. Costs MUST
  stay apart by provider, unknowns explicit, and a cost with an unknown attempt reads as
  a lower bound. Traces carry the same, with the same allowlist.
- **FR-008**: Haiku's leg MUST end as a standalone engine's would: an outage takes the
  outage path; an unusable answer escalates; a wrong model takes the identity path; a
  refusal is the documented error; a cancellation or a defect propagates. There is no third
  leg. No vendor text reaches a response, an audit row, a log or a trace.
- **FR-009**: Selecting the composite MUST be refused at start unless spec 009's
  conditions hold and Anthropic's key, declared account and ambient settings do too. The
  refusal names what is missing.
- **FR-010**: The evaluation path MUST keep its engines, guards and ledgers, and the
  plain runner MUST refuse the composite's name, before reading a case or building a
  client: the measurement tool of FR-012 is its only evaluation path. The
  Anthropic client's name and documentation MUST stop saying it is evaluation-only, its
  request parity tests staying, and no result or fingerprint changing.
- **FR-011**: ADR-009, ADR-013, the contracts, the operations guide, the README and
  `.env.example` MUST state the composite, its triggers, its bounds and the double-send
  case in the same change. ADR-009 MUST keep this to synthetic data.
- **FR-012**: The behaviour MUST be measured: fault-injection tests for every trigger
  and exclusion, and one complete run of the development cases through the composite
  with Luna disabled in the measurement tool, authorised before it is made, reserved and
  settled against the Anthropic envelope. It MUST NOT count toward spec 008, and it is
  not rerun to reach a score.
- **FR-013**: The holdout reference run MUST measure the composite, with the fallback
  on, declared before the run, its result attributing each case to the model that
  answered, and never rerun to obtain a Luna-only score. ADR-013 MUST record this
  configuration, and extend the maintainer's attestations of spec 009, FR-012, to both
  engines: the cases are entirely synthetic; generated independently of both, adjudicated
  and frozen (constitution, principle IX); and written by a model of a family other than
  OpenAI's and Anthropic's (the README's rule). A conversation's isolation shows neither of
  the last two by itself. Relaxing either goes through the change the constitution requires
  of its holdout policy, not an ordinary decision. This specification makes no change that
  enables the run.
- **FR-014**: CI MUST make no paid call. The mock baseline stays matched.

### Key Entities

- **The composite**: the two configurations, their order, their bounds, under one name.
- **A leg**: one vendor's attempt within a dispute.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For each trigger, with Haiku healthy, the dispute is answered by Haiku
  within 90 seconds.
- **SC-002**: For each exclusion, nothing reaches Anthropic.
- **SC-003**: 100% of the runs that fell back show both attempts, the answering model
  and the costs by provider.
- **SC-004**: Standalone Luna's and Claude's tests pass unchanged.
- **SC-005**: Each leg's request equals the evaluated one on every setting.
- **SC-006**: One complete run through the composite with Luna disabled, stored. Any
  unsafe action or a score below 30, the lowest of spec 008's Haiku runs, is a trigger
  for the maintainer's review, not a proven regression, and his decision is recorded.
- **SC-007**: ADR-013 records the reference configuration before the reference run.

## Assumptions

- Nothing is deployed: the service is this repository's local API, on synthetic data.
- About $0.97 of the Anthropic envelope remains, and a Haiku run has cost about $0.01.
- The composite's direct calls bypass the envelopes, as Luna's do: only the two
  accounts' prepaid credit bounds them.
- No circuit breaker, metric or alert is part of this slice.
- Prompt v4 comes after the reference run, as decided.
