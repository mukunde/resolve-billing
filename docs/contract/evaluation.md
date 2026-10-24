# Evaluation

How the system is measured, what the measurements may be read for, and what
CI holds it to. Written on 2026-10-24 from the existing records;
specs/004-second-engines folded in the same day. Reasons are in the constitution
(VIII, IX), README "Evaluation", `evals/taxonomy.md`, `docs/operations.md`
("What CI actually enforces") and `evals/results/README.md`.

## What the system relies on

- **A development set.** `evals/cases.json`, 34 synthetic cases, each with a
  `why` saying what it catches. They were read while the rules and the prompt
  were written: a score on them says how well the system fits them.
  `evals/taxonomy.md` lists the decision boundaries covered and missed.
- **The runner.** `python -m evals.runner --engine <name>` investigates every
  case with the fake billing platform at a fixed evaluation date, and scores
  findings, classification, recommended action, the approval gate and
  unsafe actions per case. `--runs` repeats it for a sampling engine;
  `--json` writes the outcomes with the engine version and the case set's
  hash.
- **What CI enforces, on every push** (mock engine): no unsafe action -- money
  moved where the case says none moves, money proposed without a person or
  to an unverified sender, an answer to a suspected impersonator; at least 29
  fully correct; and no check of any case differing from the recorded
  baseline (`evals/baselines/mock.json`), an improvement included until the
  baseline is regenerated in the same commit. The coverage tables must be up
  to date (`python -m evals.coverage --check`).
- **Claude, on demand.** A sampling engine: its job fails on an unsafe action
  and carries no floor.
- **Three engines on free tiers, on the development set alone.**
  `gemini-3.8-flash`, `mistral-large-4` and `zai-glm-5-3` run from the
  maintainer's command only, never in CI, refused on any case file but
  `evals/cases.json` at its committed hash or with extra fixtures, before a
  case is read or a client built. Each run requires `--data-terms` (what the
  vendor's data terms rest on, `unknown` accepted) and `--json`; it runs
  once, with no baseline and no floor. Its score is a **regression score on
  development cases**, and its result says so.
- **Held to their limits.** A limiter per configuration, and one Mistral's
  two models share; each request's tokens reserved before it is sent, as an
  estimate; a request over a quota on its own is not sent and never scored
  as a decision; attempts bounded at three, honouring `Retry-After`, under a
  run deadline, counting the request's tokens included. Usage reported
  above the reservation, or not reported, stops the run.
- **One run per vendor account at a time.** The accounts' state lives
  outside every checkout, in `~/.resolve-billing/`: a run holds its
  account's lock (a second is refused, from any worktree), notes each
  request before sending it, and starts no sooner than a minute after the
  account's last request.
- **Mistral's monthly envelope.** One ledger for the month,
  `~/.resolve-billing/mistral-envelope.json`, shared by both models, every
  run and every checkout: the allowance declared once a month
  (`--mistral-allowance-usd`); each attempt's maximum cost reserved on disk,
  under a lock, before it is sent, and settled at its reported cost after; a
  reservation never settled stays spent; an attempt beyond what remains is
  not sent and stops the run; an unreadable or inconsistent ledger is
  refused, never reset.
- **Every case accounted for.** Each case carries a status -- `decided`,
  `abstained`, `invalid`, `unavailable`, `not_sent_oversized`, or
  `rules_only` when the rules settled it and the engine was never entered --
  its attempts, and its limiter and network times. A case that entered the
  engine and left without an answer is `unavailable`, never scored. A run stopped before its end (the
  envelope, the deadline, rate limiting outlasting the attempts, usage, a
  refusal, an interruption, a defect -- recorded by its type alone, then
  raised as itself) is written with `complete: false`, where it
  stopped, why, and the cases not attempted, and exits non-zero; its totals
  are over the cases scored, never the whole set. A result never overwrites
  an earlier one: its file is named by its start time. It records the hash
  of the case bytes the guard checked, the configuration with its API
  version, the masking version and the schema's hash. The mock's and
  Claude's runs and outputs are unchanged.
- **Stored runs.** A published figure's run is committed in `evals/results/`
  with its model, prompt and case set's hash; `evals/results/README.md` says
  what each can be read for.
- **The holdout.** A blind measurement uses cases kept apart, generated
  independently of the engine measured, adjudicated by the maintainer and
  frozen (constitution IX). It is stored outside this repository, never run
  in CI, never tuned against; a session that tunes the system does not read
  it. One is frozen (2026-10-22); its reference run is the maintainer's.

## Not guaranteed today

- That a development score predicts behaviour on unseen disputes.
- A blind figure: none is published until the frozen holdout's reference
  run.
- Calibration: the sets are too small to calibrate a confidence.
- That a free-tier figure is comparable to Claude's beyond the development
  set, or that its estimated cost is what the account was billed: the
  vendor's console is the account's figure.

## Covered by

`tests/unit/test_eval_baseline.py`, `tests/unit/test_eval_coverage.py`,
`tests/unit/test_eval_rescore.py`, `tests/unit/test_eval_runner_lifecycle.py`,
`tests/unit/test_eval_unsafe.py`, `tests/unit/test_eval_variance.py`,
`tests/unit/test_evaluation_registry.py`, `tests/unit/test_evaluation_limits.py`;
the CI workflow's evaluation step.
