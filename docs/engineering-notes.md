# Engineering notes

The reasoning and the incidents behind the [README](../README.md): what the
first live calls found, how the evaluation was corrected, every defect the
code reviews found and how it was fixed, and the techniques deliberately not
used. Kept because each one is a decision someone will want to re-open, and
the argument should be there when they do.

## The first live calls

**`ClaudeDecisionEngine` has run against the live API**, on demo case A. One
call: `duplicate_billing`, a 99,00 € credit note, human approval required.
2 978 input tokens and 242 output, **$0.021**, 4.3 s.

That first call found three defects the test suite could not have, and the
reason is worth stating: the engine's tests drive a stub client, and a stub
accepts any request shape. It would stay green through a renamed parameter or
a schema the server rejects.

- The API refuses `minimum`/`maximum` on a number, so the confidence bound
  moved from the schema to validation on arrival — where a violation escalates
  to a human rather than being guessed at.
- The explanation came back reading *"INVOICE_1 and INVOICE_2 cover the same
  period"*. Only `evidence_refs` were being de-pseudonymised; the prose, which
  is what a reviewer actually reads, was not.
- The model proposed a **refund** for a duplicate on an active subscription.
  Safe — both actions need approval — but wrong for French billing, where an
  *avoir* corrects the invoice and a refund is the cash movement that may
  follow. The prompt listed both without saying when to prefer one.

All three were fixed, and the fix for the third was a new prompt version
rather than a patched sentence — which is the point of versioning the prompt at
all. What the whole case set then scored is in the next section.

---

## How the evaluation was read

These paragraphs comment the first measurement, on the original twelve cases,
shown in the README's evaluation table.

Cost and latency, prompt v3: 10 of the 12 disputes reached the model, at a
**mean $0.021** per call and a **median 4.7 s**. The other two stopped at rung
1 — the injection screen fired — so the whole set cost **$0.21**, or $0.0175
per dispute. That last figure is what `deterministic before AI` is worth here,
and it is divided by twelve rather than argued for.

A mean for cost and a median for latency is not an oversight. Cost is
additive — the question asked of it is what ten thousand disputes cost, and
that is mean × N, which a median will not give you. Latency is not additive
and trails to the right, so a mean reports a slow call rather than a typical
one, and the question asked of it is what a reviewer waits. The cost is tight
enough for the distinction to be academic here (every call landed between
$0.0193 and $0.0227, a 5.9 % spread, and the median is $0.0206) but the number
that gets multiplied should be the one that survives multiplication.

Five things worth reading off that table rather than past it.

**Unsafe actions is the only metric with a threshold.** A run is unsafe when
the system would move money on a dispute the set says is not a money case,
propose a financial action without requiring a human, propose money to a
sender nobody has verified, or answer a sender suspected of impersonating a
customer (ADR-007). It is judged on what the system *would do*, not on whether
the label matched. Zero, on every engine and every prompt version, when they
were measured — including the runs where the classification was wrong. The two
stored Claude runs predate the sender rules and the stricter money rules;
`evals.rescore` holds them to today's definition, with the runner's own
function, and each now counts **one** unsafe action: the proration case.
That verdict comes from the case set, not from a check of the amount -- the
stored runs do not record one. Since 2026-10-19 a partial correction gets no
amount and goes to a human, so the case expects no money, and a run that
proposed a credit note there is unsafe under today's contract. Those runs
did: the system then attached the whole invoice total to the engine's
credit-note recommendation. The engine was right; the amount was not, and the
fix was in the code around it.

**The first thing the evaluation found was a defect in the evaluation.** On
the first real run Claude scored 8/12, and two of its four errors were not
errors: it recommended a *refund* on a cancelled subscription, which is exactly
what prompt v2 requires — no future invoice can absorb a credit note — while my
case set still expected a credit note. The case set was wrong. It was corrected,
the `why` field records why, and the v2 column above is the stored outcomes of
that run rescored against the corrected set, so the three columns compare like
with like. Both runs are committed under `evals/results/`, and the rescoring is
a command rather than a claim:

```bash
python -m evals.rescore evals/results/claude-decision_v2.json
```

It re-applies today's expectations to that run and names the cases whose
verdict changed: the two cancellation cases then, and since 2026-10-19 the
proration case too, now expected to carry no amount. An evaluation you never argue with is a second opinion you
wrote yourself.

**Claude's two real errors were its two least confident answers.** 0.60 and
0.62, under a prompt that already asked it to withdraw below 0.70. Both were
cases where no invoice was identifiable and it classified `duplicate_billing`
anyway, reading the account rather than the complaint. Good news about the
confidence, bad news about the instruction — and only the instruction was
fixable.

**Prompt v3 closed both, and v1 and v2 are still on disk.** v3 forbids
asserting what the deterministic checks did not find, and makes the withdrawal
below 0.70 an instruction rather than a preference. Every audit row names the
prompt version that produced it, so the comparison above is reconstructable
rather than remembered. After v3, nothing below 0.70 was returned at all.

**Thirty-four cases is not calibration either, and none of this opens anything.**
The set is small, synthetic, and its first twelve were written by the same
person as the prompt — three of the ways an evaluation flatters itself, and the
second bullet above is what that looks like when it happens. `confidence_kind`
stays `self_reported` and the autonomy thresholds stay shut. Promoting a model
to autonomy on a clean sweep of twelve would be precisely the mistake this
repository argues against.

The mock is the other half of the reading. Forty lines of keyword heuristics
scored 9/12 on the original twelve, instantly and for nothing; the model scored
12/12 for $0.021 and 4.7 s. Both numbers are the point. The mock loses exactly
where reading is required — it cannot tell a request to cancel an account from
a billing dispute — and wins everywhere the answer was already in the findings.

---

## Framework choice (2026)

The 2026 landscape has settled into provider-native SDKs, graph orchestrators
with checkpoints, and light typed frameworks. The absence of one here is a
decision, not a gap.

The deciding argument is not simplicity, it is lock-in. A framework dependency
is a second lock beside the model, and the whole value of the `DecisionEngine`
port evaporates if the orchestration layer is captive.

The re-evaluation condition is written down: if the flow exceeds roughly five
nodes with mid-run resumption, or a refinement loop appears, move to a
checkpointed orchestrator. Both ports are built to survive that.

---

## Techniques this does not use, and what would change that

Four things a 2026 agent is expected to have are absent here. Each absence is
a decision with a condition attached, and the conditions are written so they
can be checked rather than argued.

**Function calling / tool use.** The model holds no tools. The billing context
is loaded once, in full, *before* the call, and the model receives it as
pseudonymised data. This is the security property, not an optimisation: a
model that can call a tool mid-reasoning is a model whose reach is decided at
inference time, and on a billing system that reach is customer data. The
closed `classification` enum is what a tool call would otherwise be — the
model supplies a switch value, code does the dispatching.

*What would change it:* a context too large to load eagerly. Then the tool
becomes a narrow, audited read, not a general one.

**RAG and a vector database.** The billing context is structured, small and
exact. A vector search answers *which invoice is similar*; the question here is
*which invoice is the same*, which is a date comparison and an integer
equality. Embedding an invoice to rediscover a `JOIN` would be slower, less
certain and unauditable.

*What would change it:* unstructured sources where similarity is the actual
question — contract clauses, entitlement wording, a support knowledge base.
Proactive watch over contractual deadlines is exactly that shape of problem,
and it is a different use case rather than this one made fuzzier.

**Multi-agent.** There is no second agent to talk to. Splitting one classifier
into a committee adds tokens, latency and a coordination failure mode, and
subtracts the property that matters: a single decision, attributable to a
single prompt version, in a single audit row.

**An agent framework.** See [ADR-006](adr/006-no-agent-framework.md),
which names what would be adopted and when.

What *is* used, and named for the same reason: a constrained output schema
enforced server-side and validated again on arrival, a versioned prompt file,
a deterministic screen ahead of the model, and a read-only **MCP** server over
the billing port.

---

## Review findings

Code reviews of this repository found fourteen defects, D1 to D14. Each is
fixed the same way: a test that fails on the code as it was, then the fix,
one commit per defect. Most of the fixed ones change no investigation result
-- they sit on execution, the API and the workflow; those that do (D5, D9,
D10, D14) were measured on the evaluation set before they landed. For D10
that is the mock engine only, which does not read the message: its run with
Claude is pending (bug record). None is open; the new holdout gets its first
reference run on the code that fixes them all.

**Fixed**

- **D1 -- a failed write could become `EXECUTED` without the platform**
  (`f9a22d7`). The state machine allowed `FAILED -> EXECUTED`, so a retry
  could be recorded as done with no write behind it. `EXECUTED` now takes
  the provider's reference, and a failed write is retried through a fresh
  claim. `TestNothingIsExecutedWithoutTheProvider`.
- **D2 -- two executions could both write** (`19ae815`). Both read
  `APPROVED`, both called the platform. Execution now claims the action
  first (`EXECUTING`), by an update conditional on the state it read: of
  two callers, one wins. `TestOneExecutionAtATime`.
- **D3 -- a timeout was treated as a failure** (`8f4b54d`). The write may
  have landed with its response lost, and the retry could issue a second
  credit note. No answer is now `UNKNOWN_OUTCOME`, settled by asking the
  platform what it holds under the idempotency key. ADR-008.
  `TestATimeoutIsNotAFailure`.
- **D4 -- an approved refund was executed as a credit note** (`83dbf86`).
  Execution had one write. The port gains `create_refund`; the instrument
  written is the one approved, and a type with no write is refused before
  anything is claimed. `TestARefundIsARefund`.
- **D6 -- nothing capped what an invoice could give back** (`e21efca`). A
  99 EUR credit note, then a 99 EUR refund, on a 99 EUR invoice would
  have paid back 198 EUR. The platform is the authority on what remains and refuses beyond
  it; one guard of our own keeps any action under the invoice's total.
  `TestThePlatformCapsCorrections`, `TestNeverMoreThanTheInvoice`. Still
  open: the idempotency key identifies a write, not the dispute behind it
  (ADR-008).
- **D7 -- routes that move money took no authenticated caller**
  (`683797d`). Anyone who knew an action id could approve and execute it,
  and the approver was a name typed into the form. The orchestrator now
  presents a service key, a person signs in, each does only its part, and
  the service refuses everything when neither is configured. ADR-009.
  `tests/integration/test_auth.py`.
- **D8 -- the server called back any URL a browser handed it**
  (`892a4b1`). The resume callback came through the review link, and was
  called as received: a link could make the service request an internal
  address. Only the orchestrator's own waiting webhook is called now,
  rebuilt from the configured origin. `TestOnlyTheOrchestratorIsCalledBack`.
- **D12 -- a long message was dropped in silence** (`ef0379e`). Past
  10,000 characters the API answered 422 and the workflow stopped there.
  Such a message now goes to a person without calling the engine, and
  whatever the API refuses ends in a mail. `tests/integration/test_long_message.py`.
- **D5 -- a partial error was priced at the whole invoice** (lot 1 of the
  specification decisions, 2026-10-19). A mispriced proration was proposed
  as a credit note for the invoice's total. An amount now exists only when
  the whole invoice is the error; anything else goes to a person without
  one (`app/policies/correction.py`, `tests/unit/test_correction.py`).
- **D9 -- a customer id in the request outranked the sender's address**
  (2026-10-19, bug record `.specify/bugs/d9-sender-matches-customer/`). A
  request naming `cus_acme` from any address was investigated as ACME's, up
  to a credit note proposed for approval. The address now identifies the
  sender and is required; a customer id is a claim, and a claim it does not
  support is an identity conflict: a person checks, the claimed account is
  not even loaded. `tests/integration/test_identity.py`.
- **D14 -- a rule that could not run said nothing, and silence read as
  "no anomaly"** (2026-10-19, bug record `.specify/bugs/d14-silent-rules/`).
  An invoice whose lines carry no period was never a duplicate; a
  subscription missing from the context skipped the cancellation and
  proration checks without a word. Each check now states when it applies
  and what it needs; one that applies but cannot conclude yields
  `NOT_EVALUABLE`, and -- the maintainer's option (a) -- any such uncertainty
  blocks any correction and sends "the invoice is correct" to a person.
  `TestChecksNotEvaluable` in `tests/unit/test_rules.py`.
- **D10 -- the customer's message reached the model as written**
  (2026-10-19, bug record `.specify/bugs/d10-message-masking/`). The context
  was pseudonymised, the message beside it was not: the provider received
  the real invoice ids, the customer's address and name, and any IBAN or card
  number pasted in. The Claude adapter now masks the message, giving a cited
  invoice the context's own alias. `tests/unit/test_masking.py`. Its
  before/after run with Claude is still to be made.
- **D11 -- an automatic reply was authorised for robots and strangers**
  (2026-10-19, bug record `.specify/bugs/d11-no-automatic-reply/`). "No
  approval needed" was read as "may answer automatically". Nothing was sent
  -- the reply branch is a placeholder -- but the contract would have answered
  spammers and auto-responders the day a channel was connected. A separate
  `automatic_reply_allowed` now decides, automated mail is recognised by rule,
  an unknown sender goes to a triage mailbox, and the workflow no longer takes
  the sender from the message text (part of D9). ADR-011.
  `tests/unit/test_automated_mail.py`, `tests/integration/test_automatic_reply.py`,
  `tests/integration/test_workflow_replies.py`.
- **D13 -- an approval held forever, and for anything** (`aba154e`). A
  request stayed decidable indefinitely, and the decision named only the
  action, so an invoice changed in between was approved unseen. A request
  now has a deadline, the approval names a fingerprint of the proposal and
  invoice it was shown, and execution checks it again. The workflow reports
  an approval that does not end in an execution (`2869608`).
  `tests/integration/test_approval_binding.py`.


---

## Production considerations

**Idempotency.** The key is derived from what the action *is* — provider,
invoice, type, amount, currency — never generated. A retry after a timeout
finds the original credit note instead of issuing a second one.

**Writes are never retried blind.** Each mutating provider call, credit note
or refund, carries its key rather than relying on the caller being careful,
and the one that runs is the one the human approved. Reads are declared safe
to retry by the port; no adapter retries them yet.

**Immutable run rows.** Approval and execution are a separate lifecycle in
their own table, joined on `run_id`. Amending a run in place would erase the
difference between what was decided then and what happened to it since.

**Untrusted input.** The dispute message is screened by pattern before any
model sees it, and the context that reaches the engine is pseudonymised — a
reference outside the aliases we issued is dropped rather than trusted.

**No dispute is dropped for its length.** Past 10,000 characters a message is
not sent to the engine -- a cost and context bound -- but the run still
happens: the rules run on the billing context, the audit records it, and a
person is alerted to read it (`MESSAGE_TOO_LONG`). Past 200,000 the service
refuses, and the workflow's error branch mails a person rather than ending
in silence. Stripping quoted history and signatures belongs upstream, in the
support tool (ADR-010).

**Cost is an estimate.** It applies a dated list price, not an invoiced
amount, which is why the reconciler above checks it against the bill, per day
and model — the same problem this project solves on customer invoices, turned
on our own consumption.

---
