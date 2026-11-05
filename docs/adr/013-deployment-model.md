# ADR-013 — The deployment model: `gpt-6-luna`, provisionally

**Status:** accepted, provisional · 2026-11-02 · amends ADR-002 · nothing
deployed until the deployment slice below lands

## Context

Spec 008 chose how to choose: six configurations, three runs each on the 34
development cases, under rules frozen in a manifest before any run. Its
criteria, in order, are eligibility, no unsafe action, the mean of fully
correct cases, a one-case tie band, then invalid answers, automatic replies
on misses, the cost per dispute and the median latency. The maintainer then
chooses among the candidates the rules leave (`specs/008-deployment-model-choice/spec.md`).

| configuration | role | eligibility | unsafe | runs | mean | cost per dispute | median latency |
|---|---|---|---|---|---|---|---|
| `claude-haiku-5-5` | candidate | eligible | 0 | 31, 31, 30 | 30.67 | $0.000289 | 2.6 s |
| `gpt-6-luna` | candidate | eligible provisionally | 0 | 29, 31, 31 | 30.33 | $0.000127 | 3.4 s |
| `deepseek-v4.1-flash` | candidate | eligible provisionally | 0 | 29, 30, 30 | 29.67 | $0.001099 | 7.3 s |
| `qwen3.6-35b-a3b` | candidate | eligible provisionally | **1** | 29, 30, 29 | 29.33 | $0.002899 | 18.8 s |
| `qwen3.5-397b-a17b` | candidate | eligible provisionally | 0 | 30, 28, 29 | 29.00 | $0.011613 | 57.2 s |
| `gemini-3.5-flash-lite` | comparison point | not a candidate | 0 | 30, 31, 30 | 30.33 | free tier | 1.3 s |

`evals/comparisons/008/comparison.md` is the script's own table, reproduced
from the committed files; `evals/results/README.md` lists the runs.

`qwen3.6-35b-a3b` is removed by its unsafe action. Haiku, `gpt-6-luna` and
DeepSeek are equivalent within the band. Invalid answers and automatic replies
on misses are 0 for all three, as the script counts them. The cost per dispute
then leaves **`gpt-6-luna`** alone.

Two limits of that result are part of this record.

- **It is close.** Haiku's mean is a third of a case higher, inside the band,
  and the gap that decides is $0.162 per thousand disputes. The rules make that
  gap decisive; it does not show that one engine is better.
- **It turns on one reading.** The script counts an automatic reply on a miss
  only when the case forbids it. A case with no reply expectation does not
  count. Counting every automatic reply on a miss gives Haiku 0, `gpt-6-luna`
  1 and DeepSeek 4, and Haiku alone would have remained. The reading was
  implemented, tested and reviewed before the runs. Changing it after seeing
  the results would be changing the rule.

## Decision

The maintainer retains **`gpt-6-luna`, provisionally**: the configuration the
frozen rules leave. The rationale he approves with this record: the rules were
declared before the runs so that the choice would not be argued from the
results, and they are kept, including the reading above, knowing what it
decides.

This choice is an intention, not a deployment:

- **The service's default does not change.** It stays as it is configured
  today (`DECISION_ENGINE`, by default the mock; `CLAUDE_MODEL`, by default
  `claude-opus-5`). Nothing here sends a customer's data to OpenAI:
  `docs/adr/009-trust-boundaries.md` keeps OpenAI to synthetic data.
- **A deployment slice comes first.** Spec 009 brings the engine into the
  service behind the `DecisionEngine` port, on synthetic data and the account's
  standard terms. Where this account's requests are processed and the retention
  terms that apply are established apart, below, and they gate real data.
- **The choice can fall.** If the slice cannot establish acceptable terms, this
  ADR is reopened. Claude Haiku 5.5 does not take its place by default:
  choosing it would be a new decision, with its own reasons. Its role as a
  fallback (spec 010) is a different thing: it answers when OpenAI cannot.

This amends one sentence of ADR-002, which disqualified any engine hosted
outside the EU from the production path. As spec 008 states it, Europe is
preferred and the United States accepted otherwise; the hosting and data terms
are a gate the maintainer judges for each engine in its deployment slice. A
client's own policy can still forbid a model, and the port is what keeps that
cheap.

## What "deployed" means, and what comes before the reference run

Spec 008's SC-004 runs the holdout on the chosen and deployed configuration.
Nothing here is deployed in the usual sense: the repository is a demonstrator.
**Deployed reads as selected and verified on the demonstrator's service path**
(spec 009), not as production infrastructure.

The reference run on `gpt-6-luna` waits for three things, none of them made by
spec 009, which leaves the evaluation path's guard as it is:

1. ADR-009 amended for it: OpenAI is authorised on synthetic development cases
   and on the demonstrator's synthetic data, not on the holdout;
2. the maintainer's attestation, exposing no case, that the cases are entirely
   synthetic, generated independently of this engine, adjudicated and frozen
   (constitution, principle IX), and written by a model of another family than
   this engine (the README's rule);
3. the reference configuration, recorded below, because the run measures the
   configuration actually adopted, once.

### The reference configuration

The maintainer decided on 2026-11-02 that the reference run measures
**`gpt-6-luna+claude-haiku-5-5`, with the fallback on** (spec 010). With no outage
during the run, it observes Luna's path and does not validate the fallback. If Haiku
answers some cases, the result is kept with each case attributed to the model that
answered, and it is never rerun to obtain a Luna-only score.

The attestation above therefore covers both engines: entirely synthetic; generated
independently of both, adjudicated and frozen; and written by a model of a family
other than OpenAI's and Anthropic's. A conversation's isolation shows neither of the
last two by itself. Relaxing either goes through the change the constitution requires
of its holdout policy.

On 2026-11-02 the maintainer stated that the holdout's generator is neither OpenAI's
nor Anthropic's.

**The attestation, 2026-11-03.** The maintainer attests, exposing no case, that the
holdout's cases are entirely synthetic; generated independently of `gpt-6-luna` and of
Claude Haiku 5.5; adjudicated by him and frozen (2026-10-22); and written by a model of a
family other than OpenAI's and Anthropic's. ADR-009 authorises both vendors on the
holdout's masked payload for the one reference run, on this attestation.

**How the run is read, declared before it** (spec 011). It is a descriptive reference, not
proof of deployment quality: full-set correctness, unsafe actions, cases sent to a person,
outages, fallbacks, invalid answers, the models that answered, and the spend. Any unsafe
action pauses adoption for the maintainer's review. The development set's floor does not
apply to an unseen set; any acceptance threshold is the one in the freeze record, or none.
It runs once, through `evals/reference_run.py`, by the maintainer alone; an interrupted run
is kept and never replaced. Its failures are not read to design prompt v4, which is
developed and measured on the development cases, and the holdout is not rerun to show
progress. Running it does not retire the holdout.

The measurement of the composite with Luna's leg disabled met its review trigger: 29/34
and one unsafe action, by a hole in the action policy that
`.specify/bugs/uncertain-human-review/` closes. The maintainer keeps the composite, and
the reference run waits for that fix and one measurement on the corrected commit.

**Made, 2026-11-05.** The three conditions above, the fix and the measurement on the
corrected commit (30/34) were met first. The maintainer then made the reference run once,
at commit `fc378e5`, and exported its totals only
([`evals/results/holdout-reference-totals.json`](../../evals/results/holdout-reference-totals.json)):
20 cases run, complete, 15 fully correct, no unsafe action, 14 sent to a person, every
engine answer from `gpt-6-luna`, no fallback, outage or invalid answer, no threshold in the
freeze record. Read as declared above; with no unsafe action, nothing pauses the adoption.
The paragraphs above stay as the record of what the run waited for.

Spec 008's outcome does not change. Its rules were frozen before the runs, and
`qwen3.6-35b-a3b`'s removal stands, although this fix would have stopped the reply that
removed it.

## The hosting record

Spec 009, FR-013. To fill when OpenAI answers. Asked in this order: European
processing, then explicit United States processing. Global routing does not
meet the preference above. The answer must cover, together: the endpoint, the
project, the model's support, the processing location and the retention.

| | |
|---|---|
| Asked | not yet |
| Answered | not yet |
| Data processing terms read | not yet |
| Decision on real data | none: real data stays gated |

## Consequences

These are regression scores on development cases, three runs of the same 34
cases, not a measure on unseen disputes. The holdout reference run is the
maintainer's and follows the deployment of the chosen configuration; this
record does not stand in for it.

`app/decision/hosted.py` remains the only place `gpt-6-luna` is configured.
Spec 009 selects that configuration in the service, on synthetic data. The
engine agnosticism of ADR-002 is what makes a
provisional choice affordable: if it falls, no application code has moved.

A count of 0 automatic replies on misses here means 0 forbidden ones, and the
raw counts stay published. A later comparison declares which of the two it
uses before its runs.
