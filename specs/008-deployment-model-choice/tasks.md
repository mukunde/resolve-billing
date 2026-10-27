---
description: "Tasks for the choice of the deployment model"
---

# Tasks: The choice of the deployment model

**Input**: [spec.md](spec.md) (approved at `f68b9ea`), [plan.md](plan.md)

**Tests**: required, written before the code and shown to fail on it.
**Checkpoint after each phase**: `ruff check`, `pytest`, the mock baseline and
the coverage check pass; committed locally. **[maintainer]** marks his tasks.

## Phase 1: Research, pinned

- [ ] T010 Plan "Research notes" (plan section 1): Scaleway's three Qwen and
  DeepSeek configurations (exact output maxima, reasoning values, structured
  outputs, usage and reasoning fields, returned model, prices, limits);
  Anthropic's Messages request as the service engine sends it, stop
  reasons, usage fields, returned model, errors, retries off, Haiku 5.5's
  cache prices, the organisation id's location in the console. An
  unsettled point blocks its client, except the approved unknowns. Codex
  reviews before Phase 2.
- [ ] T011 **[maintainer]** Read the Anthropic account's rate limits for
  Haiku 5.5 and its organisation id in his console; add
  `ANTHROPIC_ORGANIZATION_ID` to `.env`.

## Phase 2: Claude on the governed path

- [ ] T020 Test first, `tests/unit/test_anthropic_evaluation.py`:
  - the parity test (the adapter's request equals `ClaudeDecisionEngine`'s
    for the same dispute, on stubs);
  - stop reasons mapped, `max_tokens` and `refusal` unusable;
  - usage and cache details read, never added twice;
  - the returned model recorded;
  - errors by category without vendor text;
  - SDK retries off;
  - non-leak sentinels (key, vendor error body);
  - the configuration closed and refused by the service;
  - `evals/accounts.py` accepting an organisation-only account.
- [ ] T021 `app/decision/anthropic_eval.py`, the `claude-haiku-5-5`
  configuration in `app/decision/hosted.py`, `app/decision/vendors.py`,
  `evals/accounts.py`.

## Phase 3: Scaleway configurations and the 397B's cap

- [ ] T030 Test first, `tests/unit/test_openai_compatible_configurations.py`:
  - `qwen3.6-35b-a3b` and `deepseek-v4.1-flash` closed (developer apart
    from host, `low`, cap 16,384, prices with DeepSeek's cached rate,
    limits, preview status and unknown quantisation recorded);
  - the 397B's cap at 16,384 (or T010's maximum) and its fingerprint
    changed;
  - the four spec 004 fingerprints unchanged;
  - the service refusing each.

  In `tests/unit/test_openai_compatible_client.py`: each new model's
  request on the in-memory transport, and any response difference T010
  found.
- [ ] T031 `app/decision/hosted.py`, `app/decision/openai_compatible.py`
  where T010 requires it.

## Phase 4: Commit and clean tree in every result

- [ ] T040 Test first, `tests/unit/test_prepaid_runner.py`:
  - every result carries `commit` and `clean_tree`;
  - a modified tracked file, or an untracked file outside `evals/results/`,
    makes the tree dirty, and the runner refuses to start;
  - results written in `evals/results/` keep it clean;
  - outside git, `unknown` is recorded.
- [ ] T041 `evals/hosted_run.py`.

## Phase 5: The comparison script

- [ ] T050 Test first, `tests/unit/test_compare.py`, on synthetic result
  files and a synthetic manifest:
  - each refusal: another commit, a dirty tree, another case-set hash,
    prompt, schema, masking or fingerprint, an undeclared slot, a duplicate
    slot;
  - slot assignment by start time;
  - exactly one replacement after a qualifying interruption; "not
    completing" after a second one or another stop;
  - unsafe actions counted across all attempts;
  - the mean, the worst run, the changing and always-missed cases;
  - the tie band at 1.0 case;
  - the tie-breaks in order (invalid answers; automatic replies on missed
    cases, an expected automatic reply not counted; cost per dispute over
    102 with unknown costs at their reservation and euros at the manifest's
    rate; median latency);
  - eligibility applied first;
  - the campaign's spend reported apart;
  - the Markdown and JSON outputs.
- [ ] T051 `evals/compare.py`.

## Phase 6: Documents

- [ ] T060 The evaluation and decision-engine contracts; ADR-009's
  model-provider row (DeepSeek as developer, Scaleway as processor);
  `docs/operations.md`; `.env.example` (`ANTHROPIC_ORGANIZATION_ID`).
- [ ] T061 Final review by Codex on the final commit; the maintainer approves
  the final diff (sensitive change).

## Phase 7: After the merge

- [ ] T070 **[maintainer]** The manifest PR
  (`evals/comparisons/008/manifest.json`): his eligibility declaration, the
  date of round 1, the exchange rate's source and date; the slots and
  commands; the envelopes (Anthropic $1 declared on Haiku's first call,
  Scaleway raised to €3).
- [ ] T071 **[maintainer]** First calls on the manifest's commit for
  `claude-haiku-5-5`, `qwen3.6-35b-a3b`, `deepseek-v4.1-flash`,
  `qwen3.5-397b-a17b`. A refused or unaccepted first call is fixed before
  round 1.
- [ ] T072 **[maintainer]** Rounds 1 to 3, in the manifest's order;
  replacements only as the rules say.
- [ ] T073 `python -m evals.compare`; the results PR; the decision record
  (FR-007), with the maintainer's choice and reasons.

## Dependencies

Phase 1 before 2 and 3; Phases 2 to 6 before the PR; Phase 7 after the
merge, in order.
