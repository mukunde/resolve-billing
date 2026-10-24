# Evaluation

How the system is measured, what the measurements may be read for, and what
CI holds it to. Written on 2026-10-24 from the existing records, as they
stand, before the first change that touches the capability
(specs/004-second-engines) is folded in. Reasons are in the constitution
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

## Covered by

`tests/unit/test_eval_baseline.py`, `tests/unit/test_eval_coverage.py`,
`tests/unit/test_eval_rescore.py`, `tests/unit/test_eval_runner_lifecycle.py`,
`tests/unit/test_eval_unsafe.py`, `tests/unit/test_eval_variance.py`; the CI
workflow's evaluation step.
