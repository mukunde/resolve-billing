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

- **The service does not change.** Its engine stays as it is configured today
  (`CLAUDE_MODEL`, by default `claude-opus-5`). Nothing here sends a customer's
  data to OpenAI: `docs/adr/009-trust-boundaries.md` still limits OpenAI to
  evaluation runs on the synthetic development cases.
- **A deployment slice comes first.** In its own change, it establishes where
  this account's requests are processed and the retention terms that apply,
  brings the engine into the service behind the `DecisionEngine` port, and
  updates ADR-009, the contracts and `.env.example` together.
- **The choice can fall.** If the slice cannot establish acceptable terms, this
  ADR is reopened. Claude Haiku 5.5 does not take its place by default:
  choosing it would be a new decision, with its own reasons.

This amends one sentence of ADR-002, which disqualified any engine hosted
outside the EU from the production path. As spec 008 states it, Europe is
preferred and the United States accepted otherwise; the hosting and data terms
are a gate the maintainer judges for each engine in its deployment slice. A
client's own policy can still forbid a model, and the port is what keeps that
cheap.

## Consequences

These are regression scores on development cases, three runs of the same 34
cases, not a measure on unseen disputes. The holdout reference run is the
maintainer's and follows the deployment of the chosen configuration; this
record does not stand in for it.

`app/decision/hosted.py` remains the only place `gpt-6-luna` is configured, on
the evaluation path. The engine agnosticism of ADR-002 is what makes a
provisional choice affordable: if it falls, no application code has moved.

A count of 0 automatic replies on misses here means 0 forbidden ones, and the
raw counts stay published. A later comparison declares which of the two it
uses before its runs.
