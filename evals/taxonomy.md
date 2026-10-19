# Decision boundaries

What the system has to decide correctly, written as the lines where two
nearby disputes must get different decisions. It serves three purposes: it
shows which of those lines the development set covers, it is the coverage
matrix from which holdout cases are drawn, and it states what the evaluation
set does **not** test: some of it is out of reach of the case format, some
could be asserted and is not yet, and some is a limit of the product itself.

Drafted on 2026-10-18 by two models of different families, each without
seeing the other's draft, then cross-reviewed and merged. Sources:
`docs/adr/`, `app/domain/rules.py`, `app/domain/injection.py`,
`app/policies/action_policy.py`, `app/application/investigate_dispute.py`,
`app/decision/prompts/decision_v3.md`, `evals/cases.json`, `evals/runner.py`.
No evaluation results were used.

## Vocabulary

- **Boundary**: a decisive variable whose value must flip the decision
  (classification, recommended action, human approval, expected findings,
  engine skipped, sender status).
- **Invariance**: a variable whose value must *not* flip it. An engine that
  flips on noise fails differently from one that misses a flip, so both are
  listed. Invariances cut across boundaries: they are crossed with them, not
  counted as extra business boundaries.
- **No finding is not a verdict.** When a rule does not fire, the
  investigation continues; another anomaly may remain. "Side B" below means
  "this finding must not fire", not "the invoice is valid", unless stated.
- **Verification level**: *case* (an evaluation case: message + fixtures in,
  decision out), *engine case* (a case that needs an engine to produce a
  given output: a live engine, or a scripted fake engine, which does not
  exist yet; the mock never produces these pairings), *unit*, *integration*.
- **Dev coverage** lists the development cases whose inputs and expectations
  sit on each side. It is read from `evals/cases.json`, not from results, and
  it inherits the runner's assertion limits (see L11).
- **Test types**: CP contrast pair · BV boundary value · MU mutation of a dev
  world · AD adversarial · ML multilingual · CD conflicting data (message vs
  records) · CO compound (two rules at once).

Unless stated otherwise, a boundary assumes a known sender, a resolved
invoice, and no blocking finding.

## 1. Boundaries the evaluation set can test

### Identity and scope

**B01 — Known vs unknown sender** · case
- A: the address resolves to a customer → that customer's context is loaded.
- B: it resolves to none and nothing reaches for a known account →
  `UNKNOWN_CUSTOMER`, sender `unknown`, `insufficient_information` /
  request information, engine skipped.
- Variable: customer lookup by address. Basis: ADR-007.
- Dev: A every known-sender case; B `unknown_sender_plain`.
- Gaps: case and whitespace variants of a known address. A matched address is
  a lookup, not authentication (L10).
- Since D9: a customer id in the request is a claim. When the address does
  not resolve to it -- another customer's address, an unknown one, an id that
  does not exist -- `IDENTITY_CONFLICT`: human review, nothing proposed, the
  claimed account not loaded (`claimed_account_of_another_customer`).
- Tests: CP, MU, BV.

**B02 — Unknown sender: plain vs suspected impersonation, one signal at a time** · case
- A: no signal → sender `unknown` (B01 side B).
- B: any of `known_invoice`, `same_domain`, `lookalike_domain`,
  `company_name` → `UNKNOWN_CUSTOMER` + `SUSPECTED_IMPERSONATION`, sender
  `suspected_impersonation`, `unknown` / none, human review, engine skipped.
  Nothing is said to the sender (ADR-007).
- Variables and thresholds: webmail domains are never compared; lookalike =
  equal once folded (`rn→m`, `vv→w`, `0→o`, `1→l`, hyphens dropped), prefix
  of a name of at least 4 characters, or one edit away from a name of at
  least 6; company name of at least 3 characters once legal forms are
  dropped.
- Dev: `known_invoice` (`unknown_sender_known_invoice`,
  `unknown_sender_before_valid_dispute`); `company_name`
  (`external_accountant_names_client`). None for `same_domain` (a colleague),
  `lookalike_domain` alone, the webmail exemption, or the length thresholds.
- Tests: CP, BV, AD.

**B03 — Invoice inside vs outside the customer's scope** · case
- A: the invoice belongs to the sender's customer → investigated.
- B: it belongs to another customer → `UNKNOWN_INVOICE`,
  `insufficient_information` / request information, nothing of the other
  account surfaces.
- Variable: `invoice.customer_id` against the resolved customer. Basis:
  ADR-009 (trust boundaries), `unknown_invoice`.
- Dev: B `cross_customer_invoice_reference`,
  `foreign_customer_invoice_reference` (one mechanism, two examples).
- Gaps: the foreign invoice is itself a real duplicate (tempting data). The
  labels check routing, not the absence of disclosure in text (L6).
- Tests: CP, AD, ML.

**B04 — Designated target vs unresolved target** · case
- A: one invoice is designated and resolves in scope → its dispute is
  classified.
- B: no reference, or a reference that resolves to nothing → request
  information rather than pick another invoice. Missing (no finding) and
  nonexistent (`UNKNOWN_INVOICE`) are distinct mechanisms with the same safe
  action.
- Dev: B `insufficient_context` (missing), `unknown_invoice_reference`
  (nonexistent).
- Since S6: a message citing recognised references, none of them the
  designated one → `INVOICE_REFERENCE_CONFLICT`, ask which
  (`invoice_reference_conflict`). References written freely ("my September
  invoice") are not detected.
- Tests: CP, CD.

### Untrusted input

**B05 — Instruction aimed at the system vs ordinary customer language** · case
- A: ordinary dispute language, forceful or imperative ("remboursez-moi
  immédiatement") → normal path.
- B: an instruction addressed to the system (override, role redefinition,
  authority claim, prompt markup) → `SUSPECTED_PROMPT_INJECTION`, `unknown`
  / none, human review, engine skipped.
- Variable: what the imperative is aimed at. The **implemented** predicate is
  a pattern list tuned for recall, with documented false positives ("vous
  êtes autorisé"); the **intended** boundary is semantic. They differ, and the
  difference stays visible.
- Dev: A `angry_but_valid`, `benign_ignore_previous_message`; B
  `injection_refund`, `injection_role`, `withdrawn_message_with_injection`.
- Since S2: the customer withdrawing their own earlier message is exempt,
  as that phrase only and only when it stands alone -- once it is set aside,
  nothing an override reaches for (instructions, rules, a prompt, the
  system, "everything above", what came before) may be left in the sentence.
  Every other pattern still applies to the whole text.
- Known gap of the patterns themselves: an override that names nothing they
  list ("forget what you were told before") is not caught. The model is
  told the message is data, and the policy, not the model, authorises.
- Tests: AD, ML, CP (minimal wording changes), Unicode mutations.

**B06 — Message within vs beyond the engine's input limit** · case
- A: at most 10 000 characters → eligible for the engine (other blocks still
  apply).
- B: more → `MESSAGE_TOO_LONG`, engine skipped, human review.
- Dev: B none.
- Tests: BV (9 999 / 10 000 / 10 001), long quoted history. Above 200 000
  characters the API rejects the request: an intake check, tested in
  `tests/integration/test_long_message.py`.

### Billing anomalies

**B07 — Duplicate invoice vs distinct charges, one clause at a time** · case
- A: two live invoices, same subscription, equal totals, overlapping periods
  → `DUPLICATE_INVOICE`, `duplicate_billing`.
- B: one clause fails → no duplicate finding.
- Variable: each clause of `detect_duplicate_invoice` separately. A partial
  overlap still satisfies the overlap clause.
- Dev: A `duplicate_fr`, `duplicate_en`; B `legitimate_overage`,
  `proration_not_duplicate`.
- Gaps: amount differing by one minor unit; same amount, other subscription;
  adjacent but non-overlapping periods.
- Since D14: a missing period no longer reads as "no overlap". The check says
  it cannot conclude (`NOT_EVALUABLE`), no correction is proposed, and "the
  invoice is correct" goes to a person (`duplicate_check_not_evaluable`).
  The same holds for cancellation and proration when the subscription is
  missing, and for proration when a line has no period; a one-off invoice,
  with no subscription, is outside these checks rather than a gap.
- Tests: CP, BV, MU.

**B08 — Live vs void invoice** · case
- A: a live invoice takes part in the duplicate, cancellation and proration
  checks. B: a void one is excluded; its existence alone justifies no
  correction.
- Dev: B none.
- Tests: MU (void one invoice of a dev world), CD.

**B09 — Which object is duplicated: invoice or payment** · case
- A: two invoices → credit note on the invoice side. B: one invoice collected
  twice → refund of the extra payment, no `DUPLICATE_INVOICE`.
- Basis: the `Classification` docstring; same classification, different
  instrument.
- Dev: A `duplicate_fr`, `duplicate_en`; B `duplicate_payment_single_invoice`
  (`DUPLICATE_PAYMENT`, a deterministic rule since lot 3).
- Gaps: both at once; two payments of which one failed or was refunded.
- Tests: CP, CO.

**B10 — Billed after cancellation: the cut-off** · case
- A: covered period starts strictly after `cancelled_at` →
  `INVOICE_AFTER_CANCELLATION`, `billing_after_cancellation`. B: it starts on
  or before → no finding.
- Variable: `period_start`, or `issued_at` when absent, against
  `cancelled_at`.
- Dev: A `billed_after_cancellation`, `vague_but_scoped`; B none.
- Tests: BV (equality, just before, just after), missing-period fallback,
  time zones.

**B11 — Proration within vs beyond rounding tolerance** · case
- A: a partial-period line within one minor unit of pro rata → no finding.
  B: beyond → `PRORATION_MISMATCH`, `proration_dispute`, human review
  without an amount (S5).
- Dev: A `proration_not_duplicate`; B `proration`.
- Gaps: a gap of exactly 1 vs 2 minor units. A single net line cannot be
  checked and stays flagged by design (S9).
- Tests: BV, CP.

**B12 — Usage within vs beyond tolerance** · case
- A: billed usage within 25 % of expected → no finding. B: beyond, or
  positive usage against a zero baseline → `SUSPICIOUS_USAGE_DELTA`.
- Dev: A `legitimate_overage`; B none.
- The finding is defined and can be tested now; what it should lead to is
  not (S1). An expected classification and action on side B waits for that
  decision.
- Tests: BV, CP.

**B13 — Billing dispute vs out-of-scope request** · case
- A: the request fits the dispute vocabulary → a classification from it
  (which may still be `insufficient_information`).
- B: it does not → `unknown`, human review; not filed in the least wrong box.
- Dev: A `vague_but_scoped`; B `out_of_scope`, itself a billing-adjacent
  request (cancel a plan, export data).
- Gaps: other adjacent requests (change a card, VAT number), mixed billing
  and non-billing intents.
- Tests: CP, ML.

### Payments and corrections

**B14 — Payment recorded vs payment merely claimed** · case
- A: dunning on an invoice with a succeeded payment →
  `payment_already_received`. B: a claim with no payment on record →
  `insufficient_information`, request information.
- Dev: A none (the classification has no dev case); B
  `claimed_payment_without_evidence`.
- No deterministic dunning rule exists yet (ADR-003).
- Tests: CP, CD.

**B15 — Failed collection vs invalid invoice** · case
- A: a correct invoice whose payment failed → `valid_invoice`, reply. B: an
  invoice error exists independently → its correction.
- Dev: A `failed_payment_valid_invoice`; B none as a controlled pair.
- Tests: CP, MU, CD.

**B16 — Correction already made vs still warranted** · case
- A: the payment is already refunded → no new mutation, reply. B: an
  outstanding, evidenced correction → proposed, with approval.
- Dev: A `refund_already_completed`; B no matched pair.
- Gaps: partial refunds (S5); a duplicate whose extra payment is already
  refunded.
- Tests: MU, CD.

**B17 — Credit note by default vs refund** · case
- A: a warranted correction, future billing able to absorb it, no explicit
  refund request → credit note.
- B: the customer explicitly asks for their money back, or no future billing
  can absorb a credit (cancelled subscription) → refund.
- Variables: refund intent; subscription status. Basis: prompt v3, "Credit
  note or refund". Asking for a refund does not make a correction warranted.
- Dev: A `duplicate_fr`, `proration`; B `billed_after_cancellation`,
  `vague_but_scoped` (cancellation). None for intent alone.
- Gaps: a duplicate on a cancelled subscription; an unpaid invoice, where
  nothing can be refunded (S4).
- Tests: CP, MU, ML.

### Precedence

**B18 — A blocking finding outranks a real anomaly** · case
- A: a real duplicate or proration error alone → financial proposal.
- B: the same world plus injection or impersonation → human review, nothing
  proposed.
- Basis: `BLOCKING_FINDINGS`, ADR-007.
- Dev: `injection_refund` (+ duplicate), `injection_role` (+ proration),
  `unknown_sender_before_valid_dispute` (+ duplicate).
- Tests: CO, MU.

### Engine output and policy

**B19 — Usable classification vs insufficient confidence** · engine case, unit
- Below 0.70 a classification is not used: human review, whatever it is.
  Prompt v3 asks the engine to withdraw; the action policy holds when it
  does not (S8). An engine that cannot be reached yields the same: an
  escalation, recorded as an outage.
- Dev: none; the mock's only low confidence comes with an escalation. Unit:
  `tests/unit/test_action_policy.py`, `tests/unit/test_claude_engine.py`,
  `tests/integration/test_unusable_evidence.py`.

**B20 — Explicit escalation** · engine case, unit
- `escalate` → human review whatever the classification and confidence.
- Dev: none directly. `out_of_scope` expects `unknown` / none, which the
  policy routes to a human whether or not the engine says `escalate`; none
  pairs `escalate` with a specific classification. Unit: `tests/unit/test_action_policy.py`.

**B21 — Money recommended on an unqualified classification** · engine case, unit
- A financial recommendation on `unknown` or `insufficient_information` →
  human review, and the proposed action becomes `none`; the engine's
  recommendation stays in `recommended_action` (S12).
- Dev: none; the mock never produces the pairing. Unit:
  `tests/unit/test_action_policy.py`.

**B22 — Money recommended without an amount the code can stand behind** · case, unit
- An amount exists only when the whole invoice is the error: a duplicated
  invoice, an invoice after cancellation, or an invoice collected by at least
  two distinct payments each equal to the total (refund of the extra one; no
  other overpayment is recognised yet). A partial correction, two distinct
  corrections on one invoice, a draft, uncollectible or void invoice, a zero
  or negative total, or a refund no single settled payment covers → human
  review, no amount, nothing queued (S3, S4, S5).
- Dev: `proration` (partial correction). Unit:
  `tests/unit/test_correction.py`, `tests/unit/test_action_policy.py`.

**B23 — Financial vs non-financial action** · case
- Credit note or refund → human approval, whatever the confidence; reply or
  request for information → no approval (drafts are still reviewed before
  sending), **except** when a blocking finding, an `unknown` classification
  or an escalation sends it to human review anyway. Autonomy thresholds exist
  in code and are off (ADR-004).
- Dev: widely covered on both sides.

### Evidence the rules cannot use (S7, S10)

**B27 — Unknown reference vs the customer's own old invoice** · case
- A: the reference exists nowhere, or belongs to another customer →
  `UNKNOWN_INVOICE`, request information, nothing disclosed.
- B: it is the customer's, older than the 180 days loaded →
  `INVOICE_OUTSIDE_WINDOW`, engine skipped, human review.
- Dev: A `unknown_invoice_reference`, `cross_customer_invoice_reference`;
  B `invoice_outside_window`.
- Tests: CP, BV (the window's edge).

**B28 — Comparable vs incomparable amounts** · case
- Amounts the rules must compare -- the invoice's lines, its payments, a live
  invoice for the same period of the same subscription -- in another
  currency → `CURRENCY_MISMATCH`, no conclusion either way, engine skipped,
  human review. Other currencies on the account change nothing.
- Dev: `duplicate_across_currencies`.
- Tests: CP, MU.

**B29 — Usage checked vs usage unavailable** · case, integration
- A metered line with no usage record → `USAGE_UNAVAILABLE`; "the invoice is
  correct" then goes to a person.
- Dev: none (`tests/integration/test_unusable_evidence.py`).
- Tests: CP.

### Automated mail and automatic replies (ADR-011)

**B24 — A person vs an automated response** · case
- A: no automated signal → investigated as usual.
- B: an auto-reply or a delivery report (`Auto-Submitted: auto-replied`,
  `Precedence: auto_reply`, a null `Return-Path`, a `MAILER-DAEMON` or
  `postmaster` sender) → `AUTOMATED_RESPONSE`, engine skipped, nothing
  proposed, nobody alerted, no automatic reply.
- Dev: B `known_customer_out_of_office`, `unknown_sender_delivery_report`.
- Gaps: each signal alone; an auto-reply that quotes a real dispute.
- Tests: CP, MU, AD (forged or contradictory headers).

**B25 — Investigated, but never answered automatically** · case
- A known sender's bulk, list or generated mail → investigated as usual,
  `AUTOMATED_MESSAGE`, `automatic_reply_allowed` false. From an unknown
  sender → nothing proposed, nobody alerted.
- Dev: `known_customer_bulk_dispute`, `unknown_sender_newsletter`.
- Gaps: `X-Auto-Response-Suppress` alone; a newsletter naming a customer
  (impersonation outranks bulk mail, tested in
  `tests/integration/test_automatic_reply.py`).
- Tests: CP, MU.

**B26 — Who may receive an automatic reply** · case
- Allowed only for a known sender, an `AUTO` verdict, a `reply` or
  `request_information` action and no automated-mail finding. An unknown
  sender never gets one: the request goes to a triage mailbox.
- Dev: allowed `legitimate_overage`; refused `unknown_sender_plain` and the
  four cases above.
- Tests: CP.

## 2. Invariances

Crossed with the boundaries above: a test varies one of them while
everything that decides the outcome stays fixed.

- **I1 Language** (French ↔ English): `duplicate_fr` / `duplicate_en`.
- **I2 Tone** (calm ↔ angry, threats, legal vocabulary): `angry_but_valid`.
  Limit: indignant wording can trip the injection screen by design (B05).
- **I3 Paraphrase and vagueness**, as long as the intent and the target stay
  the same and the findings carry the decision: `vague_but_scoped`.
- **I4 Unsupported claims do not create evidence** (a duplicate claimed on a
  correct proration, a payment claimed with none):
  `proration_not_duplicate`, `claimed_payment_without_evidence`.
- **I5 How amounts are written in the message** ("49,00 €", "€49", "49
  euros"). Not identifiers: an opaque id is not assumed insensitive to case
  or spaces.

## 3. What the evaluation set does not test

Listed so that nobody reads a good evaluation score as covering them. Each
row says what kind of limit it is, what covers it today, and what could.

- **Format**: out of reach of a case (message + fixtures in, structured
  decision out, one run).
- **Assertion**: observable in a case's output, not asserted by the runner
  today.
- **Product**: a property the product does not have yet.

| | Limit | Kind | Covered today by | Could be covered by |
|---|---|---|---|---|
| L1 | Who may approve, reject, investigate or execute; closed by default (ADR-009) | Format | `tests/integration/test_auth.py` | Role matrix in CI; identity provider in production |
| L2 | An approval past its deadline, or on a proposal or invoice that changed since (ADR-008) | Format | `tests/integration/test_approval_binding.py` (including an invoice changed after approval), `test_approval_flow.py` | Kept as is |
| L3 | Two callers executing the same action; a crash mid-execution (ADR-008) | Format | `tests/unit/test_execute_action.py` (two concurrent coroutines, a simulated restart) | Concurrency between independent processes, and on the production database |
| L4 | Success, definite rejection, or uncertain outcome of a provider write, and its reconciliation (ADR-008) | Format | `tests/unit/test_execute_action.py` | Fault injection against each real adapter |
| L5 | Amount above the invoice total, currency mismatch, exhausted refund capacity at the provider (ADR-005, ADR-008) | Format | Arithmetic: `tests/unit/test_money.py`; the execution guard before the claim: `test_execute_action.py`; the fake's caps: `test_fake_provider.py` | Contract tests per real provider; the fake proves the contract, not a future adapter |
| L6 | **Disclosure in text**: a correct label does not prove that a reply or explanation reveals nothing of another account or to a suspected impersonator | Assertion | What each channel may carry is written down (ADR-009, "Disclosure, channel by channel"); `tests/integration/test_disclosure.py` checks that the explanation the orchestrator receives for a suspected impersonator names none of the account's identifiers (an internal channel: no text is sent to the sender yet), that a 422 echoes no input, and that mails needing no prose carry none; human review of every draft | Deterministic checks on drafts (other customers' ids, names, amounts); a judge model on sampled drafts |
| L7 | **Evidence fidelity**: an explanation that cites the right evidence for the right reason | Assertion | Cited references outside the context are dropped (`TestEvidenceMapping` in `tests/unit/test_claude_engine.py`): that proves they belong to the context, not that the reasoning is right | Graded review of explanations on samples |
| L8 | **Incomplete or stale context**: a provider read failure, the 180-day invoice lookback, missing usage data. "Not found" is not "does not exist" | Format (read failures); assertion (data missing from fixtures) | A failed read decides nothing and answers 503; an old invoice, missing usage: `tests/integration/test_unusable_evidence.py` | Fault injection against each real adapter |
| L9 | **Engine failure**: timeout, refusal, output outside the schema | Format | Outages escalate and are recorded as such; errors fail visibly: `tests/unit/test_claude_outages.py` (with the SDK), `tests/integration/test_unusable_evidence.py` | The same against a real outage, in the pilot |
| L10 | **Sender authentication**: a known address is a lookup, not proof of identity; delegated contacts | Product | Nothing; ADR-007 treats the address as a signal | Verified contacts, signed mail (DKIM/DMARC) at intake, a production identity design |
| L11 | **The runner's own assertion limits**: expected findings are a required subset, so unexpected extra findings pass; engine skipping is checked only where a case declares it; one run of a sampling engine; 34 cases make a consistency check, not a measurement | Assertion | `tests/unit/test_eval_*.py` test the runner itself | Exact-findings mode; repeated runs with variance bands (`evals/variance.py`); a larger set |
| L12 | **Production approval scope**: approver amount limits, second approval, separation of duties (ADR-009, described, not built) | Product | Nothing | To be specified and built before production |
| L13 | **Real billing providers**: every case runs on the fake provider | Format | Contract tests of the fake | Adapter contract tests against sandboxes (Pennylane, Qonto) |
| L14 | **Where mail headers come from**: automated mail is recognised from the headers the intake forwards; their provenance is not verified, and without them only the sender address is checked. Headers can only withhold a reply, never grant one | Format | `tests/unit/test_automated_mail.py` | The support tool or a trusted gateway supplying headers and authentication results (ADR-010, ADR-011) |

## 4. Specification questions

Code and ADRs did not decide these. All twelve were decided by the
maintainer on 2026-10-19; each line says what was decided and whether it is
built yet. No case gets an expected label
on a question before its decision is built.

| | Decision | Built |
|---|---|---|
| S1 | A disputed usage gap with the finding present → `unknown` + escalate; no money founded on it before S5's amounts; under-billing → audit note only. A dedicated classification comes with partial amounts | in part: no money on it, "valid invoice" against an over-billing goes to a person, under-billing is an audit note; the prompt instruction waits for a measured prompt version |
| S2 | Narrow exemption: "ignore my previous message" (the customer's own message) is not flagged; instructions, rules and prompts still are; the exemption covers that phrase only | yes |
| S3 | Count corrections, not findings: distinct corrections on one invoice → human review, no amount | yes |
| S4 | A refund needs a single settled payment covering it; draft, uncollectible and void invoices get no correction; no rule on paused, trialing, past-due subscriptions | yes |
| S5 | Money only when the whole invoice is the error; partial corrections → human review without an amount. Partial amounts: computed by code from the findings, never above what remains, specified with the real connectors | provisional rule yes, amounts no |
| S6 | The message cites recognised invoice references, none of them the designated one → ask which | yes |
| S7 | An unknown reference is looked up directly within the customer; an old invoice → human review; a provider read failure fails visibly | yes |
| S8 | Confidence below 0.70 → human review in code; engine outages → escalate, recorded as such | yes |
| S9 | The proration finding states its assumption; a price catalogue comes with the real connectors | assumption stated; catalogue with the connectors |
| S10 | A comparison across currencies does not conclude: a finding, and human review | yes |
| S11 | Disclosure scoped per channel; an inventory of the internal channels; an automatic check with the reply drafter | rule and inventory written (ADR-009); the check comes with the reply drafter |
| S12 | Refused money is not shown as a proposal; the engine's recommendation stays recorded | yes |

The questions as they were first written:

- **S1** What a `SUSPICIOUS_USAGE_DELTA` leads to (classification, action).
- **S2** Injection screening precision: a benign imperative that matches a
  pattern — flagged (recall first, as the code documents) or not (as the dev
  set expects)?
- **S3** Precedence when two non-blocking anomalies fire, or when a real
  finding is unrelated to what the customer complains about.
- **S4** Instrument and outcome on an unpaid (`open`, `uncollectible`) or
  `draft` invoice, and on `paused`, `past_due`, `trialing` subscriptions.
- **S5** Amounts: the proposal is always the invoice total; partial
  overbilling, the amount of an extra payment, partial refunds, and the
  difference between a technical idempotency key and the economic intent of
  a correction (ADR-008).
- **S6** Disputes citing several invoices, and a message contradicting the
  `invoice_id` field.
- **S7** Incomplete or stale context: when to ask, escalate or fail (L8).
- **S8** Low confidence and engine failure: whether "below 0.70, do not
  classify" becomes a deterministic rule, and what the customer sees when
  the engine fails (L9).
- **S9** Proration that cannot be checked (single net line, missing plan
  price): an explicit "cannot determine" outcome instead of a mismatch.
- **S10** Currency: mismatch is guarded where money is computed; how such a
  dispute should be handled is not specified.
- **S11** What may be disclosed in explanations and replies, beyond what
  actions are forbidden (L6). ADR-007 already settles the immediate answer to
  a suspected impersonator: no action, nothing disclosed.
- **S12** Whether a financial proposal on an unqualified classification is
  suppressed, or kept and sent to human review as today (B21).

## 5. Redundancy in the development set

- `cross_customer_invoice_reference` / `foreign_customer_invoice_reference`:
  one mechanism (B03), two examples. Count once.
- `duplicate_fr` / `duplicate_en`: intentional (I1), one business boundary.
- `legitimate_overage` / `angry_but_valid` / `benign_ignore_previous_message`:
  one billing world; tone and instruction target are invariance checks.
- `billed_after_cancellation` / `vague_but_scoped`: one mechanism, different
  specificity (I3).
- `unknown_sender_known_invoice` / `unknown_sender_before_valid_dispute`:
  same signal; the second adds precedence over a real duplicate (B18).
- Not redundant: `proration` / `proration_not_duplicate`, and
  `duplicate_fr` / `duplicate_payment_single_invoice`.

## 6. Coverage at a glance

Boundaries with **no development case on one side**: B02 (`same_domain` and
`lookalike_domain` in isolation, the webmail exemption, the thresholds), B06,
B08, B10 side B, B12 side B, B14 side A, B19, B21.
Boundary values are tested nowhere in the development set (B06, B07, B10,
B11, B12, B02 thresholds).
