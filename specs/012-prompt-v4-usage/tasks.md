# Tasks: Prompt v4, the engine's half of the usage gap

**Input**: [spec.md](spec.md), [plan.md](plan.md)

**Tests**: first, in every phase. No test makes a paid call, and no test reads the holdout.

## Phase 1: The engine's recommendation, scored (FR-001a)

- [ ] T010 Test first: a case expecting `escalate` fails on `unknown` + `propose_credit_note`
  with the system action `none`; no recommendation fails it; a case declaring nothing scores
  as today; the baseline records and compares `recommendation`; rescore recomputes it;
  stored results without the field keep their scores in rescore, `evals.compare` and
  `evals.variance`.
- [ ] T011 `evals/runner.py`, `evals/rescore.py`, `evals/variance.py`, `evals/campaign.py`
  as needed: the check.

## Phase 2: The prompt, selectable (FR-004, FR-005)

- [ ] T020 Test first: `decision_v4` equals `decision_v3` but for one "Usage" section; an
  unknown prompt is refused; Luna's and Haiku's v3 fingerprints are those accepted today;
  a v4 configuration's fingerprint and `prompt_version` differ; the hosted, Anthropic-hosted
  and Claude engines send the selected prompt, the Claude and Anthropic-hosted requests equal
  under each; `--prompt` on the runner and the service run reaches the engine and the result;
  the mock refuses v4; two Mistral siblings accepted under v4 admit each other, v3 unchanged;
  the service reads the default only; the reference run keeps v3.
- [ ] T021 `app/decision/prompts.py`, `app/decision/prompts/decision_v4.md`, `claude.py`,
  `hosted.py`, `first_calls.py`, `evals/runner.py`, `evals/service_run.py`, `app/api/deps.py`.

## Phase 3: The cases (FR-001, FR-002, FR-003)

- [ ] T030 Fixtures: seven worlds of their own in `fixtures/`, no existing world moved.
- [ ] T031 `evals/cases.json`: the seven cases, labels as adjudicated on 2026-11-05.
- [ ] T032 Test: the under-billing case puts no usage finding before the engine or in the
  result, and writes `usage_under_billed`.
- [ ] T033 The set's guards, each with its test: the hash, `ENGINE_CASES`, the mock
  baseline (the 34 cases unchanged case by case), the CI floor raised, never lowered, the
  coverage tables.
- [ ] T034 Spec 008's comparison reads its case ids at the manifest's commit; a test runs it
  on the stored results.

## Phase 4: The comparison tool (FR-007)

- [ ] T040 Test first, on synthetic result files: each verdict (`adopt`, `do not adopt`,
  `regression`); each refusal (incomplete run, stop, dirty tree, mixed commit, case set,
  masking or schema, duplicate run, missing or repeated case, wrong count per leg and prompt,
  missing prompt, wrong bounds or fingerprint).
- [ ] T041 `evals/prompt_compare.py`, its Markdown report.

## Phase 5: Contracts and records (FR-009)

- [ ] T050 `docs/contract/evaluation.md`, `docs/contract/decision-engine.md`,
  `evals/taxonomy.md` S1 (built so far, Q1's widening), `docs/operations.md` (the floor, the
  commands of Phase 6).

First pull request: Phases 1 to 5, no paid call.

## Phase 6: The runs (FR-006), on the maintainer's approval

- [ ] T060 Show the commands and the estimated cost; wait for the maintainer's yes.
- [ ] T061 Luna v3 and Haiku v3, three runs each.
- [ ] T062 v4 first calls, Luna and Haiku.
- [ ] T063 Luna v4 and Haiku v4, three runs each.
- [ ] T064 The comparison report.

## Phase 7: The decision (FR-007, FR-008)

- [ ] T070 The maintainer decides per leg. Adopted: `DEFAULT_PROMPT` to v4, ADR-013's note,
  the README. Not adopted: the default stays.
- [ ] T071 Second pull request: the results, the report, the decision.
