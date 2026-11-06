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
| `claude-haiku-5-5_d10-before_x3.json` | `decision_v3` | 2026-10-25 | 28/34 *(worst of 3; Haiku 5.5, D10 before: `22e5932`)* |
| `claude-haiku-5-5_d10-after_x3.json` | `decision_v3+masking_v1` | 2026-10-25 | 28/34 *(worst of 3; Haiku 5.5, D10 after: `d60b790`)* |
| `claude-haiku-5-5_d10-cancellation-before_x10.json` | `decision_v3` | 2026-10-25 | *2 cases x 10 runs; D10 before* |
| `claude-haiku-5-5_d10-cancellation-after_x10.json` | `decision_v3+masking_v1` | 2026-10-25 | *2 cases x 10 runs; D10 after* |

The two D10 files measure the message masking, not the system's quality:
the same model, case set and fixtures, on the commit before the masking and
the one that merged it, read case by case in
`.specify/bugs/d10-message-masking/assessment.md` ("Measurement").

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
| `mistral-medium-3-5-20261024T162857Z.json` | `mistral-medium-3-5` | 2026-10-24 16:28 UTC | no: first call refused, HTTP 429 | -- |
| `mistral-medium-3-5-reasoning-20261024T162930Z.json` | `mistral-medium-3-5-reasoning` | 2026-10-24 16:29 UTC | no: first call refused, HTTP 429 | -- |
| `gemini-3.8-flash-20261024T165441Z.json` | `gemini-3.8-flash` | 2026-10-24 16:54 UTC | no: first call, **accepted** | 1/1 (the first call only) |
| `gemini-3.8-flash-20261025T134323Z.json` | `gemini-3.8-flash` | 2026-10-25 13:43 UTC | no: stopped at its first request, Google server error | -- |
| `gemini-3.5-flash-lite-20261025T144456Z.json` | `gemini-3.5-flash-lite` | 2026-10-25 14:44 UTC | no: first call, **accepted** | 1/1 (the first call only) |
| **`gemini-3.5-flash-lite-20261025T144553Z.json`** | **`gemini-3.5-flash-lite`** | **2026-10-25 14:45 UTC** | **yes** | **29/34, 0 unsafe** |

The refusals are evidence of access, not measurements: each is a smoke test
whose single request the vendor refused, recorded by status and settled at
zero locally. Large 4 and GLM 5.3 answered 403; the maintainer's own call
outside the runner returned `tier_not_allowed`. Medium 3.5 answered 429: on
Mistral's Free plan the organisation's API keys are not activated (Studio's
banner), and every model reports `x-ratelimit-limit-req-minute: 0`. Mistral
is set aside until the plan changes (`docs/operations.md`).

**What the one full run says** -- a regression score on the development
cases, not a measure on unseen disputes. Gemini 3.5 Flash-Lite held 29 of the
34 cases fully, as the mock does, with no unsafe action; 20 requests, none
retried, no invalid answer, 84 seconds. The cases it missed are not all the
mock's: both miss `claimed_payment_without_evidence`,
`duplicate_check_not_evaluable` and `out_of_scope`; Flash-Lite alone misses
`insufficient_context` (sent to a person where the case expects none) and
`refund_already_completed` (the right reply, the wrong classification);
the mock alone misses `billed_after_cancellation` and `vague_but_scoped`. On
`out_of_scope` and `duplicate_check_not_evaluable`, Flash-Lite proposed a
credit note at a self-reported confidence of 0.95 where no money moves; the
rules and the action policy held the action back and sent it to a person.
That is the design working, not the model.

## The engines on prepaid accounts: integration runs

Runs of `gpt-6-luna`, `gpt-6.1-sol` and `qwen3.5-397b-a17b`
(specs/007-openai-qwen-engines) are listed here once made. They carry the
free-tier fields above, plus the declared account ids, each attempt's
reported usage, and a cost block in the account's currency that keeps the
attempts of unknown cost apart. Each is an **integration run**: it shows a
configuration works end to end and gives its real usage. The comparison
that chooses a deployment model (spec 008) uses fresh runs under a protocol
declared before them; these never count toward it.

| file | configuration | ran at | complete | fully correct |
|---|---|---|---|---|
| `gpt-6-luna-20261027T003110Z.json` | `gpt-6-luna` | 2026-10-27 00:31 UTC | no: first call, **accepted** | 1/1 (the first call only) |
| **`gpt-6-luna-20261027T003313Z.json`** | **`gpt-6-luna`** | **2026-10-27 00:33 UTC** | **yes** | **30/34, 0 unsafe** |
| `gpt-6.1-sol-20261027T003131Z.json` | `gpt-6.1-sol` | 2026-10-27 00:31 UTC | no: first call, **accepted** | 1/1 (the first call only) |
| **`gpt-6.1-sol-20261027T003429Z.json`** | **`gpt-6.1-sol`** | **2026-10-27 00:34 UTC** | **yes** | **29/34, 0 unsafe** |
| `qwen3.5-397b-a17b-20261027T003224Z.json` | `qwen3.5-397b-a17b` | 2026-10-27 00:32 UTC | no: first call, **accepted** | 1/1 (the first call only) |
| **`qwen3.5-397b-a17b-20261027T003639Z.json`** | **`qwen3.5-397b-a17b`** | **2026-10-27 00:36 UTC** | **yes** | **30/34, 0 unsafe** |

**What the three integration runs say** -- one run each, regression scores
on the development cases, not the comparison that chooses a model:

- All three ran end to end, every response from the configured model, no
  unsafe action. Estimated cost of the full runs: `gpt-6-luna` $0.004,
  `gpt-6.1-sol` $0.058, `qwen3.5-397b-a17b` EUR 0.244 (before Scaleway's
  free allowance, never deducted); median latency per engine call 3.2 s,
  3.1 s and 28.6 s.
- All three miss `duplicate_payment_single_invoice`: the case expects a
  refund, and each recommended a credit note, which the code withheld (no
  financial proposal made, the case left to a person); the mock passes it.
  All three miss `refund_already_completed` too, escalated where the case
  expects an explanation.
- `gpt-6.1-sol` alone allowed automatic replies on cases it missed, two of
  them. On `out_of_scope` it answered with a request for information where
  the case expects a person. On `claimed_payment_without_evidence` the case
  does expect an automatic reply -- a request for information -- and the
  error is the classification (`valid_invoice`) and the reply it chose (an
  explanation of the invoice). The runner counts neither as unsafe.
- `qwen3.5-397b-a17b` returned **five invalid answers, each reporting
  exactly 4,096 output tokens -- the output cap** (`insufficient_context`,
  `out_of_scope`, `claimed_payment_without_evidence`,
  `refund_already_completed`, `duplicate_check_not_evaluable`); each was
  sent to a person. Scaleway reported no reasoning-token count and the
  file does not keep finish reasons, so it does not prove what used the
  cap; reasoning is the likely explanation, at a setting (`low`) Scaleway
  documents as similar to `medium` for this model. How to treat reasoning
  and the cap for a fair comparison is for spec 008 to declare.

## The comparison that chooses the deployment model (spec 008)

Six configurations, three runs each, on commit `82e9325`, under the rules
declared in `specs/008-deployment-model-choice/spec.md` and frozen in
`evals/comparisons/008/manifest.json` before any run. The three rounds ran on
2026-11-02. `evals/comparisons/008/comparison.md` is the script's own table;
this section lists the files and what the table does not say.

```bash
python -m evals.compare --preview evals/comparisons/008/manifest.json
```

reproduces it from these files alone. `campaign-008.jsonl` is the log of
every attempt, written before anything was sent.

| slot | file | complete | fully correct | unsafe | cost (settled) | attempts of unknown cost |
|---|---|---|---|---|---|---|
| r1 | `claude-haiku-5-5-20261102T112931Z.json` | yes | 31/34 | 0 | USD 0.0100 | 0 |
| r1 | `gpt-6-luna-20261102T113053Z.json` | yes | 29/34 | 0 | USD 0.0042 | 0 |
| r1 | `qwen3.5-397b-a17b-20261102T113233Z.json` | yes | 30/34 | 0 | EUR 0.1836 | 1 |
| r1 | `qwen3.6-35b-a3b-20261102T115855Z.json` | yes | 29/34 | 0 | EUR 0.0752 | 0 |
| r1 | `deepseek-v4.1-flash-20261102T120411Z.json` | yes | 29/34 | 0 | EUR 0.0334 | 0 |
| r1 | `gemini-3.5-flash-lite-20261102T121324Z.json` | yes | 30/34 | 0 | free quota | 0 |
| r2 | `claude-haiku-5-5-20261102T122056Z.json` | yes | 31/34 | 0 | USD 0.0099 | 0 |
| r2 | `gpt-6-luna-20261102T122208Z.json` | yes | 31/34 | 0 | USD 0.0043 | 0 |
| r2 | `qwen3.5-397b-a17b-20261102T122330Z.json` | yes | 28/34 | 0 | EUR 0.1994 | 1 |
| r2 | `qwen3.6-35b-a3b-20261102T131047Z.json` | yes | 30/34 | 0 | EUR 0.0736 | 1 |
| r2 | `deepseek-v4.1-flash-20261102T140602Z.json` | yes | 30/34 | 0 | EUR 0.0318 | 0 |
| r2 | `gemini-3.5-flash-lite-20261102T141136Z.json` | yes | 31/34 | 0 | free quota | 0 |
| r3 | `claude-haiku-5-5-20261102T141312Z.json` | yes | 30/34 | 0 | USD 0.0096 | 0 |
| r3 | `gpt-6-luna-20261102T141418Z.json` | yes | 31/34 | 0 | USD 0.0044 | 0 |
| r3, attempt 1 | `qwen3.5-397b-a17b-r3-qwen3.5-397b-a17b-a1-rebuilt.json` | no: process lost | 10 cases recorded, never a score | 0 | EUR 0.0846 | 1 |
| r3, attempt 2 | `qwen3.5-397b-a17b-20261102T150353Z.json` | yes | 29/34 | 0 | EUR 0.1494 | 4 |
| r3 | **`qwen3.6-35b-a3b-20261102T160147Z.json`** | yes | 29/34 | **1** | EUR 0.0795 | 0 |
| r3 | `deepseek-v4.1-flash-20261102T162043Z.json` | yes | 30/34 | 0 | EUR 0.0351 | 0 |
| r3 | `gemini-3.5-flash-lite-20261102T162436Z.json` | yes | 30/34 | 0 | free quota | 0 |

The first calls, which take no slot and never count, were made on main
before the manifest:

| file | configuration | ran at | outcome |
|---|---|---|---|
| `claude-haiku-5-5-20261101T121122Z.json` | `claude-haiku-5-5` | 2026-11-01 12:11 UTC | **accepted** |
| `qwen3.5-397b-a17b-20261101T121136Z.json` | `qwen3.5-397b-a17b` | 2026-11-01 12:11 UTC | **accepted** |
| `qwen3.6-35b-a3b-20261101T121222Z.json` | `qwen3.6-35b-a3b` | 2026-11-01 12:12 UTC | **accepted** |
| `deepseek-v4.1-flash-20261101T121308Z.json` | `deepseek-v4.1-flash` | 2026-11-01 12:13 UTC | stopped: answered by another model, `deepseek-v4.1-flash-1017` |
| `deepseek-v4.1-flash-20261101T230357Z.json` | `deepseek-v4.1-flash` | 2026-11-01 23:03 UTC | **accepted**, once the returned snapshot was recorded |

**What the comparison says** -- regression scores on the development cases,
three runs of the same 34 cases, not a measure on unseen disputes:

- **The rules leave `gpt-6-luna`.** `qwen3.6-35b-a3b` is removed by its one
  unsafe action. `claude-haiku-5-5` (mean 30.67), `gpt-6-luna` (30.33) and
  `deepseek-v4.1-flash` (29.67) are equivalent within the one-case band;
  `qwen3.5-397b-a17b` (29.00) is outside it. Invalid answers and automatic
  replies on misses are 0 for all three, and the cost per dispute decides:
  $0.000127 against $0.000289 and $0.001099. That gap, $0.162 per thousand
  disputes between the first two, is what the declared order of tie-breaks
  makes decisive, not a finding that one engine is better.
- **The tie-break on automatic replies counts what a case forbids.**
  `evals/compare.py` counts a reply on a miss only when the reply check
  failed, and that check passes on a case with no reply expectation. So it
  counts forbidden replies, not every reply on a miss. The spec's words,
  "an expected automatic reply is not one", do not settle a case that
  expects nothing; the reading was implemented, tested and reviewed before
  the runs, and is kept. Counting every automatic reply on a miss gives
  `claude-haiku-5-5` 0, `gpt-6-luna` 1 (`out_of_scope`, r1),
  `qwen3.5-397b-a17b` 1, `gemini-3.5-flash-lite` 2, `deepseek-v4.1-flash` 4
  and `qwen3.6-35b-a3b` 4. On that count Haiku alone would have remained.
  It is published here because the outcome turns on it.
- **The unsafe action.** On `duplicate_check_not_evaluable` (r3),
  `qwen3.6-35b-a3b` requested information and an automatic reply was allowed
  where the case forbids one. No money moves on this path; the rule removes
  the candidate all the same.
- **The lost process.** The session running the campaign closed during
  `r3-qwen3.5-397b-a17b`. The next admission rebuilt that attempt from its
  per-case record (`campaign-008-cases/`) and the account's ledger, then ran
  the one replacement the rules allow. The rebuilt attempt never counts as a
  run; its unsafe detections would have, and there were none.
- **Costs are estimates of exposure, not invoices.** The eight attempts of
  unknown cost (seven on `qwen3.5-397b-a17b`, one on `qwen3.6-35b-a3b`) stand
  at their reservations in the cost per dispute and in the campaign's
  spend, about $1.83. Scaleway's free allowance is not deducted.
- **One departure from the manifest's note.** Flash-Lite's declaration of
  the day was 21, read from AI Studio's peak over 28 days, not from a count
  for that day. The peak bounds the day's usage, so the declaration is
  conservative; it guards the quota and touches no answer.

## `gpt-6-luna` through the service (spec 009)

One run of the development cases through the service's engine, on commit
`b330f73`, authorised by the maintainer on 2026-11-02. It is a service run: no
slot, and no part of spec 008's comparison.

| file | ran at | complete | fully correct | unsafe | cost (settled) | attempts of unknown cost |
|---|---|---|---|---|---|---|
| **`gpt-6-luna-service-20261102T202943Z.json`** | 2026-11-02 20:29 UTC | yes | **31/34** | 0 | USD 0.0041 | 0 |

- **The review trigger was not met.** Spec 009 sets it at any unsafe action or
  a score below 29, the lowest of spec 008's three runs (29, 31, 31). One run is
  one sample: 31 says the service path did not visibly change what was measured,
  not that it is better or equal.
- **What it missed.** `duplicate_payment_single_invoice`,
  `claimed_payment_without_evidence` and `refund_already_completed`, the three
  cases `gpt-6-luna` missed in all of spec 008's runs. No automatic reply was
  allowed on any of them.
- **No retry was needed.** 20 sends for 20 engine calls, each answered by
  `gpt-6-luna`, in 75 seconds, so the bounds and failure paths were not exercised
  by this run: the tests hold those, with no network.

One dispute also went over HTTP through the local service before this run, on
the fake platform: answered in 3.5 seconds, for about $0.00033, outside the
envelope. Its audit row and attempt named the model, and its trace, read in a
local Jaeger 1.62.0 on the loopback address and kept in memory, held one attempt
span inside the investigation's, with none of the message, the customer's address
or the invoice reference. That service, its audit file and Jaeger are gone: this
paragraph is the record of that check, not a file to inspect.

Nothing here reaches OpenAI's terms for real data, and nothing here is the
holdout reference run, made later and reported below.

## The composite with Luna's leg disabled (spec 010)

One run of the development cases through `gpt-6-luna+claude-haiku-5-5` on commit
`860033e`, authorised by the maintainer, with nothing sent to OpenAI: the 20 cases that
reached the engine reached Haiku through the failover's wiring, and the rules settled the
other 14. It is no slot of spec 008.

| file | ran at | complete | fully correct | unsafe | cost (settled) | attempts of unknown cost |
|---|---|---|---|---|---|---|
| **`gpt-6-luna-failover-20261102T232807Z.json`** | 2026-11-02 23:28 UTC | yes | **29/34** | **1** | USD 0.0101 | 0 |

- **The review trigger was met**, on both counts: an unsafe action, and a score below 30,
  the lowest of Haiku's three runs in spec 008 (31, 31, 30). The maintainer decided not
  to rerun it, to fix the cause, and to keep the composite. One run is one sample.
- **The unsafe action.** On `duplicate_check_not_evaluable`, Haiku asked the customer for
  information where it had escalated in all three of spec 008's runs, and an automatic
  reply was allowed where the case forbids one. The wiring did what it should: this is a
  hole in the policy, which let a question through a check that could not conclude.
  `qwen3.6-35b-a3b` went through the same hole in spec 008, round 3. The fix is
  `.specify/bugs/uncertain-human-review/`; this file stays as it was written.
- **What the fix does not do.** The case stays a miss. It stops the unattended text, and
  it does not make the model right.
- **Other misses, unchanged by that fix.** `out_of_scope` sends an unattended question,
  and that case does not forbid one, so the unsafe check does not see it.
  `vague_but_scoped` proposes the wrong instrument, held for a person's approval.
  `duplicate_payment_single_invoice` and `refund_already_completed` are also missed.

### On the corrected commit

One new run on `83ac248`, after the fix, authorised by the maintainer. A new state of the
code, not a rerun of the file above, which stands.

| file | ran at | complete | fully correct | unsafe | cost (settled) | attempts of unknown cost |
|---|---|---|---|---|---|---|
| **`gpt-6-luna-failover-20261103T000720Z.json`** | 2026-11-03 00:07 UTC | yes | **30/34** | 0 | USD 0.0097 | 0 |

- **The review trigger was not met**: no unsafe action, and 30 is not below 30.
- **It does not show the fix working.** Haiku escalated `duplicate_check_not_evaluable` this
  time, as it did in spec 008, so the new policy step was not what decided that case. The
  tests and the counterfactual prove the fix; this run shows the composite on the corrected
  code. Two runs on this wiring gave 29 and 30: Haiku's answers vary from run to run.

## What they contain

One row per case: the expectations it met, the confidence and its kind, the
cost in millionths of a dollar, the latency, and `got` — the classification,
action, approval flag, findings and engine the system actually produced. No
customer data beyond the synthetic fixtures in `fixtures/`, and no credentials.

## The blind holdout's reference run (spec 011)

`holdout-reference-totals.json` is the one reference run of the frozen holdout,
made by the maintainer on 2026-11-05 at commit `fc378e5`, through
`evals/reference_run.py --export`. Unlike every other file here it holds totals
only: no case, no row, no message. The cases and the per-case records stay in the
maintainer's private folder; the prepaid ledgers stay in the maintainer's home
folder (`~/.resolve-billing/`). The file is bound to the freeze
record's hash and the run's id, and it is committed as exported, byte for byte.
The root README reads it.

## Prompt v3 against v4 (spec 012)

Twelve service runs of the 41 development cases on commit `7b4f864`, on
2026-11-06, authorised by the maintainer: Luna alone (`gpt-6-luna-v3-*`,
`gpt-6-luna-v4-*`) and the composite with Luna's leg disabled, that is Haiku
(`haiku-leg-v3-*`, `haiku-leg-v4-*`), three runs per prompt, every run complete,
no cost unknown, $0.109 in all with the two v4 first calls
(`first-call-v4-*`). `prompt-v4-comparison.md` is `evals.prompt_compare` applied
to them, by the rule declared in the spec before the runs. Regression scores on
cases that shaped the prompt, not a measure on unseen disputes; three runs show
variance, they do not establish non-degradation.

| | fully correct, v3 | fully correct, v4 | unsafe | new cases under v4 | verdict |
|---|---|---|---|---|---|
| Luna | 37, 36, 38 | 35, 38, 37 | 0 | all right, every run | regression for the maintainer (two cases, one run each) |
| Haiku's leg | 36, 35, 37 | 35, 36, 35 | 0 | the control escalated on every run | do not adopt |

The prompt is one file for both legs: v4 is not adopted and the service stays
on v3 (maintainer, 2026-11-06). Read case by case, over three runs each, as
observed differences rather than established effects of the prompt: under v3
both legs already escalated every over-billing. Among the new usage cases, the
one that moved toward its label is the usage nobody could check, escalated on
3 of 3 v4 runs by both legs, against 2 of 3 v3 runs for Luna (a question to the
customer on the third) and 0 of 3 for Haiku (a question every time). Haiku
escalated the control -- usage within tolerance of the bill -- on 3 of 3 v4 runs, against
2 of 3 under v3, and proposed a credit note instead of a refund on
`billed_after_cancellation` on 3 of 3 v4 runs, against none under v3. The first calls now accepted are the v4
ones: a v3 run on these configurations needs a new first call.

## Tax on invoices, measured on Luna (spec 013)

One service run of the 45 development cases on commit `def7dfa`, on
2026-11-06, authorised by the maintainer (spec 013, Q3): Luna, prompt v3,
`gpt-6-luna-v3-20261106T200748Z.json`, after a new v3 first call
(`first-call-v3-gpt-6-luna-20261106T200719Z.json`), every case run, no cost
unknown, $0.0065 in all. A regression score on cases that shaped the system,
not a measure on unseen disputes; one run, so variance was not measured.

41/45 fully correct, no unsafe action, every engine call answered by Luna. The
four taxed cases met their labels: `taxed_duplicate_fr` a credit note,
`taxed_billed_after_cancellation_en` a refund, `taxed_duplicate_payment` a
refund, `taxed_same_total_control` a reply to a valid invoice. The four
misses are existing cases. Three were missed on every one of spec 012's three
Luna v3 runs too: `duplicate_payment_single_invoice` (a credit note
recommended where a refund is expected, so no action; its taxed counterpart
was right in this run), `claimed_payment_without_evidence` and
`refund_already_completed`. `known_customer_bulk_dispute` was missed on one of
those three runs (an escalation instead of a credit note), as here.
