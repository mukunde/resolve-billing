# Tasks: A fallback from `gpt-6-luna` to Claude Haiku 5.5

**Input**: [spec.md](spec.md), [plan.md](plan.md)

**Tests**: first, in every phase. No test makes a paid call.

## Phase 1: `BoundedEngine` under a shared deadline

- [x] T010 Test first: `leg` takes an invocation id, one absolute deadline with a non-zero
  clock origin, and a first number; `failover_eligible` true only for a sent timeout,
  transport failure, 429 or 500 to 599, false for an unexpected 2xx or 3xx, the queue, the
  limiter, the deadline and `not_sent`; a failure carries its telemetry, and the investigator's
  outage and identity paths write it. Standalone Luna's other tests pass unchanged.
- [x] T011 `app/decision/bounded.py`.

## Phase 2: The Anthropic client

- [x] T020 Rename to `app/decision/anthropic_hosted.py`, `AnthropicHostedEngine`; documentation,
  `vendors.client_for`, `hosted.py`, `evals/runner.py`, the contract, tests. Fingerprints and
  parity tests unchanged. Any result file unchanged.

## Phase 3: `FailoverEngine`

- [x] T030 Test first, `tests/unit/test_failover_engine.py`, fake legs and clock:
  - each trigger falls back: timeout, transport, 5xx, 429, no wait for `Retry-After`;
  - each exclusion does not: other 4xx, wrong model, unusable, not sent, queue, limiter,
    recording failure, budget refusal, cancellation, defect;
  - Luna capped at 30 s, Haiku at 60 s or what remains, 90 s in all, and a slow Luna leaves
    Haiku the rest;
  - each of Haiku's outcomes ends as a standalone engine's, and its identity failure keeps
    both legs' accounting and names the model that really answered, through the audit, the
    page and the workflow span; no third attempt;
  - one correlation id; attempts numbered 1 and 2; costs summed, unknowns kept, the floor
    stated; the answering model named; no state between disputes; limiters shared across
    disputes;
  - both legs get the masking hook, both close, the second even if the first fails.
- [x] T031 `app/decision/failover.py`.
- [x] T032 Test first: parity of both legs with their evaluated requests and fingerprints,
  whatever `CLAUDE_MODEL` says (SC-005); the investigator, audit, run page and API on a run
  that fell back, selected through the real wiring over the investigation endpoint with
  the SDKs on in-memory transports (SC-003); no vendor text anywhere; the workflow span's
  totals.

## Phase 4: Wiring

- [x] T040 Test first: the composite selected by one variable; each missing condition
  refused by name at start, Anthropic's included; the other selections unchanged (SC-004);
  two successive lifespans.
- [x] T041 `app/api/deps.py`.
- [x] T042 Test first, then `evals/engines.py` and `evals/runner.py`: the plain runner refuses
  the composite before reading a case or building a client; `evaluation_engines()` leaves it
  out, and the registry test says so.

## Phase 5: The measurement's tool

- [x] T050 Test first, no network: the composite needs `--primary-disabled`; no OpenAI client
  is built and no attempt recorded for it; a known zero, so no unknown cost; the Anthropic
  envelope reserved and settled through a real bounded leg, masking included; each case
  attributed; the guard, first call and account held for Haiku; the standalone tool unchanged.
- [x] T051 `evals/service_run.py`.

## Phase 6: Documents

- [x] T060 ADR-009, ADR-013 (reference configuration, attestations, the maintainer's statement),
  the contracts, operations, README, `.env.example`.
- [ ] T061 Gates. Final diff to Codex, then to the maintainer by SHA. The PR.

## Phase 7: After the merge, with the maintainer's authorisation

- [ ] T070 **[maintainer]** Authorises the measurement, on what is shown before launch.
- [ ] T071 The measurement with Luna disabled (FR-012); stored and listed. SC-006's trigger,
  if met, goes to the maintainer, and it is not rerun to reach a score.
- [ ] T072 **[maintainer]** The attestations, before the reference run (FR-013).
- [ ] T073 **[maintainer]** The reference run, on the composite, fallback on.

## Dependencies

Phase 1 and 2 before 3; 3 before 4 and 5; all before 6; 7 after the merge.
