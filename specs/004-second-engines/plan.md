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
old, chosen in T010. The clients are tested on stubs shaped like the SDKs;
and, without network, against the pinned SDKs themselves (signatures,
request construction, representative responses and exceptions): CI
installs the extra for those checks, makes no call, and the gates do not
change. Storage: a git-ignored Mistral envelope ledger (section 6).

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
model's official price for the envelope; how many tokens a request
can count at most (a vendor token count, or a justified bound), and whether
reasoning is capped by the output limit. A point the docs do not settle
**blocks the client it concerns** until settled; it is never assumed.

### 3. The shared core (`app/decision/hosted.py`)

- Pure helpers extracted from `claude.py` into `app/decision/verdict.py`:
  the verdict model (`_ModelVerdict`) and its JSON schema, and the
  escalation built when an answer is invalid. `claude.py` imports them. A
  test written **before** the extraction pins, on a fake client, the exact
  request Claude sends and the decisions it returns for a valid, an invalid
  and a truncated answer; it passes unchanged after (the mock baseline does
  not cover Claude).
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

- Limiters: per configuration (tokens/min, requests/s) and, for Mistral,
  one shared across its two models -- the shared ceiling the maintainer
  confirms in T071, and until then, conservatively, the smaller per-model
  limit applied to both together.
- Each request reserves an upper bound of its tokens: the vendor's own count
  of the whole request (system prompt, user payload, schema) where T010
  finds one; otherwise the request's UTF-8 byte count -- a token covers at
  least one byte -- plus the overhead the vendor documents; plus the maximum
  output; plus reasoning when the output limit does not cap it. If reasoning
  has no demonstrable cap, no guaranteed maximum is claimed: the cost is
  then an estimate, said so. The usage each answer reports is checked
  against the reservation: above it, or absent, the run stops. (Prompt v3 is
  5.3 kB; the byte bound keeps a request under the 20,000 tokens/min of the
  Mistral models.)
- A request whose reservation exceeds a per-minute quota on its own is not
  sent; its case is `not_sent_oversized`, never counted as a decision of the
  model.
- Attempts per case bounded (3), waits honouring `Retry-After`, a run
  deadline; SDK retries disabled and our attempts counted; durable
  exhaustion (a quota-exhausted error, or rate limiting persisting past the
  attempts) stops the run.
- Mistral envelope: one ledger for the month, persisted in a git-ignored
  file (`evals/.mistral-envelope.json`), shared by both Mistral models and
  every run. The maintainer declares the month's allowance once
  (`--mistral-allowance-usd`, refused if the month's ledger already holds
  one); each attempt is charged its reserved maximum before it is sent, and
  an attempt that would exceed what remains is not sent and stops the run.
  Switching model or starting a new run never resets it; a new month starts
  a new ledger, since the allowance resets on the 1st.
- Per case, a status: `decided`, `abstained`, `invalid`, `unavailable`,
  `not_sent_oversized`, `not_attempted`; attempts; total, limiter-wait and
  network times.
- A run stopped before its end -- exhaustion, the envelope, the deadline,
  an interruption (`KeyboardInterrupt`, cancellation) -- is written with
  `complete: false`, the cases attempted and not attempted, the stop reason,
  and exits non-zero; its totals are never presented over 34. A result file
  never overwrites an earlier one: each run's file carries its start time. The mock's and Claude's runs and
  their outputs are unchanged (the new fields appear only for the three).

### 7. Results (`evals/results/`)

One file per configuration and run, as Claude's, plus: configuration (vendor,
developer, model, parameters, model version returned), `complete`, the
statuses, cost (Gemini: "estimated zero under the free quota", source and
date; Mistral: estimated maximum -- or estimate, when reasoning is uncapped
-- the envelope's state, and the console's usage the maintainer reads
after), the data terms as declared (`--data-terms`: the basis -- the
contractual EEA terms, an opt-out setting, training allowed -- or
`unknown`; never forced into one of two values), and "regression score on
development cases". `evals/results/README.md` lists them apart.

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
