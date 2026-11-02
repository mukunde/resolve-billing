# Tasks: `gpt-6-luna` on the demonstrator's service path

**Input**: [spec.md](spec.md), [plan.md](plan.md)

**Tests**: first, in every phase, as the constitution asks. No test makes a paid call.

## Phase 1: The port's additions

- [x] T010 Test first, `tests/unit/test_bounded_engine.py`: `Bounds` pinned at 3
  attempts, 60 s, 90 s, 4 in flight; the closed set of outcomes.
- [x] T011 `app/decision/base.py`: `EngineRefused`, `EngineMisidentified`,
  `AttemptRecorder` with `AttemptStart` and `AttemptEnd`, and the telemetry's
  `usage_known`, `unknown_cost_attempts` and invocation id. Every existing engine's
  telemetry unchanged, by test.
- [x] T012 Move `attempt_cost` to `app/decision/hosted.py` and `Limiter` to
  `app/decision/limits.py`; extract `Governed`'s settlement check; public names for the
  request and the answer. The evaluation suite passes as it is.

## Phase 2: `BoundedEngine`

- [x] T020 Test first, with a fake inner engine and clock:
  - parity: the service's request equals the evaluated one on the in-memory
    transport, and the configuration's fingerprint equals the manifest's (SC-003);
  - retries: timeout, transport, 429 and 5xx retried; other 4xx, wrong model and
    unusable answers not;
  - bounds: a stalling vendor stops within the deadline; `Retry-After` honoured, one
    beyond the time left stops at once; 2 then 4 seconds without one;
  - limiter and queue waits counted against the deadline;
  - cancellation propagates, its attempt finished as `cancelled`;
  - no switch to any other engine (FR-005a);
  - the same bytes sent on every attempt;
  - concurrency never above 4, and requests per second and tokens per minute both
    held on concurrent calls;
  - concurrent calls and repeated calls for one dispute keep their attempts apart.
- [x] T021 `app/decision/bounded.py`.
- [x] T022 Test first: every failure kind, its outcome, and no vendor text in the
  response, the audit, the logs or the spans (SC-002), with a planted secret string
  in the vendor's error.
- [x] T023 `EngineRefused` handled in `app/main.py`, 502, closed category. Claude's
  `EngineRejected` behaviour unchanged, by test.
- [x] T024 Test first, then the investigator: `EngineMisidentified` writes
  `decision_engine_identity_failed` with its own explanation, escalates, and is not
  reported as an outage; the outage path unchanged.

## Phase 3: Attempts on record

- [x] T030 Test first, `tests/unit/test_attempt_store.py`: start and terminal records;
  an open row reads unfinished; records survive a refusal, an exhausted retry and a
  cancellation (SC-007a); a recorder that fails sends nothing; concurrent attempts;
  an old database file migrates and keeps its rows.
- [x] T031 `app/infrastructure/attempts.py`; `agent_runs.engine_invocation_id` and
  `unknown_cost_attempts` in `app/infrastructure/audit.py`.
- [x] T032 Test first: success after a timed-out attempt shows both attempts, and the
  run's cost reads as a lower bound; usage missing stays NULL, not zero, in the row
  and the spans; the reconciler says so; the invocation id links the run on the
  answered, outage and identity paths; Claude's records unchanged.
- [x] T033 Telemetry from the attempts; the audit writer; the reconciler's reading.
- [x] T034 Test first, then `app/api/run_view.py` and the run API: attempts shown,
  "inconnu", "non terminée"; older rows show nothing.

## Phase 4: Traces

- [x] T040 Test first: an in-memory exporter sees one span per attempt inside the
  investigation span for success after a retry, exhaustion, refusal and cancellation;
  the source test covers `bounded.py`; Claude's spans unchanged.
- [x] T041 `app/observability/tracing.py`: `chat_attributes(provider=...)`,
  `attempt_attributes`; the spans in `bounded.py`.

## Phase 5: Wiring and the refusal at start

- [x] T050 Test first: Luna selected on a non-fake platform, without
  `RESOLVE_DATA=synthetic`, without a key or declared ids, with an ambient setting:
  each refused by name, at start, through the real lifespan and through `get_engine`
  (SC-005); `mock` and `claude` never read `RESOLVE_DATA`; rollback by one variable
  (SC-004); the default stays `mock`; masking wired to the inner engine; the client
  closed on shutdown, only if built; two successive lifespans work, Claude's included.
- [x] T051 `app/api/deps.py`, `app/main.py`.
- [x] T052 Test first, then nothing else: `gpt-6-luna` in the runner stays the
  governed engine under its guard (FR-010); the full evaluation suite passes.

## Phase 6: The measurement's tool

- [x] T060 Test first, no network: `evals/service_run.py` refuses any other case file,
  undeclared accounts and a fingerprint with no accepted first call; a second run on
  the account refused by the lock; reserves before sending and refuses beyond the
  envelope, sending nothing; settles against the ledger; missing or inconsistent
  usage leaves the reservation spent and stops the run; usage above the reservation
  settles at its reported cost, then stops the run; an interrupted or
  cancelled run writes an incomplete result; the request unchanged; the result names
  itself a service run; `evals/compare.py` refuses it; the campaign's files untouched.
- [x] T061 `evals/service_run.py`.

## Phase 7: Documents

- [x] T070 ADR-009 (Model provider and Telemetry rows), `docs/contract/decision-engine.md`,
  `docs/contract/evaluation.md`, `docs/operations.md` with the local trace backend, the
  README, `.env.example`.
- [x] T071 `docs/adr/013-deployment-model.md`: the reading of "deployed", the conditions
  of the reference run, and the empty hosting record.
- [x] T072 Gates: `ruff check .`, `pytest`, the mock baseline at 29, `evals.coverage
  --check`. Final diff to Codex, then to the maintainer by SHA. The PR.

## Phase 8: After the merge, with the maintainer's authorisation

- [x] T080 **[maintainer]** Authorises the paid steps, on what is shown before launch.
- [x] T081 One dispute over HTTP on Luna (SC-001), its trace read in a local backend
  (FR-007b).
- [x] T082 The measurement through the service (FR-014); stored and listed. SC-006's
  trigger, if met, goes to the maintainer, and it is not rerun to reach a score.
- [ ] T083 **[maintainer]** Writes to OpenAI: Europe, then explicit United States
  processing; reads the data processing terms (FR-013). It blocks nothing above, and
  the answer fills the hosting record.
- [ ] T084 **[maintainer]** The holdout's attestations (FR-012), before any reference
  run, outside this slice.

## Dependencies

Phase 1 before 2; 2 before 3, 4 and 5; 3 before 6; all before 7; 8 after the merge.
