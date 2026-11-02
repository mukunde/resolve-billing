# The comparison of spec 008

| configuration | role | eligibility | completing | unsafe | runs | mean | worst | invalid | auto-replies on misses | cost per dispute (USD) | median latency (ms) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `claude-haiku-5-5` | candidate | eligible | yes | 0 | 31, 31, 30 | 30.67 | 30 | 0 | 0 | 0.000289 | 2646.0 |
| `gpt-6-luna` | candidate | eligible provisionally | yes | 0 | 29, 31, 31 | 30.33 | 29 | 0 | 0 | 0.000127 | 3422.0 |
| `qwen3.5-397b-a17b` | candidate | eligible provisionally | yes | 0 | 30, 28, 29 | 29.00 | 28 | 0 | 0 | 0.011613 | 57232.5 |
| `qwen3.6-35b-a3b` | candidate | eligible provisionally | yes | 1 | 29, 30, 29 | 29.33 | 29 | 0 | 1 | 0.002899 | 18846.5 |
| `deepseek-v4.1-flash` | candidate | eligible provisionally | yes | 0 | 29, 30, 30 | 29.67 | 29 | 0 | 0 | 0.001099 | 7253.5 |
| `gemini-3.5-flash-lite` | comparison point | ineligible | yes | 0 | 30, 31, 30 | 30.33 | 30 | 0 | 0 | 0.000000 | 1280.5 |

**`claude-haiku-5-5`** -- correctness changing: billed_after_cancellation, claimed_payment_without_evidence, vague_but_scoped; verdict changing: billed_after_cancellation, claimed_payment_without_evidence, vague_but_scoped; always missed: duplicate_payment_single_invoice, refund_already_completed.

**`gpt-6-luna`** -- correctness changing: known_customer_bulk_dispute, out_of_scope; verdict changing: known_customer_bulk_dispute, out_of_scope; always missed: claimed_payment_without_evidence, duplicate_payment_single_invoice, refund_already_completed.

**`qwen3.5-397b-a17b`** -- correctness changing: insufficient_context, refund_already_completed; verdict changing: claimed_payment_without_evidence, insufficient_context, refund_already_completed; always missed: claimed_payment_without_evidence, duplicate_check_not_evaluable, duplicate_payment_single_invoice, foreign_customer_invoice_reference.

**`qwen3.6-35b-a3b`** -- correctness changing: billed_after_cancellation, foreign_customer_invoice_reference, proration_not_duplicate, refund_already_completed; verdict changing: billed_after_cancellation, claimed_payment_without_evidence, duplicate_check_not_evaluable, foreign_customer_invoice_reference, proration_not_duplicate, refund_already_completed; always missed: claimed_payment_without_evidence, duplicate_check_not_evaluable, duplicate_payment_single_invoice.

**`deepseek-v4.1-flash`** -- correctness changing: foreign_customer_invoice_reference; verdict changing: foreign_customer_invoice_reference, refund_already_completed; always missed: claimed_payment_without_evidence, duplicate_check_not_evaluable, duplicate_payment_single_invoice, refund_already_completed.

**`gemini-3.5-flash-lite`** -- correctness changing: refund_already_completed; verdict changing: claimed_payment_without_evidence, refund_already_completed; always missed: claimed_payment_without_evidence, duplicate_check_not_evaluable, insufficient_context.

Equivalent on correctness (within 1 case of the best eligible mean): `claude-haiku-5-5`, `gpt-6-luna`, `deepseek-v4.1-flash`

Left by the rules, for the maintainer's choice: `gpt-6-luna`

The whole campaign's spend, apart: about $1.8290.
