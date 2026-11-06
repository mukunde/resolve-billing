# Decision engine

What any engine behind the `DecisionEngine` port must do, and what the rest of
the system may rely on. Written on 2026-10-24 from the existing records;
specs/004-second-engines folded in the same day,
specs/007-openai-qwen-engines on 2026-10-26,
specs/008-deployment-model-choice on 2026-10-27, and
specs/009-luna-service and specs/010-haiku-fallback on 2026-11-02. Reasons are in ADR-002 (the engine is
a deployment choice), ADR-003 (deterministic before probabilistic), ADR-006
(no agent loop) and ADR-009 (what a model provider receives).

The service runs four engines: the **mock**, deterministic and free, the
default and what CI and the demo run on; **Claude**, through the Messages API,
selected with `DECISION_ENGINE=claude` and an Anthropic key; **`gpt-6-luna`**;
and **`gpt-6-luna+claude-haiku-5-5`**, the last two on synthetic data only
(below). Four configurations exist for
the evaluation runner alone, on free tiers, and more on prepaid accounts
(below).

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
- **The prompt is a versioned file**; a changed prompt is a new version
  (`app/decision/prompts.py`). The service sends `decision_v3`, its default,
  and no setting of the service chooses another. `decision_v4` is v3 plus one
  section on usage (spec 012): an over-billing is `unknown` with `escalate`,
  disputed or not; never "valid" against it or against usage nobody could
  check. A measurement chooses it explicitly, per run; the default moves only
  on the maintainer's decision, taken on that measurement. Measured on
  2026-11-06 (twelve runs, `evals/results/prompt-v4-comparison.md`): not
  adopted. Haiku's leg escalated the control case -- usage that matches the
  bill -- on every v4 run, which the declared rule forbids, and the prompt is
  one file for both legs. The service stays on `decision_v3`.
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
differs by vendor kept there. `deepseek-v4.1-flash` answers as the dated
snapshot `deepseek-v4.1-flash-1017`, recorded in its configuration: every
answer must carry exactly that name, so a new snapshot stops the run. The
three on Scaleway cap their output at
16,384 tokens (spec 008), the two OpenAI ones at 4,096.

`claude-haiku-5-5` (spec 008) is Claude Haiku 5.5 measured as the service
would run it: a hosted client, `app/decision/anthropic_hosted.py`,
sends the request the service engine sends -- same model, cap, system
prompt, effort, schema and payload -- and reads the answer as the service
engine's parser does; tests hold the two engines' requests and decisions
equal. Only retries differ: the SDK's are off, the runner counts attempts.
Its account is the organisation the maintainer declares in
`RESOLVE_ANTHROPIC_ORG_ID`, a name the SDK does not read.

- **What the free-tier engines guarantee, they guarantee**, with reasoning
  at `low` for every one (the same label, not a proven equal budget;
  Scaleway documents that for its two Qwen `low` is "similar to `medium`",
  recorded with their configurations) and the output cap sent as
  `max_completion_tokens` (`max_tokens` for Claude).
- **The SDKs' request records are dropped.** The OpenAI and Anthropic SDKs
  log each request's options at DEBUG on their `_base_client` loggers (the
  Anthropic SDK's include the payload); a filter on those two loggers drops
  their records below INFO, whatever level the application sets. Other
  loggers -- the HTTP library's, for instance -- are not filtered.
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
  not reported stays missing. OpenAI and Scaleway count cached tokens inside
  their input total; Anthropic counts cache reads and writes apart, so the
  Claude adapter adds them to the input total and keeps them as details.
- **Failures by kind, as above.**

## `gpt-6-luna` on the service's path

The configuration spec 008 measured, under `BoundedEngine`
(`app/decision/bounded.py`), which does for one dispute what the runner does for
a run. The prompt, schema, masking and request are the evaluated ones, and a test
fails on any drift.

- **Bounds.** At most 3 attempts, each at most 60 seconds or what remains, within
  90 seconds for the whole engine stage, waits and limiter included, because the
  API answers only once the investigation ends. The runner's deadline covers a
  run: the two differ on purpose. 4 requests in flight, and the configuration's
  requests per second and tokens per minute. The limits hold within one process.
- **No fallback.** Selected alone, it never switches to another engine.
- **Failures by outcome.** A closed set, recorded per attempt:

| Outcome | Retried | Ends in |
|---|---|---|
| `answered` | -- | the decision |
| `unusable` | no | an escalation, as for any engine |
| `wrong_model` | no | an escalation with its own event, not an outage |
| `timeout`, `unavailable`, `rate_limited` | yes | exhausted, the outage path: an escalation |
| `rejected` | no | HTTP 502 with a category; nothing is decided, no run is written |
| `not_sent` | no | the time ran out after recording: nothing left, the outage path |
| `cancelled`, `failed` | no | propagates; its record stays |

- **Attempts on record.** `engine_attempts`, in the audit file, committed one by
  one, so a refusal or a stopped process keeps them. A row with no outcome reads as
  unfinished, never as a success. No vendor text is kept. A run links to them by
  `engine_invocation_id`.
- **Unknown stays unknown.** Tokens the vendor did not report are NULL, not zero.
  With `unknown_cost_attempts` above zero, `cost_micros` is a lower bound, and the
  run page and the reconciler say so. A timed-out attempt may have been billed.
- **Refused at start.** Unless the billing platform is the fake one,
  `RESOLVE_DATA=synthetic` is set, and its key and declared account ids are there.
  This guards against a slip in configuration. It does not control content: the
  service cannot tell real text pasted into a request.

## `gpt-6-luna+claude-haiku-5-5`: the fallback

`FailoverEngine` (`app/decision/failover.py`) around two bounded legs, each sending
its evaluated request, Haiku pinned whatever `CLAUDE_MODEL` says.

- **Bounds.** One Luna attempt of at most 30 seconds, then one Haiku attempt of at
  most 60 seconds or what remains, within 90 seconds for the whole stage. Spec 009's
  retries do not apply here, on purpose: a passing 429 moves on rather than waits, and
  a `Retry-After` is not waited for.
- **What triggers it.** Only a request actually sent to OpenAI that ended in a
  timeout, a broken connection, a 429, or a status from 500 to 599. A 429 can mean no
  credit: the dispute is then answered by Haiku, the attempt shows the category, and
  nothing alerts anyone.
- **What never does.** Any other status, a wrong model, an unusable answer, a failure
  before the send, a recording failure, a budget refusal, a cancellation, a defect.
- **Haiku's leg** ends as a standalone engine's would, and there is no third attempt.
- **Who answered.** `decision_engine` is the composite's name; `model_id` is the model
  that answered. Attempts share one `engine_invocation_id`, numbered 1 and 2, each with
  its provider, so costs read by provider. A failing leg's telemetry reaches the run row.
- **No memory.** The choice is made per dispute. Each leg's limits stay shared.
- **Refused at start** on spec 009's conditions and on Anthropic's key, declared
  organisation and ambient settings.

## Selection

`app/api/deps.py` names the engines the service may run (`ENGINES`: mock, claude,
`gpt-6-luna`, the composite); `DECISION_ENGINE` chooses one, and an unknown name is refused. The
evaluation runner selects from the same table plus the ten configurations
evaluated outside the service (`evals/engines.py`); for `gpt-6-luna` that means
its governed engine, under its development-set guard, not the service's. The runner
refuses the composite: `evals/service_run.py` is its only evaluation path.

## Not guaranteed today

- That a self-reported confidence means anything: no engine is calibrated.
- That Claude's retries are on record: those inside its SDK leave no attempts.
- Masking is by pattern: other people's names, free text and unusual formats
  pass (ADR-009).
- Real models measured on the development set: Claude, and Gemini 3.5
  Flash-Lite (one full run, 2026-10-25, `evals/results/README.md`);
  Flash's full run is still to come; the prepaid configurations have
  integration runs (spec 007) and the comparison of spec 008 was run on
  2026-11-02. It retained `gpt-6-luna` provisionally (ADR-013); its
  deployment slice is to come, and the service's engine is unchanged.
- The ten configurations' reservations are estimates (the maintainer's
  waivers, 2026-10-24, extended to the prepaid vendors on 2026-10-26 and
  2026-10-27): the
  request's UTF-8 bytes plus a margin plus the output cap. The estimator
  is not established as an upper bound for the served request, its
  framing and its reasoning included. Each answer's reported usage is
  checked against it, and a run stops when it exceeds one.
- Their vendors' terms, limits and models are as read on 2026-10-24
  (2026-10-26 and 2026-10-27 for OpenAI, Scaleway and Anthropic), and
  change without notice.
- For the configurations on prepaid accounts: whether the vendor honours a
  requested setting (reasoning, `store`, the schema) is often not
  observable in the answer; whether it checks the declared account against
  the key is not documented; the served quantisation is as Scaleway's
  catalogue lists it; whether Qwen's reasoning counts toward the output cap
  is not documented. OpenAI's limits are those the maintainer read in his
  console (2026-10-27); Scaleway's are its documented level for an
  account with a validated payment method, an assumption the maintainer
  accepted: his console listed no Generative APIs quota on 2026-10-27, for
  a reason unknown. Claude Haiku's are those the maintainer read in his
  console (2026-10-27), the lower token figure (2.0M output a minute)
  applied to input and output together.

## Covered by

`tests/unit/test_claude_engine.py`, `tests/unit/test_claude_outages.py`,
`tests/unit/test_claude_refusal.py`, `tests/unit/test_engine_refusal.py`,
`tests/unit/test_masking.py`, `tests/unit/test_action_policy.py`,
`tests/unit/test_verdict.py`, `tests/unit/test_claude_pinned.py`,
`tests/unit/test_hosted_engines.py`, `tests/unit/test_vendor_clients.py`,
`tests/unit/test_openai_compatible_configurations.py`,
`tests/unit/test_openai_compatible_client.py`,
`tests/unit/test_anthropic_hosted.py`, `tests/unit/test_bounded_engine.py`,
`tests/unit/test_attempt_store.py`, `tests/unit/test_luna_service.py`,
`tests/unit/test_failover_engine.py`, `tests/unit/test_failover_service.py`,
`tests/integration/test_tracing.py`.
