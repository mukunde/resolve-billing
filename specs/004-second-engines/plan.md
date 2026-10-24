# Implementation Plan: Second decision engines on free tiers, evaluated on the development cases

**Branch**: `004-second-engines` | **Date**: 2026-10-24 | **Spec**: [spec.md](spec.md), approved by the maintainer at commit `94cf4ea`

## Summary

Three engine configurations -- `gemini-3.8-flash` (Google), `mistral-large-4`
and `zai-glm-5-3` (Mistral) -- built on one shared core that reuses the
pseudonymisation, the masking, the verdict schema and the reference mapping
of the Claude engine, with a thin client per vendor. They live in an
evaluation registry the runner uses; the service never sees them. The runner
gains, for these configurations only: a development-set guard, a token
limiter, a Mistral spending envelope, and results that say whether a run is
complete. The Claude engine, the service and the policies do not change.

## Technical Context

Python 3.12 (CI), 3.13 locally. New optional extra `engines` pinning the
vendors' SDKs (`google-genai`, `mistralai`) at versions at least two weeks
old, chosen in T010; CI does not install it: the clients are tested on stubs
shaped like the SDKs, as the Stripe client is. No storage change.

## Constitution Check

| Principle | How this plan meets it |
|---|---|
| II | Unchanged: these engines recommend; policy, approval and execution untouched; they never reach the service. |
| III | Vendors behind the `DecisionEngine` port, named only in their modules, the evaluation registry and their tests. |
| V | ADR-006 holds: one request per case, no loop, no tool; the model sets no control flow. |
| VI | Each result names configuration, prompt, masking, parameters and cost regime. |
| VII | Same pseudonymised context and masked message, pinned per vendor by tests; no vendor text in logs; reasoning never stored. |
| VIII | A new measurement, published apart, with its limits stated (regression scores on dev). Mock baseline unchanged. |
| IX | The holdout is unreachable by these engines (dev-only guard). |
| Sensitive | Two model providers receive masked synthetic data: final-diff approval; ADR-009 amended. |

## Design

### 1. Contracts first (`docs/contract/decision-engine.md`, `docs/contract/evaluation.md`)

Each written as the capability stands -- decision engine from ADR-002,
ADR-006, ADR-009 (model provider) and `app/decision/`; evaluation from README
"Evaluation", `evals/taxonomy.md`, `docs/operations.md` ("What CI actually
enforces"), `evals/results/README.md` -- in a commit of their own, listed in
`docs/contract/README.md`. The change is folded in at the end (section 8).

### 2. Research pinned before code (T010)

From the vendors' official documentation and the SDK sources at the chosen
versions, recorded in this plan's "Research notes" and reviewed by Codex
before any client is written: the exact call (Gemini: native API --
`generate_content` or the newer Interactions API, whichever the docs name as
current; async form), structured-output parameters and supported JSON Schema
subset, reasoning/thinking settings, safety settings, usage fields
(including reasoning tokens), finish reasons, the model version returned,
error classes and status codes, retry options to disable; Mistral: the chat
completions call, `response_format` with a JSON schema, `reasoning_effort`
for `zai-glm-5-3`, the block-list `content`, usage fields, errors; each
model's official price for the envelope. Anything the docs do not settle is
listed, never assumed.

### 3. The shared core (`app/decision/hosted.py`)

- Pure helpers extracted from `claude.py` into `app/decision/verdict.py`:
  the verdict model (`_ModelVerdict`) and its JSON schema, and the
  escalation built when an answer is invalid. `claude.py` imports them; its
  behaviour and tests are unchanged.
- `HostedEngine` (base): `decide()` does what Claude's does -- `anonymise`,
  `mask_message`, one request built from the v3 prompt and the same
  `<customer_message>` / `<billing_context>` user payload, local validation,
  `names.restore` / `names.resolve_all`, an invalid or absent answer handed
  to a person -- around one hook per vendor: `_complete(system, user) ->
  Answer` (text or None, finish reason, usage, model version returned).
  Tracing as Claude's: counts and ids only.
- `Configuration` (frozen): name (the model id), vendor, model developer,
  endpoint, key variable, model id, prompt version label, temperature,
  maximum output tokens, reasoning setting, safety settings, limits
  (tokens/min, requests/s), price (or the free-quota regime). Three entries,
  closed; no alias, no fallback; a parameter the vendor rejects or ignores is
  reported as such.
- Errors: transport and service failures → `EngineUnavailable` (with a
  category, `rate_limited` and `quota_exhausted` among them); refusals of the
  request or key → `EngineRejected`; categories only.

### 4. The clients (`app/decision/gemini.py`, `app/decision/mistral.py`)

- Gemini: the native call pinned in T010, structured JSON with the verdict
  schema, the configuration's parameters, SDK retries off.
- Mistral: chat completions with a JSON-schema `response_format`; for
  `zai-glm-5-3`, `reasoning_effort` set as pinned (`low`, the lowest
  documented), and `content` read as blocks: the final text block only;
  reasoning blocks discarded, never logged or stored; no final text block →
  invalid answer.
- Both: truncated (length finish reason), filtered (safety finish reason)
  and empty answers → invalid; vendor text never in messages.

### 5. The evaluation registry and the guards (`evals/engines.py`, `evals/runner.py`)

- `EVALUATION_ENGINES`: the service's engines (`deps.ENGINES`) plus the three
  configurations; `CREDENTIALS` moves here and gains `GEMINI_API_KEY` and
  `MISTRAL_API_KEY`. The runner validates, builds and checks credentials
  through it. `deps.ENGINES` is unchanged; a test shows the service refusing
  `DECISION_ENGINE` set to each of the three ids.
- Development-set guard, for the three, before any read beyond it or any
  client: the `--cases` path, resolved (links followed), must be
  `evals/cases.json` of this checkout; `RESOLVE_EXTRA_FIXTURES_DIR` must be
  unset; then the file's bytes are read once, hashed against
  `DEVELOPMENT_SET_SHA256` (a constant, and a CI test that it equals the
  committed file's hash, so changing the cases is a deliberate update), and
  those same bytes are parsed.

### 6. Limits, envelope and outcomes (`evals/limits.py`, `evals/runner.py`)

- Per configuration, a limiter for tokens/min and requests/s. Each request
  reserves a conservative token count: input estimated from its characters
  (documented ratio, checked against the usage each answer reports -- a
  report above the reservation stops the run), plus maximum output, plus the
  reasoning allowance. A request whose reservation exceeds the per-minute
  quota alone is not sent; its case is marked `not_sent_oversized`.
- Attempts per case bounded (3), waits honouring `Retry-After`, a run
  deadline; SDK retries disabled and our attempts counted; durable
  exhaustion (a quota-exhausted error, or rate limiting persisting past the
  attempts) stops the run.
- Mistral envelope: `--mistral-envelope-usd` required for a Mistral
  configuration (the remaining allowance the maintainer declares); each
  attempt is charged its reserved maximum cost before it is sent; an attempt
  that would exceed it is not sent and the run stops. One envelope per run;
  a second run declares what remains.
- Per case, a status: `decided`, `abstained`, `invalid`, `unavailable`,
  `not_sent_oversized`, `not_attempted`; attempts; total, limiter-wait and
  network times.
- A run stopped before its end is written with `complete: false`, the cases
  attempted and not attempted, the stop reason, and exits non-zero; its
  totals are never presented over 34. The mock's and Claude's runs and
  their outputs are unchanged (the new fields appear only for the three).

### 7. Results (`evals/results/`)

One file per configuration and run, as Claude's, plus: configuration (vendor,
developer, model, parameters, model version returned), `complete`, the
statuses, cost (Gemini: "estimated zero under the free quota", source and
date; Mistral: estimated maximum, envelope declared, and the console's usage
the maintainer reads after), the privacy setting the maintainer declares
(`--privacy` required: `opted-out` or `training-allowed`), and "regression
score on development cases". `evals/results/README.md` lists them apart.

### 8. Documents

The two contracts with the change folded in; ADR-009's model-provider row
(the two vendors, what they receive, the terms as established, GLM's
provenance apart from its vendor); `docs/operations.md` (keys, the
per-configuration check, how to run, how to read an incomplete run);
`.env.example`; README "Evaluation" pointing to the separate results.

## Tests

| What | Where | Runs |
|---|---|---|
| Verdict helpers extracted, Claude unchanged | existing Claude tests | CI |
| Core: payload pinned per vendor, refs restored, invalid → person, errors by category, no vendor text, no reasoning stored | `tests/unit/test_hosted_engines.py` | CI, stubs |
| GLM blocks: final text only, reasoning dropped, none → invalid | same | CI |
| Registry, service refusal of the three ids, credentials | `tests/unit/test_evaluation_registry.py` | CI |
| Dev guard: external path, link elsewhere, altered file, extra fixtures → refused, no client; hash constant equals the file | same | CI |
| Limiter, oversized request, attempts, Retry-After, durable exhaustion, envelope, incomplete run, exit code, statuses | `tests/unit/test_evaluation_limits.py` | CI |
| Existing runs unchanged | mock baseline, coverage | CI |
| One run per configuration | the maintainer | local, keys |

## Project Structure

```text
specs/004-second-engines/   spec.md, plan.md, tasks.md
app/decision/verdict.py, hosted.py, gemini.py, mistral.py
app/decision/claude.py      imports the extracted helpers
evals/engines.py, evals/limits.py, evals/runner.py
tests/unit/test_hosted_engines.py, test_evaluation_registry.py, test_evaluation_limits.py
docs/contract/decision-engine.md, evaluation.md, README.md
docs/adr/009-trust-boundaries.md, docs/operations.md, evals/results/README.md
pyproject.toml (extra `engines`), .env.example, README.md
```

## Research notes

To be written in T010.

## Complexity Tracking

None.
