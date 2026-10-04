# ADR-003 — Deterministic before probabilistic

**Status:** accepted · 2026-09-20 · revised 2026-09-21

## Context

Whether two invoices cover the same period for the same subscription is a date
comparison. It costs nothing, takes microseconds, and gives the same answer
every time. Asking a model instead buys variance, latency and a bill, and
gives away the one thing a finance team needs: an answer that does not move.

Most of what a dispute requires is of that kind. What is left — what the
customer actually means, whether the findings answer their complaint — is
genuinely a reading problem.

## Decision

Three rungs, climbed only when the one below cannot settle the question.

```text
rung 1  code                 0 tokens, microseconds   duplicates, cancellation,
                                                      proration, usage, unknown
                                                      invoice, prompt injection,
                                                      unknown sender (ADR-007)
rung 2  typed System One     typed, milliseconds      not implemented
rung 3  generative model     seconds                  residual ambiguity,
                                                      explanation, drafting
```

**Never climb a rung if the one below settles it.**

The rule is not postulated. The audit records `cost_micros` and `latency_ms`,
so the gradient is a measurement: *a run costs X; the deterministic rule that
avoids it costs nothing.*

Two consequences of the rule that are easy to miss.

**The model never picks an amount.** It recommends a *kind* of action —
`propose_credit_note` — and the amount is read off the invoice in code. A
probabilistic component does not choose a number that moves money.

**Prompt-injection screening is a rule, not an intuition.** It runs on the raw
message before any engine is called. Screening untrusted input with a model is
asking the thing under attack to referee the attack. Patterns are cheap,
instant, inspectable and testable.

Injection detection is tuned for recall rather than precision, deliberately: a
false positive costs one human review, a false negative costs a financial
mutation. The asymmetry is about four orders of magnitude. One accepted false
positive — an indignant customer asking whether we were *authorised* to bill
them — is documented and tested rather than hidden.

## Consequences

Rule findings are facts with evidence, never recommendations. What to do about
them belongs to the engine and the policy.

Every rule has tests for firing **and** for not firing, and the second set
matters more. An agent that finds a duplicate everywhere is worse than no
agent: it refunds by reflex. Demo case B — a subscription charge plus a real
usage overage, both correct — exists for exactly that.

Rung 2 is empty today. Naming it is still useful: it says where a cheaper,
typed classifier would go, and ADR-002 already shapes the port to accept one.

The rules cannot express everything. Detecting that a dunning notice went out
after payment was received needs a rule that does not exist yet; the case is
in the fixtures, and the gap is named rather than papered over.
