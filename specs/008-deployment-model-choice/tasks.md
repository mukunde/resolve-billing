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
- [ ] T012 `pyproject.toml`: `anthropic==0.105.2` in the `claude` extra;
  CI's network-free job installs the `claude` extra (no call, gates
  unchanged).
- [ ] T011 **[maintainer]** Read the Anthropic account's rate limits for
  Haiku 5.5 and its organisation id in his console; add
  `RESOLVE_ANTHROPIC_ORG_ID` to `.env`.

## Phase 2: Claude on the governed path

- [ ] T020 Test first, `tests/unit/test_anthropic_evaluation.py`:
  - request parity (the adapter's request equals `ClaudeDecisionEngine`'s
    for the same dispute, on stubs);
  - decision parity, for a valid, a malformed, a refused, a truncated and
    an other-stop answer, restored references and explanation included;
  - stop reasons mapped as the service parser decides;
  - usage and cache details read, never added twice;
  - the returned model recorded;
  - errors by category without vendor text;
  - SDK retries off;
  - non-leak sentinels (key, vendor error body);
  - the configuration closed and refused by the service;
  - `evals/accounts.py` accepting an organisation-only account.
- [ ] T022 Test first, `tests/unit/test_prepaid_runner.py`: Haiku on the
  governed path, with the vendor stubbed:
  - reserve before send in the Anthropic envelope;
  - missing or inconsistent usage keeps the reservation and stops;
  - an interrupted and a refused run still written, incomplete;
  - first-call admission on its organisation-only identity;
  - the existing account identities and run locks unchanged;
  - `gpt-6-luna`'s and Flash-Lite's existing acceptances still admitting
    their full runs.
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
  - a modified tracked file, a staged change, or an untracked file outside
    `evals/results/` makes the tree dirty, and the runner refuses to start
    a slot;
  - git is asked about the runner's own checkout from another directory;
  - `--slot`: an undeclared id, an attempt the slot cannot take, a later
    round before an earlier one -- each refused before any client; the
    slot and attempt recorded; the campaign log line appended before
    sending;
  - the campaign lock: two runs on different vendors never admit at once;
  - the log line flushed and synced before the first request;
  - a run killed mid-way: its result rebuilt from the per-case sidecar at
    the next launch, incomplete, its unsafe detections counted, replaced
    once; an unreadable sidecar recorded "lost", the candidate "not
    completing", no replacement;
  - a "not completing" candidate's slots retired; a round over only when
    each slot is complete or retired; a replacement before the round
    ends;
  - results written in `evals/results/` keep it clean;
  - outside git, `unknown` is recorded.
- [ ] T041 `evals/hosted_run.py`, `evals/runner.py` (`--slot`), the
  campaign log.

## Phase 5: The comparison script

- [ ] T050 Test first, `tests/unit/test_compare.py`, on synthetic result
  files and a synthetic manifest:
  - each refusal: another commit, a dirty tree, another case-set hash,
    prompt, schema, masking or fingerprint, an undeclared slot, a duplicate
    slot, a manifest edited after the runs;
  - the campaign log against the files, read from the repository alone: a
    missing, an extra and an out-of-order attempt refused; lost attempts and
    retired slots honoured; smoke tests taking no slot;
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
  `docs/operations.md`; `.env.example` (`RESOLVE_ANTHROPIC_ORG_ID`).
- [ ] T061 Final review by Codex on the final commit; the maintainer approves
  the final diff (sensitive change).

## Phase 7: After the merge

- [ ] T070 **[maintainer]** The manifest PR
  (`evals/comparisons/008/manifest.json`): his eligibility declaration, the
  date of round 1, the exchange rate's source and date; the case-set hash,
  prompt, schema and masking; the slots in order with their commands
  (Flash-Lite's quota project and its one declaration of the day); the envelopes (Anthropic $1 declared on Haiku's first call,
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
