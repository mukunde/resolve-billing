# Implementation Plan: Prompt v4, the engine's half of the usage gap

**Branch**: `012-prompt-v4-usage` | **Date**: 2026-11-05 | **Spec**: [spec.md](spec.md), approved by the maintainer at `8dffc7c`

## Summary

Seven development cases on usage, a runner check on the engine's own recommendation, a
prompt `decision_v4` selectable per run, a comparison tool that applies the declared rule,
then twelve paid runs, approved before they start. The service's default prompt stays v3
until the maintainer adopts v4. Like specs 008 to 011, this plan is one file.

## Technical Context

Python 3.12 (CI), 3.13 locally. No new dependency. Cases and fixtures are the existing JSON
files; results are the existing service-run JSON. CI makes no paid call. The paid runs use
the existing prepaid envelopes, ledgers, account locks and first-call records under
`~/.resolve-billing/`, none of them reset.

## Constitution Check

| Principle | How this plan meets it |
|---|---|
| I, II, V | No rule or policy changes; step 4c stays as merged. The prompt widens nothing a model may set off: it asks for more escalation, never less. |
| VII | The payload, masking and schema are unchanged; only the system prompt's text differs. |
| VIII | New cases make the instruction measurable; the rule is fixed and tested before the runs; every run is stored. |
| IX | Development cases only. This session never read the holdout; its result is not read for v4, and it is not rerun. |
| Sensitive | The evaluation gates change (new check, new cases, new floor): final-diff approval. Adopting v4 changes what the service sends: the maintainer's decision, in the results' pull request. |

## Design

### 1. The cases (FR-001, FR-002, FR-003)

Seven cases appended to `evals/cases.json`, in the table's order, each with `why`,
`covers` and `label_source: "maintainer, 2026-11-05"`. Each has its own customer,
subscription, metered invoice and usage record in `fixtures/`, so that no existing case's
world moves:

- three disputed over-billings (French, English, terse), the same world shape, usage
  measured well below the billed quantity;
- the control: the French dispute's wording, a world whose usage matches the bill;
- an over-billing with a message about the collection date;
- an under-billing: usage measured above the billed quantity, a message asking what the
  invoice covers;
- a metered line whose usage record covers another period, the usage disputed.

They declare `expect_classification`, `expect_engine_action` (new, section 2),
`expect_action`, `expect_human_approval`, `expect_automatic_reply` and `expect_findings`,
as the spec's table says. A unit test runs the under-billing case through the
investigation and asserts that no usage finding reaches the engine or the result, and that
the `usage_under_billed` event is in the trace (FR-002).

Then, deliberately and each with its test:

- `evals.engines.DEVELOPMENT_SET_SHA256`, the new file's hash;
- `evals.engines.ENGINE_CASES`, counted from the mock run (every new case is expected to
  reach the engine: 27), the test that counts them updated;
- `evals/baselines/mock.json`, regenerated; the 34 existing cases keep their checks, case
  by case, and the diff shows it;
- the CI floor (`--min-fully-correct` in the workflow and `docs/operations.md`), raised by
  the new cases the mock gets right, never lowered;
- `evals/coverage` tables, regenerated and checked.

Spec 008's campaign is closed; its manifest keeps the old hash, as written. Its comparison
stays reproducible: `evals.compare.development_case_ids()` reads the case ids from the case
file as it stood at the manifest's commit (`git show <commit>:evals/cases.json`), not from
today's file, so its complete 34-case results are still admitted and never scored against
the enlarged set; a test runs it on the stored spec 008 results. No envelope, ledger or
state file is touched.

### 2. The engine's recommendation, scored (FR-001a)

`CaseOutcome` gains `recommendation_ok: bool = True`, and `CHECK_NAMES` gains
`recommendation`, so the case-by-case baseline holds it too. `_evaluate` sets it, when the
case declares `expect_engine_action`, to whether the engine's recommendation equals it; no
recommendation (the engine skipped, or none returned) fails the expectation. A case
declaring nothing scores `True`, as today; `ok` includes the check. `evals.rescore`
recomputes it from the stored `got.recommended_action` where the case declares one. The
readers of stored results (rescore, `evals.compare`, whose checks derive from
`campaign.OUTCOME_FIELDS`, `evals.variance`, whose list of checks added later gains it, and
the comparison tool) read a missing field as `True`, so results written before it keep
their scores.

Tests: a case expecting `escalate` fails on `unknown` + `propose_credit_note` although its
system action is `none` (the counterexample); no recommendation fails it; a case declaring
nothing scores as today; the baseline records and compares the new check; rescore
recomputes it.

### 3. The prompt, selectable (FR-004, FR-005)

- `app/decision/prompts/decision_v4.md`: `decision_v3.md` with one section, "Usage", added
  after "The deterministic findings come first", carrying FR-004's four sentences. A test
  holds the two files equal but for that section.
- `app/decision/prompts.py` (new): `PROMPTS = {"decision_v3": path, "decision_v4": path}`
  and `DEFAULT_PROMPT = "decision_v3"`; `prompt_text(version)` refuses an unknown one.
  `claude.PROMPT_VERSION` and `PROMPT_PATH` become the default's, so every importer keeps
  reading the service's prompt.
- `Configuration` gains `prompt: str = DEFAULT_PROMPT`; `prompt_version` returns
  `f"{name}/{prompt}"`. `first_calls.ADDED_SINCE_SPEC_004` lists `"prompt": "decision_v3"`,
  so every v3 fingerprint stays the one its first call was accepted on: a test pins the
  current fingerprints of Luna and Haiku before and after.
- `HostedEngine` reads `prompt_text(config.prompt)`; the Anthropic-hosted engine inherits
  it; `ClaudeEngine` takes `prompt=DEFAULT_PROMPT`. A test holds the Claude and
  Anthropic-hosted requests equal under each prompt.
- `--prompt {decision_v3,decision_v4}`, default `decision_v3`, on `evals.runner` and
  `evals.service_run`. The runner applies it to `claude` (the service's Claude engine,
  built with that prompt) and to every hosted configuration, prepaid and free-tier, first
  calls included; the mock reads no prompt, and the runner refuses `--prompt decision_v4`
  with it rather than ignoring the flag. It builds a hosted engine
  from `dataclasses.replace(configuration(name), prompt=...)`; the composite's version
  string and the result's `prompt_version`, `engine_version` and
  `configuration_fingerprint` follow it. `evals.reference_run` keeps v3, as frozen.
- The service (`app/api/deps.py`) reads `DEFAULT_PROMPT` and nothing else: no environment
  variable chooses a prompt in the service.

### 4. The comparison tool (FR-007), before any paid run

`evals/prompt_compare.py`: given the twelve result files, it refuses unless:

- each is a complete service run (`run_kind`, `complete`, no stop, nothing not attempted),
  on one commit with a clean tree, one case-set hash, one masking version and one schema;
- each file is a distinct run (distinct `ran_at` and file, no two with the same attempts);
- each holds exactly the development set's case ids, each once;
- per leg and prompt there are three, and each names its prompt, its engine, its bounds and
  its configuration fingerprint, the fingerprint being the one that leg and prompt produce
  on that commit (Luna alone with spec 009's bounds; the composite with Luna's leg disabled
  and spec 010's secondary bounds).

Counter-tests, written before the paid runs, cover each refusal. Per leg it reports:

- unsafe actions, per run;
- each new case's classification, recommendation and system action, per v4 run;
- on the 34 existing cases, every case whose outcome (classification, recommendation,
  action, approval, automatic reply) differs between any v3 run and any v4 run, listed;
  a regression is a case all three v3 runs got fully right and any v4 run did not;
- a verdict: `adopt`, `do not adopt` (an unsafe action, or a new case missed), or
  `regression: the maintainer decides`, with the regressions named;
- the sentence: three runs show variance, they do not establish non-degradation.

Tested on synthetic result files for each verdict, for an incomplete run, a mixed commit
and a missing prompt. It writes a Markdown report next to the results.

### 5. The runs (FR-006), on the maintainer's approval

On the commit that holds sections 1 to 4, with a clean tree, in this order:

1. Luna v3, three service runs; Haiku v3 (`--primary-disabled`), three runs. Both
   configurations' v3 first calls are already accepted, and their fingerprints do not move.
2. A v4 first call for each: `evals.runner --engine gpt-6-luna --prompt decision_v4
   --first-call-only`, and the same for `claude-haiku-5-5`. First calls are recorded by
   configuration name, so this replaces the v3 acceptance; no state is reset.
3. Luna v4, three runs; Haiku v4, three runs.
4. `python -m evals.prompt_compare` on the twelve files.

Before step 1, the exact commands and the estimated cost, from the last runs' settled
costs per case, are shown to the maintainer, and nothing runs without his yes. A run that
stops is kept and does not count; a replacement waits for his approval. After the
measurement, a v3 run needs a new v3 first call, by the same procedure.

### 6. Contracts, taxonomy, adoption (FR-008, FR-009)

- `docs/contract/evaluation.md`: the recommendation check; the case count; `--prompt`.
- `docs/contract/decision-engine.md`: the prompt is selectable per run, the service on the
  default; `decision_v4` exists, measured, adopted or not.
- `evals/taxonomy.md` S1: the instruction built and measured; the maintainer's widening
  (Q1, 2026-11-05).
- If adopted: `DEFAULT_PROMPT` moves to v4 in the results' pull request, with ADR-013 noting
  that the holdout's reference measured v3, and the README's figures updated. If not: the
  default stays, and the results and report are committed as they are.

## Delivery

Two pull requests: the first holds sections 1 to 4 and the contract changes that do not
depend on the result, with no paid call; the second holds the twelve results, the report,
and the adoption or not, after the maintainer's decision.

## Gates

`ruff check .`, `python -m pytest`, `python -m evals.runner --engine mock
--min-fully-correct <new floor> --baseline evals/baselines/mock.json`,
`python -m evals.coverage --check`. Codex reviews every diff; the maintainer approves the
final SHA of each pull request.
