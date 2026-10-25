---
description: "Tasks for second decision engines on free tiers"
---

# Tasks: Second decision engines on free tiers, evaluated on the development cases

**Input**: [spec.md](spec.md) (approved at `94cf4ea`), [plan.md](plan.md)

**Tests**: required, written before the code and shown to fail on it.
**Checkpoint after each phase**: `ruff check`, `pytest`, the mock baseline and
the coverage check pass; committed locally. **[maintainer]** marks his tasks.

## Phase 1: Contracts as they stand

- [x] T001 `docs/contract/decision-engine.md` and `docs/contract/evaluation.md`,
  as the capabilities stand; `docs/contract/README.md` lists them. Own commit.

## Phase 2: Research, pinned

- [x] T010 Plan "Research notes" (plan section 2): SDK versions for the extra
  `engines`, each vendor's call, parameters, usage, errors, prices, token
  counting or bound, and reasoning caps, from official docs and SDK sources.
  An unsettled point blocks the client it concerns. Codex reviews before
  Phase 3.
- [x] T011 `pyproject.toml`: the extra `engines`, pinned; CI installs it for
  the network-free SDK checks (no call, gates unchanged).

## Phase 3: The shared core

- [x] T020 Test first, before the extraction: on a fake client, the exact
  request Claude sends and its decisions for a valid, an invalid and a
  truncated answer, pinned; the helpers importable from
  `app/decision/verdict.py`. Both pass unchanged after T021.
- [x] T021 `app/decision/verdict.py`; `claude.py` imports it.
- [x] T022 Test first, `tests/unit/test_hosted_engines.py`: `HostedEngine` on a
  fake `_complete`: payload holds the pseudonymised context and masked
  message only; references restored, not-issued ones dropped; invalid or
  absent answer → person; error categories; no vendor text; configurations
  closed (exact ids, endpoint, key; an ignored parameter reported).
- [x] T023 `app/decision/hosted.py`.

## Phase 4: Registry and guards

- [x] T030 Test first, `tests/unit/test_evaluation_registry.py`: the runner
  builds through `EVALUATION_ENGINES`; the service refuses
  `gemini-3.8-flash`, `mistral-large-4`, `zai-glm-5-3`; missing key named, no
  call; dev guard refuses an external path, a link elsewhere, an altered
  file and extra fixtures before any client; the hash constant equals the
  committed file's.
- [x] T031 `evals/engines.py`, `evals/runner.py`.

## Phase 5: The clients

- [x] T040 Test first: Gemini on an SDK-shaped stub -- exact request (model,
  schema, parameters, SDK retries off), usage and model version read,
  truncated / filtered / empty → invalid, errors by class and status; and,
  without network, against the pinned SDK: signatures, request objects
  built, representative responses and exceptions mapped.
- [x] T041 `app/decision/gemini.py`.
- [x] T042 Test first: Mistral on a stub -- `mistral-large-4` request and
  parsing; `zai-glm-5-3` with `reasoning_effort` pinned, blocks: final text
  only, reasoning dropped and never stored, no final text → invalid; errors;
  the same network-free checks against the pinned SDK.
- [x] T043 `app/decision/mistral.py`.

## Phase 6: Limits, envelope, outcomes

- [x] T050 Test first, `tests/unit/test_evaluation_limits.py`: limiters per
  configuration and shared across Mistral's two models (alternating them);
  the reservation bound on Unicode text, a request over a quota on its own
  not sent and never counted as a decision, reported usage above the
  reservation or absent stops the run; attempts bounded, `Retry-After`
  honoured, a timeout during a call or a wait, durable exhaustion stops;
  the Mistral ledger charged per attempt before sending, persisted, shared
  by both models and runs, never reset by switching model, a new month a
  new ledger, a second allowance declaration refused; two processes
  reserving at once never both spend the last of it (the lock); an
  interruption after a reservation leaves it counted; an unreadable or
  inconsistent ledger refused, never reset; a configuration with uncapped
  reasoning blocked; `KeyboardInterrupt`
  and cancellation write an incomplete result, non-zero exit, no total over
  34, never overwriting an earlier file; statuses per case; `--data-terms`
  accepting `unknown`; mock and Claude outputs unchanged.
- [x] T051 `evals/limits.py`, `evals/runner.py`; split for size into
  `evals/ledger.py` (the envelope), `evals/governed.py` (the engine held to
  its limits) and `evals/hosted_run.py` (the run and its result).

## Phase 7: Documents

- [x] T060 The two contracts with the change; ADR-009's model-provider row;
  `docs/operations.md`; `evals/results/README.md`; `.env.example`; README.

## Phase 7a: The daily quota and the first call (amendment, plan A1-A7)

- [x] T090 Test first: the configurations' new fields and Gemini's real
  limits; the shared estimator (`utf8-bytes-v1`) on Unicode text; Gemini
  sending no counting request.
- [x] T091 `app/decision/hosted.py`, `app/decision/gemini.py`,
  `app/decision/mistral.py`.
- [x] T092 Test first, `tests/unit/test_daily_quota.py`: the 20th request of
  a day sent, the 21st refused; persistence across restarts and two
  checkouts under one lock; an interruption after `take` kept counted; the
  day changing at Pacific midnight, daylight saving included; a clock gone
  back refused before a declaration, an admission or a send, nothing
  mutated; an unreadable or inconsistent state refused, never reset; a
  declaration initialising its day once, never lowering; an undeclared day
  stopping the run; `admit_full_run`; Pacific dates converted without the
  system's time zone database (`tzdata`).
- [x] T093 `evals/quota.py`; `tzdata` in the `engines` extra.
- [x] T094 Test first: attempts from the configuration; the order of the
  pre-send steps, each refusal sending nothing; `sends`; a Gemini outage
  stopping the run with the later cases not attempted; Mistral unchanged;
  `--first-call-only` (one attempt, at most one send, zero on a refusal
  before sending, the reasons); a Gemini run, full or smoke test,
  requiring today declared -- already or by `--gemini-requests-used-today`
  -- the flag refused on a declared day and for Mistral;
  `GEMINI_QUOTA_PROJECT` required for every Gemini run and its format
  checked; the result's new fields; the investigator reaching the
  instrumented engine exactly 20 times.
- [x] T095 `evals/governed.py`, `evals/hosted_run.py`, `evals/runner.py`.
- [x] T096 Docs: the evaluation contract, `docs/operations.md` (T071's
  check as read, the Gemini procedure), `.env.example`.
- [ ] T097 Codex reviews the diff until nothing is left; the maintainer
  approves the final diff by its commit; push and pull request.

## Phase 7b: Mistral Medium 3.5 at two reasoning efforts (second amendment, plan B1-B5)

- [x] T100 Test first: the two old names refused by the runner and absent
  from the service; the two new configurations closed, refused by the
  service, their requests equal but for `reasoning_effort`; thinking
  chunks dropped at effort `high`; cost from Medium 3.5's prices; the shared
  Mistral limiter, envelope and lock; earlier tests moved off the old
  names.
- [x] T101 **[maintainer]** Read the per-minute limits of
  `mistral-medium-3-5` in the console; recorded in the configurations.
  Before T102: no placeholder limit is merged.
- [x] T102 `app/decision/hosted.py`, `app/decision/mistral.py`.
- [x] T103 Test first: the first call's acceptance and its reasons, for
  every configuration, a returned model version required;
  `first_call_accepted` in the result; the first-calls state (atomic,
  locked, refused when corrupt), recorded only after the result is
  written; the full-run gate (own acceptance, the sibling's for Mistral, a
  change of each fingerprinted element, another key or project).
- [x] T104 `evals/hosted_run.py`, `evals/state.py` (or a small
  `evals/first_calls.py`).
- [x] T105 Docs: the two contracts, ADR-009's row, `docs/operations.md`,
  `evals/results/README.md`; the two refusal results committed.
- [ ] T106 Codex reviews the diff until nothing is left; the maintainer
  approves the final diff by its commit; push and pull request.

## Phase 7c: Gemini 3.5 Flash-Lite (third amendment, plan C1-C4)

- [x] T110 Test first: the configuration (equal to Flash's but for the model,
  limits, attempts, outage policy); `gemini-3.8-flash`'s complete
  configuration and fingerprint unchanged, its existing acceptance record
  and its existing count (today's spent request included) still read and
  still refusing a full run today; per-model counts, Flash's in its
  existing file, Flash-Lite's apart; the admission rule for both models;
  Flash-Lite's outage escalating after three attempts.
- [x] T111 `app/decision/hosted.py`, `evals/quota.py`, `evals/hosted_run.py`.
- [x] T112 Docs: `docs/operations.md`, the two contracts, `evals/results/README.md`.
- [ ] T113 Codex reviews the diff until nothing is left; the maintainer
  approves the final diff by its commit; push, pull request, merge -- then
  Flash-Lite's first call and full run.

## Phase 8: The maintainer's runs

- [ ] T070 **[maintainer]** Keys: `GEMINI_API_KEY` (AI Studio),
  `MISTRAL_API_KEY` (Mistral admin), in his `.env`.
- [ ] T071 **[maintainer]** Per configuration, the documented check (spec,
  Assumptions): access on the free level (Mistral's "Activer"), what the
  quota counts and any shared ceiling, the model's licence, status and data
  regime (GLM 5.3's in particular), the privacy setting; recorded in
  `docs/operations.md`.
- [ ] T072 **[maintainer]** A first minimal call per configuration
  (`--first-call-only`), then one run of the 34 cases each, on another
  occasion -- for Gemini, another Pacific quota day; results stored
  (SC-001). After phases 7a and 7b; for Mistral, the two Medium 3.5
  configurations, both smoke tests accepted before either full run.

## Phase 9: Review and delivery

- [ ] T080 Codex reviews the final diff until nothing is left.
- [ ] T081 **[maintainer]** Final-diff approval by its commit; push and pull
  request ("Behaviour changed": Decision engine, Evaluation).
