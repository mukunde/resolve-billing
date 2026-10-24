# Decision engine

What any engine behind the `DecisionEngine` port must do, and what the rest of
the system may rely on. Written on 2026-10-24 from the existing records, as
they stand, before the first change that touches the capability
(specs/004-second-engines) is folded in. Reasons are in ADR-002 (the engine is
a deployment choice), ADR-003 (deterministic before probabilistic), ADR-006
(no agent loop) and ADR-009 (what a model provider receives).

Two engines exist: the **mock**, deterministic and free, the default and what
CI and the demo run on; and **Claude**, through the Messages API, selected
with `DECISION_ENGINE=claude` and an Anthropic key.

## What the system relies on

- **One port, one method.** `decide(dispute, context, findings)` returns a
  `Decision`; the engine fetches nothing, writes nothing, executes nothing.
  `name` and `version` (the prompt version for Claude) are written to every
  audit row.
- **A closed classification, with its escape hatch inside.** `Classification`
  is an enumerated set ending in `unknown`, which the action policy treats as
  non-actionable; `recommended_action` is enumerated too. Routing is a
  deterministic function of these values (ADR-006).
- **No amount from the engine.** It names the kind of action; what a credit
  note is worth is computed from the invoice, in code.
- **Confidence carries its nature.** `confidence_kind` is `self_reported`,
  `distribution_derived` or `calibrated`; no engine sets `calibrated`, and a
  self-reported confidence never opens an autonomy threshold.
- **Explanation optional.** When an engine gives none, the application
  assembles one from the rule findings.
- **Telemetry on the decision.** Model id, prompt version, tokens, latency,
  and a cost in millionths of a dollar: `None` when no rate is known, `0` only
  when the call genuinely had no per-call price, with the date of the price
  list it came from.
- **Failures by kind.** A transport or service failure is
  `EngineUnavailable` -- the investigation escalates and records the outage;
  a request the provider refuses is `EngineRejected` -- the investigation
  fails, no escalation; their text is a category, never the provider's words.
  A malformed answer is the engine's own: it returns an escalation to a
  person.

## The Claude engine

- **The model never sees a real identifier.** The context and findings are
  pseudonymised (`anonymise`); the message is masked for the formats the
  masking recognises (`mask_message`, D10); references it cites are mapped
  back, and those not issued are dropped.
- **The output shape is constrained server-side and validated again on
  arrival.** An answer that does not fit, is cut off or is empty goes to a
  person.
- **The prompt is a versioned file** (`decision_v3` today); a changed prompt
  is a new version.
- **Traces carry counts and ids**, never the prompt, the context or prose.
- **One request per dispute**, no loop, no tool.

## Selection

`app/api/deps.py` names the engines the service may run (`ENGINES`: mock,
claude); `DECISION_ENGINE` chooses one, and an unknown name is refused. The
evaluation runner selects from the same table.

## Not guaranteed today

- That a self-reported confidence means anything: no engine is calibrated.
- Masking is by pattern: other people's names, free text and unusual formats
  pass (ADR-009).
- Only Claude has been measured among real models, last on the development
  set of its time.

## Covered by

`tests/unit/test_claude_engine.py`, `tests/unit/test_claude_outages.py`,
`tests/unit/test_claude_refusal.py`, `tests/unit/test_engine_refusal.py`,
`tests/unit/test_masking.py`, `tests/unit/test_action_policy.py`.
