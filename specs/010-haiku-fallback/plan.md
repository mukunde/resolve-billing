# Implementation Plan: A fallback from `gpt-6-luna` to Claude Haiku 5.5

**Branch**: `010-haiku-fallback` | **Date**: 2026-11-02 | **Spec**: [spec.md](spec.md), approved by the maintainer at `a45c9d5` with its threshold of 30

## Summary

Add one engine, `FailoverEngine`, around two `BoundedEngine`s: Luna's leg, then
Haiku's, under a shared deadline. It decides to fall back from a typed failure
of a request actually sent, and nothing else. Both requests stay the ones spec 008
measured. Standalone Luna, Claude, the mock and the evaluation path do not change.

Like specs 008 and 009, this plan is one file.

## Technical Context

Python 3.12 (CI), 3.13 locally. No new dependency: `anthropic` and `openai` are
already pinned. Storage stays SQLite, in `engine_attempts`. CI installs the extras
for the network-free checks and makes no paid call.

## Constitution Check

| Principle | How this plan meets it |
|---|---|
| I | Unchanged: rules first; a dispute they settle sends nothing. |
| II | Unchanged: the same policy and approval, whichever leg answers. |
| III | `FailoverEngine` names no vendor. Vendors stay in configurations, clients and `deps.py`. |
| V | ADR-006 holds: no loop over a model's output, no tool. The order is fixed by code, and every call is traceable. |
| VI | One correlation id across both legs; the answering model named; costs by provider. |
| VII | Each leg sends its evaluated request, pinned by tests. |
| VIII | Fault-injection tests, and one declared run through the composite with Luna disabled. |
| IX | The holdout is untouched; the guards hold; the attestations extend to both families. |
| Sensitive | Final-diff approval. Any paid step is authorised at launch. |

## Design

### 1. What `BoundedEngine` gains

- `decide` stays as it is. A new `leg(dispute, context, findings, invocation_id,
  ends_at, first_number)` does its work under a given correlation id, one absolute
  monotonic deadline, and a first attempt number. `decide` calls it with a fresh id,
  `clock() + bounds.deadline` and 1. The composite passes 2 to Haiku's leg, so attempts
  are numbered across the dispute and `for_invocation` keeps their order.
- **Typed failures.** `hosted.unavailable` keeps the response's HTTP status on the
  exception. The wrapper sets `failover_eligible` from what actually happened: true only
  for a sent attempt ending in a timeout, a transport failure, a 429, or a status from 500
  to 599. An unexpected 2xx or 3xx that `_translated` calls `server_error` is not
  eligible, and neither is any failure before the send. An `EngineUnavailable` with no
  attribute reads as not eligible, so the composite fails safe.
- **Carried accounting.** A failing leg attaches a `telemetry` (`EngineTelemetry`) to its
  exception: the requested model, `usage_known=False`, the known cost, the unknown
  attempts, the invocation id. The investigator's outage and identity paths pass it to
  their escalation decision, so the run row holds them and the page and workflow span read
  them. Standalone Luna gains the same, which is more exact than a NULL.
- `retry_after` is ignored by the composite: no wait before asking the other vendor.

Nothing changes for standalone Luna: its tests stay as they are (SC-004).

### 2. `FailoverEngine` (`app/decision/failover.py`, new)

```text
FailoverEngine(primary: BoundedEngine, secondary: BoundedEngine, clock, name, version)
```

`decide`:

1. `invocation_id`, and one absolute deadline, 90 seconds from the clock.
2. `primary.leg(...)` with `Bounds(attempts=1, per_attempt=30)`, set at construction.
3. On a decision: return it. Its telemetry is the leg's.
4. On `EngineUnavailable` with `failover_eligible`: `secondary.leg(...)` with the same
   deadline, its own `per_attempt=60`, one attempt. If no time remains, the outage stands.
5. On anything else: let it through, unchanged. `EngineRefused`,
   `EngineMisidentified`, cancellation and defects are never caught.
6. Haiku's outcome is final. Its telemetry adds the primary's known cost and unknown
   attempts: `cost_micros` is the sum, `unknown_cost_attempts` the sum, `model_id` the
   model that answered, and `invocation_id` the shared one. Its own failure carries the
   shared id and the sum of both legs' accounting.

The failover choice keeps no state. The limiters live in each leg, built once, so they
stay shared across disputes. `invoice_references` sets both legs; `aclose` closes both,
the second even if the first fails.

**Name.** `gpt-6-luna+claude-haiku-5-5`, as `deps.FAILOVER`. Version:
`gpt-6-luna+claude-haiku-5-5/failover_v1/<prompt version>`. `decision_engine` holds the
composite's name, and `model_id` the model that answered, so the answer is never Luna's by
accident. Costs by provider are read from `engine_attempts`. The run page and API read
the same rows, already showing each attempt's model; a test holds them, numbered 1 and 2.

### 3. The Anthropic client

`app/decision/anthropic_eval.py` becomes `app/decision/anthropic_hosted.py`, and
`AnthropicEvaluationEngine` becomes `AnthropicHostedEngine`, by `git mv`; its docstring
stops saying evaluation-only. Its parity tests with `ClaudeDecisionEngine` stay, and
fingerprints do not depend on the module's name. The old module is removed: no result
references it. Every reference moves with it: `vendors.client_for`, `hosted.py`,
`evals/runner.py`'s ambient check, the contract, and the tests.

Haiku's leg uses `client_for(configuration("claude-haiku-5-5"))`, so `CLAUDE_MODEL`
cannot reach it.

### 4. Wiring and the refusal at start (FR-001, FR-009)

- `deps.ENGINES` gains `FAILOVER`, built from both legs and the shared `SqliteAttemptStore`.
- `selection_refusal` generalises: a table from engine name to the configurations it
  needs. Luna's conditions stand; the composite adds the `anthropic` package, Anthropic's
  key, `RESOLVE_ANTHROPIC_ORG_ID` and `anthropic_eval`'s ambient check.
- `close_engine` is unchanged: it calls `aclose`.

### 5. The plain runner refuses the composite (FR-010)

`evals.engines.SERVICE_ONLY = frozenset({deps.FAILOVER})`. `runner.main` refuses such a
name before it reads a case or builds a client, and `evaluation_engines()` leaves it
out; the registry test's equality with `deps.ENGINES` takes that out too. Standalone Luna
is already diverted to `hosted_run`.

### 6. The measurement (FR-012, SC-006)

`evals/service_run.py` gains `--engine`, default `gpt-6-luna`, or the composite. For the
composite it requires `--primary-disabled`:

- the primary leg is a `DisabledLeg`, in `evals/`, no engine at all: its `leg` raises an
  eligible `EngineUnavailable("disabled")` whose telemetry holds a known zero. No client for
  OpenAI is built, no attempt is recorded, and nothing leaves;
- the secondary is a real `BoundedEngine` with the `LedgerRecorder` on Anthropic's envelope,
  so the failover wiring runs as the service runs it, with its attempts numbered from 2;
- the same safeguards on Anthropic's account: the guard, the accepted first call for
  `claude-haiku-5-5`, `AccountRun`, a result on every exit;
- `ENGINE`, `RUN_KIND` and the statement follow the selection; the result names the
  primary as disabled, and attributes each case to its answering model.

`evals/compare.py` is not touched: the result is no slot.

### 7. Traces and the page

No new attribute. Each leg's spans are its own attempts, with `gen_ai.provider.name` its
vendor; `billing.attempt.invocation_id` correlates them. The investigation's span gets the
composite's totals from `FailoverEngine`, through `tracing.set_on_current`, replacing the
leg's. The allowlist does not grow.

### 8. Documents (FR-011, FR-013)

- ADR-009: the Model provider row names the composite, synthetic only, and the double-send
  case.
- ADR-013: the reference configuration, the attestations extended to both families, and the
  maintainer's statement of 2026-11-02 that the holdout's generator is neither OpenAI's nor
  Anthropic's. That statement is recorded, and it is not yet the attestation.
- `docs/contract/decision-engine.md`, `evaluation.md`, `docs/operations.md`, the README and
  `.env.example`, with the deliberate difference from spec 009's bounds.

## Paid steps

Nothing paid without his authorisation at launch. One step, about $0.01: the
measurement, through the Anthropic envelope.

There is no HTTP check with Luna's leg forced to fail: the service has no switch for it,
and adding one for a check is not worth it. The SDK on an in-memory transport covers that
path end to end, and the measurement runs the real Haiku leg.

## Risks

- **A 429 that means no credit** moves to Haiku: the dispute is answered, and the attempts
  show the category. Nothing alerts anyone: monitoring stays out of scope.
- **A timed-out Luna request may have been processed**: the payload reaches two vendors,
  and two bills. Accepted on synthetic data, stated in ADR-009.
- **Both legs share one deadline**: a slow Luna leaves Haiku at most 60 seconds; a queue on
  Luna's leg that uses it up ends as an outage with nothing sent, by design.
- **`failover_v1` is a policy version**: a change of order or bounds changes it.
