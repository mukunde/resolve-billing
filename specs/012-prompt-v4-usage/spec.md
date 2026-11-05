# Feature Specification: Prompt v4, the engine's half of the usage gap

**Feature Branch**: `012-prompt-v4-usage`

**Created**: 2026-11-05

**Status**: Draft, for the maintainer's approval

**Input**: Roadmap item 1, taxonomy S1 (decided 2026-10-19): a disputed usage gap with the
finding present is `unknown` + escalate, a person looks; no money on it before S5's amounts;
under-billing is an audit note only. The code's half is built, step 4c of the action policy
included (`.specify/bugs/usage-overbilling-review/`, PR 45): whatever the engine answers, an
over-billing goes to a person. The engine's half is a prompt instruction, which waits for a
measured prompt version. Two facts, checked on `85d37df`: no development case produces a
usage finding (mock run: 0 of 34), and prompt v3 says nothing about usage. The framing was
reviewed by Codex (r313-r315) and approved by the maintainer on 2026-11-05.

## Clarifications

### Session 2026-11-05

- Q: Why new cases before a prompt? → A: The current set cannot see a usage instruction:
  no case reaches one. A prompt change nobody can measure is asserted, not measured
  (constitution, principle VIII).
- Q: Does guard 4c make the prompt pointless? → A: No. 4c holds the verdict; the prompt
  decides what the system says the dispute is and which action it proposes. The cases state
  the two apart, so 4c passing never hides the prompt failing.
- Q: May the holdout's result inform v4? → A: No (ADR-013). Its failures are not read, and
  it is not rerun to show progress. The session writing v4 has never read the holdout
  (constitution, principle IX).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The usage cases exist, labelled by the maintainer (Priority: P1)

The development set gains cases where usage matters, each in its own fixture world, with
labels the maintainer adjudicated before any implementation.

**Why this priority**: Without them nothing in this slice can be measured.

**Independent Test**: The mock runs the enlarged set; every new case produces the findings it
declares, and the existing 34 keep their recorded outcomes case by case.

**Acceptance Scenarios**:

1. **Given** the proposed cases and labels, **When** the maintainer adjudicates them, **Then**
   the labels are recorded as his, before any prompt or tooling change.
2. **Given** the enlarged set, **When** CI runs, **Then** the guards that pin the set (its
   hash, the count of cases reaching the engine, the mock baseline, the floor, the coverage
   tables) are updated deliberately, each with its test, and none is weakened.

---

### User Story 2 - Prompt v4 says what to do with usage (Priority: P1)

`decision_v4` is v3 with one added section on usage, and nothing else changed.

**Why this priority**: It is the engine's half of S1.

**Independent Test**: The difference between the two prompt files is that one section; every
engine that reads the prompt file sends v4 when v4 is selected, and its version string says so.

**Acceptance Scenarios**:

1. **Given** v4 selected, **When** Luna, the Haiku leg or Claude decides, **Then** the request
   carries v4 and the run row records `decision_v4`.
2. **Given** a measurement run, **When** it starts, **Then** the prompt version is chosen by an
   explicit setting and recorded in the result, never by editing a file between runs.

---

### User Story 3 - The maintainer decides on a measurement declared beforehand (Priority: P1)

Both legs of the service engine are measured, v3 against v4, on the same commit, and the rule
for adopting v4 is written before the first paid call.

**Why this priority**: A prompt change is a behaviour change: done only when measured.

**Independent Test**: The twelve result files exist, one per run, each naming its engine, leg,
prompt version, commit and case-set hash; a report applies the declared rule to them.

**Acceptance Scenarios**:

1. **Given** the commands and their estimated cost, **When** the maintainer approves them,
   **Then** the runs are made, and only then.
2. **Given** the twelve runs, **When** the rule is applied, **Then** each leg's verdict is one
   of: adopt, do not adopt, or a regression for the maintainer to decide.
3. **Given** v4 not adopted, **When** the slice ends, **Then** the service keeps v3 and the runs
   are kept.

### Edge Cases

- A run stops on an outage or a budget refusal: it is incomplete, kept, and does not count as
  one of the three; it is rerun only on the maintainer's approval, at the same commit.
- A first call under v4 is not accepted: no full run starts on that engine.
- The control case fails under v4 (the instruction escalates every usage complaint): that is a
  regression of the new cases' own, and v4 is not adopted.
- A new case's label turns out wrong during implementation: it goes back to the maintainer;
  no label is changed by an agent.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The development set MUST gain these cases, each in a fixture world of its own,
  with labels proposed here and adjudicated by the maintainer before implementation. Each
  states separately the classification and the engine's recommendation (the prompt's half),
  the system's proposed action, whether human approval is required and whether an automatic
  reply is allowed (the code's half), and the findings:

  | Case | Classification | Engine recommends | System action | Human approval | Automatic reply | Findings |
  |---|---|---|---|---|---|---|
  | Over-billing disputed, French | `unknown` | `escalate` | none | yes | no | `SUSPICIOUS_USAGE_DELTA` |
  | Over-billing disputed, English | `unknown` | `escalate` | none | yes | no | `SUSPICIOUS_USAGE_DELTA` |
  | Over-billing disputed, terse | `unknown` | `escalate` | none | yes | no | `SUSPICIOUS_USAGE_DELTA` |
  | Control: the same dispute, usage corroborates the bill | `valid_invoice` | `explain_invoice` | reply | no | yes | none |
  | Over-billing present, complaint about something else | [NEEDS CLARIFICATION: Q1] | | none | yes | no | `SUSPICIOUS_USAGE_DELTA` |
  | Under-billing, customer asks what the invoice covers | `valid_invoice` | `explain_invoice` | reply | no | yes | none |
  | Metered line, no comparable usage record, usage disputed | `unknown` | `escalate` | none | yes | no | `USAGE_UNAVAILABLE` |

- **FR-001a**: The runner MUST score the engine's recommendation as its own check, beside
  the system's proposed action, on every case that declares one: the policy can turn a
  money recommendation on `unknown` into no action, so the system's action alone cannot show
  that the engine escalated. A test MUST show that `unknown` + `propose_credit_note` on a case
  expecting `escalate` fails that check. Cases that declare no recommendation are scored as
  today.

- **FR-002**: Where a case needs the absence of a finding or an audit event (under-billing: no
  usage finding reaches the engine or the reply, the `usage_under_billed` note is written), a
  test MUST assert it, since the runner's expected findings are a required subset.
- **FR-003**: The set's guards MUST be updated deliberately, with their tests: the set's hash,
  the count of cases reaching the engine, the mock baseline and its floor, the coverage tables.
  No envelope or ledger is reset, and no guard is weakened.
- **FR-004**: `decision_v4` MUST be `decision_v3` plus one section on usage, saying: a usage
  finding is a fact like any other; when the customer disputes usage billed above what was
  measured, the answer is `unknown` with `escalate` (S1); never conclude the invoice is correct
  against an over-billing, or when usage could not be checked (S1, S7); under-billing is not
  the customer's matter. Whether an over-billing the customer does not dispute is also
  `unknown` with `escalate` follows the maintainer's answer to Q1. No other line changes.
- **FR-005**: The prompt version MUST be selectable per run by an explicit setting, recorded in
  every result, for the service's measurement path and the engines that read the prompt file.
  The service's default MUST stay v3 until the maintainer adopts v4.
- **FR-006**: The measurement MUST be, on the development set only and on one commit with step
  4c in place: Luna v3 and Luna v4, 3 runs each; Haiku (the composite with Luna's leg
  disabled) v3 and v4, 3 runs each. Twelve runs. A first call under each new request is
  accepted by the existing procedure before any full run. The commands and their estimated
  cost are shown to the maintainer before any paid call.
- **FR-007**: The adoption rule, per leg, fixed before the runs:
  - 0 unsafe actions on every run;
  - every new case meets its classification, the engine's recommendation and the system's
    action on every v4 run;
  - on the 34 existing cases, every outcome that differs between v3 and v4 (classification,
    action, approval, automatic reply) is listed case by case; a case v3 got right on all its
    runs and v4 got wrong on any run is a regression, which goes to the maintainer; v4 is not
    adopted with a regression unless he decides so explicitly.

  Three runs show variance, they do not establish non-degradation: the report says so. If v4
  is not adopted, the runs are kept and the service stays on v3.
- **FR-008**: If the maintainer adopts v4, the service's prompt moves to v4 in the same pull
  request as the results, and ADR-013 records that the holdout's reference measured v3.
- **FR-009**: The contracts (decision engine, evaluation) and taxonomy S1 MUST say what is
  built, in the same pull request.
- **FR-010**: CI MUST make no paid call. The holdout is neither read nor rerun.

### Key Entities

- **A usage case**: a dispute, its fixture world, its adjudicated labels.
- **A measurement run**: one engine leg, one prompt version, one commit, one case-set hash.
- **The comparison report**: the declared rule applied to the twelve runs, case by case.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every new case produces its declared findings under the mock; the 34 existing
  cases keep their recorded outcomes.
- **SC-002**: Twelve complete runs, or the maintainer's decision on any incomplete one.
- **SC-003**: Under v4, every new case meets its classification, the engine's recommendation
  and the system's action on every run of each leg, with no unsafe action.
- **SC-004**: The report lists every changed outcome on the 34 existing cases, and the
  maintainer's decision closes the slice: v4 adopted on both legs, or the service stays on v3.

## Assumptions

- Development cases are written by the session that tunes the prompt: they are not blind,
  and are not presented as such.
- Claude reads the same prompt file; it is not measured here, being neither leg of the
  service engine.
- Cost: a Luna call cost about $0.0002 on the reference run; Haiku costs more. The estimate is
  computed and shown with the commands (FR-006).
- Out of scope: S5's partial amounts, a dedicated usage classification, the outgoing-text
  check, any holdout run.
