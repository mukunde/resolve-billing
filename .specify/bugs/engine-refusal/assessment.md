# Bug Assessment: a refused request stops an evaluation run with a traceback

- **Slug**: engine-refusal
- **Created**: 2026-10-22
- **Source**: observed on 2026-10-19, when the API account ran out of credit
  during the D10 measurement
- **Verdict**: valid
- **Severity**: low -- no wrong decision and no money involved; a measurement
  fails unreadably, and the cause is easy to miss

Written in the format of Spec Kit's `bug` extension.

## Symptom

**Observed.** `python -m evals.runner --engine claude` against an account out
of credit printed a Python traceback ending in
`anthropic.BadRequestError: Error code: 400 ... credit balance is too low`,
from inside the engine adapter, on the first case.

**Expected.** One line saying the engine refused the request and why, that
the run stopped, and that no result was written; a non-zero exit code.

## Root Cause

The design is right and stays: a request the provider refuses (a bad
request, a rejected key, an account out of credit) is not an outage -- trying
later changes nothing -- and must not become an escalation, so it fails the
run. But it failed with the SDK's own exception, which nothing above the
adapter could name without importing the vendor's SDK (ADR-002), so nothing
caught it.

## Remediation

1. `EngineRejected` in `app/decision/base.py`, beside `EngineUnavailable`: the
   provider refused the request. Engine-agnostic.
2. The Claude adapter raises it for any SDK status error that is not
   transient (`_TRANSIENT`), with the status code and the provider's message
   (cut at 300 characters). Transient statuses stay `EngineUnavailable`; a
   programming error or a lookalike exception stays what it is.
3. The evaluation runner catches it around each run: one line on standard
   error, the run stops whole, no result file, exit code 2.
4. Unchanged: the investigation does not catch it. Through the API, a refused
   request still fails the request, as before -- an escalation would hide a
   configuration problem behind a plausible decision.

## Tests

- `tests/unit/test_engine_refusal.py`: the runner stops with the diagnosis, no
  traceback, no result file, exit 2 (fails on the code before the fix: the
  exception did not exist); the Claude engine reports HTTP 400, 401, 403, 404
  as `EngineRejected` with the provider's message.
- `tests/unit/test_claude_outages.py`: 400 and 401 now expected as refusals;
  outages and other errors unchanged.

## Acceptance

- A run against a refusing engine ends with the provider's reason and exit 2,
  writes nothing, and shows no traceback.
- Outages still escalate; the mock baseline is unchanged.
