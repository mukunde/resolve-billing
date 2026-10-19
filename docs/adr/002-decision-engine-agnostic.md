# ADR-002 — Decision-engine agnostic

**Status:** accepted · 2026-09-20 · revised 2026-09-21

## Context

Which model answers a billing dispute is not only a quality question. It is a
data-residency question, a contractual question, a cost question and sometimes
a client-policy question. A French client may forbid a US-hosted model for
customer data regardless of how well it scores.

That constraint is not hypothetical. A calibrated-probability engine evaluated
for this project turned out to be hosted outside the EU. In an architecture
that names its model in the application layer, that discovery arrives late and
costs a rewrite.

## Decision

`DecisionEngine` is an independent port: a name, a version, and one method
that takes a dispute, its billing context and the rule findings, and returns a
`Decision`.

Three properties of that port matter more than its existence.

**No prose is required.** `Decision.explanation` is optional. A port that
demanded a string could only ever be implemented by a generative model; with
it optional, an engine returning a typed choice and a probability distribution
implements the same port. When an engine returns none, the application layer
assembles an explanation from the rule findings — traceable line by line,
which suits an audit row better than model prose would.

**The classification is a closed set, with an escape hatch inside it.** With a
generative engine, an aberrant answer violates the schema and is caught. With
an engine whose output is typed by construction, that branch is unreachable —
everything conforms, always. So the escape hatch has to be a value of the
enum. Hence `unknown`, treated by the action policy as non-actionable. Staying
inside the frame and being right are different things.

**Confidence carries its own nature.** `confidence_kind` is one of
`self_reported`, `distribution_derived` or `calibrated`. The engine never sets
the last one: only measurement on an evaluation set can, and no vendor can
grant it because it depends on the domain, the case set and the language.

`name` and `version` are written to every audit row. Without them a decision
is neither replayable nor comparable against another engine, which would empty
this ADR of its content.

## Consequences

The engine is a deployment choice. Changing it is an edit in `app/api/deps.py`.

An engine hosted outside the EU is not disqualified from being *evaluated* —
the comparison runs offline on synthetic fixtures, so no customer data leaves.
It is disqualified from the production path, and the port is what makes that
distinction cheap.

The context handed to any engine is pseudonymised. It sees `INVOICE_1`, not
`inv_002`, and references outside the aliases we issued are dropped rather
than corrected. An engine cannot name another customer's invoice because it
was never told one exists.

The port cannot express an engine that needs to fetch data or call tools
mid-decision. That is intentional and is the same fact ADR-006 rests on.

## Amendment, 2026-10-19 -- an engine that cannot be reached

An engine raises `EngineUnavailable` for transport and service failures only
-- a timeout, a dropped connection, an overloaded or failing service. The
investigation turns it into an escalation and records the outage as such,
distinct from an engine that answered and abstained; no cost is invented. A
malformed answer stays the engine's own escalation, and a bad request or a
missing key stays an error. Below a confidence of 0.70 the action policy
sends any classification to a person (taxonomy S8).
