# Feature Specification: The holdout's reference run on the composite

**Feature Branch**: `011-reference-run`

**Created**: 2026-11-03

**Status**: Approved by the maintainer on 2026-11-03, at `e8eac64`

**Input**: The holdout is frozen (2026-10-22) and its reference run is the
maintainer's (`docs/contract/evaluation.md`). ADR-013 makes the composite
`gpt-6-luna+claude-haiku-5-5`, fallback on, the configuration it measures, and lists
what the run waits for. On 2026-11-03 the maintainer attested everything ADR-013 asks.
No path runs it today: every guard refuses any case file but the development set, on
purpose. The framing was worked out with Codex (r294) and decided by the maintainer on
2026-11-03:

- **a separate reference tool**, with private output, leaving every development guard as
  it is;
- **the authorisation consumed once sending may have started**: an interrupted run is
  kept as it is, never replaced, and can only be rebuilt from its own records, with no new
  call;
- **a descriptive reading**: any unsafe action pauses for his review, no rerun follows a
  score, and any acceptance threshold is set before the run.

## Clarifications

### Session 2026-11-03

- Q: Why totals only, on every output? → A: Constitution, principle IX: a session that
  tunes the system does not read the cases. The session that will write prompt v4 must
  not see a case-level result, by the console, an error, a log or a trace. The maintainer
  sees everything, in a private place.
- Q: Is a provider outage an interruption? → A: No. The adopted composite handles it as
  the service does: Haiku, then a person. Outages are counted beside the score. A budget
  refusal, a stopped process or a defect makes the run incomplete.
- Q: Does the run retire the holdout? → A: No. Vendors processing it does not stop it
  serving blind. Retiring it is a decision under constitution IX.
- Q: What comes after? → A: Prompt v4, developed and measured on the development cases
  only. The reference's failures are not read to design it, and the holdout is not rerun
  to show progress.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The maintainer runs the reference once (Priority: P1)

He points the tool at the frozen holdout and a private folder, sees what it will measure
and spend, confirms, and gets totals; the details stay in his folder.

**Why this priority**: It is the feature: the first blind figure.

**Independent Test**: With fake legs and a synthetic stand-in set in a temporary folder,
a run writes totals to the console and details only to the private folder.

**Acceptance Scenarios**:

1. **Given** the holdout and a private folder outside the repository, **When** he
   prepares the run, **Then** the tool writes the freeze record to that folder and shows
   it, with the estimated admission budget, and sends nothing.
2. **Given** a freeze record he has read, **When** he arms the run with that record's
   hash, confirming the frozen configuration, the estimated spend and the one-shot
   consumption, **Then** the run starts on exactly those inputs, or refuses.
3. **Given** a complete run, **When** it ends, **Then** the console shows totals only,
   and the private folder holds every case's record.

---

### User Story 2 - Nothing case-level reaches a place the tuning session reads (Priority: P1)

Whatever happens, no case id, message, verdict, explanation or engine answer appears
outside the private folder.

**Why this priority**: Constitution, principle IX.

**Independent Test**: With a stand-in set holding sentinel strings in every field, a
complete run, a stopped run and a defect leave no sentinel in the console, the error
stream, the logs, the repository or any span.

**Acceptance Scenarios**:

1. **Given** a defect mid-run, **When** it surfaces, **Then** the console says the run
   stopped and where its private record is, with no traceback naming a case.
2. **Given** a tracing endpoint configured, **When** the tool starts, **Then** it refuses,
   or runs with export off, by a rule fixed in the plan.

---

### User Story 3 - One run, and only one (Priority: P1)

The authorisation is spent before the first possible send. A second run is refused, and
editing a file does not open it again.

**Why this priority**: A rerun after a score would turn the measure into tuning.

**Independent Test**: After a claim, a second start is refused before any client is built;
a failure before any possible send leaves the claim open to a retry with the same inputs.

**Acceptance Scenarios**:

1. **Given** a run that has claimed, **When** another starts, **Then** it is refused.
2. **Given** a preflight failure before any possible send, **When** he retries unchanged,
   **Then** it may proceed, while the state is still prepared.
3. **Given** a run stopped after sending began, **When** he looks, **Then** the run is
   incomplete, kept, and can be rebuilt from its records only, with no new call.

---

### User Story 4 - Totals that say what they are (Priority: P2)

The totals read as a descriptive reference: over the full set, with what was unsafe, what
went to a person, what the vendors failed, which model answered, and the spend.

**Independent Test**: A stand-in run reports every total named in FR-009, and an
incomplete run never reports a score over the full set.

### Edge Cases

- OpenAI unreachable for part of the run: Haiku answers those cases; the totals say how
  many.
- Both vendors unreachable: those cases go to a person, counted as outages.
- An envelope cannot admit an attempt: the run stops, incomplete.
- The private folder is inside the repository, or not writable: refused before anything.
- The holdout's inputs differ from what was frozen: refused before anything.
- A tracing endpoint is set in the environment: the rule of User Story 2 applies.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The reference run MUST have its own entry point. The development guards,
  `evals.service_run` and the plain runner MUST stay as they are, and refuse the holdout as
  today.
- **FR-002**: It MUST run the adopted composite, fallback on, with the service's policy and
  bounds, through the same factory and accounting. No instrumentation may change the retry or
  fallback behaviour.
- **FR-003**: The run MUST be bound to a freeze record the maintainer approves before it is
  armed: a separate preparation writes it, sending nothing; arming names its hash. It holds
  the commit and a clean tree, a commit that contains the fix of
  `.specify/bugs/uncertain-human-review/`, both configurations' fingerprints, the composite's
  policy version and bounds, the prompt version, the schema's hash, the masking version, the
  case file's hash, every fixture input's hash, the repository's fixtures included, the
  evaluation date, and the scoring definition. Any mismatch at arming or at start refuses.
- **FR-004**: Before any send, it MUST check the inputs locally, both accounts' declared ids,
  both accepted first calls, both envelopes' room for the estimated admission budget, and take
  both account locks in a fixed order. The budget is an estimate, as every reservation is since
  spec 007; reservations are kept unsettled when usage is unknown, and usage above a
  reservation settles at its reported cost, then stops the run.
- **FR-005**: The authorisation MUST have two states, prepared and consumed, under one stable
  reference identity that does not depend on the inputs: this holdout's reference run, frozen
  2026-10-22. It is kept outside the repository and every checkout, and the approved freeze
  record and case hash are bound to it immutably, so that no other folder, freeze record or
  case file opens a second run. Consumption MUST be
  persisted, atomically, before the first send. A failure while only prepared may be retried on
  the same freeze record; a crash whose state is ambiguous counts as consumed. A consumed run
  MUST refuse any other start. Opening a new run after consumption is a policy decision,
  recorded apart, never a file edit.
- **FR-006**: The private record MUST be written as the run goes, to the private folder only:
  each attempt's start and end, with its ledger ticket, and each case's outcome, written
  atomically. The folder MUST be given by the maintainer, writable, and outside every
  repository and worktree, its path canonicalised and links followed, checked before
  anything is loaded.
- **FR-007**: No case id, message, finding, verdict, explanation, engine answer or vendor text
  MAY reach the console, the error stream, a log, the repository, a trace or a span. The
  protections MUST be in place before any case is loaded and before any module that logs or
  traces is imported: log capture, SDK request logging off, and tracing export refused at start
  if configured, whatever the environment or an earlier import set. Tracebacks are caught and
  replaced by a category and the private record's path.
- **FR-008**: A provider outage MUST be handled as the service handles it, and counted. A budget
  refusal, a stopped process or a defect MUST end the run as incomplete, its records kept, with
  no automatic restart. An incomplete run MAY be rebuilt from its records only: no case
  re-executed, its unknowns kept unknown, scored by the definition in its freeze record, never
  by a newer policy.
- **FR-009**: The totals MUST be: cases total, run and not run; cases settled by the rules alone;
  fully correct over the full set; unsafe actions; cases sent to a person; cases where Luna
  failed and Haiku answered; cases where both failed and went to a person; invalid answers;
  cases answered by each model, counting only a model that answered, never a requested one;
  spend known and unknown, by account; completion. An incomplete run MUST say so before any
  figure, and never present one over the full set.
- **FR-010**: The run MUST complete the declared set whatever its answers. Any unsafe action MUST
  be stated first, and pauses the adoption for the maintainer's review. Any further acceptance
  threshold MUST be recorded in the freeze record, or none applies.
- **FR-011**: A summary of totals only MAY be committed, by the maintainer's decision, with the
  freeze record's hashes. Nothing case-level is committed.
- **FR-011a** (amendment, 2026-11-05): `run` and `rebuild` MAY export the totals to a `.json`
  file the maintainer names, outside the private folder, through no link, written whole, so
  that nobody copies them by hand. The export is an allowlist: the counts, completion, a stop
  from a closed set of categories, answering models that read as model ids, spend by known
  provider, the threshold, bound to the identity, the freeze record's hash, the commit and the
  run. Any other key or value refuses the export. Committing it stays the maintainer's decision.
- **FR-012**: ADR-009 MUST authorise OpenAI and Anthropic on the holdout's masked payload, for
  this run, on the maintainer's attestation. ADR-013 MUST record the attestation, its date and
  its four points.
- **FR-013**: CI MUST make no paid call, and test the tool on a stand-in set with sentinels.
- **FR-014**: The tool MUST NOT be run on the real holdout by an agent: the maintainer runs it,
  and the documentation says so. Agents run it only on stand-in sets, network-free, in tests.

### Key Entities

- **The freeze record**: the hashes and settings the run is bound to, declared before it.
- **The claim**: the durable mark that the authorisation was spent.
- **The private record**: each case's outcome, in the maintainer's folder.
- **The totals**: what may leave that folder.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In the stand-in tests, no sentinel appears in any output, for a complete run, a
  stopped run and a defect.
- **SC-002**: A second start after consumption, or after an ambiguous crash, is refused in every
  test, before any client is built, whatever folder, freeze record or case file it names.
- **SC-003**: Every total of FR-009 is reported, and an incomplete run never shows a full-set
  score.
- **SC-004**: The development guards' tests pass unchanged.
- **SC-005**: The maintainer runs it once, and only totals come back to the repository and the
  conversation.

## Assumptions

- The maintainer holds the holdout and its fixtures outside the repository and runs the tool
  himself, on main.
- The spend is not estimated here: the holdout's size is his to know, and the preparation
  computes the estimated admission budget privately.
- Prompt v4 comes after, on the development cases only.
- No change to the composite, its prompt, its schema or its masking is part of this slice.
