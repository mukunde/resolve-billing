# ADR-010 — Target integration: the support tool owns the case, a durable workflow owns the lifecycle

**Status:** accepted as the target · 2026-10-08 · **not built**

## Context

The demonstrator runs as follows:
- n8n receives a dispute by webhook and calls the API synchronously;
- the API investigates and queues an approval in SQLite;
- n8n mails the approver and waits;
- the approver decides on a page the API serves;
- n8n resumes and asks for the write.

That was the right size for showing the decision architecture. It is the
wrong shape for production, and two code reviews and a production-readiness
question made the gaps concrete:

- **The customer channel is ours.** Mail arrives here directly, so this
  system ends up handling spam, threading, quoting, attachments and sender
  identity. Support tools already do all of that.
- **The lifecycle is held together by hand.** An approval can wait for days,
  must be able to expire, and its execution must survive a restart. ADR-008
  built a state machine for that. n8n's Wait node is a convenience, not a
  durable workflow, which is why this project already forbids it to hold
  state.
- **There is no stable identity for a dispute.** The idempotency key cannot
  be tied to the dispute it corrects (ADR-008, D6).

ADR-006 wrote the condition under which this flow would move to a
checkpointed orchestrator: mid-run resumption across more than a handful of
steps. A human approval that waits for days, then an exact-once write with
reconciliation, is that case.

## Decision

The target integration has four parts. It is stated here, and not built.

**1. The case lives in the existing support tool** (Zendesk, Salesforce
Service Cloud, ServiceNow, or whatever the company runs).
- **Ingestion.** A billing-tagged ticket triggers ResolveBilling through an
  idempotent `POST /cases` keyed on the ticket id.
- **Writing back.** ResolveBilling posts back an internal note (the findings,
  the proposal and its evidence), a draft reply, and status changes.
- **What it never owns** is the customer channel. Spam, threading,
  attachments and contact identity stay where they are already handled.
- **The ticket id is the stable dispute identity** that the idempotency key
  needs (ADR-008, D6), and that a replayed request keeps.

**2. The lifecycle runs in a durable workflow engine.** Temporal is the
reference: one workflow per case.

| step | as |
|---|---|
| load the billing context, run the rules, call the decision engine | activities; the model's answer is recorded in the workflow's history |
| wait for a human decision | a signal, with a timer that expires the approval |
| execute the correction | an activity under ADR-008: idempotent, `UNKNOWN_OUTCOME` reconciled through `find_action` |
| report to the ticket | an activity |

Two limits to keep in mind:
- **Deterministic replay covers the workflow code, not the model.** The
  model call is an activity whose result the history records, so a replay
  re-reads the decision instead of asking again. That is also the audit
  property this project wants.
- **Activities run at least once, not exactly once.** ADR-008's discipline
  stays: an idempotency key on every write, and no blind retry after an
  unknown outcome. Temporal hosts the state machine; it does not replace
  it.

Camunda is the alternative where the company already models human tasks in
BPMN.

**3. The billing platform computes and caps** (ADR-008, "The platform is
authoritative on amounts and limits"). ResolveBilling keeps no ledger.

**4. Approval happens behind the company's identity provider.** That means
the support tool's own approval step, or this system's page behind SSO. The
approver's identity comes from authentication, never from a form field
(ADR-009, to come).

```text
support tool              ResolveBilling (durable workflow per case)        billing platform
ticket, contact,  ──►  investigate · findings · proposal           ──►   computes, caps,
channel, spam          │ human approval: signal + expiry timer              issues the document
       ▲               │ execute once, reconcile an unknown outcome
       └── internal note, draft reply, status ◄──┘
```

ResolveBilling sits where an investigation takes a person tens of minutes,
and around the decision that a person keeps. It replaces neither the support
tool nor the billing platform.

## Consequences

**Some defects found in review move upstream once this target is reached.**
Not replying to spam (D11) and long or quoted messages (D12) become the
support tool's job. The sender's identity largely does too, although
checking that a ticket's contact matches the account it names (D9) stays
here. Until then, each is handled or documented in this repository.

**What is built now has to survive the move.**
- Kept, because they hold whatever the orchestrator: the execution state
  machine (ADR-008), service authentication and the approver's identity
  (D7), input limits (D12), and approval expiry tied to the proposal shown
  (D13).
- Fixed minimally: the n8n resume URL (D8), because it is code this target
  removes.

**n8n stays the demo orchestrator.** Migrating costs a running cluster or a
paid service, and learning time that the current plan does not have. A
short, time-boxed spike that moves the approval lifecycle into a Temporal
workflow is the way to test this ADR before adopting it.

**Re-evaluation condition:** a pilot with a real support tool and a real
billing platform. At that point this target stops being a description and
becomes the work.
