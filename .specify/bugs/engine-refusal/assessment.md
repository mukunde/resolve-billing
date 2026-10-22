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
   transient (`_TRANSIENT`), with the status code and a category (request,
   authentication, permission, not found, too large; "account out of credit"
   recognised from the SDK's structured error body). **Never the provider's
   own text**, which can quote the request, a URL or a credential.
   Transient statuses stay `EngineUnavailable`; a programming error or a
   lookalike exception stays what it is.
3. The evaluation runner catches it around each run: one line on standard
   error, naming the run in a series; the whole series stops, earlier
   complete runs included; nothing is written, and an existing `--json` file
   is left as it was (the message says so); exit code 2.
4. Unchanged in behaviour: the investigation does not catch it. Through the
   API a refused request still fails the request (500), as before, now with
   `EngineRejected` rather than the SDK's exception; an escalation would hide
   a configuration problem behind a plausible decision. ADR-002 names the
   contract.

## Tests

- `tests/unit/test_engine_refusal.py` (no SDK needed, so it runs in the free
  CI job): the runner stops with the diagnosis, no traceback, no result file,
  exit 2; a series stops at the refused run and writes nothing; an existing
  result file is left and named.
- `tests/unit/test_claude_refusal.py` (with the SDK): HTTP 400, 401, 403, 404
  become `EngineRejected` with a category; sentinels in the provider's
  message (a key, a URL, newlines, long text) never reach it; the credit case
  is recognised from the structured body.
- `tests/unit/test_engine_refusal.py` also: the investigation lets a refusal
  through rather than turning it into an escalation (what the API then
  answers with a 500, as before).
- `tests/unit/test_claude_outages.py`: 400 and 401 now expected as refusals;
  outages and other errors unchanged.

## Acceptance

- A run against a refusing engine ends with a status and a category, exit 2,
  writes nothing, and shows no traceback.
- Outages still escalate; the mock baseline is unchanged.
