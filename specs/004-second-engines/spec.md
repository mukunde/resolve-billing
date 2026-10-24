# Feature Specification: Second decision engines on free tiers, evaluated on the development cases

**Feature Branch**: `004-second-engines`

**Created**: 2026-10-24

**Status**: Draft -- for the maintainer's approval

**Input**: The maintainer has no Anthropic API credit, so step 4 (D10
measured on the Claude engine, the reference run on the frozen holdout,
prompt v4) waits. He asked whether free models could be used; the framing,
worked out with Codex (r149-r152) and approved by the maintainer on
2026-10-24, is a separate slice: three engine configurations on free
tiers, measured on the development cases only.

**Behaviour changed**: capability *Decision engine* (no contract yet:
ADR-002, ADR-006 and ADR-009 are authoritative) -- three engine
configurations selectable by the **evaluation runner only**. The service,
its engine choice (`DECISION_ENGINE`), its policies and the Claude engine do
not change. *Evaluation*: a new published measurement, apart from Claude's.

**Sensitive change**: yes -- two model providers (Google; Mistral, also
serving a third-party model) receive masked synthetic data (constitution
VII; ADR-009's disclosure inventory). Final-diff approval by the maintainer;
ADR-009's model-provider row amended.

## What was established (2026-10-24)

- **Google, Gemini API.** `gemini-3.8-flash` is stable, on the free tier,
  and the model Google recommends for new projects (official model,
  deprecation and pricing pages, updated 2026-10-22). Free services may be
  used to improve Google's products, but for users in the EEA, Switzerland
  and the UK the paid-service data terms apply to free services too
  (official terms, modified 2026-04-28) -- the maintainer's situation, to be
  confirmed by him; no statement on residency or retention. A client offered
  to users in those regions may use only paid services.
- **Mistral.** The maintainer's console: the Free plan includes **$10 of API
  usage per month**, reset on the 1st; keys can be created "for the free
  level within the limits page"; an "API Pay-As-You-Go -- Activer" control
  whose requirements are not known yet; no payment method on file.
  Per-model limits: `mistral-large-4` 20,000 tokens/min, 0.83 req/s;
  `zai-glm-5-3` 20,000 tokens/min, 1 req/s. Free mode may use API inputs
  for training unless opted out (a Privacy toggle, separate from Vibe's).
- **GLM 5.3** is Z.ai's model, served by Mistral on Mistral's own
  infrastructure (Mistral Inference page): the API vendor is Mistral, the
  model's developer Z.ai. Mistral documents for it a reasoning effort among
  `low|high|max` (no `none`) and a `message.content` returned as a list of
  blocks.
- **The maintainer's decision on training**: the development cases are
  synthetic; their use for training is acceptable. Opting out stays
  recommended, not required.
- **Not taken**: local inference (the maintainer's laptop has no dedicated
  GPU); Hugging Face (no free inference credits); DeepSeek (no free tier);
  Qwen (a one-off new-user quota; later, if results justify it).

## Clarifications

### Session 2026-10-24

- Q: Does this close step 4? → A: No. Another engine measures another
  engine: D10 on Claude, the holdout reference run and prompt v4 stay open.
  These results are published apart, from Claude's and from each other.
- Q: Which configurations? → A: Three, closed: `gemini-3.8-flash` (Google,
  `GEMINI_API_KEY`), `mistral-large-4` and `zai-glm-5-3` (Mistral,
  `MISTRAL_API_KEY`). Exact ids, no `latest` alias, each bound to its
  endpoint and its key. No fallback between vendors, to another model, or to
  a paid offer.
- Q: Where can they run? → A: In the evaluation runner, on the development
  cases (`evals/cases.json`, identified by its hash), and nowhere else: not
  in the service, not on another case file, not with extra fixtures, never
  on the holdout.
- Q: One adapter? → A: A shared core -- pseudonymised context, masked
  message, local validation, references mapped back -- and a thin client per
  vendor for what differs: reasoning settings, accepted schema, parsing,
  finish reasons, errors, token counts. Gemini through its native API, as
  Google recommends for new integrations, unless the plan shows the
  compatible interface preserves every requirement.
- Q: Which prompt? → A: v3, as written, under each configuration's own
  version label. Not a contemporaneous comparison with Claude's figures.
- Q: What does a result mean? → A: A regression score on the development
  cases. If a vendor trains on these inputs, a future model may have seen
  them: no claim of uncontaminated measurement; the date, model and privacy
  setting are recorded. A later opt-out never erases an earlier exposure.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The maintainer measures a free engine on the development cases (Priority: P1)

The maintainer runs the evaluation with one of the three configurations and
gets a result file of the same shape as Claude's, naming the configuration,
with per-case outcomes, unsafe actions, abstentions, invalid answers,
unavailability, latency and calls made.

**Independent Test**: in CI, with each vendor's client replaced by a stub:
the runner produces a complete result for each configuration, and no
network call is made.

**Acceptance Scenarios**:

1. **Given** `GEMINI_API_KEY` set, **When** the runner runs
   `gemini-3.8-flash` on the development cases, **Then** each case is
   decided or handed to a person, and the result records model, prompt,
   masking version, parameters, the model version returned when available,
   and cost as "estimated zero under the free quota", dated.
2. **Given** `MISTRAL_API_KEY` set, **When** the runner runs
   `mistral-large-4` or `zai-glm-5-3`, **Then** the same, with the cost
   estimated from Mistral's official prices against the shared $10
   allowance.
3. **Given** a missing key, **When** the runner starts, **Then** it says
   which key is missing and calls nothing.

### User Story 2 - Nothing leaves that should not (Priority: P1)

**Acceptance Scenarios**:

1. **Given** any configuration, **When** a case is decided, **Then** the
   payload sent holds the pseudonymised context and the masked message only
   (pinned per vendor by a test), and references the engine cites that were
   not issued are dropped.
2. **Given** `DECISION_ENGINE=gemini`, `mistral` or `glm` (any of the
   three), **When** the service starts, **Then** it refuses: these engines
   are not on its path.
3. **Given** a case file other than the development set, or extra fixtures,
   **When** the runner is asked for one of the three, **Then** it refuses
   before reading the file or building a client.

### User Story 3 - Limits and failures never pass for the model's answers (Priority: P2)

**Acceptance Scenarios**:

1. **Given** a vendor answering 429 or exhausting its quota, **When** the
   run goes on, **Then** attempts are bounded, a durable exhaustion stops
   the run, and the result counts unavailability apart from invalid answers
   and abstentions.
2. **Given** a request whose reserved tokens -- input, output and reasoning
   -- exceed the per-minute quota on their own, **When** it would be sent,
   **Then** it is not sent, and the case is marked so.
3. **Given** a truncated, filtered or empty answer, or (GLM) no final text
   block, **When** it arrives, **Then** the case is an invalid answer,
   handed to a person, never a classification.

### Edge Cases

- GLM 5.3's reasoning blocks: never parsed as the answer, never stored.
- A vendor ignoring a requested parameter: signalled, never dropped
  silently.
- The $10 Mistral allowance shared by two models: one budget, never reset by
  switching model.
- The "Activer" control requiring a payment method: Mistral stops there;
  Gemini goes on alone.

## Requirements *(mandatory)*

- **FR-001**: An evaluation registry MUST hold the existing engines and the
  three configurations, and drive the runner's validation, construction and
  credential check; `app/api/deps.py`'s `ENGINES` MUST stay without them,
  and a test MUST show the service refusing each.
- **FR-002**: The runner MUST refuse the three on any case file but the
  development set at its recorded hash, and with extra fixtures configured,
  before reading cases or building a client.
- **FR-003**: Each configuration MUST be closed: exact model id, endpoint,
  key variable, vendor, model developer; no alias, no fallback; an
  unsupported parameter MUST be reported.
- **FR-004**: The shared core MUST reuse `anonymise` and `mask_message`,
  validate the answer locally against the closed classification, map
  references back and drop those not issued, and hand an invalid or absent
  answer to a person. A test per vendor MUST pin the exact payload sent.
- **FR-005**: Vendor errors MUST be categorised, without vendor text in logs
  or results; reasoning content MUST never be stored.
- **FR-006**: Each configuration MUST pin and record: model id, API version,
  schema, temperature, maximum output tokens, reasoning setting, safety
  filters, prompt version, masking version; and the model version returned
  when available.
- **FR-007**: Limits: per-vendor pacing from its published or console
  limits, a token reservation per request (input, output, reasoning),
  bounded attempts and duration, 429 handled, the SDKs' own retries bounded
  and counted, stop on durable exhaustion; total duration, limiter wait and
  network latency recorded apart.
- **FR-008**: Cost: Gemini recorded as "estimated zero under the free
  quota" with source and date, or unknown if the regime applied is not
  shown; Mistral estimated at its maximum (retries and reasoning included)
  from official prices, before any run, against the shared allowance, kept
  apart from the allowance and from what is billed. Claude's price table is
  untouched; never a billed zero.
- **FR-009**: Results MUST be stored in `evals/results/` with the
  configuration, the case set's hash, the date, the privacy setting declared
  by the maintainer, and the statement that they are regression scores on
  development cases; `evals/results/README.md` lists them apart from
  Claude's.
- **FR-010**: CI MUST make no real call; the network-free tests of the
  core, the clients, the registry, the refusals and the limits run there.
  The mock baseline stays matched.
- **FR-011**: ADR-009's model-provider row MUST name the two vendors, what
  they receive, and the terms as established, with GLM's provenance
  (developer Z.ai, served by Mistral) recorded apart from its vendor.

## Success Criteria *(mandatory)*

- **SC-001**: For each configuration whose access is confirmed, one run of
  the 34 development cases by the maintainer, stored and listed, with
  unsafe actions, abstentions, invalid answers and unavailability counted.
- **SC-002**: No case leaves the development set, no engine reaches the
  service, and no vendor receives more than the masked, pseudonymised
  payload -- each shown by a test.
- **SC-003**: CI green; the mock baseline matched.

## Assumptions

- Before the first Mistral call the maintainer confirms the API is usable
  on the free level (what "Activer" requires) and what the quota counts; a
  first minimal call precedes the 34 cases.
- Keys are created and kept by the maintainer, in his `.env`; no agent sees
  them.
- The vendors' terms, models and limits are those observed on 2026-10-24;
  they are read again before each measurement and any change is recorded.
