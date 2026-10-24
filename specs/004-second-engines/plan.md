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
change. Storage: the Mistral envelope ledger and the accounts' run state,
outside the checkout (section 6, amended after review r160).

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
  least one byte, a bound T010 must justify for each vendor's tokenizer,
  never assume -- plus the overhead the vendor documents; plus the maximum
  output; plus reasoning when the output limit does not cap it. A
  configuration whose reasoning has no demonstrable billable cap is
  **blocked** -- never sent -- until one is established: without a maximum,
  the envelope cannot be guaranteed, and a waiver is the maintainer's
  explicit decision, not this plan's. The usage each answer reports is
  checked against the reservation: above it, or absent, the run stops.
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
  one). Under an interprocess lock held across read, check and reserve, each
  attempt's reserved maximum is written durably -- a temporary file, then an
  atomic replace -- before it is sent; an attempt that would exceed what
  remains is not sent and stops the run. After the answer, the attempt is
  settled at its reported cost; a reservation never settled (an interrupted
  run) stays counted as spent. An unreadable or inconsistent ledger is a
  refusal, never a reset. Switching model or starting a new run never resets
  it; a new month starts a new ledger, since the allowance resets on the 1st.
- Per case, a status: `decided`, `abstained`, `invalid`, `unavailable`,
  `not_sent_oversized`, `not_attempted`; attempts; total, limiter-wait and
  network times.
- *Amended after review r160 (2026-10-24), each change raised by the
  review:* the ledger, and per account a run lock and the time of the last
  request, live in `~/.resolve-billing/`, outside every checkout -- an
  allowance and a quota belong to an account, and two worktrees on one key
  must share them; one run per account at a time, starting a minute after
  the account's last request, which makes the shared Mistral limiter the
  running process's own. Counting tokens is held to the attempts and the
  deadline, and so are limiter waits. A case the rules settle without
  entering the engine is `rules_only` (scored); one that entered and left
  without an answer is `unavailable` (not scored). A defect is never
  translated into an outage: it stops the run, recorded by type. The result
  records the hash of the bytes the guard checked, the API version, the
  masking version and the schema's hash (FR-006). The ungoverned runner
  entry point refuses the three. Gemini's client is pinned to the Gemini API
  at the configured address and version, whatever the environment says.
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

Written 2026-10-24 (T010), from the official documentation and the SDK
sources at the pinned versions. Each point says where it comes from.

### Versions

- `google-genai==2.29.0` (released 2026-10-07) and `mistralai==3.2.0`
  (2026-10-09), both at least two weeks old (PyPI release history). In
  `mistralai` 3.x the client lives under `mistralai.client`.

### Google, `gemini-3.8-flash`

- **Call** (SDK source, `models.py`): `client.aio.models.generate_content(
  model=..., contents=..., config=types.GenerateContentConfig(...))`. The
  structured-output docs page now shows the newer Interactions API; the
  pinned SDK keeps `generate_content`, stable, with every field we need, and
  it is used here (to revisit when the SDK is upgraded).
- **Config fields** (SDK `types.py`): `system_instruction`, `temperature`,
  `max_output_tokens`, `response_mime_type="application/json"`,
  `response_json_schema` (the verdict schema, as a JSON Schema),
  `thinking_config=ThinkingConfig(thinking_level=LOW)`, `safety_settings`
  (left at the vendor's defaults, recorded as such; a safety finish is an
  invalid answer), `http_options=HttpOptions(retry_options=
  HttpRetryOptions(attempts=1))` -- no SDK retry.
- **Schema subset** (structured-output docs): types, `enum`, `properties`,
  `required`, `additionalProperties`, `items`, `minimum`/`maximum`;
  `minLength`/`maxLength` accepted but not enforced. The verdict schema
  (enums, a number, strings, an array of strings, `additionalProperties:
  false`) fits; its bounds stay enforced on arrival by `_ModelVerdict`.
- **Thinking** (thinking docs): on by default; `gemini-3.8-flash` takes
  `low`, `medium` (default) or `high`, cannot be turned off; thought tokens
  are billed as output, and `max_output_tokens` "sets the maximum number of
  tokens a response can generate, including thought tokens" -- a cap. A
  response that reaches it may come back truncated or empty: an invalid
  answer. Pinned: `low`, `max_output_tokens` 4096.
- **Input count** (SDK `models.py`, Codex r158): in the Gemini Developer API
  mode, `count_tokens` refuses `system_instruction`, `tools` and a generation
  config, and its config takes no output schema -- it cannot count the
  request as sent. The reservation is therefore: `count_tokens` on one text
  content holding the system prompt, the user payload and the serialised
  schema together, plus a fixed allowance for the request's framing (256
  tokens, checked against `prompt_token_count` on every answer -- above the
  reservation, the run stops), plus `max_output_tokens`. This is **not a
  demonstrated bound** (Codex r159): the 256-token margin is an estimate,
  and an undercount is caught only after the request is sent. Gemini stays
  **blocked** until the maintainer accepts an explicitly estimative
  reservation (margin, check after each answer, stop on overrun) or a bound
  is justified. The counting call is itself a network request: SDK retries
  off, inside the run's deadlines, attempts and measures. A test builds it
  without network against the pinned SDK.
- **Response** (SDK `types.py`): `usage_metadata.prompt_token_count`,
  `candidates_token_count`, `thoughts_token_count`, `total_token_count`;
  `candidates[0].finish_reason` among `STOP`, `MAX_TOKENS`, `SAFETY`,
  `RECITATION`, `BLOCKLIST`, `PROHIBITED_CONTENT`, `SPII`, `OTHER`...;
  `model_version`. Anything but `STOP` with a valid verdict is invalid.
- **Errors** (SDK `errors.py`): `APIError` with `code` (HTTP) and `status`;
  `ClientError` (4xx), `ServerError` (5xx). 429 (`RESOURCE_EXHAUSTED`) →
  `rate_limited`; 401/403 → rejected; 5xx and transport → unavailable. A
  per-minute limit and a daily quota are not told apart by class: rate
  limiting persisting past the attempts is treated as durable exhaustion.
- **Cost**: free tier -- "estimated zero under the free quota", with the
  pricing page and its date.

### Mistral, `mistral-large-4` and `zai-glm-5-3`

- **Call** (SDK source, `client/chat.py`): `client.chat.complete_async(
  model=..., messages=[system, user], temperature=..., max_tokens=...,
  response_format={"type": "json_schema", "json_schema": {"name":
  "verdict", "schema": ..., "strict": True}}, reasoning_effort=...,
  retries=None, timeout_ms=...)` -- `retries=None` disables the SDK's
  retries (`chat.py`), where omitting it inherits the client's.
- **Content** (SDK models; reasoning docs): `message.content` is a string,
  or a list of chunks -- `ThinkChunk` (`type: "thinking"`) and `TextChunk`
  (`type: "text"`). The final text chunk is the answer; thinking chunks are
  dropped, never logged or stored.
- **Usage** (SDK `usageinfo.py`): `prompt_tokens`, `completion_tokens`,
  `total_tokens`; no separate reasoning count. The SDK defaults each to `0`
  when absent: presence is read from the fields actually set on the
  response (`model_fields_set`), never from the values; usage absent or
  incomplete stops the run, and the attempt's reservation stays charged.
- **`mistral-large-4`** (model page): **Public Preview**, API names
  `mistral-large-4` / `mistral-large-4-0`, 1M context, "structured-outputs"
  listed; licence not stated ("Open weights: coming soon"). The reasoning
  docs describe `reasoning_effort` `none` (thinking chunk omitted, content a
  string) and `high`, without listing this model's values. Pinned: `none`.
- **`zai-glm-5-3`** (reasoning docs): `reasoning_effort` `low`, `high` or
  `max` -- not `none`; content always chunks. Pinned: `low`.
- **Prices** (docs.mistral.ai/inference/pricing, read 2026-10-24), per
  million tokens: Large 4 $1.36 in, $4.18 out (a 50% launch discount from
  2026-10-06 for two weeks, likely over -- the regular price is used); GLM
  5.3 $1.40 in, $4.40 out. Whether reasoning is billed as output is not
  stated.
- **Errors** (SDK `client/errors`): `MistralError` with `status_code`,
  `SDKError`, `HTTPValidationError` (422), `NoResponseError`. 429 →
  `rate_limited`; 401/403/422 → rejected; 5xx, no response, transport →
  unavailable.

### Unsettled, and what each blocks

1. **Mistral: does `max_tokens` cap the reasoning, and is it billed within
   `completion_tokens`?** The API reference defines `max_tokens` as "the
   maximum number of tokens to generate in the completion", and the
   thinking chunk is part of the completion -- but no page says so
   explicitly, and one call cannot prove a bound for the next (Codex r158).
   Both Mistral configurations stay **blocked** until a documentary
   guarantee or a confirmation from Mistral; a first minimal call then
   checks that reading. A waiver is the maintainer's explicit decision.
2. **Mistral: the input bound.** No token-count endpoint for chat. A UTF-8
   byte count bounds the ordinary tokens of a byte-level BPE tokenizer
   (Tekken is one), but which tokenizer serves `mistral-large-4` is not
   established, nor GLM 5.3's; and the template's and schema's added tokens
   need a bound of their own. Both configurations stay **blocked** on this
   too, until the tokenizer of the served model and those overheads are
   documented.
3. **Terms**: Large 4 is a Public Preview, and GLM 5.3 a third-party model;
   which data terms apply to each (and whether the opt-out covers them) is
   the maintainer's documented check (T071).

**The maintainer's decisions (2026-10-24).** He accepted, explicitly:
(1) for Gemini, an estimative input reservation -- the counted text plus a
256-token margin, checked against `prompt_token_count` after each answer,
the run stopping on an overrun; (2) for both Mistral configurations, a
waiver of points 1 and 2 -- the reasoning cap and the tokenizer bound stay
undemonstrated; with no payment method on file, the worst case is the
month's free allowance spent. So every configuration runs with an
**estimated**, not guaranteed, reservation and cost, said so in every result;
the envelope, the checks after each answer and the stops stay. These
waivers are recorded in the configurations (`reservation: estimated`) and in
`docs/operations.md`.

**Before the waivers.** Gemini was blocked on its input reservation (above).
Both Mistral configurations were blocked on points 1 and 2. They are
still built and tested on stubs -- the shared core, the chunk parsing, the
envelope -- and the runner refuses a blocked configuration, naming what
blocks it; unblocking one is a recorded decision.

## Amendment (2026-10-24): the daily quota and the first call

For the spec's amendment of the same date (approved at `b84a92d`). Branch
`004-daily-quota`, its own pull request, before any of the maintainer's
runs (T072).

### A1. The configurations (`app/decision/hosted.py`)

New fields on `Configuration`:

- `requests_per_day: int | None` and `quota_timezone: str | None` --
  `gemini-3.8-flash`: 20, `America/Los_Angeles`; None for Mistral, whose
  monthly envelope is the binding limit.
- `attempts: int` -- `gemini-3.8-flash`: 1; Mistral: 3, as today.
- `stop_on_outage: bool` -- True for Gemini (FR-007d), False for Mistral.
- `limits_source: str` -- where and when the limits were read: Gemini, "AI
  Studio rate-limit page, project resolve-billing-eval, read 2026-10-24";
  Mistral, "admin console limits page, read 2026-10-24".
- `reservation_estimator: str` -- `utf8-bytes-v1` for all three.

Gemini's limits become the ones read: `requests_per_second = 5 / 60`,
`tokens_per_minute = 250_000`. Mistral's are unchanged (they match the
console).

### A2. Gemini's reservation, local (`app/decision/gemini.py`)

One estimator shared by both clients, `estimated_tokens(system, user,
margin, max_output)` in `hosted.py`: the UTF-8 bytes of `system + user +
json.dumps(verdict_schema())` plus the margin plus the output cap. Gemini's
margin becomes 512 (as Mistral's): the system instruction and the schema
travel as fields, and their framing is not documented. `GeminiEngine` no
longer calls `count_tokens`; `counts_remotely` is False and `_count_config`
goes. An estimate under the new waiver; each answer's usage is still
checked (unchanged code in `evals/governed.py`).

### A3. The daily quota (`evals/quota.py`, new)

`DailyQuota(project, limit, timezone, now)`, its state in
`STATE_DIR/quota-<project>.json`, under its own lock file
`quota-<project>.lock` (`state.try_lock`), so every key and every checkout
of one project share it (FR-007b). The project is the Google project id,
from `GEMINI_QUOTA_PROJECT` in the maintainer's `.env` (not a secret).
Any Gemini run, smoke test included, without it -- or with a value that is
not a Google project id (lowercase letters, digits and hyphens, 6 to 30
characters) -- is refused before any client is built; the value names a
file, so it is checked before use. The association between the key and
the project is the maintainer's declaration, not verified by the code; the
documentation says so.

State: `{"project", "days": {"YYYY-MM-DD": {"declared": n, "sent": m}},
"last_seen": <epoch>}`, written atomically (`state.write_atomically`).
Read failures or an inconsistent shape refuse (`QuotaRefused`), never
reset. The quota day is the date in `quota_timezone` (`zoneinfo`, daylight
saving included); `tzdata` joins the `engines` extra, since Windows ships
no IANA database, and a test converts Pacific dates without the system's.

Every operation -- `declare`, `admit_full_run`, `take` -- holds the lock,
reads the clock once, refuses before mutating anything if `now <
last_seen` (a clock gone back), and on success advances `last_seen` to
`now`.

- `declare(used)` -- initialises today once, with the count read in the
  console; a second declaration for the same day is refused, so a count
  is never lowered (FR-007c).
- `admit_full_run()` -- refused unless today is declared and
  `declared + sent == 0` (FR-007c).
- `take()` -- the day re-read from the clock; an undeclared day stops the run
  (`RunStopped`, "quota day <d> not declared", FR-007e); `declared + sent
  + 1 > limit` stops it ("daily quota exhausted (local count)"); otherwise
  `sent += 1` persisted before returning. Never decreased.
- `state()` -- for the result.

`--gemini-requests-used-today N` declares today before the run, when today
is not declared yet. A Gemini run, full or smoke test, requires today to be
declared -- already, or by this flag; the flag on a day already declared is
refused (a count is never lowered), and so is the flag for a configuration
without a daily limit.

### A4. The governed engine (`evals/governed.py`)

- Attempts come from the configuration (`attempts`), forced to 1 by
  `--first-call-only`.
- Before each send, in this order: the limiters; `quota.take()` when the
  configuration has a daily limit; the ledger's reservation (Mistral);
  `on_send`; the send. A refusal at any step sends nothing.
- `sends` counts requests actually handed to the vendor.
- On the last attempt's outage: with `stop_on_outage`, `RunStopped("outage:
  <category>")`, the case `unavailable`, nothing sent after it; otherwise
  as today (the investigation escalates, the run goes on). A vendor 429 on
  the last attempt stays "rate limiting persisted", told apart from the
  local count's exhaustion.

### A5. The first call (`evals/hosted_run.py`, `evals/runner.py`)

`--first-call-only`: the governed engine with one attempt; after the first
case that entered `decide` (whatever came of it), the run stops: written
incomplete, `"smoke_test": true`, `"sends": 0 or 1`, its reason "first call
only" on success, otherwise the refusal's or failure's own (a
`not_sent_oversized` case reads "not sent: oversized"). Allowed for every
configuration; `admit_full_run` is not required for it (a smoke test
needs one request, not twenty).

### A6. The result

Adds `reservation_estimator`, `smoke_test`, `sends`, and for a configuration
with a daily limit `daily_quota`: `{project, limit, timezone, days: {date:
{declared, sent}}, run_sends, assumptions: "dedicated project, no other
caller; every attempt counted, never refunded"}` (FR-009 amended).

### A7. Tests and documents

Tests before code, as SC-004 lists them, plus: the instrumented,
network-free engine reached exactly 20 times by the investigator on the
development set (a pinned count; a change of the set updates it in the
same commit). Docs: the evaluation contract; `docs/operations.md` (the
check recorded for both vendors as read on 2026-10-24, the Gemini
procedure -- declare, first call one Pacific day, full run another --
`GEMINI_QUOTA_PROJECT`); `.env.example`; ADR-009's row unchanged.

## Complexity Tracking

None.
