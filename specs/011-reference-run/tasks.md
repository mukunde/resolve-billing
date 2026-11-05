# Tasks: The holdout's reference run on the composite

**Input**: [spec.md](spec.md), [plan.md](plan.md)

**Tests**: first, in every phase, on stand-in sets with sentinel strings in every field. No
test makes a paid call, and no test reads the holdout.

## Phase 1: Lockdown

- [x] T010 Test first, `tests/unit/test_reference_lockdown.py`: an exporter variable or an
  installed tracer provider refuses; a private folder inside the repository, a worktree, under
  `.git`, through a link, missing or read-only refuses; a defect prints its type and the folder
  only; logs, SDK logs, named loggers with handlers of their own, stdout, stderr and file
  descriptors 1 and 2 go to the private log, before preparation and imports, checked in a
  subprocess; only the allowlist reaches the console, through the duplicated descriptor;
  `app.main` is never imported.
- [x] T011 `evals/reference_run.py`: the lockdown.

## Phase 2: `prepare`

- [x] T020 Test first: the check's failure prints nothing case-level; the freeze record holds every
  field of FR-003; a commit without the uncertainty fix refuses; the budget is computed without a
  send; undeclared accounts, a missing first call or a short envelope refuse; the state becomes
  `prepared`; a second prepare with other inputs is refused once a record exists; the snapshot is
  what runs: a case, a holdout fixture or a repository fixture changed, or a fixture file added,
  after preparation changes nothing or refuses; the loader's default base is unchanged for the
  service.
- [x] T021 `prepare`, the dry engine.

## Phase 3: `run` and the state

- [x] T030 Test first, fake legs: a wrong hash refuses; anything but `RUN ONCE` stops, still
  prepared; `consumed` is written before the first send, under both locks, after the envelopes
  are rechecked, and a short envelope then leaves it `prepared`; two competing starts consume once;
  a second start is refused for another folder, freeze record or case file; an unreadable state or
  a held lock refuses; the factory takes the recorder and the service's default is unchanged;
  masking reaches both legs; both legs route to their ledgers; attempts and cases are whole files,
  even with a crash during a write or between a settlement and its end record; an outage is
  counted and the run
  goes on; a ledger refusal, an interruption and a defect end it incomplete; no sentinel in any
  output for a complete run, a stopped run and a defect; spans are not exported.
- [x] T031 `run`, `RoutingRecorder`, the state.

## Phase 4: Totals and `rebuild`

- [x] T040 Test first: every total of FR-009 on a stand-in; a failure before any send is not
  counted as rules-only; `requires_approval` counts as sent to a person; the fallback and
  both-failed counts;
  models counted only when they answered; an incomplete run shows no full-set rate; an unsafe
  action comes first; `rebuild` gives the same totals and calls nothing.
- [x] T041 Totals and `rebuild`.

## Phase 5: Documents

- [x] T050 ADR-009, ADR-013, the evaluation contract, operations, README.
- [ ] T051 Gates. Final diff to Codex, then to the maintainer by SHA. The PR.

## Phase 5a: The totals export (FR-011a, 2026-11-05)

- [x] T055 Test first: the export holds only the allowlisted totals, bound to the freeze hash,
  the commit and the run; no sentinel; a planted key, a free-text stop or model, an unknown
  provider refuses it; a path inside the private folder, through a link, or not `.json` refuses
  it; `rebuild` exports the same totals.
- [x] T056 `evals/reference_run.py`: `--export` on `run` and `rebuild`.

## Phase 6: After the merge, the maintainer alone

- [ ] T060 **[maintainer]** `prepare`, reads the freeze record.
- [ ] T061 **[maintainer]** `run`, once.
- [ ] T062 **[maintainer]** Decides whether a summary of totals is committed.

## Dependencies

Phase 1 before all; 2 before 3; 3 before 4; all before 5; 6 after the merge.
