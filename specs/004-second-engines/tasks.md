---
description: "Tasks for second decision engines on free tiers"
---

# Tasks: Second decision engines on free tiers, evaluated on the development cases

**Input**: [spec.md](spec.md) (approved at `94cf4ea`), [plan.md](plan.md)

**Tests**: required, written before the code and shown to fail on it.
**Checkpoint after each phase**: `ruff check`, `pytest`, the mock baseline and
the coverage check pass; committed locally. **[maintainer]** marks his tasks.

## Phase 1: Contracts as they stand

- [ ] T001 `docs/contract/decision-engine.md` and `docs/contract/evaluation.md`,
  as the capabilities stand; `docs/contract/README.md` lists them. Own commit.

## Phase 2: Research, pinned

- [ ] T010 Plan "Research notes" (plan section 2): SDK versions for the extra
  `engines`, each vendor's call, parameters, usage, errors, prices, token
  counting or bound, and reasoning caps, from official docs and SDK sources.
  An unsettled point blocks the client it concerns. Codex reviews before
  Phase 3.
- [ ] T011 `pyproject.toml`: the extra `engines`, pinned; CI installs it for
  the network-free SDK checks (no call, gates unchanged).

## Phase 3: The shared core

- [ ] T020 Test first, before the extraction: on a fake client, the exact
  request Claude sends and its decisions for a valid, an invalid and a
  truncated answer, pinned; the helpers importable from
  `app/decision/verdict.py`. Both pass unchanged after T021.
- [ ] T021 `app/decision/verdict.py`; `claude.py` imports it.
- [ ] T022 Test first, `tests/unit/test_hosted_engines.py`: `HostedEngine` on a
  fake `_complete`: payload holds the pseudonymised context and masked
  message only; references restored, not-issued ones dropped; invalid or
  absent answer → person; error categories; no vendor text; configurations
  closed (exact ids, endpoint, key; an ignored parameter reported).
- [ ] T023 `app/decision/hosted.py`.

## Phase 4: Registry and guards

- [ ] T030 Test first, `tests/unit/test_evaluation_registry.py`: the runner
  builds through `EVALUATION_ENGINES`; the service refuses
  `gemini-3.8-flash`, `mistral-large-4`, `zai-glm-5-3`; missing key named, no
  call; dev guard refuses an external path, a link elsewhere, an altered
  file and extra fixtures before any client; the hash constant equals the
  committed file's.
- [ ] T031 `evals/engines.py`, `evals/runner.py`.

## Phase 5: The clients

- [ ] T040 Test first: Gemini on an SDK-shaped stub -- exact request (model,
  schema, parameters, SDK retries off), usage and model version read,
  truncated / filtered / empty → invalid, errors by class and status; and,
  without network, against the pinned SDK: signatures, request objects
  built, representative responses and exceptions mapped.
- [ ] T041 `app/decision/gemini.py`.
- [ ] T042 Test first: Mistral on a stub -- `mistral-large-4` request and
  parsing; `zai-glm-5-3` with `reasoning_effort` pinned, blocks: final text
  only, reasoning dropped and never stored, no final text → invalid; errors;
  the same network-free checks against the pinned SDK.
- [ ] T043 `app/decision/mistral.py`.

## Phase 6: Limits, envelope, outcomes

- [ ] T050 Test first, `tests/unit/test_evaluation_limits.py`: limiters per
  configuration and shared across Mistral's two models (alternating them);
  the reservation bound on Unicode text, a request over a quota on its own
  not sent and never counted as a decision, reported usage above the
  reservation or absent stops the run; attempts bounded, `Retry-After`
  honoured, a timeout during a call or a wait, durable exhaustion stops;
  the Mistral ledger charged per attempt before sending, persisted, shared
  by both models and runs, never reset by switching model, a new month a
  new ledger, a second allowance declaration refused; `KeyboardInterrupt`
  and cancellation write an incomplete result, non-zero exit, no total over
  34, never overwriting an earlier file; statuses per case; `--data-terms`
  accepting `unknown`; mock and Claude outputs unchanged.
- [ ] T051 `evals/limits.py`, `evals/runner.py`.

## Phase 7: Documents

- [ ] T060 The two contracts with the change; ADR-009's model-provider row;
  `docs/operations.md`; `evals/results/README.md`; `.env.example`; README.

## Phase 8: The maintainer's runs

- [ ] T070 **[maintainer]** Keys: `GEMINI_API_KEY` (AI Studio),
  `MISTRAL_API_KEY` (Mistral admin), in his `.env`.
- [ ] T071 **[maintainer]** Per configuration, the documented check (spec,
  Assumptions): access on the free level (Mistral's "Activer"), what the
  quota counts and any shared ceiling, the model's licence, status and data
  regime (GLM 5.3's in particular), the privacy setting; recorded in
  `docs/operations.md`.
- [ ] T072 **[maintainer]** A first minimal call per configuration, then one
  run of the 34 cases each; results stored (SC-001).

## Phase 9: Review and delivery

- [ ] T080 Codex reviews the final diff until nothing is left.
- [ ] T081 **[maintainer]** Final-diff approval by its commit; push and pull
  request ("Behaviour changed": Decision engine, Evaluation).
