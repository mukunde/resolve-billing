# Bug Fix: an over-billing usage finding does not force human review

- **Slug**: usage-overbilling-review
- **Fixed**: 2026-11-05
- **Assessment**: ./assessment.md
- **Status**: applied

## Summary

`ActionPolicy.evaluate` gains step 4c: a `SUSPICIOUS_USAGE_DELTA` finding that is not an
under-billing gives `HUMAN_REVIEW`, whatever the engine classified or asked. Placed after
the money branch, the escalation and step 4b, it only turns an `AUTO` into a
`HUMAN_REVIEW`.

## Changes

| File | Change | Notes |
|------|--------|-------|
| `app/policies/action_policy.py` | modified | step 4c, its own reason |
| `tests/unit/test_usage_overbilling_review.py` | added | the policy, and the investigation end to end |
| `docs/contract/approval-and-execution.md` | modified | the guarantee stated |
| `evals/taxonomy.md` | modified | S1's "Built" column |

## Tests Added or Updated

- `TestThePolicy::test_it_goes_to_a_person` -- six non-financial answers, none escalating,
  each with an over-billing finding: human review and its reason.
- `TestThePolicy::test_a_confident_engine_changes_nothing` -- 0.99 changes nothing.
- `TestThePolicy::test_the_earlier_steps_keep_their_reasons` -- `valid_invoice`, `escalate`
  and a credit note with no amount keep their verdicts and reasons.
- `TestThePolicy::test_under_billing_does_not_trigger_it`.
- `TestThePolicy::test_without_the_finding_it_stays_automatic` -- the same six answers.
- `TestThroughTheInvestigation::test_no_automatic_reply` -- the world of
  `legitimate_overage` with its usage record lowered to 20 (billed 47), an engine that
  asks the customer: the finding is there, human review, no automatic reply.

## Local Verification

- Before the fix, on `d2143ed`: 8 failed, 8 passed. The investigation test showed the
  verdict `auto` and an automatic reply **allowed**.
- After the fix: the new file and the existing policy tests, 67 passed; `ruff check .`
  clean. The full suite and the mock evaluation: see the commit's gates.

## Deviations from Assessment

None.

## Follow-ups

- Spec 012: the development cases on usage and prompt v4, measured (roadmap item 1).
