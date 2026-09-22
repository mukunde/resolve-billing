# Stored evaluation runs

The two files here are the evidence behind the table in the root README. They
are committed rather than ignored because a published accuracy figure whose
run nobody can inspect is an assertion, not a result.

| file | prompt | ran at | fully correct |
|---|---|---|---|
| `claude-decision_v2.json` | `decision_v2` | 2026-09-21 23:04 UTC | 10/12 *(rescored)* |
| `claude-decision_v3.json` | `decision_v3` | 2026-09-21 23:08 UTC | 12/12 |

The mock's numbers are not stored: it is deterministic and free, so
`python -m evals.runner --engine mock` reproduces them in under a second.

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
cases whose verdict changed. That is possible only because the runner stores
`got` — what the system actually did — alongside the booleans it derived from
it at the time.

## What these files do not carry

Both predate two fields the runner now writes: `engine_version` and
`cases_sha256`. Their prompt version is recorded in their **filename** and in
this table, which is a label rather than a record, and the honest way to say
so is to say so. Runs written from now on carry both in the file itself, so a
later reader does not have to take a filename's word for it.

The gap is the same one the project argues against elsewhere: a score whose
exam is unnamed is comparable to nothing. It was found by hitting it.

## What they contain

One row per case: the expectations it met, the confidence and its kind, the
cost in millionths of a dollar, the latency, and `got` — the classification,
action, approval flag, findings and engine the system actually produced. No
customer data beyond the synthetic fixtures in `fixtures/`, and no credentials.
