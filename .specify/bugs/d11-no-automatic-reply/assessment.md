# Bug Assessment: an automatic reply is authorised for automated mail and unknown senders

- **Slug**: d11-no-automatic-reply
- **Created**: 2026-10-19
- **Source**: code review finding D11 ("no automatic reply to an unknown sender
  without filtering automated mail first"), reassessed against `main` `4554a99`
- **Verdict**: valid
- **Severity**: high (no harm today, because nothing is sent; a risk of mail
  loops and backscatter the day a reply channel is connected)

Written in the format of Spec Kit's `bug` extension, by hand: Spec Kit is not
merged into `main` yet, and this record does not depend on its tooling.

## Report (summarized)

A dispute from an address that matches no customer, with no sign of
impersonation, is answered with a request to write from the known address,
authorised as an automatic action. Nothing distinguishes a person from an
auto-responder, a delivery report or a newsletter. A known customer's
out-of-office reply goes to the decision engine like a dispute, and usually
comes back as an automatic reply too.

## Symptom

**Observed.** The investigation returns `requires_human_approval: false` with
a `request_information` or `reply` action for automated mail and for every
unknown sender not suspected of impersonation (those already went to a
person), and the n8n workflow routes every such result to its
"automatic reply" branch.

**Today that branch sends nothing**: it is a no-op node, and no reply drafter
exists. The defect is in the contract -- the system declares an automatic reply
authorised where it must not be. The day a reply channel is connected (a
reply drafter, the support tool of ADR-010), the system answers spammers,
answers addresses it cannot verify (backscatter to forged senders), and loops
with auto-responders (RFC 3834).

**Expected.** No automatic reply to automated mail, ever; no automatic reply
to a sender the system cannot verify; a person sees the unknown human senders;
nobody is alerted about auto-responders, delivery reports or bulk mail, and
every one of them is still recorded.

## Reproduction

1. `POST /api/v1/disputes/investigate` with `customer_email` set to an address
   no customer holds and a message naming no customer, invoice or domain.
2. Observe `requires_human_approval: false`, `proposed_action.type:
   request_information`, `sender: unknown`.
3. In n8n, the result takes the "Reponse automatique" branch.
4. Repeat with a known customer's address and an out-of-office message: the
   engine is called, and the result takes the same branch.

## Suspected Code Paths

- `app/application/investigate_dispute.py:_ask_for_known_address` -- an
  unknown sender is answered with `request_information`, verdict `AUTO`.
- `app/application/investigate_dispute.py:_investigate` -- no check of
  whether a message is automated before the engine.
- `app/api/disputes.py:InvestigateRequest` -- the intake carries no mail
  metadata, so automated mail cannot be recognised.
- `n8n/billing-dispute-workflow.json`, node "Validation humaine requise ?" --
  `requires_human_approval: false` is treated as permission to reply.
- `n8n/billing-dispute-workflow.json`, node "Extraction deterministe" --
  **found while assessing, part of D9**: without a sender field, the first
  e-mail address found *in the message text* becomes the sender. A message
  quoting a customer's address becomes that customer's message.

## Root Cause Hypothesis

"No human approval is needed" and "an automatic reply may be sent" are two
different questions, and the system answers both with one flag. The policy
verdict says whether an *action on the account* needs a person; nothing says
whether *sending text to this address* is safe. Confidence: high.

## Proposed Remediation

**Preferred** (decided by the maintainer on 2026-10-19):

1. **Intake.** The API accepts optional mail headers; only a whitelist is
   read. n8n forwards them when its source provides them, and **stops taking
   the sender from the message text** -- with no sender field, the API refuses
   the request and the workflow's error branch mails a person.
2. **A deterministic rule on automated mail** (ADR-003), meaning "do not reply
   automatically", not "spam":
   - `AUTOMATED_RESPONSE` -- an auto-reply or a delivery report
     (`Auto-Submitted: auto-replied`, `Precedence: auto_reply`, a null
     `Return-Path`, a `MAILER-DAEMON` or `postmaster` sender). Nothing to
     investigate: the engine is not called, nothing is proposed, nobody is
     alerted, the run is recorded.
   - `AUTOMATED_MESSAGE` -- any other `Auto-Submitted` value but `no`,
     `Precedence: bulk|junk|list`, `List-Id`, `List-Unsubscribe`, an
     `X-Auto-Response-Suppress` value asking for no reply. A known sender's
     message is investigated as usual -- a real invoice can come from
     software -- but never answered automatically. From an unknown sender,
     there is nobody to ask: nothing is proposed and nobody is alerted.
3. **No automatic reply to an unknown sender**, whatever the message. The
   request to write from the known address (ADR-007) is still the proposed
   action, but it goes to a person: n8n sends it to a **triage mailbox**,
   separate from the approvers.
4. **One authority on automatic replies.** The result carries
   `automatic_reply_allowed` and a stable reason, computed by code after the
   policy: true only for a known sender, an `AUTO` verdict, a `reply` or
   `request_information` action, and no automated-mail finding. False by
   default. n8n routes on it, and on nothing else.
5. **ADR-011** records the policy; ADR-007 links to it.

**Alternatives considered**:
- Keep the automatic request to unknown senders unless an
  `Authentication-Results` header shows a DMARC failure: rejected for now. The
  intake carries no authentication results a gateway vouches for, and an
  absent header is not a pass (RFC 8601).
- A human review alert for every automated message: rejected, it buries the
  approvers under out-of-office replies.
- Filtering in n8n before the API: rejected, it leaves no trace.

**Files likely to change**:
- `app/domain/automated.py` (new), `app/domain/enums.py`, `app/domain/models.py`
- `app/application/investigate_dispute.py`, `app/policies/reply.py` (new)
- `app/api/disputes.py`
- `n8n/billing-dispute-workflow.json`, `n8n/README.md`
- `evals/cases.json`, `evals/runner.py`, `evals/check.py`, `evals/baselines/mock.json`
- `docs/adr/011-automated-mail-and-automatic-replies.md` (new),
  `docs/adr/007-unknown-sender.md`, `evals/taxonomy.md`, `README.md`,
  `docs/engineering-notes.md`

**Tests to add or update**:
- the rule, header by header: absent, `no`, each automated value, malformed
  values, repeated and contradictory headers, case and whitespace;
- a known customer's out-of-office reply: engine not called, nothing
  proposed, no alert, no automatic reply;
- a known customer's bulk message: investigated, never answered
  automatically;
- an unknown human sender: request proposed, automatic reply refused
  (`unknown_sender`), routed to triage;
- an unknown sender's newsletter and a delivery report: nothing proposed,
  nobody alerted;
- a forged `Authentication-Results` header changes nothing;
- the API's header limits;
- the workflow: no branch replies on `requires_human_approval: false` alone,
  and the sender is never read from the message text;
- evaluation cases for each of the above, and an unsafe-action rule for an
  automatic reply allowed where the case forbids it or to an unverified sender.

## Risks & Considerations

- **Mail software that marks real disputes as automated** (some ticketing or
  ERP systems set `Auto-Submitted: auto-generated`): from a known sender such
  a message is still investigated, only the automatic reply is withheld. From
  an unknown sender it is recorded and nobody is alerted -- the accepted cost of
  never answering robots.
- **Headers are untrusted input.** None can grant an automatic reply or widen
  what the system may do; an auto-reply marker can set a message aside
  without alert, which mostly silences its own author (ADR-011).
- **Metadata absent.** When the source passes no headers, only the sender
  address check applies. The upstream support tool (ADR-010) is where spam
  filtering belongs; this rule is defence in depth until then.
- **D9 is not closed.** Only the extraction of the sender from the message
  text is fixed here; matching the customer found to the sender stays open.

## Acceptance

- No result can carry `automatic_reply_allowed: true` for an unknown or
  suspected sender, or with an automated-mail finding.
- No workflow branch sends a reply unless `automatic_reply_allowed` is true.
- Every message still produces an audited run.
- The development set and the new cases pass in CI, with no unsafe action.
