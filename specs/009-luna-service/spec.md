# Feature Specification: `gpt-6-luna` on the demonstrator's service path

**Feature Branch**: `009-luna-service`

**Created**: 2026-11-02

**Status**: Draft, for the maintainer's approval

**Input**: ADR-013 retained `gpt-6-luna` provisionally and requires a
deployment slice before any adoption (spec 008, FR-007). The framing was
worked out with Codex (r268) and decided by the maintainer on 2026-11-02:

- **scope**: Luna selectable on the demonstrator's local service path, on
  synthetic data only; real customer data goes through a separate decision;
- **hosting**: Europe investigated first, explicit United States processing
  as the fallback; global routing is not assumed acceptable for real data;
- **"deployed"**, for spec 008's SC-004: selected and verified on the
  demonstrator's service path, not production infrastructure. This reading is
  recorded before the holdout reference run.

The repository's status holds: a demonstrator on fake billing data, with
nothing deployed. This specification does not change that.

## Clarifications

### Session 2026-11-02

- Q: What does this slice authorise? → A: Sending the service's masked,
  pseudonymised payload to OpenAI when the operator selects Luna, on synthetic
  data, on the account's standard terms. Those terms are accepted for that
  limited use only: abuse-monitoring logs may keep prompts and responses for
  up to 30 days, whatever `store` says, and no region is committed.
- Q: Does the code wait for OpenAI's answer on hosting? → A: No. The two run in
  parallel: nothing real is sent, so the answer gates real data, not this
  slice. If the answer or the maintainer's later decision makes Luna fall,
  ADR-013 is reopened and rollback is one variable.
- Q: What must stay what spec 008 measured? → A: The prompt, the schema, the
  masking and the request settings. The service sends what was evaluated, or
  it is not the configuration that was chosen.
- Q: Is there an automatic fallback to another engine? → A: No, in this slice.
  When Luna fails, the outcome is the controlled one of FR-004; the only change of engine
  is the operator's, by one variable. A fallback to Claude Haiku 5.5 was
  discussed with the maintainer (r271) and goes to its own specification, 010,
  decided before the holdout reference run, because that run measures the
  configuration actually adopted.
- Q: Tracing and monitoring now? → A: Attempt-level tracing and audit, yes,
  because retries and constitution principle V require them (r272). Metrics,
  dashboards, alerts and aggregate summaries, no: nothing runs continuously, and
  they stay in the roadmap's step 4, in their own specification.
- Q: What about the holdout? → A: This slice neither runs it nor opens the way
  to it. ADR-009 authorises OpenAI on the synthetic development cases only, and
  the evaluation path's guard stays as it is. The reference run needs, first,
  an explicit ADR-009 amendment and the maintainer's two attestations
  (FR-012).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The operator runs the service on Luna (Priority: P1)

The operator selects Luna by one variable, on fake billing data, sends a
dispute through the service, and gets the same kind of investigation Claude
gives: rules first, then the engine, the policy, and a person for any money.

**Why this priority**: It is the slice: the model the rules left, running
where a decision is actually made, not only in the evaluation runner.

**Independent Test**: A dispute sent over HTTP to the local service, with Luna
selected and the fake billing platform, returns an investigation, and its audit
row names the engine and the model that answered.

**Acceptance Scenarios**:

1. **Given** Luna selected on the fake billing platform, **When** a dispute
   reaches the engine, **Then** the decision goes through the same action
   policy and human approval as any engine's.
2. **Given** a dispute the rules settle, **When** it is investigated, **Then**
   no request reaches OpenAI.
3. **Given** Luna selected, **When** a decision is recorded, **Then** its
   confidence stays self-reported: it cannot grant financial autonomy, and
   the existing review floor applies unchanged.

---

### User Story 2 - A failure of the vendor never becomes an actionable recommendation (Priority: P1)

When OpenAI refuses, stalls, answers from another model or returns no usable
answer, the outcome is the service's controlled one: an escalation to a person,
or a documented error category. No vendor text is shown or stored, and no model
recommendation is invented.

**Why this priority**: Constitution, principles II and V: the engine going
wrong must not widen what the system may do.

**Independent Test**: With a fake client, each failure kind is provoked; each
ends in the outcome the plan names for it, and none of the vendor's text
appears in the response, the audit row, the logs or the traces.

**Acceptance Scenarios**:

1. **Given** an answer from a model other than the configured one, **When** it
   arrives, **Then** it is discarded and the dispute escalates to a person.
2. **Given** a vendor that stalls, **When** the deadline passes, **Then** the
   service stops waiting, within a bound the configuration states.
3. **Given** a refused request, **When** the error is reported, **Then** it
   names a category, never the vendor's message.
4. **Given** an answer with no usage, **When** it is recorded, **Then** its cost
   reads unknown, not zero.

---

### User Story 3 - The way back is one variable (Priority: P2)

The operator returns to Claude, or to the mock, by changing the selection, with
no code change and nothing else to undo.

**Why this priority**: The choice is provisional, and it can fall.

**Independent Test**: The same service, the selection changed, runs on the
other engine and passes the same smoke test.

**Acceptance Scenarios**:

1. **Given** Luna selected, **When** the selection returns to `claude`, **Then**
   the service runs Claude as it does today, with the same settings.
2. **Given** no selection, **When** the service starts, **Then** it runs the
   mock, as it does today.

---

### User Story 4 - A configuration slip does not point Luna at real data (Priority: P1)

Luna does not start on anything but the fake billing platform, nor without the
operator declaring the data synthetic. This is a safeguard against a slip in
configuration, not proof of what the data is: the service cannot tell real text
pasted into a request from a synthetic one.

**Why this priority**: The terms are accepted for synthetic data only. The code
can hold the configuration; the content stays the operator's responsibility.

**Independent Test**: Starting the service with Luna selected and each missing
condition is refused, by name, before any request.

**Acceptance Scenarios**:

1. **Given** Luna selected and a real billing platform, **When** the service
   starts, **Then** it refuses, and says why.
2. **Given** Luna selected and no declaration, **When** the service starts,
   **Then** it refuses, and says why.

---

### User Story 5 - The hosting answer is on record (Priority: P2)

The maintainer asks OpenAI, in writing, what this account can have: European
processing and retention, then explicit United States processing. The answer
and the data processing terms are recorded, and real data stays gated until he
decides on them.

**Why this priority**: ADR-013's condition. The public documentation cannot say
what this account is eligible for.

**Independent Test**: The record states what was asked, what was answered and
when; the service's real-data gate is unchanged by it.

**Acceptance Scenarios**:

1. **Given** OpenAI's answer, **When** it is recorded, **Then** it covers the
   endpoint, the project, the model's support, the processing location and the
   retention together.
2. **Given** a regional configuration chosen later, **When** it is adopted,
   **Then** it is a new configuration, verified afresh, and any measured figure
   says which configuration it describes.

### User Story 6 - Every attempt can be read afterwards (Priority: P1)

The operator opens a run and sees each attempt Luna made: which model, how long,
how it ended, what it cost or that the cost is unknown. An attempt that never
finished shows as unfinished.

**Why this priority**: With retries, the final answer no longer tells what
happened, and constitution principle V requires failed and interrupted calls to
be traceable.

**Independent Test**: With a fake client, a success after a retry, an exhausted
retry, a refusal and a cancellation each leave the attempt records and spans the
requirements state, with no text of the vendor's or the customer's in them.

**Acceptance Scenarios**:

1. **Given** a success after a timed-out attempt, **When** the run is read,
   **Then** both attempts appear, and the total cost is not presented as fully
   known.
2. **Given** a refusal that ends without a completed run, **When** the records
   are read, **Then** the attempt is there, with its outcome category.
3. **Given** a process stopped during an attempt, **When** the records are read,
   **Then** the attempt shows as unfinished, never as successful.
4. **Given** tracing enabled, **When** a dispute goes through Luna, **Then** the
   investigation's trace holds one span per attempt.

### Edge Cases

- The vendor reports a different model identifier: the identity check stops
  it, as it did for DeepSeek, and the change is a decision, not an update. A
  change of weights under the same identifier is not detected.
- The account's credit runs out: a refusal like any other, in its controlled
  outcome.
- Several disputes at once: the vendor's limits are respected, and a rate limit
  ends bounded, not in an unbounded wait.
- A customer's message on the fake platform is real text pasted by an operator:
  the declaration is the operator's, and it is what the bound rests on.
- The service shuts down during a request: the client closes, and nothing is
  recorded as decided.
- `gpt-6-luna` is the evaluation runner's name too: the runner keeps its
  governed engine and its guards, whatever the service registers.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The service MUST offer Luna as a closed choice of its engine
  selection, behind the `DecisionEngine` port, named in the wiring module only.
  The default selection and Claude's behaviour MUST NOT change.
- **FR-002**: The service MUST send what spec 008 evaluated: the configuration
  frozen in its manifest, with the same prompt, schema, masking and request
  settings. A test MUST hold the service's request equal to the evaluated one,
  and fail on any drift of its fingerprint.
- **FR-003**: Every answer's reported model MUST be checked against the
  configured one. Any other is discarded, and the dispute escalates to a person.
- **FR-004**: Refusals, stalls, rate limits, unusable answers and unavailability
  MUST each end in a controlled outcome the plan names: an escalation recorded in
  the audit, or a documented error category. No actionable recommendation is
  invented. No vendor text reaches a response, an audit row, a log or a trace.
- **FR-005**: Retries and the time a request may take MUST be bounded, stated in
  the configuration, and tested. The SDK's own retries stay off. At most three
  attempts, each at most 60 seconds or what remains, within 90 seconds for the
  whole engine stage, waits and limiter included, because the API answers only
  once the investigation ends. Only connection failures, timeouts, HTTP 429 and
  500 to 599 are retried. Any other 4xx, a wrong model, an invalid, truncated or
  refused answer, and a programming error are not. A valid `Retry-After` is
  honoured, and when it exceeds the time left the engine stops rather than
  retrying early; with no valid `Retry-After`, the wait is 2 then 4 seconds. Cancellation
  propagates. Exhaustion is the existing outage path, an escalation to a person,
  and an unanswered attempt's cost stays unknown. These bounds differ from the
  evaluation runner's, whose deadline covers a whole run, and that difference is
  documented.
- **FR-005a**: The service MUST NOT switch to another engine on its own. After
  Luna fails, nothing is sent to any other vendor. Specification 010 decides
  whether that changes.
- **FR-006**: Requests in flight at once MUST be bounded, within the account's
  limits. The client MUST close with the service.
- **FR-007**: The audit row MUST carry the engine's name and version and, when
  a model answered, that model. Cost and usage MUST stay unknown when they are
  unknown, through the audit and the traces, and be priced by category when
  reported. Claude's records MUST stay as they are.
- **FR-007a**: Every Luna attempt MUST have durable, correlated start and terminal
  records: attempt number, provider, requested and returned model when known,
  timestamps, duration, a closed outcome category, reported usage and estimated
  cost, with unknowns explicit. The records MUST remain available after a refusal,
  an exhausted retry or a cancellation. An attempt with no terminal record MUST
  stay visibly unfinished, never successful. A completed investigation MUST link
  its attempts to the run, and the run page MUST show them. A success after an
  attempt of unknown cost MUST NOT present the total as fully known. Recording
  MUST use vendor-neutral interfaces.
- **FR-007b**: When tracing is enabled, Luna MUST emit a span per attempt, failed
  and cancelled ones included, correlated with the investigation. Only allowlisted
  metadata may be exported: no message, context, explanation, reasoning, credential
  or exception text. The chat span MUST name the real provider instead of assuming
  one, with Claude's output unchanged. Tests MUST cover success after a retry,
  exhaustion, refusal and cancellation, unresolved cost included. One check against
  a local trace backend MUST reuse a call already authorised, and it is documented:
  access, storage, retention, and that it exports nowhere else. Trace delivery is
  not durable audit storage. SDK or HTTP auto-instrumentation stays outside the
  supported setup.
- **FR-007c**: Claude's behaviour MUST stay unchanged in this slice; the retries
  hidden inside its SDK gain no attempt records, and nothing claims they do.
  Metrics pipelines, dashboards, alerts, aggregate audit summaries and production
  monitoring are out of scope: this slice adds durable Luna attempt evidence and
  optional trace export, not continuous supervision.
- **FR-008**: The confidence MUST stay self-reported: it grants no financial
  autonomy, and the existing review floor applies unchanged. Masking and the restoring
  of references MUST work as they do for Claude, the platform's reference
  patterns included.
- **FR-009**: Selecting Luna MUST be refused when the service starts, before it
  accepts a request, unless the billing platform is the fake one and the operator
  has declared the data synthetic. The refusal MUST name the missing condition.
  This is a configuration safeguard, not a control of content. Lifting it is the
  separate real-data decision, never a configuration change alone.
- **FR-010**: The evaluation path MUST keep its engine, its guards and its
  ledgers: registering Luna in the service MUST NOT change what the runner does
  under that name, and its development-only guard MUST hold.
- **FR-011**: ADR-009, the decision-engine contract, the operations guide, the
  README and `.env.example` MUST state this path, its terms and its bound in the
  same change. ADR-009 MUST keep OpenAI to synthetic data, name the service path
  and the terms accepted for it, and state the tested scope of the telemetry.
- **FR-012**: The holdout reference run MUST NOT be made on Luna until: ADR-009
  is explicitly amended for it, and the maintainer has attested, without
  exposing a case, that: the cases are entirely synthetic; they were generated
  independently of this engine, adjudicated and frozen (constitution, principle
  IX); and they were written by a model of another family than this engine (the
  README's rule). This specification records that reading of "deployed" and those
  conditions; it makes no change that enables the run.
- **FR-013**: The maintainer's enquiry to OpenAI and its answer MUST be recorded,
  with the data processing terms read, in an order: Europe, then explicit United
  States processing. Global routing MUST NOT be presented as meeting ADR-013's
  preference. The enquiry does not block this slice.
- **FR-014**: The behaviour MUST be measured: one complete run of the development
  cases through the service engine, authorised by the maintainer before it is
  made, stored and listed, its comparison to spec 008's three runs stated. It
  MUST reserve and settle against the OpenAI envelope without changing the
  request the service sends. It MUST NOT count toward spec 008 and MUST NOT change
  its result. It is not rerun to reach a score.
- **FR-015**: CI MUST make no paid call. The failure paths, the start refusals
  and the parity test run without network. The mock baseline stays matched.

### Key Entities

- **An attempt record**: one send to the vendor, from its start to its outcome,
  kept whether or not the investigation completes.

- **Luna's service configuration**: the configuration spec 008 froze, as the
  service selects it; its fingerprint is what ties it to the measurement.
- **The data declaration**: the operator's statement that this instance handles
  synthetic data; a condition of starting, not evidence of provenance.
- **The hosting record**: what was asked of OpenAI, what it answered, the terms
  read, and the maintainer's decision.
- **The attestations**: the maintainer's two statements about the holdout,
  required before its reference run.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A dispute goes end to end through the local service on Luna, on the
  fake platform, with its audit row naming the model that answered.
- **SC-002**: 100% of the failure kinds listed end in a person or a documented
  category, with no vendor text anywhere it could be read.
- **SC-003**: The service's request equals the evaluated one on every setting; any
  drift fails the suite.
- **SC-004**: Going back to Claude takes one changed variable and no code change,
  and Claude's own tests pass unchanged.
- **SC-005**: Every start without the two conditions is refused before the service
  accepts a request.
- **SC-006**: One complete run through the service, stored. Any unsafe action or a
  score below 29, the lowest of spec 008's three, is a trigger for the maintainer's
  review, not a proven regression: one run is one sample. He decides what follows,
  and the decision is recorded before this slice is called done.
- **SC-007a**: For success after a retry, exhaustion, refusal and cancellation,
  100% of the attempts can be read afterwards, and none shows as successful without
  having succeeded.
- **SC-007**: The real-data gate, the hosting record and the holdout's conditions
  are on record, and nothing in this change opens them.

## Assumptions

- The "service" is this repository's local API: nothing is hosted, and no
  production infrastructure is part of this slice.
- The account's standard terms stay as OpenAI's public documentation states them;
  this account's own eligibility is unknown until the answer comes.
- The service adds no ledger of its own, so the evaluation envelope does not
  bound its direct calls: for this local, synthetic use, only the account's
  prepaid credit does. About $4.92 of the envelope remains, and a full run has
  cost about $0.004.
- The maintainer sends the enquiry to OpenAI himself; no agent writes to a vendor
  on his behalf.
- Prompt v4 comes after the reference run, as decided, and is not touched here.
