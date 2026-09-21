# Agentic Billing Exception Handler

Provider- and model-agnostic agentic workflow for investigating billing
disputes and safely recommending corrective actions.

> **The billing domain is stable. Billing providers and decision engines are
> replaceable.**
>
> **Deterministic where possible. Probabilistic where useful. Human-controlled
> where risky.**

---

## Why this exists

A customer writes in saying they were charged twice. Answering that well needs
three different kinds of certainty, and most of the work is deciding which one
applies where.

Whether two invoices cover the same period for the same subscription is
arithmetic. It does not need a model, it needs a date comparison, and it will
give the same answer every time. Whether the customer is actually complaining
about that, or about something else entirely, is a reading problem. And
whether to hand back 99 € is neither — it is an authorisation question, and
the answer is that a person decides.

This repository is small on purpose. It is an argument about where each of
those three lines sits, made in code you can run.

---

## What it does

One use case, end to end:

```text
dispute → billing context → deterministic rules → decision engine
        → action policy → human approval → idempotent execution → audit
```

A dispute arrives. The billing context is loaded once, in full. Deterministic
rules run first because they are free, instant and certain. What they cannot
settle goes to a decision engine, whose answer is a classification and a
confidence — never an amount. An action policy decides whether anything may
happen. A human approves. Only then does money move, once, whatever the
network does.

---

## Architecture

The system is agnostic on two independent axes, and neither the domain nor the
application layer knows the name of any vendor.

```text
                        ┌──────────────────┐
  dispute ──────────────│  n8n             │  trigger + notification only
  (webhook / mail)      │  orchestration   │  holds no state
                        └────────┬─────────┘
                                 │
                        ┌────────▼─────────┐
                        │  Billing API     │
                        │  FastAPI         │
                        └────────┬─────────┘
               ┌─────────────────┼──────────────────┐
               ▼                 ▼                  ▼
     ┌──────────────────┐ ┌─────────────┐ ┌─────────────────┐
     │ Billing domain   │ │ Decision    │ │ Audit + state   │
     │ rules, Money     │ │ Engine port │ │ runs, events,   │
     └────────┬─────────┘ └──────┬──────┘ │ pending actions │
              │                  │        └─────────────────┘
     ┌────────▼─────────┐ ┌──────▼─────────────┐
     │ BillingProvider  │ │ MockDecisionEngine │
     │ port             │ │ ClaudeDecisionEng. │
     └────────┬─────────┘ └────────────────────┘
              │
     ┌────────▼───────────┐
     │ FakeBillingProvider│
     └────────────────────┘
```

`investigate_dispute.py` imports `BillingProvider`. It never imports `stripe`.
That is the whole point, and it is checkable: `app/api/deps.py` is the only
module that names a concrete implementation.

---

## Key design principles

### 1. Billing-provider agnostic

Every platform maps its payloads onto one normalised domain — customer,
subscription, invoice, line, payment, usage. The rules, the engines and the
policy see nothing else. Adding Stripe means writing a mapper, not touching
the rules. See [ADR-001](docs/adr/001-provider-agnostic-billing.md).

### 2. Decision-engine agnostic

`DecisionEngine` is an independent port, so the engine can change for reasons
that have nothing to do with quality: data residency, contractual constraints,
cost, latency, or a client's internal policy.

The port requires no prose. That is deliberate: an engine that returns a typed
choice and a probability distribution, rather than text, implements it just as
well. See [ADR-002](docs/adr/002-decision-engine-agnostic.md).

### 3. Deterministic before probabilistic, in three rungs

```text
rung 1  code                 0 tokens, microseconds   duplicates, dates,
                                                      amounts, proration,
                                                      cancellation, injection
rung 2  typed System One     typed, milliseconds      not yet implemented
rung 3  generative model     seconds                  residual ambiguity
```

Never climb a rung if the one below settles it. The audit records
`cost_micros` and `latency_ms`, so this is a measurement rather than a
slogan. See [ADR-003](docs/adr/003-deterministic-before-ai.md).

### 4. Human approval for financial mutations

The engine *recommends*. The policy *authorises*. They are separate objects so
that swapping the engine — or having it go wrong — cannot widen what the
system is permitted to do.

Autonomy thresholds exist in the code and are **off**. The lock is not the
switch: it is `required_confidence_kind="calibrated"`, which a self-reported
0.99 does not clear. See [ADR-004](docs/adr/004-human-approval-financial-actions.md).

### 5. Money is an integer count of minor units

One representation, one rounding function, one rounding mode, and a
per-currency exponent table so `JPY 1000` is not read as ten yen. Floats are
rejected at runtime, not merely discouraged. Cross-currency arithmetic raises.
See [ADR-005](docs/adr/005-money-as-minor-units.md).

### 6. Auditable by design

Every run leaves a row naming the provider, the engine **and its version**,
the classification, the confidence **and its kind**, the findings with their
evidence, the authorisation verdict and its reason, and what the call cost.
Events are collected as the run proceeds, not derived from its outcome — which
is why a blocked run can show `decision_engine_skipped` with no
`decision_created` after it.

### 7. No agent framework

The flow contains no agent loop: nothing routes a model's output back into a
model, the model holds no tools, and `classification` takes values in a closed
set. A graph orchestrator would add no node. See
[ADR-006](docs/adr/006-no-agent-framework.md).

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

## Current implementation

Only what is actually built:

```text
Billing providers : FakeBillingProvider
Decision engines  : MockDecisionEngine, ClaudeDecisionEngine
Orchestration     : n8n (trigger + notification), verified end to end
Persistence       : SQLite
```

**Not built, and named here so nothing is implied:** no Stripe, Lago, OpenCell
or Zuora adapter; no Jev or Vertex engine; no reply drafter; no evaluation
suite; no approval expiry; no read retry with backoff; no OpenTelemetry; no
MCP server; no cloud deployment.

**`ClaudeDecisionEngine` has never run against the live API.** Its tests drive
a stub client, which proves the logic around the call — pseudonymisation,
schema rejection, evidence mapping, cost accounting — and proves nothing about
the call itself. A stub accepts any keyword argument, so it would stay green
through a renamed parameter. Until a real run happens, treat it as unverified.

**No accuracy or cost figures are published**, because the evaluation suite has
not been written, let alone run. Any number in this README would be invented.

---

## Demo scenarios

| Case | Message | Expected |
|---|---|---|
| A | « J'ai été facturé deux fois en septembre » | duplicate detected, 99 € credit note, human approval |
| B | « Pourquoi deux prélèvements ce mois-ci ? » | metered usage corroborates the overage, no action |
| C | « Ma facture est fausse » | insufficient information, ask the customer |
| D | plan change mid-month | proration mismatch, human approval |
| E | cancelled in August, billed in September | billing after cancellation |
| F | dispute with an injected instruction | flagged before the engine is called |

Case B is the one that matters most. An agent that finds a duplicate
everywhere is worse than no agent: it refunds by reflex. The rules have as
many tests for not firing as for firing.

Case F is the one that lands. The disputed invoice really is a duplicate; the
injection stops everything anyway, and the audit shows the model was never
asked rather than that it declined.

---

## Running locally

```bash
pip install -e ".[api,dev]"
pytest
uvicorn app.main:app --port 8000
```

```bash
curl -X POST http://localhost:8000/api/v1/disputes/investigate \
  -H 'content-type: application/json' \
  -d '{"message":"Je crois que jai ete facture deux fois en septembre","customer_email":"alice@acme.fr","invoice_id":"inv_002"}'
```

```json
{
  "run_id": "run_77c770ff",
  "classification": "duplicate_billing",
  "confidence": 0.95,
  "confidence_kind": "self_reported",
  "decision_engine": "mock",
  "findings": ["DUPLICATE_INVOICE"],
  "recommended_action": {"type": "credit_note", "amount_minor": 9900, "currency": "EUR"},
  "requires_human_approval": true,
  "action_id": "act_fe9694e793a2"
}
```

Set `DECISION_ENGINE=claude` and `ANTHROPIC_API_KEY` to use the real engine.
The default is the mock, so tests and demos cost nothing.

### Endpoints

```text
POST /api/v1/disputes/investigate
GET  /api/v1/actions/{id}/review      the approval page
POST /api/v1/actions/{id}/review      a human decision
POST /api/v1/actions/{id}/approve     | reject | execute
GET  /api/v1/runs/{id}                the run, its trace, what is waiting
GET  /health                          which implementations are wired in
```

### The audit, in SQL

```bash
sqlite3 audit.db "SELECT classification, verdict, decision_engine, cost_micros FROM agent_runs"
sqlite3 audit.db "SELECT action_id, state, approved_by, provider_action_id FROM pending_actions"
```

---

## The orchestrator, and what it is not

See [n8n/README.md](n8n/README.md) for the workflow.

n8n triggers the run and notifies the approver. It holds no state. By the time
its Wait node suspends, the action is already `pending_approval` in the
database with a derived idempotency key. n8n restarting, being upgraded or
disappearing loses nothing — which is the reason the state is not in it.

The approval happens on a page the API serves, not in an n8n form. That is
what finance approval systems do, for three reasons: identity (a controller
must be able to prove *who* approved), transport (email is forwarded, quoted
and archived), and prefetching (mail scanners follow links, so a one-click
approve URL gets exercised by a scanner rather than a person).

The honest limit, printed on the page itself: the identifier field is **not**
authentication. In production that route sits behind the company's SSO.

---

## Production considerations

**Idempotency.** The key is derived from what the action *is* — provider,
invoice, type, amount, currency — never generated. A retry after a timeout
finds the original credit note instead of issuing a second one.

**Reads retry, the write does not.** The single mutating provider call carries
its key rather than relying on the caller being careful.

**Immutable run rows.** Approval and execution are a separate lifecycle in
their own table, joined on `run_id`. Amending a run in place would erase the
difference between what was decided then and what happened to it since.

**Untrusted input.** The dispute message is screened by pattern before any
model sees it, and the context that reaches the engine is pseudonymised — a
reference outside the aliases we issued is dropped rather than trusted.

**Cost is an estimate.** It applies a dated list price, not an invoiced
amount. Reconciling estimated against billed is on the roadmap, and it is the
same problem this project solves on customer invoices, turned on our own
consumption.

---

## Deployment target

GCP, région Paris (`europe-west9`): Cloud Run for the API, Cloud SQL for the
audit, Secret Manager, Cloud Logging. Not deployed.

The interchangeable `DecisionEngine` is what makes the backend a choice rather
than a constraint — data residency, contractual limits, sovereignty, cost,
latency, internal policy.

> **The best model is not necessarily the one you are allowed to use.**

That is not a slogan. A calibrated-probability engine evaluated for this
project turned out to be hosted outside the EU. In an ordinary architecture
that discovery arrives too late and costs a rewrite; here it is a line of
configuration, and the comparison stays usable as a reference for choosing a
deployable engine.

---

## Roadmap

Ordered by what would be built next, each with its reason rather than as a
wish list.

- **Evaluation suite, then CI.** Produces the accuracy, cost and
  calibration-by-confidence-band figures this README deliberately does not
  publish. It is also what could ever promote a confidence from
  `self_reported` to `calibrated`, and with it open the autonomy thresholds.
- **Reply drafter.** A second port, already argued for: prose is not the
  decision engine's job, and separating them is what made the decision port
  implementable by a non-generative model.
- **Approval expiry and read retry.** A request that sits for three weeks
  should not be approvable by someone who has forgotten the context.
- **A second engine** behind the same port, evaluated head to head.
- **Stripe adapter**, to prove the billing boundary against a real API.
- **OpenTelemetry GenAI instrumentation**, exporter left to the deployment —
  the `DecisionEngine` argument applied to telemetry. No provider exposes
  prices through an API, so the price table stays ours or the backend's.
- **Read-only MCP server** over `BillingProvider`, so any MCP-capable agent can
  investigate a dispute without being able to mutate anything.
- **A checkpointed orchestrator**, under the condition stated in ADR-006.

---

## Architecture decision records

| | |
|---|---|
| [ADR-001](docs/adr/001-provider-agnostic-billing.md) | Provider-agnostic billing |
| [ADR-002](docs/adr/002-decision-engine-agnostic.md) | Decision-engine agnostic |
| [ADR-003](docs/adr/003-deterministic-before-ai.md) | Deterministic before probabilistic |
| [ADR-004](docs/adr/004-human-approval-financial-actions.md) | Human approval for financial mutations |
| [ADR-005](docs/adr/005-money-as-minor-units.md) | Money as integer minor units |
| [ADR-006](docs/adr/006-no-agent-framework.md) | No agent framework |
