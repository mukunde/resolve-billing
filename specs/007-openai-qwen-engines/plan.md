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

Two kinds of point:

- **Must be settled** before the client it concerns is written: the call
  and its parameters, the schema mode, how reasoning is set, usage fields
  and what totals include, finish reasons and refusals, the returned
  model string, errors, retries off, the base URL. One left open blocks
  that client; it is never assumed.
- **May stay unknown**, as the approved spec allows, recorded as unknown
  and never claimed: Scaleway's serverless quantisation (FR-003), whether
  Qwen's reasoning counts toward the cap (FR-006b; the
  usage-above-reservation stop bounds it), whether the vendor enforces
  the declared account (section 4).

### 2. Configurations (`app/decision/hosted.py`)

`Configuration` gains optional fields, each defaulting to `None` or
`False`, so that the four existing configurations keep their values:

- `currency` (`"USD"`, `"EUR"`);
- `cached_input_price_per_mtok` and `cache_write_price_per_mtok`, where
  the vendor prices them (OpenAI);
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
  cached 0.01, cache write 0.125; sol 2 / 10, cached 0.10, cache write
  2.50), location "global, no regional commitment".
- **`qwen3.5-397b-a17b`**: vendor `scaleway`, developer Alibaba (Qwen),
  endpoint per T010, key `SCALEWAY_API_KEY`, account
  `SCALEWAY_ORGANIZATION_ID` and `SCALEWAY_PROJECT_ID` (in the base URL),
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
declared organisation and project, sent as the SDK's organisation and
project headers. A test proves the headers are sent; whether OpenAI
refuses a key that does not belong to them is a server behaviour T010
looks for in the documentation. If it is documented, the declaration is
said to be checked by the vendor; if not, it is recorded as the
maintainer's declaration, as Gemini's project is today. Scaleway's key
belongs to one project of one organisation; the declared organisation id
is the maintainer's declaration, bound to the key by no request field
unless T010 finds one. `app/decision/vendors.py` routes `openai` and
`scaleway` to it.

`Answer` gains optional usage details, `None` unless a client sets them,
so the existing clients are unchanged: `cached_input_tokens`,
`cache_write_tokens` (both included in `input_tokens`, as OpenAI reports
them) and `reasoning_tokens` (included in `output_tokens`). They are
recorded, never added a second time.

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
- **Settling**, from `Answer`: each input category is priced at its rate
  only when it is reported -- cached tokens at the cached rate, cache
  writes at theirs. An absent category is never read as zero. When every
  priced category is reported, the remainder of `input_tokens` is at the
  input rate; when any is absent, the unclassified remainder is at the
  highest input-side rate that may apply (cache write). Categories summing
  above `input_tokens`, or reasoning above `output_tokens`, make the usage
  inconsistent: the reservation stays spent and the run stops. All of
  `output_tokens`, reasoning included, at the output rate; reasoning is
  never priced a second time. Missing totals keep the reservation spent.
  Unreadable or inconsistent ledgers are refused, never reset.
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
  `SCALEWAY_API_KEY`, `SCALEWAY_ORGANIZATION_ID`, `SCALEWAY_PROJECT_ID`, all empty.
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

Read 2026-10-26 from the official pages named, the pinned SDK's sources
(`openai` 3.29.0) and Scaleway's published OpenAPI schema
(`scaleway.com/en/developers/api/generative-apis/v1/schema.yml`).

### Settled

- **SDK `openai==3.29.0`** (published 2026-10-11). `AsyncOpenAI(api_key,
  base_url, organization, project, max_retries, default_headers)`.
  - With `None`, the client falls back to the environment:
    `OPENAI_ORG_ID`, `OPENAI_PROJECT_ID` and `OPENAI_BASE_URL`. So the
    Scaleway client must pass `base_url` explicitly and remove the two
    OpenAI headers (`default_headers` with `Omit()` for
    `OpenAI-Organization` and `OpenAI-Project`); otherwise OpenAI's ids
    would reach Scaleway. A test pins it on a network-free transport.
  - Retries are off with `max_retries=0`.
  - Errors: `APIStatusError` (`status_code`, `response`; subclasses
    include `RateLimitError` and `AuthenticationError`), `APIConnectionError`
    and its `APITimeoutError` subclass, and `APIResponseValidationError` for
    an unreadable response.
- **OpenAI chat completions** (`developers.openai.com`, chat create
  reference, structured outputs, reasoning and models pages): chat
  completions are supported on `gpt-6-luna` and `gpt-6.1-sol`, structured
  outputs included.
  - `max_completion_tokens` is "an upper bound … including visible output
    tokens and reasoning tokens". `max_tokens` is deprecated and not
    compatible with reasoning models.
  - `reasoning_effort` takes `low` on both models.
  - `response_format` `{"type": "json_schema", "json_schema": {name,
    schema, strict: true}}` uses a strict subset: `additionalProperties`
    false and every property required, as `verdict_schema()` already is
    for Mistral's strict mode.
  - `store` is "whether or not to store the output … for use in our model
    distillation or evals products". Its default is not stated, so it is
    sent `false` explicitly.
  - Response: `message.content`, `message.refusal` (a refusal is
    unusable), `finish_reason` among `stop`, `length`, `content_filter`,
    `tool_calls`, `function_call` (all but `stop` unusable), and `model`,
    "the model used".
  - Usage: `prompt_tokens`, `completion_tokens`;
    `prompt_tokens_details.cached_tokens` and `.cache_write_tokens`;
    `completion_tokens_details.reasoning_tokens`. Reasoning is included in
    `completion_tokens`; cached tokens are included in `prompt_tokens`.
- **Scaleway chat completions** (OpenAPI schema, API overview):
  - Base URL `https://api.scaleway.ai/{project_id}/v1`. The project id is
    optional ("your default Project" otherwise) and is sent explicitly
    from `SCALEWAY_PROJECT_ID`. Bearer secret key.
  - Parameters `max_completion_tokens` (`max_tokens` deprecated),
    `reasoning_effort` (`none`, `low`, `medium`, `high`; default
    `medium`), and `response_format` `json_schema` with `strict`, "only
    `true` is supported". `store` is listed as **not supported**, so it is
    not sent.
  - Response: `message.content` and `message.reasoning_content` (dropped,
    never kept), `finish_reason` among `stop`, `length`, `tool_calls`, and
    `model`.
  - Usage: `prompt_tokens`, `completion_tokens`,
    `completion_tokens_details.reasoning_tokens`,
    `prompt_tokens_details.cached_tokens`. Scaleway prices cached input
    only for some models; for `qwen3.5-397b-a17b` no cached rate is
    listed, so all input is priced at the input rate.
  - **For `qwen3.5-397b-a17b`, "`low` and `high` values are similar to
    `medium`"**: the configuration sends `low` as the spec says, and
    records that the host treats it as `medium`.
  - Rate limits are per organisation, shared by all projects. They apply
    "once you have a valid payment method", and the figures are on the
    organisation's quotas page (T060). An exceeded limit returns
    `x-ratelimit-*` headers; the status code is not stated, so 429 is
    handled as for the others, and any other 4xx is a refusal.

### Approved unknowns, recorded as unknown

- Whether OpenAI refuses a key outside the declared organisation or
  project: the authentication overview says the headers choose where a
  request runs and where usage counts, not what a mismatch does. Recorded
  as the maintainer's declaration.
- Whether Scaleway refuses a key outside the declared project: IAM
  "allows to limit access", not stated for every key. Recorded as the
  maintainer's declaration.
- Whether Qwen's reasoning counts toward `max_completion_tokens` on
  Scaleway: the cap's description does not say. FR-006b's
  usage-above-reservation stop bounds it.
- Serverless quantisation of `qwen3.5-397b-a17b`: the catalogue lists
  `int4` in its serverless table. Recorded as "int4 (catalogue,
  serverless table)", the source named.

### For the maintainer (T060)

Scaleway applies its rate limits only with a valid payment method on the
account. The free tier and the €2 envelope both assume one. Adding it is
his decision, made in his console, before the first call.
