# Implementation Plan: The choice of the deployment model

**Branch**: `008-deployment-model-choice` | **Date**: 2026-10-27 | **Spec**: [spec.md](spec.md), approved by the maintainer at commit `f68b9ea`

## Summary

Make the five candidates and the comparison point runnable under one
discipline, then compare them from their result files alone:

- Claude Haiku 5.5 joins the governed evaluation path, with the envelope,
  the first call and the per-case statuses the other vendors already have.
  It does so through an evaluation-only configuration whose request is the
  service engine's own, pinned by a test;
- two Scaleway configurations are added: `qwen3.6-35b-a3b` and
  `deepseek-v4.1-flash`;
- the 397B's output cap is raised;
- every result records the commit it ran on and whether the tree was clean;
- a comparison script applies the spec's criteria to the files listed by a
  run manifest.

The service and its Claude engine do not change.

## Technical Context

Python 3.12 (CI), 3.13 locally. No new dependency: `anthropic` (the
`claude` extra) and `openai==3.29.0` (the `engines` extra) are already
pinned. CI installs both extras for the network-free checks, and makes no
paid call. State stays in `~/.resolve-billing/`.

## Constitution Check

| Principle | How this plan meets it |
|---|---|
| II | Unchanged: no engine reaches the service; policies, approval and execution untouched. |
| III | Vendors are named only in configurations, their clients, `vendors.py`, the registry and tests. |
| V | ADR-006 holds: one request per case, no loop, no tool. |
| VI | Each result names configuration, prompt, masking, model returned, commit and clean-tree flag, account ids, cost. |
| VII | The same pseudonymised payload and masked message. Claude's evaluation request is pinned equal to the service engine's. |
| VIII | The evidence for a deployment decision: rules declared before runs, a manifest, a script that refuses undeclared files. |
| IX | The holdout is untouched; the development-set guard applies to every configuration. |
| Sensitive | Final-diff approval; the decision record (FR-007) is the maintainer's. |

## Design

### 1. Research pinned before code (T010)

Read from official pages and the pinned SDKs, recorded in "Research
notes", reviewed by Codex before any client changes. Two kinds of point,
as in spec 007.

**Must be settled:**
- **Scaleway, for each of `qwen3.6-35b-a3b`, `deepseek-v4.1-flash` and
  `qwen3.5-397b-a17b`:**
  - the exact serverless output maximum: the catalogue's "16k" and "32k"
    in tokens; whether 16,384 is accepted;
  - the `reasoning_effort` values accepted, and where reasoning text
    arrives (to be dropped);
  - structured outputs on chat completions;
  - the usage fields (DeepSeek prices cached input apart: €0.12);
  - the returned `model` string;
  - prices and documented limits.
- **Anthropic, for `claude-haiku-5-5`:**
  - the Messages API request the service engine sends;
  - `stop_reason` values;
  - usage fields (`input_tokens`, `output_tokens`,
    `cache_read_input_tokens`, `cache_creation_input_tokens`) and whether
    cached tokens are inside `input_tokens`;
  - the returned `model` string;
  - error classes and statuses, and how to turn SDK retries off;
  - Haiku 5.5's cache-read and cache-write prices;
  - the account's rate limits, read by the maintainer in his console;
  - the non-secret organisation id the console shows.

**May stay unknown**, recorded as unknown: quantisation where not stated
(DeepSeek), and whether a vendor checks the declared account against the
key.

A first call that the vendor refuses for a parameter (an output cap above
the maximum, say) is not a counted run: the configuration is corrected
before round 1, never after a score is seen.

### 2. Claude on the governed path (FR-005, FR-005b)

The service engine `ClaudeDecisionEngine` stays as it is. A new
evaluation-only `AnthropicEvaluationEngine(HostedEngine)`, in
`app/decision/anthropic_eval.py`:

- **`_complete(system, user)`** sends one Messages request with the
  service engine's exact parameters: model, `max_tokens` 8,192, the same
  system prompt, `output_config={"effort": "low", "format": ...}`, and one
  user message built from the shared core's payload. SDK retries are off.
  It maps, as the service engine's parser decides:
  - `stop_reason` `max_tokens` → `length` and `refusal` → `filtered`
    (both unusable); every other stop reason → `stop`, its text then
    validated like any answer;
  - the text block → the answer;
  - the usage, with `cache_read_input_tokens` and
    `cache_creation_input_tokens` as details;
  - `response.model` → the returned model.

  Errors go through the Claude engine's own `_outage` and `_refusal`
  helpers, by category, without the vendor's text. An unreadable response
  is unusable, its usage unknown.
- **Parity tests**, with each engine on a stub client and the same
  dispute:
  - **request parity**: the two requests are equal, field for field;
  - **decision parity**: for a valid answer, a malformed one, a refusal, a
    truncated one and an answer under another stop reason, both engines
    return the same decision, the restored references and explanation
    included.

  So what is measured is what the service would send and decide.
- **What differs, documented:** outages and retries. The service engine
  keeps its own client settings. On the governed path, SDK retries are off
  and the runner counts attempts, three per case, as for the other
  vendors.
- **A configuration `claude-haiku-5-5`** (vendor `anthropic`, developer
  Anthropic):
  - key `ANTHROPIC_API_KEY`, account variable `ANTHROPIC_ORGANIZATION_ID`
    (the organisation alone). `evals/accounts.py` accepts a configuration
    with no project; existing identities and locks stay as they are, by
    test;
  - prices in USD with cache-read and cache-write rates from T010;
  - effort `low`, cap 8,192, `check_identity`;
  - limits as the maintainer reads them.
- **It runs through `evals/hosted_run.py`**, like the others: one run per
  invocation, a result file written even when the run stops, per-case
  statuses, a first call before the full run, the prepaid envelope
  (`--envelope 1` on the first run, in dollars). The older `--engine
  claude` path stays for the service's own checks and the live-engine CI
  job, unchanged.

As a consequence, every configuration in the comparison writes **one
result shape**. The comparison script does not need to read the older
Claude shape.

### 3. Two Scaleway configurations and the 397B's cap (FR-002, FR-003)

- `qwen3.6-35b-a3b` and `deepseek-v4.1-flash` are built with the existing
  `_prepaid` helper on the Scaleway account: reasoning `low`, cap 16,384,
  Scaleway's documented limits (the maintainer's accepted assumption), and
  prices with source and date (DeepSeek's cached rate included).
  - The developer is recorded apart from the host: Alibaba (Qwen) and
    DeepSeek.
  - DeepSeek's preview status and unknown quantisation are recorded in the
    configuration.
- `qwen3.5-397b-a17b`'s cap becomes 16,384, or the maximum T010 finds.
  Its fingerprint changes, so its first call is needed again.
- The two new configurations are checked in the existing Scaleway client
  for any field they return differently (T010). A difference found is
  mapped in the client, with a test.

### 4. The commit and the clean tree in every result (FR-005a)

`evals/hosted_run.py` records `commit` (`git rev-parse HEAD`) and
`clean_tree` in every result. Git is asked about the checkout the runner's
own code lives in, whatever the invocation directory. A tree is clean when
no tracked file is modified, nothing is staged, and no untracked file
exists outside `evals/results/`, where runs write. The runner refuses to
start a slot of this comparison on a dirty tree. Outside a git checkout,
it records `unknown` and the comparison refuses the file.

### 5. The run manifest (FR-006)

`evals/comparisons/008/manifest.json`, committed **after** this
implementation merges, in its own small pull request the maintainer
approves. It holds:

- the five candidates and the comparison point, with their configuration
  fingerprints;
- the case-set hash, the prompt version, the schema's hash and the
  masking version every result must carry;
- the maintainer's eligibility declaration for each;
- the slots in their global order: round r, configuration c, each with
  its command and its slot id, plus the one replacement each slot may
  take. Flash-Lite's commands carry its quota project and declare the
  Pacific day once, before its first request; a day already declared is
  not declared again;
- the envelopes;
- the date of round 1;
- the exchange rate's source and date.

The runs execute on the commit of the approved manifest revision. The
script finds it with git (the last commit touching the manifest) and
requires every result to carry it. A later edit of the manifest makes
every earlier result refused, by test.

This is a clarification of the spec's "committed with this
specification's implementation": a file cannot name the commit that
contains it. The plan's approval includes it.

### 6. The comparison script (FR-004)

`python -m evals.compare evals/comparisons/008/manifest.json`. It reads
only the result files.

**It refuses a file** whose commit, clean-tree flag, case-set hash,
prompt, schema, masking or configuration fingerprint differs from the
manifest's, or that fills an undeclared slot, or a slot already filled
except by the replacement the rules allow. Each refusal is tested.

**Slots are bound at launch, not inferred.** Each run of the comparison
is launched with `--slot <id>`. The runner refuses an id the manifest does
not declare, and an attempt the slot cannot take: a replacement whose
first attempt did not stop for a qualifying reason, a third attempt, a
slot of a later round started before every slot of the earlier one. It
records the slot and attempt in the result. It also appends, before
sending anything, one line to an append-only campaign log in the private
state (`~/.resolve-billing/campaign-008.jsonl`): the slot, the attempt,
the start time, the result's path.

**The script checks the log against the files.** Every logged attempt
has its file; every file is logged. Rounds come in order; each
replacement is linked to its first attempt; smoke tests (`--first-call-only`)
take no slot. A missing, extra or out-of-order attempt is refused, by
test. A qualifying interruption opens exactly one replacement; a second
one, or any other stop, marks the candidate "not completing".

**It computes, in order:**
- eligibility;
- unsafe actions across every attempt;
- per run fully correct, the mean, the worst run, the changing and
  always-missed cases;
- the tie band (best eligible mean − 1.0);
- the tie-breaks: invalid answers, automatic replies on missed cases
  (a reply the case expects does not count), cost per dispute (complete
  runs' estimated cost ÷ 102, unknown-cost attempts at their reservation,
  euros at the manifest's rate), median latency of engine calls.

**It reports apart** the whole campaign's spend, and the candidates the
rules leave.

**Output:** a Markdown table in the criteria's order and a JSON copy, both
written beside the manifest.

### 7. Documents

- The evaluation contract: the comparison, the manifest and the script.
- The decision-engine contract: the Claude evaluation configuration and
  the two Scaleway configurations.
- ADR-009's model-provider row: DeepSeek as developer, Scaleway as
  processor.
- `docs/operations.md`: the comparison's commands.
- `.env.example`: `ANTHROPIC_ORGANIZATION_ID`.

## Amendments to the approved spec (covered by this plan's approval)

- **FR-004/FR-005 — one result shape.** Claude is measured through the
  governed path, so every configuration of the comparison writes the
  hosted result shape, and the script reads that one shape. The older
  `--engine claude` path and its results stay unchanged, outside the
  comparison.
- **FR-006 — the manifest after the implementation.** It is committed in
  its own pull request after this implementation merges; the runs execute
  on its approved revision's commit.

## After the merge (the maintainer's, with me where he asks)

1. The manifest PR: his eligibility declaration and the date of round 1.
2. First calls for the configurations whose fingerprint is new or changed:
   `claude-haiku-5-5`, `qwen3.6-35b-a3b`, `deepseek-v4.1-flash` and
   `qwen3.5-397b-a17b`. `gpt-6-luna` and Flash-Lite keep theirs.
3. Rounds 1 to 3 on the manifest's commit, Flash-Lite's on one Pacific day.
4. `python -m evals.compare`, the results PR, then the decision record
   (FR-007).

## Risks

- **The parity test cannot cover the server.** Two equal requests may
  still be served differently over time. Its value is that the measured
  configuration is the deployed one at the request level.
- **A preview model (DeepSeek) may change or be withdrawn mid-campaign.**
  The candidate is then "not completing"; the list is not reopened.
- **Envelope admission can refuse a run** whose actual cost would have
  fitted. That candidate is "not completing" unless the maintainer raises
  the envelope before round 1. Raising it after a score is seen is not
  allowed.

## Research notes

*(T010: filled before any client changes, reviewed by Codex.)*
