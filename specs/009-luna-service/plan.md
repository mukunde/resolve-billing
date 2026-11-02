# Implementation Plan: `gpt-6-luna` on the demonstrator's service path

**Branch**: `009-luna-service` | **Date**: 2026-11-02 | **Spec**: [spec.md](spec.md), approved by the maintainer at `65669cb` with its four choices

## Summary

Add one vendor-neutral wrapper around an evaluated hosted engine, `BoundedEngine`,
that gives it what the service needs and the evaluation runner gave it from
outside: bounded retries, a deadline, the identity check, in-flight limits and a
record of every attempt. Register `gpt-6-luna` in the service through it, behind a
refusal at start. The prompt, schema, masking and request do not change, and
neither do the evaluation path or Claude's engine.

Like spec 008's, this plan is one file: the design below stands in for the
separate research, data-model and quickstart documents.

## Technical Context

Python 3.12 (CI), 3.13 locally. No new dependency: `openai==3.29.0` (the `engines`
extra) and the OpenTelemetry packages are already pinned. Storage stays SQLite. CI
installs the extras for the network-free checks and makes no paid call.

## Constitution Check

| Principle | How this plan meets it |
|---|---|
| I | Unchanged: rules first; a dispute they settle sends nothing. |
| II | Unchanged: every failure ends in an escalation or an error; policy, approval and execution are untouched. |
| III | The wrapper, the attempt port and the audit name no vendor. Vendors stay in configurations, clients, `deps.py` and tests. |
| V | ADR-006 holds: one request per attempt, no loop over a model's output, no tool. Failed and interrupted calls become traceable. |
| VI | Each attempt carries provider, models, times, outcome, usage and cost, unknowns explicit. |
| VII | The same pseudonymised payload and masked message; a test pins the request to the evaluated one. |
| VIII | A declared run through the service, authorised, stored; fault-injection tests for what no score measures. |
| IX | The holdout is untouched; the development-set guard holds; the conditions for the reference run are recorded. |
| Sensitive | Final-diff approval. Any paid step is authorised at launch. |

## Design

### 1. `BoundedEngine` (`app/decision/bounded.py`, new)

Implements `DecisionEngine`, wraps a `HostedEngine`, keeps its `name`, `version` and
`invoice_references`, and exposes `aclose`. `decide` does, in order:

1. `request_texts`: the same system prompt, payload and pseudonyms, built once, so
   every attempt sends identical bytes. A fresh `invocation_id`, local to this call,
   groups its attempts: `dispute.id` alone cannot, since evaluation repeats a case.
   All bookkeeping stays local to the call, so one wrapper serves concurrent requests.
2. A loop of at most `Bounds.attempts`, under one `deadline` measured with an
   injected monotonic clock:
   - take a slot: a semaphore of `Bounds.in_flight` (4), then the configuration's
     `requests_per_second` and `tokens_per_minute`, the latter admitted on the
     request's estimated tokens (`engine.reservation`). `evals.limits.Limiter` moves
     to `app/decision/limits.py` and `evals/limits.py` imports it back. Waiting counts
     against the deadline, and a wait that cannot fit ends the loop as exhausted. The
     limits hold within one process: one service process per account is the supported
     setup, stated in the operations guide, and a vendor's 429 is the backstop;
   - `recorder.started(...)`, before anything is sent. If it fails or refuses, nothing
     is sent;
   - `asyncio.timeout(min(per_attempt, remaining))` around the engine's request;
   - `recorder.finished(...)` with the outcome, in every path, `CancelledError`
     included, which then propagates.
3. Outcomes, a closed set in `AttemptOutcome`:

| Outcome | From | Retried | Ends in |
|---|---|---|---|
| `answered` | a usable answer of the right model | -- | the decision |
| `unusable` | cut off, filtered, empty, invalid, unreadable | no | `escalation(telemetry)`, as today |
| `wrong_model` | a returned model other than `returned_model or model` | no | `EngineMisidentified`, its own escalation |
| `timeout` | the attempt's time ran out | yes | exhausted: `EngineUnavailable` |
| `unavailable` | connection failure, 5xx | yes | exhausted: `EngineUnavailable` |
| `rate_limited` | 429 | yes | exhausted: `EngineUnavailable` |
| `rejected` | any other 4xx | no | `EngineRefused`, an HTTP error |
| `cancelled` | the caller went away | no | propagates |
| `failed` | an unexpected exception | no | propagates; its type is recorded, never its text |

`wrong_model` costs what its usage says, and its answer is discarded before any
decision is built. It is not an outage: an answer came and failed the check. The
investigator catches `EngineMisidentified`, a vendor-neutral exception of the port,
writes its own `decision_engine_identity_failed` event and explanation, and
escalates. No other engine raises it. `openai_compatible._translated` already sorts the errors this
way: `EngineUnavailable` carries `rate_limited`, `server_error` or `transport`, and
`EngineRejected` any other 4xx.

Waits: a valid `retry_after` is honoured, and when it exceeds the time left the
loop stops at once as exhausted. Without one, 2 then 4 seconds. `Bounds` is frozen
and lives with the configuration: `attempts=3`, `per_attempt=60.0`, `deadline=90.0`,
`in_flight=4`, in `app/decision/bounded.py`, stated once and pinned by a test. The
difference from the evaluation's `DEADLINE`, which covers a run, is documented
there.

**Why a wrapper.** `Governed` holds the same ideas but is tied to the runner's
ledgers and limiter, and lives in `evals/`, which `app/` must not import. The wrapper
reuses `_complete`, `request_texts` and `decision_from`; they become public, under
names that keep `Governed` working, so nothing about the request is restated.
`attempt_cost` moves from `evals/governed.py` to `app/decision/hosted.py`, and
`evals/governed.py` imports it back.

### 2. The refusal an HTTP caller sees

`EngineRefused(EngineRejected)` in `app/decision/base.py` carries a closed category,
never vendor text. `app/main.py` handles it: 502, `{"detail": "decision engine
refused the request: nothing was decided", "category": ...}`. It is a subclass so
that Claude's `EngineRejected` keeps its behaviour (FR-007c), and the runner's
handlers still catch it.

### 3. Attempts: port and record

`AttemptRecorder`, a `Protocol` in `app/decision/base.py`:

```text
started(attempt: AttemptStart) -> None      # ids, number, provider, requested model,
                                            # time, the request's estimated tokens
finished(attempt: AttemptEnd) -> None       # outcome, returned model, times, the
                                            # Answer's usage fields, cost when known
```

`decide` receives no extra argument: the port stays as it is. The two types carry
what a ledger needs to reserve before the send and to check usage after it, so a
recorder can refuse (section 9).

`SqliteAttemptStore` (`app/infrastructure/attempts.py`, new) on the audit file:

```text
engine_attempts(
  attempt_id PK, invocation_id NOT NULL, dispute_id NOT NULL, number NOT NULL,
  provider NOT NULL,
  requested_model NOT NULL, returned_model, started_at NOT NULL, ended_at,
  duration_ms, outcome, input_tokens, output_tokens, details_json, cost_micros,
  pricing_as_of)
index on (invocation_id, number), and on dispute_id
```

- Rows are written in their own committed transaction, outside the run's, so a
  refusal or a crash keeps them. `outcome IS NULL` reads as **unfinished**, and
  nothing ever updates it to success after the fact.
- `details_json` holds token categories only: cached, cache write, reasoning.
- The run links to its attempts by a new column, `agent_runs.engine_invocation_id`,
  added by the existing `_migrate` pattern, NULL for every other engine and on older
  rows. It comes from the telemetry, or from the exception on a path with none, and is
  written with the row, which stays immutable. Claude's rows see only NULL. A refusal
  leaves no run: its attempts are found by `dispute_id`.
- An unanswered attempt has NULL tokens and NULL cost. An answer with no usage has
  NULL tokens and NULL cost, never zero.

### 4. Telemetry, unknowns included (FR-007)

`EngineTelemetry` gains `usage_known: bool = True` and `unknown_cost_attempts: int =
0`, defaults that leave every other engine as it is.

- `BoundedEngine` builds the telemetry from the attempts: `model_id` is the model
  that answered; `cost_micros` sums the known attempts when at least one is known;
  `latency_ms` covers the whole stage.
- `agent_runs` gains `unknown_cost_attempts` (NULL on older rows). When `usage_known`
  is false the row's token columns hold NULL. A row with `unknown_cost_attempts > 0`
  is read as a lower bound, and the reconciler and the run page say so.
- An outage writes no `decision_created` event today and its escalation carries no
  telemetry, so the investigator takes the invocation id from the exception.

### 5. Traces (FR-007b)

- `chat_attributes` takes `provider`, defaulting to Anthropic's value, so Claude's
  spans do not change. `GEN_AI_PROVIDER_NAME` for OpenAI uses the convention's value.
- `BoundedEngine` opens one `chat <model>` span per attempt, inside the investigation
  span, using a new `attempt_attributes` allowlist: number, outcome, durations,
  returned model, tokens, cost, unknown flags. `error.type` holds the outcome
  category, never exception text.
- The test that reads the instrumentation's source covers `bounded.py`; an in-memory
  exporter test covers the four paths. No auto-instrumentation is installed, and the
  operations guide says it is unsupported.

### 6. The run page

`run_view` reads `attempts_for(invocation_id)` and shows number, model, duration,
outcome, tokens, cost or "inconnu", and "non terminée" for an open row, with the
trace. `GET /api/v1/runs/{id}` returns the same list. Older rows show nothing.

### 7. Wiring and the refusal at start (FR-001, FR-009)

- `deps.ENGINES` gains `"gpt-6-luna"`, a function building
  `BoundedEngine(OpenAICompatibleEngine(CONFIGURATIONS["gpt-6-luna"]), recorder)`, the
  recorder sharing the audit file. `wire` reaches the inner engine through
  `invoice_references`, a property of the wrapper.
- `deps.selection_refusal() -> str | None` names what is missing: the billing
  platform must be `fake`, `RESOLVE_DATA=synthetic` must be set, by that exact word
  as `STRIPE_WRITES=enabled` is, the key and declared account ids must be there, and
  `ambient_refusal()` must be None.
- `app/main.py` gains a lifespan: it calls `selection_refusal()` and raises before
  the service accepts a request. On shutdown, `deps.close_engine()` closes the engine
  only if one was built, when it has `aclose`, and clears `get_engine`'s cache, so a
  second lifespan builds a fresh client. `get_engine` checks the same refusal, so a
  path that skips the lifespan cannot build the engine.
- The `claude` and `mock` paths never read `RESOLVE_DATA`.

### 8. The evaluation path stays (FR-010)

`evals.engines.engines()` already lets `HOSTED_ENGINES` override `deps.ENGINES`, and
`runner.py` sends a hosted name to `hosted_run`. A test pins both: `gpt-6-luna` in the
runner is the governed engine, under its development-set guard, after registration.

### 9. The measurement (FR-014, SC-006)

`evals/service_run.py`, new: `python -m evals.service_run --json ...`. A separate
tool, so it does not pass through the runner's hosted diversion, and it keeps that
path's safeguards:

- the development set's guard, the declared accounts, the accepted first call for this
  fingerprint, and the whole-run `AccountRun` lock, so no other run shares the account;
- the investigator with `BoundedEngine`, scored by the runner's own `_evaluate`, paced
  by the wrapper's limits;
- a `LedgerRecorder` around the store and the account's `PrepaidLedger`. `started`
  reserves from the estimated tokens, and refuses beyond the envelope, so nothing is
  sent. `finished` settles through the check `Governed` uses, extracted so both share
  it. Missing or inconsistent usage leaves the reservation spent and stops the run;
  usage above the reservation settles at its reported cost, then stops the run;
- a result written on every exit, an interrupted run included, marked incomplete.

The request is not touched. The result names itself a service run, with the commit,
the clean-tree flag and the fingerprint, in `evals/results/`, listed in the results
README. It is not a slot, and it takes no part in `evals/compare.py`.

### 10. Documents (FR-011, FR-013)

- ADR-009: the Model provider row names this path, on synthetic data, on the terms
  accepted; the Telemetry row states the tested scope.
- `docs/contract/decision-engine.md`, `evaluation.md`, `docs/operations.md` (selection,
  refusal, rollback, bounds, the local trace backend's access, storage, retention and
  no onward export), the README and `.env.example`, in the same change.
- `docs/adr/013-deployment-model.md`: the reading of "deployed" and the conditions of
  the reference run (FR-012), and the hosting record, as a section the maintainer
  fills when OpenAI answers.

## Paid steps

Nothing paid happens without his authorisation at launch. Three, in order, about
$0.005 in all:

1. One dispute over HTTP on the local service (SC-001). It bypasses the envelope, by
   the spec's fourth choice.
2. The same call's trace, read in a local backend (FR-007b): the call above, not a
   second one.
3. The measurement (FR-014), through the envelope.

## Risks

- **A 429 can mean no credit.** `_translated` sorts 429 as `rate_limited`, so an
  account out of credit is retried within the bounds, then escalates. It is bounded
  and controlled, and it costs nothing more; the attempts show the category.
- **`_unreadable`** returns an answer with no usage: `unusable`, cost unknown, not
  retried, though the vendor may have billed it.
- **A timed-out attempt may have been processed and billed.** It stays unknown and is
  not settled.
- **The audit file now takes writes outside a run.** SQLite's one connection is used
  from one event loop; a test covers concurrent attempts.
- **The run page's language** stays French, as it is today.
