# Stored evaluation runs

The first two files are the evidence behind the table in the root README; the
third is a variance band, described below. They are committed rather than
ignored because a published accuracy figure whose run nobody can inspect is
an assertion, not a result.

| file | prompt | ran at | fully correct |
|---|---|---|---|
| `claude-decision_v2.json` | `decision_v2` | 2026-09-21 23:04 UTC | 10/12 *(rescored)* |
| `claude-decision_v3.json` | `decision_v3` | 2026-09-21 23:08 UTC | 12/12 |
| `claude-haiku-5-5_decision_v3_x5.json` | `decision_v3` | 2026-10-08 17:47 UTC | 14/19 *(worst of 5 runs; Haiku 5.5)* |

The mock's numbers are not stored: it is deterministic and free, so
`python -m evals.runner --engine mock` reproduces them in under a second.

## The variance band is Haiku 5.5, on another exam

`claude-haiku-5-5_decision_v3_x5.json` is five runs of the 19-case
development set with Claude Haiku 5.5, not the model behind the two rows
above. Its fully correct count, run by run, is 15, 15, 14, 15, 14, and no
run proposed an unsafe action (0/95). It measures how much one model's
verdicts move from run to run, not how good the system is.

It is not comparable to the 12/12 above either: those runs were scored on
the earlier 12-case set. This one carries `engine_version` and
`cases_sha256`, which name its model, prompt and case set in the file.

```bash
python -m evals.variance evals/results/claude-haiku-5-5_decision_v3_x5.json
```

prints the band case by case.

## Why the v2 figure says "rescored"

That run was scored against a case set that **contradicted the prompt**. On a
cancelled subscription the engine recommended a refund, which prompt v2
requires — no future invoice can absorb a credit note — while the case set
still expected a credit note. Two of its four "errors" were not errors.

Correcting `cases.json` changed the score, from 8/12 to 10/12. Which means the
numbers from before the correction and the numbers after it were never
comparable: publishing both in one table without saying so would have compared
two exams and called it progress.

So the README's v2 column is produced, not remembered:

```bash
python -m evals.rescore evals/results/claude-decision_v2.json
```

It re-applies today's expectations to that run's stored outcomes and names the
cases whose verdict changed. It recounts unsafe actions the same way, with the
runner's own definition as it stands today, and exits 1 if any remains; the
two v2 and v3 files come out at 0/12. That is possible only because the runner stores
`got` — what the system actually did — alongside the booleans it derived from
it at the time.

## What these files do not carry

The first two predate two fields the runner now writes: `engine_version` and
`cases_sha256`. Their prompt version is recorded in their **filename** and in
this table, which is a label rather than a record, and the honest way to say
so is to say so. Runs written from now on carry both in the file itself, so a
later reader does not have to take a filename's word for it.

The gap is the same one the project argues against elsewhere: a score whose
exam is unnamed is comparable to nothing. It was found by hitting it.

## The engines on free tiers, listed apart

Runs of `gemini-3.8-flash`, `gemini-3.5-flash-lite`, `mistral-medium-3-5`
and `mistral-medium-3-5-reasoning` (specs/004-second-engines) are not rows of
the table above. Each file is one run, named by its start time,
and carries beyond the usual fields: the configuration (vendor, developer,
model, every parameter, the limits, the prices and their source), the model
versions the vendor reported, `complete` and, if not, where and why it
stopped and the cases not attempted, a status per case, the attempts and
times, the cost (Gemini: estimated zero under the free quota; Mistral: the
estimate from reported usage, the envelope's state, and the console's usage
the maintainer reads after), the data terms as declared, and the statement
that its score is a regression score on development cases.

An incomplete run is kept, not replaced: its totals are over the cases it
scored, and it is never read as a score over 34.

| file | configuration | ran at | complete | fully correct |
|---|---|---|---|---|
| `mistral-large-4-20261024T132525Z.json` | `mistral-large-4` (since removed) | 2026-10-24 13:25 UTC | no: first call refused, HTTP 403 | -- |
| `zai-glm-5-3-20261024T135710Z.json` | `zai-glm-5-3` (since removed) | 2026-10-24 13:57 UTC | no: first call refused, HTTP 403 | -- |

The two refusals are evidence of access, not measurements: each is a smoke
test whose single request Mistral refused (the runner recorded the status
and settled it at zero locally; the maintainer's own call outside the runner
returned `tier_not_allowed`). They are why the Mistral configurations became
the two Medium 3.5 (`docs/operations.md`).

## What they contain

One row per case: the expectations it met, the confidence and its kind, the
cost in millionths of a dollar, the latency, and `got` — the classification,
action, approval flag, findings and engine the system actually produced. No
customer data beyond the synthetic fixtures in `fixtures/`, and no credentials.
