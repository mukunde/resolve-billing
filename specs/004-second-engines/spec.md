# Feature Specification: Second decision engines on free tiers, evaluated on the development cases

**Feature Branch**: `004-second-engines`

**Created**: 2026-10-24 · **Revised**: 2026-10-24, after review r153 ·
**Amended**: 2026-10-24, the daily quota and the first call (below)

**Status**: Approved 2026-10-24 (`94cf4ea`); merged (PR 20). The amendment
below: draft, for the maintainer's approval

**Input**: The maintainer has no Anthropic API credit, so step 4 (D10
measured on the Claude engine, the reference run on the frozen holdout,
prompt v4) waits. He asked whether free models could be used; the framing,
worked out with Codex (r149-r152) and approved by the maintainer on
2026-10-24, is a separate slice: three engine configurations on free
tiers, measured on the development cases only.

**Behaviour changed**: capabilities *Decision engine* and *Evaluation*,
neither with a contract yet. Following the constitution ("Product
Contract"), each contract is first written as the capability stands -- from
ADR-002, ADR-006, ADR-009 and README "Evaluation", `evals/taxonomy.md`,
`docs/operations.md` -- in a commit of its own, then the change is folded
in; `docs/contract/README.md` lists both. The change: three engine
configurations selectable by the **evaluation runner only**, and a new
published measurement apart from Claude's. The service, its engine choice
(`DECISION_ENGINE`), its policies and the Claude engine do not change.

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
- Q: Which configurations? → A: Three, closed, named in the evaluation
  registry by their model ids: `gemini-3.8-flash` (Google,
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
2. **Given** `DECISION_ENGINE` set to `gemini-3.8-flash`,
   `mistral-large-4` or `zai-glm-5-3`, **When** the service builds its
   engine, **Then** it refuses each: these engines are not on its path.
3. **Given** a case file other than the development set -- another path, a
   link leading elsewhere, the development file with altered content -- or
   extra fixtures, **When** the runner is asked for one of the three,
   **Then** it refuses, and no client is built.

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
4. **Given** GLM 5.3's answer as a list of blocks, reasoning blocks
   included, **When** it is parsed, **Then** only the final text block is
   read, and no reasoning is stored anywhere.
5. **Given** a run stopped before its end -- exhaustion, the Mistral
   envelope reached, an interruption -- **When** the result is written,
   **Then** it is marked incomplete, with the cases attempted and not
   attempted and the reason it stopped, the runner exits non-zero, and no
   score is presented as one over 34 decisions.

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
- **FR-002**: For the three, the runner MUST first refuse any case path
  other than the development file's own (resolved, links included) and any
  extra fixtures configured; then read the development file's bytes once,
  check them against the recorded hash, and parse those same bytes; any
  refusal before a client is built. Tests: an external path, a link leading
  elsewhere, an altered file, extra fixtures -- each refused, no client
  built.
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
  from official prices, before any run, kept apart from the allowance and
  from what is billed. Claude's price table is untouched; never a billed
  zero.
- **FR-008a**: One spending envelope MUST cover both Mistral models: set
  from the remaining allowance the maintainer declares, it is charged with
  each attempt's reserved maximum before the attempt is sent; an attempt
  that would exceed it is not sent and the run stops. Switching model never
  resets it. Per-model limits and any shared ceiling are kept apart, the
  latter confirmed by the maintainer.
- **FR-008b**: A run stopped before its end MUST be written as incomplete:
  cases attempted and not attempted, the reason it stopped, and a non-zero
  exit; it never meets SC-001 and never presents a score over 34 decisions.
  The existing engines' runs behave as today.
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

- Before the first call of each configuration, a documented check by the
  maintainer: the API usable on the free level (for Mistral, what "Activer"
  requires), what the quota counts and whether a shared ceiling exists, and
  the model's terms -- licence, stable or preview status, and the data
  regime that applies (whether a preview or third-party regime overrides
  the ordinary opt-out, for GLM 5.3 in particular). A first minimal call
  precedes the 34 cases.
- Keys are created and kept by the maintainer, in his `.env`; no agent sees
  them.
- The vendors' terms, models and limits are those observed on 2026-10-24;
  they are read again before each measurement and any change is recorded.

## Amendment (2026-10-24): the daily quota and the first call

**Why.** Reading his limits after creating his keys (T070-T071), the
maintainer found what the specification did not foresee: on his Gemini
project (`resolve-billing-eval`, free tier, no billing), `gemini-3.8-flash`
allows **5 requests/min, 250,000 tokens/min and 20 requests/day**. Twenty
of the 34 development cases reach the engine (the other fourteen are
settled by the rules), so a full run needs the whole day's quota. And no
command performs the first minimal call the assumptions require. The
framing was reviewed by Codex (r164) and approved by the maintainer on
2026-10-24, with the two decisions recorded below.

**Established.** Mistral's console matches the configurations
(`mistral-large-4` 20,000 tokens/min and 0.83 req/s; `zai-glm-5-3` 20,000
tokens/min and 1 req/s), shows no shared ceiling, and includes the free
level by default: "Activer" enables paid usage beyond the $10 included,
and stays off. Google documents its limits per project, and its daily
quotas reset at midnight Pacific time.

**The maintainer's decisions (2026-10-24).**

- **Gemini's reservation is estimated locally**, no longer by Google's
  `count_tokens`: the UTF-8 bytes of the request's exact material (system
  instruction, user payload, serialised schema), plus a margin, plus the
  output cap. A new estimate, not a bound -- Gemini's tokenizer is not
  documented -- replacing the waiver's counted text for Gemini; each
  answer's reported usage is still checked against it, and the run stops
  above it.
- **One attempt per case for Gemini.** With 20 requests for 20 cases, a
  retry spends another case's request. An outage stops the run, written
  incomplete.

**Requirements.**

- **FR-007a**: A configuration MUST be able to carry a daily request limit
  with its time zone (`gemini-3.8-flash`: 20, `America/Los_Angeles`), and
  the real per-minute limits read by the maintainer, with source and date.
- **FR-007b**: The daily count MUST be kept per vendor project in the
  accounts' state outside every checkout, persisted before each send and
  never decreased -- a failed, refused, cancelled or timed-out attempt
  counts. The quota day is re-evaluated at each send; a clock that goes
  back, or a state that cannot be read, is refused, never reset. A send
  that would exceed the limit is not sent: the run stops, written
  incomplete, its reason telling local exhaustion apart from the vendor's
  429.
- **FR-007c**: A full run of a configuration with a daily limit MUST start
  only on a quota day with no request made. A missing count does not prove
  none was made: on each quota day before the first request, the
  maintainer declares the requests already used, as he reads them in the
  vendor's console (`0` on a fresh day).
- **FR-007d**: Gemini's reservation MUST be computed locally (decision
  above), its estimator named and versioned in the result; no counting
  request is sent. Gemini makes one attempt per case.
- **FR-012**: `--first-call-only` MUST run the development set up to the
  first case that reaches the engine, make that one attempt, and stop --
  whether it succeeds or fails, never moving on to another case. Its
  result is written incomplete, labelled a smoke test, its reason "first
  call only" on success or the failure's own. It spends one request.
- **FR-009 (amended)**: The result MUST also record, for a configuration
  with a daily limit: the limit, its time zone, the count declared at the
  start of the day, this run's sends, the count at its end, and the
  accounting assumptions.

**Success criteria.**

- **SC-004**: Tests show: the 20th request of a quota day sent and the 21st
  refused; the count surviving a restart and shared across worktrees; an
  interruption right after a count kept counted; the day changing at
  Pacific midnight, daylight saving included; a clock gone back and an
  unreadable state refused; no counting request sent for Gemini; one
  attempt per Gemini case; `--first-call-only` sending at most one request;
  the instrumented, network-free engine reached exactly 20 times on the
  development set; Mistral's behaviour unchanged.

**Order of the maintainer's runs.** For each configuration, the first call
on one occasion, the full run on another; for Gemini, on two different
Pacific quota days.

