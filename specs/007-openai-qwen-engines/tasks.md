---
description: "Tasks for OpenAI and Qwen on Scaleway as evaluation engines"
---

# Tasks: OpenAI and Qwen on Scaleway, evaluated on the development cases

**Input**: [spec.md](spec.md) (approved at `4118da6`), [plan.md](plan.md)

**Tests**: required, written before the code and shown to fail on it.
**Checkpoint after each phase**: `ruff check`, `pytest`, the mock baseline and
the coverage check pass; committed locally. **[maintainer]** marks his tasks.

## Phase 1: Research, pinned

- [ ] T010 Plan "Research notes" (plan section 1): both vendors' chat
  completions on the three models, the pinned SDK's request and response
  shapes, usage and its details, finish reasons and refusals, the returned
  model string, errors, retries off, OpenAI's organisation and project
  headers, Scaleway's base URL, rate limits and serverless quantisation,
  and whether reasoning counts toward the output cap. An unsettled point
  blocks the client it concerns, except the approved unknowns (plan
  section 1), recorded as unknown. Codex reviews before Phase 3.
- [ ] T011 `pyproject.toml`: `openai==3.29.0` in the extra `engines`; CI
  already installs it (no call, gates unchanged).

## Phase 2: Configurations and fingerprints

- [ ] T020 Test first, `tests/unit/test_hosted_engines.py`: the four existing
  configurations' fingerprints pinned to their values on `main`; the three
  new configurations closed (exact ids, vendor, developer, endpoint, key and
  account variables, currency, prices with source and date, reasoning
  `low`, cap 4096, `store` false for OpenAI, `check_identity`, location,
  quantisation per T010); the service refusing each. End to end, through
  the real gate: old-format acceptance records with their unchanged
  evidence files still admit the four existing configurations' full runs,
  Mistral's sibling requirement included; Mistral's existing ledger file
  still reads.
- [ ] T021 `app/decision/hosted.py` (fields defaulted, three
  configurations), `evals/first_calls.py` (defaults left out of the
  fingerprint).

## Phase 3: The client

- [ ] T030 Test first, `tests/unit/test_vendor_clients.py`: on SDK-shaped
  stubs, per vendor, the exact request (model, messages from the shared
  core, strict schema, reasoning effort, cap parameter, `store` false for
  OpenAI and absent for Scaleway, retries off, organisation and project for
  OpenAI); the final content read, reasoning content dropped and never
  kept; refusal, `length`, filter and empty answers unusable; usage read
  only from fields set, with its details; the returned model recorded;
  errors by category (429 and 5xx with `Retry-After`, other 4xx as status
  alone, transport, unreadable); usage details (cached, cache write,
  reasoning) read and never added to the totals twice; an unexpected
  programming error propagating as itself. Non-leak, with sentinels for the
  key, a vendor error body and a `reasoning_content` text: none of them in
  the result file, stdout, stderr or captured logs, on success, on a
  refusal, on an unreadable response and on a cancelled run. Without
  network, against the pinned SDK: the client built, the request object
  constructed, representative responses and exceptions mapped.
- [ ] T031 `app/decision/openai_compatible.py`, `app/decision/vendors.py`.

## Phase 4: Accounts, envelopes, identity

- [ ] T040 Test first, `tests/unit/test_accounts.py`: the identity of an
  existing configuration unchanged; a new one's from its declared variables,
  a missing one refused before any client; the run lock per billing
  organisation; first-call acceptance per project; no key or key hash in
  the new identity.
- [ ] T041 `evals/accounts.py`; `evals/first_calls.py` and
  `evals/hosted_run.py` use it.
- [ ] T042 Test first, `tests/unit/test_prepaid_ledger.py`: declare, raise,
  never below spent; reserve before send at the highest rate; refusal
  beyond what remains stops the run; settle by category (cached and
  cache-write only when reported, undivided input at the input-side rate,
  reasoning at the output rate); missing usage keeps the reservation;
  interrupted attempt stays spent, after a restart too; concurrent reserves
  from two processes against one remaining balance admit at most what it
  covers; a declaration never lowers the total, never erases spending or
  earlier declarations; settlement by category without double counting,
  at the cache-write rate for the unclassified remainder when any detail is
  missing (cached reported without cache writes, and the reverse), and an
  inconsistent breakdown (categories above the input total, reasoning
  above the output total) keeping the reservation and stopping the run; unreadable or
  inconsistent refused, never reset; two keys and two projects of one
  organisation share one ledger,
  two organisations keep two; USD and EUR apart; the free tier never
  deducted; Mistral's ledger file and behaviour unchanged.
- [ ] T043 `evals/ledger.py` (`PrepaidLedger`), `evals/governed.py` (one
  ledger interface).
- [ ] T044 Test first, `tests/unit/test_evaluation_limits.py`: identity
  checked on every response of a `check_identity` configuration -- a
  mismatch or a missing identity stops the run after settling, case not
  scored, result incomplete; the first call not accepted on a mismatch;
  existing configurations unaffected.
- [ ] T045 `evals/governed.py`, `evals/hosted_run.py` (first-call reason).

## Phase 5: Runner and results

- [ ] T050 Test first, `tests/unit/test_first_call_gate.py` and
  `tests/unit/test_evaluation_registry.py`: `--envelope` required on a
  first run of a new account, refused for Gemini and Mistral; the old
  flags refused for the new vendors; missing account variable named, no
  call; the result records currency, account ids, location, quantisation,
  `store`, and "integration run"; the development-set guard holding for
  the three; one run per organisation at a time; an unreadable response
  and a cancellation still writing an incomplete result with their
  reservations kept.
- [ ] T051 `evals/hosted_run.py`, `evals/runner.py`, `evals/engines.py`.

## Phase 6: The maintainer's settings and first calls

- [ ] T060 **[maintainer]** Create the OpenAI project and key, the Scaleway
  project and key; read each account's limits in its console (tokens and
  requests per minute per model, the organisation-wide Scaleway limits)
  and the remaining credit or free-tier balance; put keys and account ids
  in his `.env`. The limits are pinned in the configurations with source
  and date, in a commit of their own, reviewed.
- [ ] T061 **[maintainer]** One first call per configuration
  (`--first-call-only`, `--data-terms contractual`, `--envelope` on the
  account's first run: $5 OpenAI, €2 Scaleway). A first call not accepted
  stops that configuration until its cause is settled.
- [ ] T062 **[maintainer]** One full integration run per accepted
  configuration (SC-001); results committed in a separate results PR,
  listed in `evals/results/README.md`.

## Phase 7: Documents

- [ ] T070 `docs/contract/decision-engine.md`, `docs/contract/evaluation.md`
  amended (plan section 8).
- [ ] T071 ADR-009's model-provider row; `docs/operations.md`;
  `.env.example`; README where it lists the evaluation engines.
- [ ] T072 Final review by Codex on a read-only checkout of the final
  commit; the maintainer approves the final diff by its commit (sensitive
  change).

## Dependencies

Phase 1 before 3 (the client); Phase 2 before 3 and 4; Phase 4 before 5;
Phases 1-5 and 7 before the PR; Phase 6 after the merge, on `main`, except
T060's limits, which precede the merge.
