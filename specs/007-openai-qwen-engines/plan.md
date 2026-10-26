# Implementation Plan: OpenAI and Qwen on Scaleway, evaluated on the development cases

**Branch**: `007-openai-qwen-engines` | **Date**: 2026-10-26 | **Spec**: [spec.md](spec.md), approved by the maintainer at commit `4118da6`

## Summary

Three closed configurations -- `gpt-6-luna` and `gpt-6.1-sol` (OpenAI),
`qwen3.5-397b-a17b` (Scaleway) -- join the evaluation registry on spec 004's
shared core. One client reaches both vendors through their
OpenAI-compatible chat completions, with a mapping per vendor. The runner
gains three things for them:

- a prepaid envelope per billing organisation, in the account's currency;
- an account identity the maintainer declares, sent to the vendor where it
  can check it;
- the returned model's identity checked on every response.

The service, the Claude engine, the prompt and the four existing
configurations do not change, their first-call acceptances included.

## Technical Context

Python 3.12 (CI), 3.13 locally. The extra `engines` gains the `openai` SDK,
pinned exactly: `openai==3.29.0`, published 2026-10-11, at least two weeks
old. The SDK's `base_url` reaches Scaleway, as Scaleway documents. Clients
are tested on SDK-shaped stubs, and without network against the pinned SDK
itself: request construction, representative responses and exceptions. CI
already installs `engines` and makes no call. Storage: new ledgers in the
accounts' state (`~/.resolve-billing/`), beside Mistral's.

## Constitution Check

| Principle | How this plan meets it |
|---|---|
| II | Unchanged: these engines recommend; policy, approval and execution are untouched, and they never reach the service. |
| III | Vendor names appear only in `app/decision/hosted.py` (configurations), the new client module, `app/decision/vendors.py`, the evaluation registry and tests. Domain and application code are unchanged. |
| V | ADR-006 holds: one request per case, no loop, no tool. The model sets no control flow. |
| VI | Each result names the configuration, prompt, masking, parameters, account identity (non-secret), processing location and estimated cost. |
| VII | The pseudonymised payload and masked message are pinned per vendor by tests; OpenAI requests carry `store` false. No vendor text in logs; reasoning never stored. |
| VIII | Integration runs, published apart, labelled as such. Mock baseline unchanged. |
| IX | The development-set guard applies; the holdout is unreachable. |
| Sensitive | Two more model providers receive masked synthetic data: final-diff approval; ADR-009 amended. |

## Design

### 1. Research pinned before code (T010)

Read from the official documentation and the pinned SDK's sources,
recorded in "Research notes" below, and reviewed by Codex before any client
is written:

- **OpenAI chat completions** on `gpt-6-luna` and `gpt-6.1-sol`:
  `response_format` with a strict JSON schema and its supported subset;
  `reasoning_effort` (`low`); the output cap parameter for reasoning models
  (`max_completion_tokens`) and whether reasoning counts toward it;
  `store` (default and effect); usage fields (prompt, completion,
  reasoning, cached and cache-write details); finish reasons; refusals
  (`message.refusal`); the returned `model` string; errors and status
  codes; turning SDK retries off (`max_retries=0`); the organisation and
  project headers, and the vendor's answer when they do not match the key.
- **Scaleway chat completions** on `qwen3.5-397b-a17b`: base URL and
  whether it carries the project id; the same parameters as above;
  `reasoning_effort` and where reasoning text arrives (to be dropped);
  whether reasoning counts toward the cap; usage fields; the returned
  `model`; errors; the organisation-wide rate limits; whether the
  catalogue's `int4` holds for serverless.
- **Chat completions rather than Responses for OpenAI**: one shared
  transport. Accepted only if T010 shows chat completions offer everything
  the spec requires on these models (strict schema, reasoning effort,
  `store` false, usage with reasoning). Otherwise the OpenAI client uses
  Responses, still behind the same `_complete` hook.

A point the documentation does not settle **blocks the client it
concerns**; it is never assumed.

### 2. Configurations (`app/decision/hosted.py`)

`Configuration` gains optional fields, each defaulting to `None` or
`False`, so that the four existing configurations keep their values:

- `currency` (`"USD"`, `"EUR"`);
- `input_side_price_per_mtok`, the highest input-side rate (OpenAI's
  cache-write rate);
- `processing_location`;
- `quantisation`;
- `account_variables`, the non-secret variables naming the account;
- `store` (OpenAI: `False`, sent explicitly);
- `check_identity`.

`vendor` widens to `"openai"` and `"scaleway"`.

**Fingerprints do not move.** `evals/first_calls.fingerprint` drops a new
field while it holds its default. A test pins the four existing
configurations' fingerprints to the values computed on `main`. Without
this, Gemini Flash's accepted first call would be lost, and with it a day
of its 20-request quota.

The three new configurations:

- **`gpt-6-luna` and `gpt-6.1-sol`**: vendor `openai`, developer OpenAI,
  endpoint `https://api.openai.com/v1`, key `OPENAI_API_KEY`, account
  `OPENAI_ORG_ID` and `OPENAI_PROJECT_ID`, prices in USD (luna 0.10 / 0.50,
  input side 0.125; sol 2 / 10, input side 2.50), location "global, no
  regional commitment".
- **`qwen3.5-397b-a17b`**: vendor `scaleway`, developer Alibaba (Qwen),
  endpoint per T010, key `SCALEWAY_API_KEY`, account
  `SCALEWAY_ORGANIZATION_ID` (and the project, if the endpoint needs it),
  prices in EUR (0.60 / 3.60), location Paris, France, quantisation per
  T010.

All three share: reasoning `low`; output cap 4096, as the others;
temperature at the vendor's default; three attempts, an outage
escalating the case; `check_identity` true; reservation `estimated` with
`utf8-bytes-v1`. Limits are filled from the maintainer's consoles (T060),
with source and date.

### 3. The client (`app/decision/openai_compatible.py`)

`OpenAICompatibleEngine(HostedEngine)`. `_complete` sends one chat
completion:

- the system and user messages of the shared core;
- the strict verdict schema;
- `reasoning_effort` and the output cap;
- `store` false for OpenAI only;
- SDK retries off.

A small per-vendor mapping says which parameters are sent and how usage is
read. Then:

- the final message content is the answer; reasoning content (Scaleway)
  and reasoning summaries are dropped, never kept;
- a refusal, a cut-off (`length`), a content filter or an empty answer is
  unusable and goes to a person;
- usage is read only from the fields the response actually set, with
  prompt, completion, reasoning and cached/cache-write details kept apart
  for costing;
- the returned `model` becomes `Answer.model_version`;
- errors map by category as Mistral's do (429 and 5xx are outages with
  `Retry-After`; another 4xx is a refusal carrying its status alone;
  transport errors are outages; an unreadable response keeps its
  reservation and stops the run).

The client is built with the key, the base URL and, for OpenAI, the
declared organisation and project. A key that does not belong to them is
refused by the vendor: the declaration is checked, not assumed (T010
confirms the behaviour). `app/decision/vendors.py` routes `openai` and
`scaleway` to it.

### 4. Account identity (`evals/accounts.py`, used by first calls, locks and ledgers)

`account_identity(config)`:

- **Existing configurations**: today's identity, unchanged (key hash, plus
  the Gemini project).
- **New configurations**: the declared variables. A missing one is refused
  before any client is built. The billing organisation keys the envelope
  and the run lock (`openai-<org>`, `scaleway-<org>`). The project keys
  first-call acceptance; pacing stays per configuration, as today, plus
  any shared limiter.

These ids are not secrets; they appear in private state and in results,
never a key or a key hash. Changing a key changes nothing here.

### 5. Prepaid envelopes (`evals/ledger.py`)

The Mistral ledger is kept byte-for-byte: same file, format and monthly
period. A `PrepaidLedger` beside it, in its own file per billing
organisation (`envelope-openai-<org>.json`, `envelope-scaleway-<org>.json`),
holds the account, the currency, a list of declarations (each with its date
and amount, never edited) and the attempts.

- **Declaring**: `--envelope AMOUNT` (in the account's currency, refused
  for Gemini and Mistral). The first declaration opens the ledger. A later
  one raises it only by a stated new total, recorded with the old kept, and
  never lowers it below what is spent.
- **Reserving**: tokens reserved × the highest rate of the configuration
  (input side or output), on disk under the lock, before sending. An
  attempt beyond what remains stops the run.
- **Settling**: prompt tokens at the input rate, cached ones at the cached
  rate only when reported as such, cache writes at their rate, and any
  input not broken down at the input-side rate; completion tokens,
  reasoning included, at the output rate. Missing usage keeps the
  reservation spent. Unreadable or inconsistent ledgers are refused, never
  reset.
- **Scaleway's free million tokens** is never deducted.

`Governed` takes either ledger through one small interface (reserve,
settle, state). The result's cost names its currency.

### 6. Identity on every response (`evals/governed.py`)

For a configuration with `check_identity`, `_check_usage` settles the
attempt first, then compares `answer.model_version` with `config.model`.
A mismatch or a missing identity raises `RunStopped`: the run is written
incomplete, its usage and cost kept, and the case never scored. The first
call's acceptance also requires the equality (FR-008). Existing
configurations are unaffected.

### 7. Runner and CLI (`evals/hosted_run.py`, `evals/runner.py`)

- `--envelope` as above. `--mistral-allowance-usd` and
  `--gemini-requests-used-today` are unchanged, and refused for the new
  vendors.
- `missing_credential` also names a missing account variable.
- `write()` records the currency, the account identity (ids only), the
  processing location, the quantisation, `store` where set, and the
  statement "integration run" for the three (SC-001), besides spec 004's
  regression-score statement.

### 8. Documents (folded in last)

- `docs/contract/decision-engine.md` and `docs/contract/evaluation.md`:
  the three configurations, the envelopes, the identity checks, and the
  limits ("not guaranteed").
- ADR-009's model-provider row: OpenAI (location, `store` false, 30-day
  abuse monitoring) and Scaleway (Paris, zero retention by default, up to
  two weeks on errors, no training, Alibaba receives nothing), each
  developer apart from its host.
- `docs/operations.md` (the engines section): commands, envelopes,
  account variables.
- `.env.example`: `OPENAI_API_KEY`, `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID`,
  `SCALEWAY_API_KEY`, `SCALEWAY_ORGANIZATION_ID` (and the project if
  needed), all empty.
- `evals/results/README.md`: a section for integration runs.
- README's mention of the evaluation engines, if it lists them.

## Risks

- **OpenAI returns a dated model name.** The equality check would stop
  every run. The first call shows it at a cost of cents; the remedy is a
  spec amendment naming the accepted form, never a silent relaxation.
- **Scaleway's schema mode on Qwen returns invalid JSON.** Counted
  `invalid`, never repaired; if it makes the configuration unusable, the
  spec's OVHcloud amendment applies.
- **Reasoning that does not count toward the cap** (Qwen): the
  usage-above-reservation stop bounds the damage to one request.
- **Fingerprint drift**: covered by the pinned-fingerprint test above.

## Research notes

*(T010: filled before any client is written, reviewed by Codex.)*
