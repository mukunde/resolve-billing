# Feature Specification: The choice of the deployment model

**Feature Branch**: `008-deployment-model-choice`

**Created**: 2026-10-27

**Status**: Draft, for the maintainer's approval

**Input**: Step 4 of the roadmap needs the model the service will run. Spec
007 integrated OpenAI and Qwen on Scaleway as evaluation engines and gave
each an integration run; it chose nothing. The framing of this choice was
worked out with Codex (r231-r235) and decided by the maintainer on
2026-10-27 (coordination decision 41):

- **eight candidates**, the list frozen before any run:
  - Claude Haiku 5.5, Sonnet 5.5 and Opus 5.5;
  - `gpt-6-luna` and `gpt-6.1-sol`;
  - `qwen3.5-397b-a17b` and `qwen3.6-35b-a3b`, on Scaleway;
  - `deepseek-v4.1-flash`, on Scaleway;
- Gemini 3.5 Flash-Lite and Gemini 3.8 Flash as **comparison points
  only**: free tier, not deployable without a separate paid-tier
  decision;
- **DeepSeek reopened, as served by Scaleway only.** Decision 40 excluded
  it for its first-party API (processing in the PRC). In Paris, DeepSeek
  develops the model and Scaleway processes the request, as for Qwen;
- three fresh runs per configuration, under rules declared here before
  any of them;
- the Scaleway envelope raised to €3.

**Behaviour changed**: capability *Evaluation* (contract
`docs/contract/evaluation.md`), and *Decision engine* for the configurations
it adds or changes (`docs/contract/decision-engine.md`). The service, its
engine choice and the Claude engine's behaviour do not change in this
specification; the change of the service's default model, if Claude wins,
is the decision's consequence, recorded below and made in its own commit.

**Sensitive change**: yes -- the evaluation gates' evidence for a
deployment decision (constitution VIII) and a model provider's
configurations (VII). Final-diff approval by the maintainer.

## What was established (2026-10-26/27)

- **Measured so far**, each run on the development set as it stood then.
  None of these counts toward this choice:

  | configuration | runs | fully correct |
  |---|---|---|
  | Claude Haiku 5.5 (`decision_v3+masking_v1`, D10 "after", 2026-10-25) | 3 | 28, 28, 30 |
  | Gemini 3.5 Flash-Lite (2026-10-25) | 1 | 29 |
  | `gpt-6-luna`, integration run (2026-10-27) | 1 | 30 |
  | `gpt-6.1-sol`, integration run (2026-10-27) | 1 | 29 |
  | `qwen3.5-397b-a17b`, integration run (2026-10-27) | 1 | 30 |

  Every run had 0 unsafe actions. The Qwen run had five invalid answers,
  each at exactly the 4,096-token output cap.
- **Not yet measured**: Claude Sonnet 5.5 and Opus 5.5 on the current set;
  Gemini 3.8 Flash's full run (first call accepted; 20 requests a day);
  `qwen3.6-35b-a3b`; `deepseek-v4.1-flash`.
- **Scaleway** (catalogue and pricing, read 2026-10-26/27):
  - `qwen3.6-35b-a3b`: serverless, bf16/fp8, reasoning `none` to `high`
    ("`low` and `high` values are similar to `medium`"), 32k output cap,
    €0.25 / €1.50 per million tokens;
  - `deepseek-v4.1-flash`: serverless, released 2026-09, "preview"
    (context capped at 256k during it), reasoning `none`, `low`, `high`,
    `max`, structured outputs listed, 32k output cap, quantisation not
    stated, €0.60 / €0.12 cached / €1.80 per million tokens;
  - `qwen3.5-397b-a17b`: its serverless output cap is listed as "16k".
- **Anthropic**: Opus 5.5 at $4 / $20 per million tokens is not in
  `PRICING_PER_MTOK` (its cost would read unknown). The engine runs at effort
  `low` with an 8,192-token output cap; `CLAUDE_MODEL` selects the model.
  Claude runs go through the plain runner (`--runs N`, no envelope, no
  first-call gate, another result shape); the hosted ones through
  `evals/hosted_run.py`.
- **Budgets left**: Anthropic about $4.9 (no envelope in code); OpenAI
  about $4.93 (envelope); Scaleway envelope €1.75 before its raise to €3,
  beyond a free million tokens partly used.

## Clarifications

### Session 2026-10-27

- Q: What counts? → A: Only the fresh runs this specification declares, on
  one commit, after every prerequisite below is merged. Spec 007's
  integration runs, the D10 runs and Flash-Lite's earlier run never count.
- Q: What is held fixed? → A: The prompt (`decision_v3`), the verdict
  schema, the masking (`masking_v1`), the development cases' bytes and the
  fixtures. Each run records the requested and returned model, the
  reasoning setting, the output cap, the attempts, the deadline and the
  configuration's fingerprint.
- Q: Which reasoning and caps? → A: Effort `low` for every candidate.
  Output caps: 8,192 for Claude (unchanged); 4,096 for OpenAI (unchanged);
  16,384 for the two Qwen and for DeepSeek, which corrects the cut-off
  answers seen on the 397B without changing its reasoning. If the 397B's
  documented "16k" is below 16,384, its cap is that maximum, stated. Equal
  labels do not mean equal reasoning compute; this is recorded, not
  corrected.
- Q: How many runs, in what order? → A: Three per configuration, in
  rounds: round 1 runs every configuration once, then round 2, then round 3
  -- never all three runs of one configuration in a row. Gemini 3.8 Flash
  takes one run per Pacific quota day. It is included only if its three
  runs are done within seven days of round 1's start, otherwise it is
  excluded, without extension.
- Q: What if a run stops? → A: A ranked candidate needs three complete
  runs. An incomplete run is kept, never deleted, and listed. It may be
  replaced once, by one more run in the same round, when it stopped for a
  reason outside the model's answers: an outage, a rate limit, a deadline,
  an interruption. A run stopped by the model's own behaviour (an answer
  from another model, usage above its reservation, an inconsistent usage)
  is not replaced: the candidate is listed as not completing.
- Q: How is the choice made? → A: By the criteria below, computed from the
  result files only by a comparison script, then by the maintainer, who
  chooses among the candidates the rules leave, with his reasons recorded.

## The criteria, in order (declared before any run)

1. **No unsafe action.** One runner-detected unsafe action in any of a
   candidate's runs removes it. Zero detections is not demonstrated
   safety: financial recommendations the policy withheld, and automatic
   replies on cases that expect a person, are listed per candidate and
   read.
2. **Correctness**: the mean of fully correct cases over the three runs.
   Reported beside it: each run's score, the worst run, the cases whose
   verdict changes between runs, and the cases every run misses.
3. **The tie rule.** Candidates within one case of the best mean (1/34 or
   less) are equivalent on correctness. This is a practical equivalence
   rule, not statistical confidence: three runs repeat the same 34 cases.
   Among equivalent candidates, in order:
   - invalid answers, plus automatic replies on missed cases, over the
     three runs: fewer is better;
   - the estimated cost per dispute, at list prices, reasoning, retries and
     cache treatment included, converted to one currency at a rate
     recorded with its date: lower is better;
   - the median latency of the engine calls: lower is better.
4. **Deployment eligibility** is a gate the maintainer judges, not a score:
   the hosting and data terms (Europe preferred, the United States
   otherwise), the cost of operation, the path into the service, and for
   DeepSeek its "preview" status. A non-Anthropic candidate is chosen only
   provisionally, until its deployment slice lands.
5. **The maintainer chooses** among the candidates the rules leave, his
   reasons recorded with the table.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The maintainer sees one table, computed from the files (Priority: P1)

After the 24 runs (eight candidates × three) and the comparison points',
a script reads the result files only -- both shapes, Claude's and the
hosted engines' -- and prints the table the criteria need, in the order
they apply.

**Acceptance Scenarios**:

1. **Given** the declared runs' result files, **When** the comparison script
   runs, **Then** it prints per candidate:
   - the unsafe count;
   - each run's fully correct score, the mean and the worst run;
   - the changing and always-missed cases;
   - the invalid answers and the automatic replies on missed cases;
   - the estimated cost per dispute in one currency;
   - the median latency;
   - whether its three runs are complete;

   and the candidates the rules leave.
2. **Given** a result file from another commit, another case set or another
   configuration than declared, **When** the script reads it, **Then** it
   refuses it, naming why.
3. **Given** an incomplete run, **When** the script reads it, **Then** it is
   listed, never counted as complete, and a replacement is accepted only as
   the rules above allow.

### User Story 2 - Every candidate can be run as declared (Priority: P1)

**Acceptance Scenarios**:

1. **Given** `claude-opus-5-5`, **When** its cost is estimated, **Then** it
   is priced at $4 / $20, with source and date, and a test pins it.
2. **Given** `qwen3.6-35b-a3b` and `deepseek-v4.1-flash`, **When** the
   runner is asked for them, **Then** each is a closed configuration on the
   Scaleway account, held to its envelope, to its model's identity on every
   answer, and to the development set, as spec 007's are. Each has its own
   accepted first call.
3. **Given** the two Qwen and DeepSeek at a 16,384 cap, **When** a request
   is reserved, **Then** the reservation fits the account's per-minute
   limits. A changed configuration needs a fresh accepted first call.

### User Story 3 - Paid runs stay within what the maintainer allowed (Priority: P2)

**Acceptance Scenarios**:

1. **Given** the batch of Claude runs, which have no envelope in code,
   **When** they are launched, **Then** they follow a declared batch budget,
   about $2 for nine runs: the maintainer authorises them explicitly, and
   the batch stops at the first run that would pass it.
2. **Given** a hosted run its envelope cannot admit, **When** it is refused,
   **Then** the refusal is reported. The envelope is never raised without
   the maintainer's decision.

### Edge Cases

- A candidate stops completing (preview model withdrawn, account refused):
  it is listed as not completing; the list is not reopened.
- Two candidates equivalent on every criterion: the maintainer chooses,
  reasons recorded.
- A comparison point outscores every candidate: recorded; it does not
  become a candidate without the maintainer's separate decision on a paid
  tier and its own deployment slice.
- Holdout results never reopen this choice or tune the prompt.

## Requirements *(mandatory)*

- **FR-001**: `PRICING_PER_MTOK` MUST price `claude-opus-5-5` ($4 / $20,
  source and date), and a test MUST pin it; Haiku 5.5 and Sonnet 5.5 stay
  as they are.
- **FR-002**: Two configurations MUST be added to the evaluation registry,
  closed as spec 007's are (FR-003 there), after a documentation check:
  `qwen3.6-35b-a3b` and `deepseek-v4.1-flash`, on the Scaleway account,
  `low`, cap 16,384. Each MUST record its developer apart from its host, its
  prices, its documented limits and, for DeepSeek, its preview status. The
  service refuses both.
- **FR-003**: `qwen3.5-397b-a17b`'s cap MUST become 16,384, or its
  documented serverless maximum if lower, stated. Its fingerprint changes,
  so its full runs need a fresh accepted first call.
- **FR-004**: A comparison script (`evals/compare.py`) MUST compute the
  criteria from result files only, read both result shapes, and refuse any
  file whose commit, case-set hash, prompt, masking or configuration
  fingerprint differs from the declared ones. Its output MUST be a table in
  the criteria's order, and a machine-readable copy.
- **FR-005**: Every result file the comparison reads MUST carry the commit
  it ran on. Claude's results MUST carry the model returned, the effort and
  the output cap.
- **FR-006**: The declared runs MUST be listed before any is made, in a
  run manifest committed with this specification's implementation:
  configurations, order of rounds, commands, budgets, the date of round 1
  and Flash's seven-day window.
- **FR-007**: The decision MUST be recorded -- an ADR amending ADR-002 for a
  non-Anthropic choice, or a decision section for a Claude one -- with the
  table, the maintainer's choice and his reasons. Then, in its own change:
  for Claude, the default model, `.env.example` and the docs updated
  together; for any other, a deployment slice opened before adoption.
- **FR-008**: CI MUST make no paid call; the comparison script, the pricing
  row and the two configurations are tested without network. The mock
  baseline stays matched.

## Success Criteria *(mandatory)*

- **SC-001**: Three complete runs for each candidate that completes,
  stored and listed, and the comparison points' runs where made.
- **SC-002**: One table, reproduced by the script from the committed files.
  The candidates it leaves and the maintainer's choice are recorded with
  his reasons.
- **SC-003**: Spending within the declared budgets: Anthropic about $2,
  OpenAI's and Scaleway's envelopes; no envelope raised without the
  maintainer's decision.
- **SC-004**: The holdout untouched until the maintainer's single reference
  run, on the chosen and deployed configuration.

## Assumptions

- The vendors' models, prices, limits and terms are those read on
  2026-10-26/27, read again before round 1; a change is recorded and, if it
  touches a candidate's configuration, it is decided before round 1, never
  after a score is seen.
- Keys and envelopes stay the maintainer's; runs are launched with his
  explicit authorisation.
- Scaleway's limits stay the documented ones the maintainer accepted as an
  unconfirmed assumption (spec 007, T060).

## Out of scope

- Prompt v4, after the choice.
- The holdout's reference run (the maintainer's), after the choice and its
  deployment.
- A paid Gemini tier, Mistral, DeepSeek's own API, a third Qwen,
  self-hosting.
