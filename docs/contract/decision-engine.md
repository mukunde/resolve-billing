# Decision engine

What any engine behind the `DecisionEngine` port must do, and what the rest of
the system may rely on. Written on 2026-10-24 from the existing records;
specs/004-second-engines folded in the same day,
specs/007-openai-qwen-engines on 2026-10-26, and
specs/008-deployment-model-choice on 2026-10-27. Reasons are in ADR-002 (the engine is
a deployment choice), ADR-003 (deterministic before probabilistic), ADR-006
(no agent loop) and ADR-009 (what a model provider receives).

The service runs two engines: the **mock**, deterministic and free, the
default and what CI and the demo run on; and **Claude**, through the
Messages API, selected with `DECISION_ENGINE=claude` and an Anthropic key.
Four more configurations exist for the evaluation runner alone, on free
tiers, and three on prepaid accounts (below).

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

## The engines evaluated on free tiers

`gemini-3.8-flash` and `gemini-3.5-flash-lite` (Google's Gemini API, equal
but for the model, the limits, the attempts and what an outage does),
`mistral-medium-3-5` and
`mistral-medium-3-5-reasoning` (Mistral's API: one model, Mistral Medium 3.5,
at reasoning effort `none` and `high`, equal in everything else), each a
closed configuration in `app/decision/hosted.py`: the exact model, the endpoint, the
key's variable, every request parameter, the limits, the prices and their
source.

- **What the Claude engine guarantees, they guarantee.** The same prompt
  file and version, the same pseudonymised payload and masked message, the
  same verdict schema, validated again on arrival; an answer cut off,
  filtered, empty or out of the closed sets goes to a person. Their version
  is `<configuration>/decision_v3`.
- **One request, the SDK's retries off.** Attempts are the runner's to
  count. Reasoning is pinned per configuration (`low` for Gemini, the
  lowest it allows; `none` and `high` for the two Medium 3.5); thinking
  text is dropped, never kept.
- **Failures by kind, as above.** Rate limiting, server errors and transport
  failures are `EngineUnavailable` by category, with the wait the vendor
  asked for when it sent one; anything else the vendor refuses is
  `EngineRejected` carrying the status code alone.
- **Reported usage, as reported.** A count the vendor did not send is
  recorded as missing, never as zero. Cost is not set per call: the runner
  costs the run.
- **The evaluation runner's alone, on the development set alone.** They are
  not in the service's table, and the runner refuses them on any case file
  but `evals/cases.json` at its committed hash, or with extra fixtures
  configured (contract: evaluation).

## The engines evaluated on prepaid accounts

`gpt-6-luna` and `gpt-6.1-sol` (OpenAI's API), and, on Scaleway's
OpenAI-compatible API in Paris, `qwen3.5-397b-a17b` and `qwen3.6-35b-a3b`
(models Alibaba developed) and `deepseek-v4.1-flash` (DeepSeek's, in
preview), closed configurations in `app/decision/hosted.py` like the four
above, through one client, `app/decision/openai_compatible.py`, with what
differs by vendor kept there. The three on Scaleway cap their output at
16,384 tokens (spec 008), the two OpenAI ones at 4,096.

`claude-haiku-5-5` (spec 008) is Claude Haiku 5.5 measured as the service
would run it: an evaluation-only adapter, `app/decision/anthropic_eval.py`,
sends the request the service engine sends -- same model, cap, system
prompt, effort, schema and payload -- and reads the answer as the service
engine's parser does; tests hold the two engines' requests and decisions
equal. Only retries differ: the SDK's are off, the runner counts attempts.
Its account is the organisation the maintainer declares in
`RESOLVE_ANTHROPIC_ORG_ID`, a name the SDK does not read.

- **What the free-tier engines guarantee, they guarantee**, with reasoning
  at `low` for all three (the same label, not a proven equal budget;
  Scaleway documents that for this Qwen `low` is "similar to `medium`",
  recorded with the configuration) and the output cap sent as
  `max_completion_tokens`.
- **No request reaches a log.** The SDKs log each request's options at
  DEBUG; a filter on their request loggers drops those records whatever
  level the application sets (the Anthropic SDK's include the payload).
- **Every setting passed, none left to the environment.** The address, the
  key and the declared account are passed to the SDK; OpenAI's requests
  carry the declared organisation and project and ask not to be stored
  (`store` false); Scaleway's carry its project in the address and never
  OpenAI's account headers. Extra headers or request logging from the
  environment (`OPENAI_CUSTOM_HEADERS`, `OPENAI_LOG`) are refused before a
  client is built.
- **Read strictly.** A response that does not match the SDK's types, or is
  not JSON, is an unreadable answer: no verdict, its usage unknown. A
  refusal's text and reasoning text are never read or kept.
- **Usage with its details.** Cached, cache-write and reasoning tokens are
  read when reported, as parts of the totals, never added to them; a detail
  not reported stays missing.
- **Failures by kind, as above.**

## Selection

`app/api/deps.py` names the engines the service may run (`ENGINES`: mock,
claude); `DECISION_ENGINE` chooses one, and an unknown name is refused. The
evaluation runner selects from the same table plus the seven configurations
(`evals/engines.py`).

## Not guaranteed today

- That a self-reported confidence means anything: no engine is calibrated.
- Masking is by pattern: other people's names, free text and unusual formats
  pass (ADR-009).
- Real models measured on the development set: Claude, and Gemini 3.5
  Flash-Lite (one full run, 2026-10-25, `evals/results/README.md`);
  Flash's full run and the three prepaid configurations' runs are still to
  come.
- The seven configurations' reservations are estimates (the maintainer's
  waivers, 2026-10-24, extended to the prepaid vendors on 2026-10-26): the
  request's UTF-8 bytes plus a margin plus the output cap. The estimator
  is not established as an upper bound for the served request, its
  framing and its reasoning included. Each answer's reported usage is
  checked against it, and a run stops when it exceeds one.
- Their vendors' terms, limits and models are as read on 2026-10-24
  (2026-10-26 for OpenAI and Scaleway), and change without notice.
- For the three on prepaid accounts: whether the vendor honours a
  requested setting (reasoning, `store`, the schema) is often not
  observable in the answer; whether it checks the declared account against
  the key is not documented; the served quantisation is as Scaleway's
  catalogue lists it; whether Qwen's reasoning counts toward the output cap
  is not documented. OpenAI's limits are those the maintainer read in his
  console (2026-10-27); Scaleway's are its documented level for an
  account with a validated payment method, an assumption the maintainer
  accepted: his console listed no Generative APIs quota on 2026-10-27, for
  a reason unknown.

## Covered by

`tests/unit/test_claude_engine.py`, `tests/unit/test_claude_outages.py`,
`tests/unit/test_claude_refusal.py`, `tests/unit/test_engine_refusal.py`,
`tests/unit/test_masking.py`, `tests/unit/test_action_policy.py`,
`tests/unit/test_verdict.py`, `tests/unit/test_claude_pinned.py`,
`tests/unit/test_hosted_engines.py`, `tests/unit/test_vendor_clients.py`,
`tests/unit/test_openai_compatible_configurations.py`,
`tests/unit/test_openai_compatible_client.py`,
`tests/unit/test_anthropic_evaluation.py`.
