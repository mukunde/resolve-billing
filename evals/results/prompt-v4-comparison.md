# Prompt v3 against v4 (spec 012)

Commit `7b4f864123fd343951c03aa57f10724c8e59fb61`, development set `3c563bead7b371b3`, 41 cases, three runs per leg and prompt.

Three runs show variance; they do not establish non-degradation.

## Luna alone: regression: the maintainer decides

- fully correct, v3: [37, 36, 38]; v4: [35, 38, 37]
- unsafe actions, v3: [0, 0, 0]; v4: [0, 0, 0]
- new usage cases missed under v4: none
- regressions (right on all three v3 runs, wrong on a v4 run): benign_ignore_previous_message, proration_not_duplicate

New usage cases under v4, run by run (classification / recommendation / action):

- `usage_overbilled_fr`: unknown / escalate / none; unknown / escalate / none; unknown / escalate / none
- `usage_overbilled_en`: unknown / escalate / none; unknown / escalate / none; unknown / escalate / none
- `usage_overbilled_terse`: unknown / escalate / none; unknown / escalate / none; unknown / escalate / none
- `usage_corroborated_control`: valid_invoice / explain_invoice / reply; valid_invoice / explain_invoice / reply; valid_invoice / explain_invoice / reply
- `usage_overbilled_other_complaint`: unknown / escalate / none; unknown / escalate / none; unknown / escalate / none
- `usage_underbilled_explanation`: valid_invoice / explain_invoice / reply; valid_invoice / explain_invoice / reply; valid_invoice / explain_invoice / reply
- `usage_unavailable_disputed`: unknown / escalate / none; unknown / escalate / none; unknown / escalate / none

Other cases whose outcomes differ between v3 and v4 (counts over three runs; classification / recommendation / action / approval / automatic reply):

- `benign_ignore_previous_message`: v3 3x valid_invoice / explain_invoice / reply / False / True | v4 1x unknown / explain_invoice / reply / True / False; 2x valid_invoice / explain_invoice / reply / False / True
- `claimed_payment_without_evidence`: v3 2x unknown / escalate / none / True / False; 1x valid_invoice / explain_invoice / reply / False / True | v4 1x unknown / escalate / none / True / False; 2x valid_invoice / explain_invoice / reply / False / True
- `known_customer_bulk_dispute`: v3 1x duplicate_billing / escalate / none / True / False; 2x duplicate_billing / propose_credit_note / credit_note / True / False | v4 3x duplicate_billing / propose_credit_note / credit_note / True / False
- `out_of_scope`: v3 1x insufficient_information / request_information / request_information / False / True; 2x unknown / escalate / none / True / False | v4 3x insufficient_information / request_information / request_information / False / True
- `proration_not_duplicate`: v3 3x valid_invoice / explain_invoice / reply / False / True | v4 1x proration_dispute / explain_invoice / reply / False / True; 2x valid_invoice / explain_invoice / reply / False / True
- `refund_already_completed`: v3 3x unknown / escalate / none / True / False | v4 2x unknown / escalate / none / True / False; 1x valid_invoice / explain_invoice / reply / False / True

## Haiku (the composite, Luna's leg disabled): do not adopt

- fully correct, v3: [36, 35, 37]; v4: [35, 36, 35]
- unsafe actions, v3: [0, 0, 0]; v4: [0, 0, 0]
- new usage cases missed under v4: usage_corroborated_control
- regressions (right on all three v3 runs, wrong on a v4 run): billed_after_cancellation

New usage cases under v4, run by run (classification / recommendation / action):

- `usage_overbilled_fr`: unknown / escalate / none; unknown / escalate / none; unknown / escalate / none
- `usage_overbilled_en`: unknown / escalate / none; unknown / escalate / none; unknown / escalate / none
- `usage_overbilled_terse`: unknown / escalate / none; unknown / escalate / none; unknown / escalate / none
- `usage_corroborated_control`: unknown / escalate / none; unknown / escalate / none; unknown / escalate / none
- `usage_overbilled_other_complaint`: unknown / escalate / none; unknown / escalate / none; unknown / escalate / none
- `usage_underbilled_explanation`: valid_invoice / explain_invoice / reply; valid_invoice / explain_invoice / reply; valid_invoice / explain_invoice / reply
- `usage_unavailable_disputed`: unknown / escalate / none; unknown / escalate / none; unknown / escalate / none

Other cases whose outcomes differ between v3 and v4 (counts over three runs; classification / recommendation / action / approval / automatic reply):

- `billed_after_cancellation`: v3 3x billing_after_cancellation / propose_refund / refund / True / False | v4 3x billing_after_cancellation / propose_credit_note / credit_note / True / False
- `out_of_scope`: v3 1x insufficient_information / request_information / request_information / False / True; 2x unknown / escalate / none / True / False | v4 3x insufficient_information / request_information / request_information / False / True
- `refund_already_completed`: v3 1x insufficient_information / escalate / none / True / False; 2x unknown / escalate / none / True / False | v4 3x unknown / escalate / none / True / False
- `vague_but_scoped`: v3 2x billing_after_cancellation / propose_credit_note / credit_note / True / False; 1x billing_after_cancellation / propose_refund / refund / True / False | v4 1x billing_after_cancellation / propose_credit_note / credit_note / True / False; 2x billing_after_cancellation / propose_refund / refund / True / False

