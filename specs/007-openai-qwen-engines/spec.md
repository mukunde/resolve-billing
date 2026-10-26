# Feature Specification: OpenAI and Qwen on Scaleway, evaluated on the development cases

**Feature Branch**: `007-openai-qwen-engines`

**Created**: 2026-10-26

**Status**: Draft, for the maintainer's approval

**Input**: Step 4 of the roadmap needs a deployment model. The framing
reviewed with Codex (r205-r206) screened Claude's three current models on the
development set before the maintainer chooses. The maintainer widened it on
2026-10-26: before choosing, measure other vendors' models too. Reviewed with
Codex (r207-r210) and decided by the maintainer the same day:

- two specifications in order: this one integrates the new vendors as
  evaluation engines; spec 008 chooses the deployment model among every
  measured configuration;
- OpenAI, and Qwen served by **Scaleway** (France) in serverless mode;
  **not DeepSeek**; Mistral and Qwen through Alibaba stay set aside;
- one Qwen model to start; **OVHcloud** (France) only if Scaleway fails the
  documentation check below;
- deployment hosting, when a non-Anthropic model is chosen: Europe ideally,
  the United States otherwise -- a later decision, not this one;
- evaluation on synthetic cases under each vendor's declared terms accepted;
  keys created and kept by the maintainer alone, and every paid run
  authorised by him;
- envelopes: OpenAI $5 (credit bought), Anthropic $5 for specs 007 and 008;
  Scaleway's set by the maintainer with his approval of this specification.

**Behaviour changed**: capabilities *Decision engine* and *Evaluation*, both
with contracts (`docs/contract/decision-engine.md`,
`docs/contract/evaluation.md`), amended in this change. The change: new
engine configurations selectable by the **evaluation runner only**, held to
a prepaid spending envelope per vendor account. The service, its engine
choice (`DECISION_ENGINE`), its policies, the Claude engine and the four
existing evaluation configurations do not change.

**Sensitive change**: yes -- two more model providers (OpenAI; Scaleway,
serving a model Alibaba developed) receive masked synthetic data
(constitution VII; ADR-009's disclosure inventory). Final-diff approval by
the maintainer; ADR-009's model-provider row amended.

## What was established (2026-10-26, official pages)

- **OpenAI** (`developers.openai.com`, pricing, models and "your data"
  pages, read 2026-10-26).
  - `gpt-6-luna`: "our most efficient model for focused, high-volume
    tasks", recommended for cost-sensitive workloads; $0.10 input / $0.50
    output per million tokens; reasoning effort `none` to `max`, default
    `medium`; structured outputs supported; Responses and Chat Completions.
  - `gpt-6.1-sol`: "near-Astra performance for complex work at a lower
    cost"; $2 / $10; reasoning effort `low` to `max` (no `none`);
    structured outputs supported.
  - Each lists one snapshot, under the same name: no dated id to pin.
  - The lowest usage tier allows 5,000 requests/min; tokens/min differ by
    model. The maintainer's own limits are read in his console before the
    first call.
  - Data: API inputs are not used for training by default. Abuse-monitoring
    logs are kept up to 30 days. A Responses request with `store` unset
    keeps its state at least 30 days, so `store` must be false. EU data
    residency (`eu.api.openai.com`) requires an approved retention
    arrangement the maintainer does not have. Without it, no regional
    processing is committed: the location is recorded as "global, no
    regional commitment", not as a country.
  - Pricing has more categories than input and output: cached input, and
    cache writes ($0.125 for `gpt-6-luna`, $2.50 for `gpt-6.1-sol`).
- **Scaleway Generative APIs** (`scaleway.com`, pricing, supported models,
  structured outputs and data-privacy pages, read 2026-10-26).
  - Serverless, Paris region, OpenAI-compatible.
  - Qwen models served, as the catalogue lists them for serverless:
    `qwen3.6-35b-a3b` (bf16/fp8, reasoning `none` to `high`, €0.25 /
    €1.50), `qwen3.8-27b` (quantisation not given, €0.60 / €3.30),
    `qwen3.5-397b-a17b` (int4, reasoning `none` to `high`, 16k output,
    €0.60 / €3.60), `qwen3-235b-a22b-instruct-2507` (€0.75 / €2.25).
    Prices are per million tokens, before VAT. Whether the reasoning
    tokens count toward the output cap is not documented.
  - The first million tokens are free per organisation. Beyond that, usage
    is billed automatically.
  - Structured outputs with a JSON schema are offered for every model in
    the library; "accuracy may vary between models".
  - Data: a zero-data-retention policy by default. Full request content
    may be retained "for up to two weeks" when a request triggers an HTTP 500 or
    harms the service, and when traffic may be malicious (no duration
    given). Inputs are not used to train base models, and are not
    accessible to the models' creators. Prompt caching is automatic,
    isolated per project, ephemeral, and keeps no prompt text. Personal
    data "may be stored" in Paris, France.
- **Cost of one development run** (20 of the 34 cases reach an engine; an
  estimated 100,000-180,000 input tokens and a few thousand output tokens,
  more with reasoning): `gpt-6-luna` about $0.01-0.02; `gpt-6.1-sol` about
  $0.20-0.40; `qwen3.5-397b-a17b` about €0.08-0.13; `qwen3.6-35b-a3b`
  about €0.03-0.05. Actual usage comes from the first call.

## Clarifications

### Session 2026-10-26

- Q: Which configurations? → A: Three, closed, named by their model ids:
  - `gpt-6-luna` and `gpt-6.1-sol` (OpenAI, `OPENAI_API_KEY`): the
    efficient tier and the mid tier, as for Claude's Haiku and Sonnet;
  - `qwen3.5-397b-a17b` (Scaleway, `SCALEWAY_API_KEY`): the largest Qwen
    the host serves.

  Exact ids, each bound to its endpoint and its key. No fallback between
  vendors, models or regions. If the maintainer prefers `qwen3.6-35b-a3b`,
  the cheaper model with disclosed bf16/fp8 weights, it replaces the
  397B: one Qwen either way.
- Q: Which reasoning setting? → A: `low` for all three: the setting
  Claude's engine and both Gemini configurations use, and the lowest
  `gpt-6.1-sol` allows. The same label, not a proven equal compute budget;
  recorded per configuration.
- Q: Which prompt? → A: v3 with the D10 masking, as written, under each
  configuration's own version label (`<configuration>/decision_v3`). No
  change to the prompt in this slice.
- Q: One adapter? → A: The shared core of spec 004, which already
  guarantees what Claude's engine does, and a thin client per vendor. OpenAI
  and Scaleway may share an OpenAI-compatible transport. Each vendor still
  has its own request mapping, schema handling, usage, refusal and error
  mapping, and its own tests: compatibility does not mean identical
  behaviour. The CLI never supplies an endpoint.
- Q: What does a result mean? → A: The same as for spec 004: a regression
  score on development cases, under one configuration, on one date. These
  vendors do not train on these inputs by their terms, but no claim of
  uncontaminated measurement is made.
- Q: Does this choose a model? → A: No. Spec 008 does, with the Claude
  models and the Gemini results. A non-Anthropic model chosen there needs
  its own deployment slice: service engine, ADR, contracts, a paid tier's
  terms, and its hosting under the maintainer's preference.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The maintainer measures a new configuration on the development cases (Priority: P1)

The maintainer runs the evaluation with one of the three configurations and
gets a result file of the same shape as the existing evaluation engines',
naming the configuration, with per-case outcomes, unsafe actions,
abstentions, invalid answers, unavailability, latency, calls made, reported
usage and estimated cost.

**Independent Test**: in CI, with each vendor's client replaced by a stub:
the runner produces a complete result for each configuration, and no
network call is made.

**Acceptance Scenarios**:

1. **Given** `OPENAI_API_KEY` set and an OpenAI envelope declared, **When**
   the runner runs `gpt-6-luna` or `gpt-6.1-sol` on the development cases,
   **Then** each case is decided or handed to a person, and the result
   records the model, prompt, masking version, parameters, the model
   identity returned, reported usage and the cost estimated from the
   recorded prices, dated.
2. **Given** `SCALEWAY_API_KEY` set and a Scaleway envelope declared,
   **When** the runner runs the Qwen configuration, **Then** the same, its
   cost in euros, the free tier never deducted from the estimate.
3. **Given** a missing key or no envelope declared, **When** the runner
   starts, **Then** it says which is missing and calls nothing.

### User Story 2 - Nothing leaves that should not, and storage is declined by request (Priority: P1)

**Acceptance Scenarios**:

1. **Given** any of the three, **When** a case is decided, **Then** the
   payload sent holds the pseudonymised context and the masked message only
   (pinned per vendor by a test), and references the engine cites that were
   not issued are dropped.
2. **Given** an OpenAI request, **When** it is built, **Then** it asks the
   vendor not to store the response (`store` false), pinned by a test. The
   test proves the request; what the vendor keeps rests on its terms
   (abuse monitoring included), recorded in ADR-009.
3. **Given** `DECISION_ENGINE` set to any of the three, **When** the service
   builds its engine, **Then** it refuses it.
4. **Given** a case file other than the development set, or extra fixtures,
   **When** the runner is asked for one of the three, **Then** it refuses,
   and no client is built.

### User Story 3 - Spending stays within what the maintainer allowed (Priority: P1)

**Acceptance Scenarios**:

1. **Given** an envelope declared for a vendor account, **When** an attempt
   would be sent, **Then** its maximum estimated cost is first reserved on
   disk, and an attempt that would exceed what remains is not sent and stops
   the run, written incomplete.
2. **Given** two configurations of one account (`gpt-6-luna`,
   `gpt-6.1-sol`), **When** both run, on any day or checkout, **Then** they
   spend one envelope, and switching model never resets it.
3. **Given** an attempt interrupted before its answer, **When** the ledger
   is read again, **Then** its reservation stays spent.

### User Story 4 - Limits and failures never pass for the model's answers (Priority: P2)

As for spec 004: bounded attempts, 429 handled, the SDKs' retries off, a
truncated, filtered, empty or schema-invalid answer handed to a person and
counted `invalid`, reasoning content never stored, a run stopped before its
end written incomplete with a non-zero exit.

### Edge Cases

- OpenAI's reasoning tokens are billed as output and count toward the
  output cap: a verdict cut off by them is `invalid`, and the reservation
  includes the cap.
- Scaleway's free tier is organisation-wide and its remaining balance is
  not visible to the runner: the envelope estimates every token at list
  price; the console is the account's figure.
- A requested parameter the vendor may not honour (reasoning, `store`,
  the schema): each is used only where documented, pinned in the exact
  request by a test, and an explicit refusal by the vendor is a refusal.
  Whether it was honoured is often not observable in the answer; that is
  recorded as a limitation, never claimed.
- A model id answered under another name, or routed elsewhere (Scaleway
  documents routing for retired models): on any response, first call or
  full run, the run stops (FR-008). An unchanged name still does not prove
  unchanged weights.
- Scaleway fails the documentation check or its first call (structured
  output unusable, terms not as read): Qwen moves to OVHcloud by an
  amendment of this specification, not silently.

## Requirements *(mandatory)*

- **FR-001**: The evaluation registry MUST hold the three configurations
  next to the existing ones and drive the runner's validation, construction
  and credential check. `app/api/deps.py`'s `ENGINES` MUST stay without
  them, and a test MUST show the service refusing each.
- **FR-002**: The development-set guard of spec 004 (FR-002) MUST apply to
  the three unchanged: refused before a case is read or a client built.
- **FR-003**: Each configuration MUST be closed and recorded: exact model
  id, vendor, model developer (OpenAI; Alibaba for Qwen), endpoint and
  region, API, key variable, temperature (the vendor's default, recorded),
  output cap, reasoning setting, the strict verdict schema, `store` false
  for OpenAI, prices with source and date, limits with source and date,
  attempts, and quantisation only where the plan's documentation check
  finds it stated for the serverless configuration (the catalogue lists
  `int4` for `qwen3.5-397b-a17b`); otherwise recorded as unknown. No
  alias, no fallback.
- **FR-004**: The shared core MUST be reused as it stands: `anonymise`,
  `mask_message`, local validation against the closed classification,
  references mapped back and those not issued dropped, an unusable answer
  to a person. A test per vendor MUST pin the exact payload sent.
- **FR-005**: Vendor errors MUST be categorised as spec 004 does, without
  vendor text in logs or results; reasoning content and summaries MUST
  never be stored. Diagnostics MUST carry no secret.
- **FR-006**: A **prepaid envelope per vendor account** MUST hold every
  run's spending, generalising the Mistral ledger. Ledgers MUST stay apart
  per account: OpenAI in dollars, Scaleway in euros, Mistral's monthly one
  unchanged. Each one:
  - is declared once by the maintainer; it does not reset monthly. Raising
    it is a new declaration, recorded with the old figure kept;
  - is shared by every configuration, run and checkout of that account,
    under one lock;
  - reserves each attempt's maximum estimated cost (input estimate plus
    output cap, at list price) on disk before sending;
  - settles the reservation at the reported usage's cost after the answer.
    A reservation never settled stays spent;
  - does not send an attempt beyond what remains, and stops the run;
  - is refused when unreadable or inconsistent, never reset.

  The envelope is admission accounting, not a billing cap: the vendor's
  prepaid credit or console is the account's figure.
- **FR-006a**: Each vendor account MUST be named by a stable, non-secret
  identity the maintainer declares, in the way `GEMINI_QUOTA_PROJECT`
  names a Google project: the billing organisation (OpenAI's organisation
  id, Scaleway's organisation id), and for OpenAI the project the key
  belongs to. The envelope and the account lock are kept per billing
  organisation, the one that holds the prepaid balance; the pacing and the
  first-call acceptance per project where the vendor scopes limits to
  projects. Never per key: rotating or adding a key never replenishes an
  envelope. Tests: two keys of one organisation, and two projects of one
  organisation, spend one envelope; two organisations keep two.
- **FR-006b**: Reservations and costs MUST be estimates, under the
  maintainer's explicit acceptance given with this specification,
  extending his spec 004 waiver to these two vendors: no vendor documents
  a bound on the tokens of a request. Each reservation:
  - prices input at the highest input-side rate that may apply (the
    cache-write rate for OpenAI), and output at the output rate over the
    whole output cap, reasoning included;
  - for Qwen, whose reasoning may not count toward the cap, is checked
    against the reported usage after each answer, and an excess stops the
    run;
  - is settled from the reported usage by billing category. Missing usage
    keeps the full reservation spent; an excess records the estimated
    actual cost, then stops the run.
- **FR-007**: Limits as spec 004 requires (FR-007): per-account pacing from
  the limits the maintainer reads in his console, token reservations with
  the `utf8-bytes-v1` estimator, bounded attempts and run duration, the
  SDKs' retries off, one run per account at a time. Reported usage above
  the reservation, or missing, stops the run.
- **FR-008**: The first call (`--first-call-only`) MUST precede each
  configuration's full run, and is accepted only as FR-016 of spec 004
  says. It MUST also show the returned model identity equal to the
  configured id. Each configuration's own accepted first call opens its
  full run, on its own fingerprint and declared account identity. During a
  full run, every response's returned identity MUST equal the configured
  id; a mismatch stops the run, written incomplete, its usage and cost
  kept, and the substitute's answer is never scored.
- **FR-009**: Each run MUST record `--data-terms` as declared by the
  maintainer (`contractual` expected for both vendors), the processing
  location as documented ("global, no regional commitment" for OpenAI;
  Paris, France, for Scaleway), and the statement that the score is a regression score on
  development cases. Results are stored in `evals/results/` and listed in
  `evals/results/README.md` apart from the others.
- **FR-010**: CI MUST make no real call; the network-free tests of the
  clients, the registry, the refusals, the envelopes and the limits run
  there. The mock baseline stays matched.
- **FR-011**: ADR-009's model-provider row MUST name the two vendors, what
  they receive, where they process it and their terms as established, with
  Qwen's developer (Alibaba) recorded apart from its host (Scaleway). The
  decision-engine and evaluation contracts MUST be amended in the same
  change. `.env.example` names the two new key variables, empty.
- **FR-012**: Any new SDK dependency MUST be pinned to an exact version
  and declared in the project's dependency files, its choice justified in
  the plan.

## Success Criteria *(mandatory)*

- **SC-001**: For each configuration whose access is confirmed, an accepted
  first call, then one full run of the 34 development cases by the
  maintainer, stored and listed, with unsafe actions, abstentions, invalid
  answers, unavailability and estimated cost counted. These are
  **integration runs**: they show the configuration works end to end and
  give its real usage. Spec 008's comparison uses fresh runs under a
  protocol declared before any of them (runs per candidate, criteria, tie
  handling); 007's runs never count toward it.
- **SC-002**: No case leaves the development set, no new engine reaches the
  service, no vendor receives more than the masked, pseudonymised payload,
  and every OpenAI request asks for no storage (`store` false) -- each
  shown by a test.
- **SC-003**: Tests show the envelopes: the reservation before sending, the
  refusal beyond the remaining amount, two OpenAI configurations sharing
  one envelope, a reservation kept after an interruption, an unreadable
  ledger refused, ledgers of different accounts kept apart, and Mistral's
  monthly ledger unchanged.
- **SC-004**: CI green; the mock baseline matched; the four existing
  evaluation configurations and the Claude engine unchanged.

## Assumptions

- With his approval of this specification, the maintainer accepts the
  estimated reservations of FR-006b and sets the Scaleway envelope.
- Before each configuration's first call, the maintainer checks in his
  console: the limits of his account, the credit available, and for
  Scaleway the free-tier balance and the payment requirements. Each first
  call precedes the 34 cases.
- Keys are created and kept by the maintainer, in his `.env`; no agent sees
  them.
- The vendors' models, prices, limits and terms are those read on
  2026-10-26; they are read again before each measurement, and any change is
  recorded.
- Holdout discipline (decision 23) holds: nothing here touches the holdout.
  The maintainer's single reference run comes after spec 008's choice.

## Out of scope

- Choosing the deployment model (spec 008).
- Running any new engine in the service, or a paid deployment tier.
- DeepSeek; Mistral's paid plan; Qwen through Alibaba Cloud; self-hosting
  an open-weight model.
- A prompt change (prompt v4 follows the choice).
