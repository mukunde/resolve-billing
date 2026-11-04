# Implementation Plan: The holdout's reference run on the composite

**Branch**: `011-reference-run` | **Date**: 2026-11-03 | **Spec**: [spec.md](spec.md), approved by the maintainer at `e8eac64`

## Summary

One new module, `evals/reference_run.py`, with three commands: `prepare`, `run` and
`rebuild`. It runs the adopted composite on the holdout, once, with every case-level
output kept in the maintainer's private folder and only totals on the console. It reuses
the composite's factory, the recorders and ledgers of `evals/service_run.py`, the runner's
scoring, and the check of `evals/check.py`. The development guards do not change.

Like specs 008 to 010, this plan is one file.

## Technical Context

Python 3.12 (CI), 3.13 locally. No new dependency. Private records are JSON and JSON Lines
in the maintainer's folder; the one-shot state is a JSON file under `~/.resolve-billing/`.
CI tests the tool on stand-in sets with sentinel strings, network-free, and makes no paid call.

## Constitution Check

| Principle | How this plan meets it |
|---|---|
| II, V | The composite as adopted: same policy, bounds, fallback; nothing changes what it may do. |
| VI | The freeze record binds every input; the private record keeps each attempt and case. |
| VIII | A declared, single measurement; its reading fixed before it. |
| IX | Nothing case-level leaves the private folder; the run is once; no agent runs it on the holdout. |
| Sensitive | Final-diff approval; ADR-009 and ADR-013 change with it. |

## Design

### 1. Lockdown first (FR-007)

`evals/reference_run.py` imports only the standard library at module level. `main` first:

- refuses if `OTEL_EXPORTER_OTLP_ENDPOINT` or any `OTEL_*` exporter variable is set, or if a
  tracer provider other than the default one is already installed;
- canonicalises the private folder (`Path.resolve(strict=True)`, links followed) and refuses it
  when it is not a writable directory, or when it, or any parent, holds a `.git` entry, or when
  it lies under the repository or any of its `git worktree list` paths;
- before anything else is done, the original descriptor 1 is duplicated (`os.dup(1)`) and a
  console channel is opened on that duplicate; then descriptors 1 and 2 are redirected onto
  `private/log.txt` and `sys.stdout` and `sys.stderr` replaced by writers to it. The console
  channel carries only an allowlist: the summary, the prompt, the totals, a refusal's category;
- replaces the root logger's handlers with one that writes to `private/log.txt`, and, for every
  logger already created, removes its handlers and sets it to propagate, so that no named
  logger keeps a handler of its own; turns off the SDKs' request logging through
  `silence_request_logging`;
- wraps everything after that in one handler: an exception prints its type and the private
  folder's path only, and writes the traceback to `private/log.txt`.

Only then does it import `app` and `evals` modules. `app.main` is never imported, so tracing is
never configured.

### 2. `prepare` (FR-003, FR-004, US-1)

`python -m evals.reference_run prepare --cases PATH --fixtures DIR --private DIR`

- copies once into `private/snapshot/` the case file, the repository's own fixtures as
  `base/`, and the holdout's as `extra/`, hashes those bytes, and from then on reads only the
  snapshot. The fixture loader and the fake provider gain an optional base directory,
  defaulting to the repository's as today; the tool passes `base/` and `extra/` explicitly, to
  the check and to the provider, and never sets `RESOLVE_EXTRA_FIXTURES_DIR`. `run` rehashes
  the snapshot, not the originals, and uses it;
- runs `evals.check.check` on the snapshot, its output going to the private log, and refuses on
  any failure, printing only "the check failed";
- builds the freeze record: commit, clean tree, a commit containing
  `ActionPolicy`'s uncertainty step (checked by `git merge-base --is-ancestor` with the fix's
  merge commit, `83ac248`), both configurations' fingerprints, `FAILOVER`'s version, policy,
  deadline and both bounds, `PROMPT_VERSION`, the schema's hash, `MASKING_VERSION`, the case
  file's hash, the hash of every fixture file, the repository's own fixtures included,
  `EVALUATION_DATE`, the scoring definition's hash (the source of `runner._evaluate` and
  `runner.unsafe_reason`), and an optional acceptance threshold given by `--threshold`;
- computes the estimated admission budget locally: each case through the investigator with a
  dry engine that builds both legs' requests and sums their reservations, at
  `reservation_cost`, without sending; the sum per account;
- checks both accounts' declared ids, both accepted first calls (`FirstCalls.admit_full_run`),
  and that both envelopes' room covers the budget;
- writes `private/freeze.json`, prints its hash, the budget per account and the case count, and
  records the state `prepared` with that hash (section 4).

### 3. `run` (FR-005, FR-006, FR-008, FR-010, US-1, US-3)

`python -m evals.reference_run run --freeze HASH --cases PATH --fixtures DIR --private DIR`

1. Lockdown; recompute the freeze record from the inputs and refuse unless its hash is `HASH`
   and equals the prepared one.
2. Ask the maintainer to type `RUN ONCE` after a summary: the configuration, the budget, the
   one-shot consumption. Anything else stops, still prepared.
3. Take both account locks, Anthropic's then OpenAI's, and wait each account's window.
4. Under both locks, and under the state's lock: recheck that the state is `prepared` with this
   freeze hash and case hash, and that both envelopes still admit the budget; then write the
   state `consumed` atomically (section 4). A refusal here leaves it `prepared`. From the
   write on, the run is spent.
5. Build the composite with `deps._failover(recorder=...)`: the factory gains an optional
   recorder, defaulting to `get_attempts()` as today, so the service is unchanged and no
   `audit.db` is created here. The recorder is a `RoutingRecorder`: OpenAI's attempts to a
   `LedgerRecorder` on OpenAI's envelope, Anthropic's to one on Anthropic's, and each start and
   end also as a record file (section 3a). The composite is wired to the fake provider loaded
   on the snapshot, so the platform's invoice-reference masking reaches both legs. No bound,
   policy or fallback changes.
6. Run every case through the investigator, at `EVALUATION_DATE`, scored by `runner._evaluate`,
   each outcome written as a record file (section 3a) with the routing the totals need: the
   verdict, whether the engine was called, the attempts' ids, the outage and identity events.

### 3a. Records that survive a crash

One file per record: `private/attempts/<attempt>-start.json`, `-end.json`, and
`private/cases/<n>.json`, each written to a temporary name in the same folder, fsynced, renamed
with `os.replace`, and the folder fsynced. A crash leaves whole files or none. A start with no
end is an open attempt; its ledger ticket says what was reserved. The freeze record holds the
declared case count, so a rebuild needs no input.
7. A provider outage is the composite's business and is counted. A `LedgerRefused`,
   `RunStopped`, `KeyboardInterrupt` or defect stops the run: records kept, `incomplete`.
8. Totals (section 5) to `private/totals.json` and the console.

### 4. The one-shot state (FR-005, SC-002)

`~/.resolve-billing/reference/holdout-2026-10-22.json`, one identity whatever the inputs:

```text
{"identity": "holdout-2026-10-22", "state": "prepared" | "consumed",
 "freeze_hash": ..., "case_hash": ..., "prepared_at": ..., "consumed_at": ...}
```

- written with `state.write_atomically`, under a lock file beside it;
- `prepare` may write `prepared` only when the file is absent or `prepared` with the same case
  hash; any other freeze record or case hash is refused once a record exists;
- `run` writes `consumed` before the first send, and refuses when the state is `consumed`, when
  the file is unreadable, or when the lock is held: an ambiguous crash reads as consumed;
- nothing in the tool turns `consumed` back; the documentation says that only a recorded policy
  decision may, by the maintainer's own hand, never by the tool.

### 5. Totals (FR-009, FR-010)

From `cases.jsonl` and `attempts.jsonl` alone:

- cases total (from the freeze record), run, not run; settled by the rules alone: the
  investigation recorded the engine skipped, not merely no attempt;
- fully correct, over the full set when complete; otherwise only "incomplete", run and scored
  counts, with no full-set rate;
- unsafe actions, first, as a pause for review;
- sent to a person: verdict `human_review` or `requires_approval`; and of those, the cases
  whose event is an outage, an identity failure, or a failure before any send;
- Luna failed and Haiku answered (an attempt 1 eligible failure, attempt 2 answered); both failed;
- invalid answers (`unusable` attempts);
- answered by each model: a terminal attempt `answered`, its returned model only;
- spend settled and unknown, per account; completion.

### 6. `rebuild` (FR-008)

`python -m evals.reference_run rebuild --private DIR` reads the records and the freeze record,
recomputes the totals, never calls a vendor, never re-scores a case: the outcomes are the stored
ones, scored at the time by the frozen definition. Open attempts stay unknown.

### 7. Documents (FR-012, FR-014)

- ADR-009: the Model provider row adds the holdout's masked payload for OpenAI and Anthropic,
  for this one run, on the attestation.
- ADR-013: the attestation, 2026-11-03, its four points; the reading of the result.
- `docs/contract/evaluation.md`, `docs/operations.md` ("The reference run", maintainer only),
  the README.

## Risks

- **The dry engine's budget** is an estimate, as every reservation: real usage can differ; the
  ledgers still refuse beyond an envelope, which ends the run incomplete.
- **An interrupted run is spent**: by design.
- **A sentinel test cannot cover a library that writes to the file descriptor directly**: the
  SDKs log through `logging`; stdout and stderr are also redirected to the private log during
  the run, and tested.
