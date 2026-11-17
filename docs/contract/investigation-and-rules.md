# Investigation and rules

How a dispute is investigated: the order of the steps, the deterministic
rules and what each one stops, and what the rest of the system may rely on.
Written on 2026-11-15 from the existing records, as they stood, with spec 016
(specs/016-bank-evidence), the first change to touch the capability since
contracts exist: it adds the bank check. Reasons are in ADR-003
(deterministic before probabilistic), ADR-007 (unknown senders), ADR-011
(automated mail), ADR-014 (a bank as evidence) and `evals/taxonomy.md`, which
lists every decision boundary and the cases that hold it. This contract says
what the investigation does; the taxonomy says where each boundary lies.

The investigation is `DisputeInvestigator.investigate`
(`app/application/investigate_dispute.py`), behind `POST
/disputes/investigate`, the evaluation runner and, for its rules alone, the
MCP server.

## What the system relies on

- **One order, the architecture in miniature.** Every run, in this order:
  1. find the customer from the sender's address; a claimed customer the
     address does not support is an identity conflict, and an address that
     matches no one is an unknown sender -- both branch here, before any
     billing context is loaded (ADR-007, below);
  2. load the billing context once, in full: the customer, subscriptions,
     invoices issued in the last 180 days, payments, usage for the disputed
     invoice's period (`billing-platform.md`);
  3. run every deterministic rule;
  4. stop before the engine when a rule says a person or a question must come
     first (below);
  5. read the merchant's bank, when one is configured
     (`bank-evidence.md`);
  6. ask the decision engine to classify what is left, showing it the
     findings -- a bank finding as its code alone (`decision-engine.md`);
  7. compute the action -- its amount included -- in code, apply the action
     policy, then decide whether an automatic reply may go
     (`disclosure-and-replies.md`).
- **The rules run before any model** (constitution I), free, instant and
  certain. The injection screen runs first, on the raw message, before
  anything else reads it. The MCP server calls the same `run_rules`, so the
  two cannot drift.
- **What the rules find** (`RuleCode`, a closed set):

  | Code | What it says |
  |---|---|
  | `SUSPECTED_PROMPT_INJECTION` | the message tries to address the system |
  | `AUTOMATED_RESPONSE`, `AUTOMATED_MESSAGE` | an auto-reply or delivery report; generated, bulk or list mail (ADR-011) |
  | `UNKNOWN_CUSTOMER`, `SUSPECTED_IMPERSONATION`, `IDENTITY_CONFLICT` | the sender is no customer; looks like one; or claims an account the address does not support |
  | `INVOICE_REFERENCE_CONFLICT` | the message cites invoices, none the designated one |
  | `UNKNOWN_INVOICE`, `INVOICE_OUTSIDE_WINDOW` | the disputed invoice is not the customer's, or older than what is loaded |
  | `MESSAGE_TOO_LONG` | longer than what is sent to a model: a person reads it |
  | `DUPLICATE_INVOICE`, `DUPLICATE_PAYMENT` | the same charge invoiced twice; one invoice collected twice |
  | `INVOICE_AFTER_CANCELLATION`, `PRORATION_MISMATCH` | billed after the subscription ended; a prorated line that does not match its period |
  | `SUSPICIOUS_USAGE_DELTA`, `USAGE_UNAVAILABLE` | billed usage beyond tolerance; no usage record to compare with |
  | `CURRENCY_MISMATCH` | two amounts to compare are in different currencies |
  | `NOT_EVALUABLE` | a check that applies and cannot conclude, for lack of data -- said, never read as "nothing found" (D14); the bank's own when its evidence says `"check": "bank"` |
  | `BANK_CREDIT_OBSERVED`, `BANK_CREDIT_TO_RECONCILE` | a bank credit citing the invoice, observed, or one a person must reconcile (spec 016) |

- **Before any account is loaded.** An identity conflict proposes and says
  nothing: a person checks; an injection found in its message takes
  precedence as the blocking code. For an unknown sender, in this order: an
  injection, an automated response (set aside: nothing proposed, no alert,
  no automatic reply), a suspected impersonation, other automated mail, and
  otherwise a request to write again from the address their account uses --
  never sent automatically (ADR-007, ADR-011). Nothing about any account is
  said in any of these.
- **Where a known customer's run stops before the engine**, in this order,
  the engine never called:

  | Found | The run's result |
  |---|---|
  | prompt injection | `unknown`, no action, human review |
  | automated response | set aside: `unknown`, no action, nothing to answer |
  | message too long | `unknown`, no action, human review |
  | invoice reference conflict | `insufficient_information`, a request asking which invoice |
  | currency mismatch, invoice outside the window | `unknown`, no action, human review |

  A run that stops here never reads the bank; its bank record says
  `not_run` (`short_circuit`).
- **Under-billing is not the customer's dispute** (taxonomy S1): a usage
  delta in the business's disfavour is an audit note in the trace, never a
  finding shown to the engine or used in a reply.
- **When the billing platform cannot answer, nothing is decided.** An
  unavailable platform aborts the investigation: no run is recorded, and the
  API answers 503 so the caller can retry. Data the domain cannot represent
  aborts it too, answering 422 with a category and a reason
  (`billing-platform.md`). A failed bank read is the opposite: it becomes a
  bank `NOT_EVALUABLE` and the run goes on.
- **The bank, last.** Read only once nothing else can stop the run, only
  for an invoice unpaid on the platform. A read that fails is a bank
  `NOT_EVALUABLE` and the run goes on: the bank is evidence for a person,
  never a condition of the run.
- **The amount is the code's.** The engine names a kind of action; what a
  credit note is worth comes from the invoice (constitution IV, ADR-005), and
  `correction_for` refuses a figure when a check could not conclude
  (`NOT_EVALUABLE`, `USAGE_UNAVAILABLE`) or the bank has something to say.
- **Who decides what.** The action policy (`app/policies/action_policy.py`)
  turns the classification, the findings and the action into a verdict:
  blocking findings (injection, impersonation, identity conflict) first, then
  a bank finding, then the engine's confidence, then the rest -- a financial
  action always needs a person (ADR-004; `approval-and-execution.md`).
- **Every run is recorded** when the audit is wired: the run row holds the
  findings with their evidence, the verdict and its reason, the engine's
  telemetry and the bank check's record (constitution VI); the trace holds
  each step as an event, the bank's as its outcome alone.

## Not guaranteed today

- **No deterministic dunning rule** (taxonomy B14): a claim of payment with
  nothing on the platform and nothing at the bank is a request for
  information.
- **Rules see what is loaded.** Invoices older than 180 days, other
  customers' invoices and payments outside the platform are not read; a
  reference to them is unknown, never resolved.
- **The boundaries the taxonomy lists as missed** are not covered by any
  case; a change there is unmeasured until a case holds it.

## Covered by

`tests/unit/test_rules.py`, `tests/unit/test_bank_check.py`,
`tests/unit/test_action_policy.py`, `tests/integration/test_bank_evidence.py`,
`tests/integration/test_unusable_evidence.py`, and the development set
(`evals/cases.json`), boundary by boundary in `evals/taxonomy.md`.
