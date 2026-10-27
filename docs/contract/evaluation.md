# Evaluation

How the system is measured, what the measurements may be read for, and what
CI holds it to. Written on 2026-10-24 from the existing records;
specs/004-second-engines folded in the same day, and
specs/007-openai-qwen-engines on 2026-10-26. Reasons are in the constitution
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
- **Four engines on free tiers, on the development set alone.**
  `gemini-3.8-flash`, `gemini-3.5-flash-lite`, `mistral-medium-3-5` and
  `mistral-medium-3-5-reasoning` run from the
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
  run deadline. Every reservation is the same local estimate
  (`utf8-bytes-v1`), named in the result.
  An answer the vendor's SDK cannot read keeps its reservation spent and
  stops the run; only a 4xx status is a refusal. Usage reported
  above the reservation, or not reported, stops the run.
- **Gemini's daily quota** (spec 004 amendments). Counted per Google
  project, named by `GEMINI_QUOTA_PROJECT`, and per model, reset at
  midnight Pacific: Flash 20 a day, Flash-Lite 500. Kept outside every
  checkout under one lock per model: the maintainer declares each day
  once, before its first request, as the console shows it; each request is
  counted before it is sent, never refunded. A full run starts only if the
  day's remaining requests cover the 20 cases that reach the engine times
  the configuration's attempts; passing the limit, reaching an undeclared
  day or a clock gone back stops the run before sending. Flash makes one
  attempt per case and an outage stops the run; Flash-Lite three, and an
  outage escalates the case. Reservations are estimated locally, never by a
  counting request.
- **A first call** (`--first-call-only`, every configuration): one attempt
  on the first case that reaches the engine, then the run stops, written
  incomplete as a smoke test with its sends (0 or 1). It is **accepted** on
  a valid verdict, its usage reported and within the reservation, and a
  model version returned ("first call accepted"); otherwise its result says
  why. An accepted first call is recorded in `~/.resolve-billing/`, after
  its result is written, with the result's hash, a fingerprint of the
  configuration (its fields, the prompt, the schema, the masking, the
  estimator) and the account's non-secret identity.
- **A full run only after an accepted first call** -- for Mistral, both
  of its configurations' -- matching the configuration and the account as
  they are now; refused otherwise, before any client is built.
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
- **Three engines on prepaid accounts** (spec 007). `gpt-6-luna`,
  `gpt-6.1-sol` and `qwen3.5-397b-a17b` run as the four above -- the
  maintainer's command only, the development set only, a first call
  before the full run -- with:
  - **a declared account**: the billing organisation and project, by
    their non-secret ids, never the key; the run lock and the envelope
    belong to the organisation, a first call's acceptance to the project,
    and rotating a key changes neither. A missing or malformed id refuses
    the run before any client is built;
  - **a prepaid envelope per billing organisation**, in its currency,
    `~/.resolve-billing/envelope-<vendor>-<organisation>.json`: declared
    once with `--envelope`, only ever raised, each declaration kept; each
    attempt reserved before it is sent at the highest rate that may apply
    (an estimate, not a bound),
    settled from its reported usage category by category (an absent
    category never read as zero; details above their totals, or counts
    that are not counts, keep the reservation spent and stop the run); an
    attempt it cannot cover is not sent and stops the run; never deducting
    a vendor's free allowance; an unreadable or inconsistent ledger is
    refused, never reset. Admission accounting under the maintainer's
    waiver, not a billing cap;
  - **the model's identity on every answer**: one that names another
    model, or none, stops the run after its cost is settled, is never
    scored, and the name is recorded only if it reads as a model id;
  - **results that say what they are**: the declared account ids, each
    attempt's reported usage, the settled cost apart from the attempts of
    unknown cost, and the label **integration run** -- spec 008's
    comparison uses fresh runs under a protocol declared before them.
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
`tests/unit/test_evaluation_registry.py`, `tests/unit/test_evaluation_limits.py`,
`tests/unit/test_accounts.py`, `tests/unit/test_prepaid_ledger.py`,
`tests/unit/test_prepaid_runs.py`, `tests/unit/test_prepaid_runner.py`;
the CI workflow's evaluation step.
