# Bug Assessment: a rule that cannot run says nothing, and silence reads as "no anomaly"

- **Slug**: d14-silent-rules
- **Created**: 2026-10-19
- **Source**: code review finding D14 ("give each rule its preconditions, and a
  `NOT_EVALUABLE` outcome when they do not hold"), reassessed against `main`
  `7ec2a2f`
- **Verdict**: valid
- **Severity**: medium -- no fixture triggers it today, but real billing data
  will: a missing period or subscription turns into "the invoice is correct"

Written in the format of Spec Kit's `bug` extension, by hand.

## Report (summarized)

Each billing rule returns a finding or `None`. When the data it needs is
missing, it also returns `None` -- the same answer as "checked, nothing
found". The engine is told that the findings' silence is an answer (prompt
v3), and the mock reads it as a valid invoice.

## Symptom

**Observed.**
- `detect_duplicate_invoice` compares periods with `overlaps_period_of`, which
  returns `False` when a period is missing: an invoice whose lines carry no
  period is never a duplicate.
- When the invoice's subscription is not in the loaded context,
  `invoice_after_cancellation` and `proration_mismatch` are not run at all.
- `proration_mismatch` skips any line without a period, and returns nothing
  when the subscription's period length is unknown.
- In each case the dispute can end as `valid_invoice` with an automatic
  reply, or with a correction proposed on the strength of the checks that did
  run.

**Expected.** A check that does not apply is silent; a check that applies but
cannot conclude says so. "The invoice is correct" never rests on a check that
could not conclude, and no correction is proposed while one could not.

## Reproduction

1. An invoice on a subscription, whose lines carry no period, with another
   invoice of the same total on the same subscription.
2. Investigate a "charged twice" dispute on it: no `DUPLICATE_INVOICE`, the
   mock answers `valid_invoice`, a reply is authorised.
3. Same with an invoice whose subscription the provider does not return.

## Suspected Code Paths

- `app/domain/rules.py:detect_duplicate_invoice`, `proration_mismatch`;
  `app/domain/models.py:Invoice.overlaps_period_of`
- `app/application/investigate_dispute.py:_subscription_rules` -- returns
  nothing when the subscription is missing.

## Root Cause Hypothesis

The rules have one output for two different facts: "nothing wrong" and
"nothing knowable". Confidence: high.

## Proposed Remediation

**Preferred** (decided by the maintainer on 2026-10-19, option a):

1. **Each check states when it applies and what it needs.**

   | Check | Applies when | Needs | Cannot conclude when |
   |---|---|---|---|
   | duplicate invoice | the invoice is on a subscription | the invoice's period, and the period of every other live invoice on that subscription with the same total | either period is missing |
   | billing after cancellation | the invoice is on a subscription | that subscription | the subscription is not in the context |
   | proration | the invoice is on a subscription | that subscription, its period length, a period on every line | any of them is missing |
   | usage | the invoice has a metered line | a usage record for its metric | no record (already `USAGE_UNAVAILABLE`) |

   An invoice with no subscription -- a one-off charge -- is not a gap: the
   subscription checks do not apply to it.
2. **`NOT_EVALUABLE`**, one finding per invoice, listing each check that
   could not conclude and why. `USAGE_UNAVAILABLE` stays as it is, as the
   usage case of the same idea.
3. **Option (a): any uncertainty blocks any correction.** With a
   `NOT_EVALUABLE` or `USAGE_UNAVAILABLE` finding, no amount is proposed
   (human review), and "the invoice is correct" goes to a person. Option (b),
   letting a correction through when the uncertain check does not concern it,
   waits for real billing data.

**Files likely to change**: `app/domain/rules.py`, `app/domain/enums.py`,
`app/application/investigate_dispute.py`, `app/policies/correction.py`,
`app/policies/action_policy.py`, `fixtures/`, `evals/cases.json`, tests, the
taxonomy, README, engineering notes.

**Tests to add or update**:
- each "cannot conclude" condition, and each "does not apply" one (a one-off
  invoice stays silent);
- a missing period no longer reads as "not a duplicate";
- a subscription missing from the context;
- a real duplicate next to a check that could not conclude: no amount;
- `valid_invoice` with `NOT_EVALUABLE`: a person;
- usage unavailable now also blocks a correction (option a).

## Risks & Considerations

- **Option (a) is conservative**: a proven duplicate on an invoice whose
  usage line could not be checked now goes to a person without an amount.
  That is the decided trade-off.
- **More reviews on poor data.** That is the point: the data is poor.
- **`NOT_EVALUABLE` is not an anomaly**; it is an absence of evidence. It
  never becomes a reason to move money, and it is low severity.

## Acceptance

- No dispute is called correct, and no correction is proposed, on the strength
  of a check that could not run.
- A check that does not apply stays silent.
- The development set and the new case pass in CI, with no unsafe action.
