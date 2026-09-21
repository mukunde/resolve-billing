# ADR-004 — Human approval for financial mutations

**Status:** accepted · 2026-09-20 · revised 2026-09-21

## Context

A decision engine returns a confidence. It is tempting to gate money on it:
above 0.95, issue the credit note.

The number does not support that. A model announcing 0.97 is stating its
opinion of itself. Nothing establishes that it is right 97 times in a hundred
— only measurement on a real case set can, and that measurement does not exist
here yet. Gating money on an unmeasured number is gating it on nothing.

Separately, a system that can move money is a system whose blast radius is set
by whatever component decides. If that component is the engine, then changing
the engine changes the blast radius.

## Decision

The engine **recommends**. `ActionPolicy` **authorises**. They are separate
objects, and the policy never reads the customer's message — nothing a
customer writes should be able to widen what the system may do to their
account.

```text
read-only                         always allowed
reply or request for information  automatic; the draft is reviewed before sending
credit note, any amount           human approval
refund                            human approval
blocking finding (injection)      human review, no action proposed
classification `unknown`          human review
money on an unqualified dispute   human review
```

Autonomy thresholds exist in the code, are configurable, and are **off**.

The lock is not the switch. `AutonomyPolicy.required_confidence_kind` defaults
to `calibrated`, so even with `enabled=True` a self-reported 0.99 does not
clear it. There is a test for exactly that, and another showing what a
calibrated score under the thresholds would buy.

Approval and execution are two steps. Approving records *who*; executing rings
the provider. "Who authorised this" and "did it go through" are different
questions for an auditor, and a provider outage should never cost a second
human decision.

The approval happens on a page the API serves, reached from a notification
email. Not on a link in the email itself: a GET link is followed by mail
scanners and antivirus gateways, so a one-click approve URL gets exercised by
a scanner rather than a person. Decisions arrive by POST, from a form a human
submitted, and a test asserts that a GET moves nothing.

## Consequences

Swapping the engine, or having it go wrong, cannot widen what may be executed.
An identical decision from the cheapest and the dearest model authorises the
same thing — tested, because `Decision` does carry `telemetry.model_id` and
the guarantee is behavioural rather than structural.

A blocking finding outranks everything. The strongest test in the suite gives
the policy a genuine duplicate, a calibrated confidence of 1.0 and a known
amount — everything says *issue the credit note* — plus an injection attempt.
The finding wins.

One departure from a literal reading of the plan, argued in the module:
requesting information from a customer does not need a human. It is not an
action on their account, it is the safe non-action, and the draft is reviewed
before sending like every other reply. Routing it to a person would put a
human in the loop of "we need more information", which buys nothing and trains
reviewers to click through. Moving money on an unqualified dispute always
needs one.

The identifier field on the approval page is **not** authentication. In
production that route sits behind the company's SSO. The page says so.

What is missing and named: expiry. A request that sits for three weeks should
not be approvable by someone who has forgotten the context, and above some
amount a second approver should be required. The policy is already shaped to
express both.
