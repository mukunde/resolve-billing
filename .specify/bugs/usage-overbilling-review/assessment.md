# Bug Assessment: an over-billing usage finding does not force human review

- **Slug**: usage-overbilling-review
- **Created**: 2026-11-05
- **Source**: pasted text, from the prompt v4 framing (Codex r313-r315), approved by the
  maintainer on 2026-11-05
- **Verdict**: valid
- **Severity**: high

## Report (verbatim or summarized)

With a `SUSPICIOUS_USAGE_DELTA` finding (direction over) on the disputed invoice and a
confidence of 0.85, `ActionPolicy().evaluate` returns `auto` for
`insufficient_information` + `request_information` and for `proration_dispute` +
`explain_invoice`. Only `valid_invoice` (step 1c) and the financial actions (no amount)
reach human review. Taxonomy S1, decided on 2026-10-19: a disputed usage gap with the
finding present is `unknown` + escalate, and a person looks. Today that holds only if the
model says so.

## Symptom

When the rules find the customer billed for more usage than was measured, an engine that
answers with a question or an explanation, under any classification but `valid_invoice`,
is authorised with no person, and the automatic reply is allowed. Expected: the case goes
to a person, whatever the engine answers (taxonomy S1; constitution, principles II and V).

## Reproduction

1. Give `ActionPolicy().evaluate` a finding `SUSPICIOUS_USAGE_DELTA` with
   `direction: over`, as `InvestigateDispute._usage_rules` produces for a metered line
   billed above its usage record.
2. Pass a decision `insufficient_information` / `request_information` (or
   `proration_dispute` / `explain_invoice`) at a confidence of 0.70 or more.
3. The verdict is `auto`, reason "Action non financière, sans effet sur le compte du
   client."

Checked on `85d37df`. No development case produces a usage finding (a mock run of the 34
cases: 0), so neither the suite nor the baseline meets it.

## Suspected Code Paths

- `app/policies/action_policy.py`, step 1c: `_USAGE_DOUBTS` stops only `VALID_INVOICE`.
- `app/policies/action_policy.py`, step 4b: stops `UNCERTAIN` (`NOT_EVALUABLE`,
  `USAGE_UNAVAILABLE`), not `SUSPICIOUS_USAGE_DELTA`.
- `app/policies/action_policy.py`, step 5: everything else that moves no money is `AUTO`.
- `app/application/investigate_dispute.py`, the under-billing filter: removes
  `direction: under` findings before the engine and the policy, so any
  `SUSPICIOUS_USAGE_DELTA` the policy sees is an over-billing.
- `app/policies/correction.py`, `PARTIAL_FINDINGS`: already withholds any amount on it.

## Root Cause Hypothesis

High confidence. S1's code half stopped money and "the invoice is correct" on an
over-billing, and left the rest to a prompt instruction that was never written: a question
or an explanation is a non-action at step 5. It is the same shape as
`uncertain-human-review`, for a finding that is not an uncertainty but a possible error in
the customer's favour that no code can price yet (S5).

## Proposed Remediation

**Preferred**: in `ActionPolicy.evaluate`, beside step 4b and before step 5, an
over-billing usage finding gives `HUMAN_REVIEW`, whatever the classification or the action,
with its own reason. It is keyed on `SUSPICIOUS_USAGE_DELTA` with `direction` other than
`under`, so it holds even if the upstream filter ever changed. Placed after the money branch
and the escalation, it only turns an `AUTO` into `HUMAN_REVIEW`: every other verdict and
reason stays as it is. The reply gate then refuses by itself, and
`requires_human_approval` is true.

**Alternatives**:
- Add `SUSPICIOUS_USAGE_DELTA` to `UNCERTAIN`: refused. That set also blocks every
  correction in `correction_for`, including a whole-invoice one that S3 already routes, and
  calls a measured gap "a check that could not conclude", which it is not.
- Prompt only: refused. The policy has to hold whatever the engine, as
  `uncertain-human-review` showed.

**Files likely to change**:
- `app/policies/action_policy.py`
- `tests/unit/test_usage_overbilling_review.py`
- `docs/contract/approval-and-execution.md` (states the human-review findings)
- `evals/taxonomy.md` (S1's "Built" column)

**Tests to add or update**:
- An over-billing finding, with `request_information` and `explain_invoice`, on each
  classification that reached `AUTO`: human review, its reason, and no automatic reply.
- `unknown` + `escalate` and the financial actions keep their present verdicts and reasons.
- An under-billing finding, if it ever reached the policy, does not trigger the step.
- The same answers with no usage finding stay `AUTO`.
- Shown red on the code before the fix.

## Risks & Considerations

- More disputes go to a person; none fewer. No amount is computed differently.
- No development case changes outcome (none has a usage finding), so the mock baseline and
  the CI floor do not move. The new cases come with spec 012.
- It does not make an engine's classification right: the prompt v4 instruction, measured
  in spec 012, is that half.
- Sensitive change (constitution, Development Workflow: the money-moving core and its
  policies): the maintainer approves the final diff by its commit.

## Open Questions

None.
