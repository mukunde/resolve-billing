# ADR-011 — Automated mail, and who may receive an automatic reply

**Status:** accepted · 2026-10-19 · fixes D11; amends ADR-007

## Context

The policy verdict answers one question: does an action on the customer's
account need a person? A reply or a request for information moves no money,
so the verdict is `AUTO`, and until now that was read as permission to answer
automatically. It is a second question, and it was never asked.

Answering it wrongly has three known costs:

- **Mail loops.** An automatic reply to an auto-responder gets an automatic
  reply back. RFC 3834 exists because of this: automated mail is marked, and
  a responder must not answer what is marked.
- **Backscatter.** A spammer forges the sender; the reply goes to whoever the
  address really belongs to.
- **Talking to strangers.** ADR-007 asked an unknown sender to write from the
  address we know -- automatically. Without authentication results a gateway
  vouches for, the system cannot tell a colleague from a forged address.

No reply was actually sent: the workflow's automatic-reply branch is a no-op
and no reply drafter exists. The contract was wrong all the same, and it
would have become behaviour the day a reply channel was connected.

## Decision

**Two questions, two answers.** The policy verdict still says whether an
action needs a person. A separate, deterministic computation, after the
policy, says whether an automatic reply may be sent: `automatic_reply_allowed`,
with a stable reason. It is true only when all of these hold, and false by
default:

| Condition | Reason when it fails |
|---|---|
| no automated-mail finding | `automated_message` |
| the sender is a known customer | `unknown_sender`, `suspected_impersonation` |
| the verdict is `AUTO` | `needs_a_person` |
| the action is a `reply` or a `request_information` | `nothing_to_send` |

Downstream, an automatic reply is sent on `automatic_reply_allowed` and on
nothing else.

**Automated mail is recognised by rule** (ADR-003), from optional mail headers
the intake forwards and from the sender address. The rule means "do not reply
automatically", not "spam":

| Finding | Signals | Effect |
|---|---|---|
| `AUTOMATED_RESPONSE` | `Auto-Submitted: auto-replied`; `Precedence: auto_reply`; a null `Return-Path` (`<>`); a `MAILER-DAEMON` or `postmaster` sender | an auto-reply or a delivery report: nothing to investigate. The engine is not called, nothing is proposed, nobody is alerted, the run is recorded |
| `AUTOMATED_MESSAGE` | any other `Auto-Submitted` value but `no`, malformed ones included; `Precedence: bulk`, `junk` or `list`; `List-Id`; `List-Unsubscribe`; `X-Auto-Response-Suppress` asking to suppress replies | from a known sender, investigated as usual -- a real invoice can come from software -- and never answered automatically. From an unknown sender there is nobody to ask: nothing is proposed, nobody is alerted |

Header names are compared without case, values are trimmed, and a header
repeated with contradictory values counts as automated if any value says so.

**An unknown sender never gets an automatic reply** (amends ADR-007). The
request to write from the address we know is still what the system proposes,
but a person sends it, or not: the workflow mails it to a triage mailbox,
separate from the approvers.

**Order on an unknown sender:** a prompt injection, then an automated
response, then a suspected impersonation, then an automated message, then the
request to a person. Impersonation outranks bulk mail on purpose: a marketing
mail that names a customer costs one review; a fraud attempt that looks like
marketing costs more.

**The sender is never read from the message text.** The workflow used to fall
back on the first address found in the body; a message quoting a customer's
address became that customer's message. Without a sender, the API refuses the
request and the workflow's error branch mails a person. (This is the part of
D9 this change needs; the rest of D9 stays open.)

## Consequences

Headers are untrusted input, and the design holds anyway: a header can only
take an automatic reply away. None can grant one, call the engine, or widen
what the policy allows.

A sender whose software marks real mail as automated loses the automatic
reply, not the investigation, when they are a known customer. An unknown
sender in that situation is recorded and nobody is alerted -- the accepted
cost of never answering robots.

Without headers -- a source that does not forward them -- only the sender
address check applies. Spam filtering belongs to the support tool in the
target integration (ADR-010); this rule stays as defence in depth.

`Authentication-Results` is deliberately not read. Only a gateway the system
trusts can vouch for it, the intake has none yet, and an absent result is not
a pass (RFC 8601). When the support tool or a trusted gateway supplies
authentication results, an authenticated unknown sender could get the
automatic request back -- a decision to take then, with this ADR amended.

The evaluation runner counts an automatic reply allowed to a sender who is
not known, or on a case that forbids one, as an unsafe action, and fails the
build on it like any other.
