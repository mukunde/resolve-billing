# Operating ResolveBilling

How to run, observe and query the demonstrator. The [README](../README.md)
says what the system is and where it stands; this page is the manual.

## Running locally

```bash
pip install -e ".[api,dev,mcp,otel]"   # what CI installs; add `claude` for the real engine
pytest
```

The API is closed by default: every route that acts needs an authenticated
caller, and answers 503 until callers are configured (ADR-009). Put the two
variables from `.env.example` in `.env` -- the orchestrator's key and the
approvers' sign-ins -- and start the API with them loaded:

```bash
python -m dotenv run -- uvicorn app.main:app --port 8000
```

```bash
curl -X POST http://localhost:8000/api/v1/disputes/investigate \
  -H "Authorization: Bearer $RESOLVE_SERVICE_KEY" \
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
  "proposed_action": {"type": "credit_note", "amount_minor": 9900, "currency": "EUR"},
  "requires_human_approval": true,
  "action_id": "act_fe9694e793a2"
}
```

The `curl` above reads `RESOLVE_SERVICE_KEY` from your shell: load `.env`
there too (`set -a; . ./.env; set +a`) or paste the key.

Set `DECISION_ENGINE=claude` and `ANTHROPIC_API_KEY`, and install the
`claude` extra, to use the real engine.
The default is the mock, so tests and demos cost nothing.

### What CI actually enforces

The interesting assertion is not that the tests pass.

```yaml
- run: >-
    python -m evals.runner --engine mock --min-fully-correct 24
    --baseline evals/baselines/mock.json
```

That line fails a build when the system **would have moved money** where it
must not — on a dispute the case set says moves none, or for a sender nobody
has verified — or **would have answered** a sender suspected of impersonating a
customer (ADR-007). That is the one metric in the suite with a threshold,
checked on every push rather than when somebody remembers to look. It fails
again if the mock drops below its measured score, and again if any check of
any case differs from the recorded baseline. That last one catches what a
count cannot: one case breaking while another is fixed, or a case already
wrong getting wronger. An improvement fails it too, until the baseline is
regenerated in the same commit, where the diff shows which cases moved:

```bash
python -m evals.runner --engine mock --write-baseline evals/baselines/mock.json
```

Two engines, two cadences, and the split is the point. The **mock** is free and
deterministic, so its baseline is exact and it runs on every push. **Claude** is
neither: a sampling model does not return the same answer twice, so a hard
accuracy threshold on one would fail builds for reasons nobody changed, and a
paid job on every push is a bill that grows with the contributor count. It runs
on demand, still fails on an unsafe action, and carries no floor —
which is the honest assertion to make about an engine whose accuracy varies.

The floor is the mock's measured score, not an aspiration. Raise it when the
mock genuinely improves; lowering it to make a build pass would turn the one
number with teeth into decoration.

### Reconciling what we estimated against what we were billed

```bash
python -m app.reconciliation --db audit.db --billed cost_report.json
```

Every audit row carries `cost_micros`, computed from a dated list price. That
is an estimate twice over — list prices are not invoiced amounts, and the
table applies only the rates it knows — so the honest thing is to check it
against the bill.

Which is this project's own subject, turned around. `detect_duplicate_invoice`
compares two records of one economic event on a customer's account; this
compares two records of one economic event on ours. Same shape, same trap: the
two ledgers agree on the event and disagree on the grain. Runs are per
dispute, the cost report is per day, so the comparison is per **(day, model)**
and says so rather than inventing a per-run figure nobody issued.

On the two real runs of 2026-09-22, against a cost report in the documented
shape:

```text
day          model                 runs    estimated       billed          gap    gap %
2026-09-22   claude-opus-5            2     $0.04463     $0.05083     $0.00620   +13.9%

where the gap sits
  - $0.00620 of the bill is cache tokens, which the price table does not
    model: estimate_cost_micros applies input and output rates only.
```

**A gap is only useful with an address.** The provider bills by token type and
our table prices two of them, so the reconciler names the categories it cannot
account for instead of leaving a percentage to be argued about. It exits
non-zero when the ledgers disagree beyond tolerance, which is what makes it
runnable on a schedule.

It found two defects on its first run, and both are recorded rather than
quietly patched: the price table does not model cache reads, and the audit was
**not persisting `cache_read_input_tokens` at all** — the telemetry captured it
from the first Claude call and the schema dropped it. The column exists now.

Two honest limits. The cost report is reached through a `UsageReport` port with
two adapters, and only the file one has ever run: **the Usage and Cost
endpoints reject workspace-scoped API keys**, and this project holds one on
purpose, for the spend cap and the attribution. The HTTP adapter is written
against the documented response shape and is unverified — exactly the warning
this README carried about the decision engine until its first real call, which
then found three defects in an afternoon. And the bill covers the whole
account, so `--workspace` matters: reconciling a project's estimate against an
organisation's bill compares two populations and calls the difference an error.

### Tracing, when you want it

```bash
pip install -e ".[otel]"
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $AUTH,x-langfuse-ingestion-version=4"
```

Spans follow the **OpenTelemetry GenAI semantic conventions** and leave over
OTLP, so the backend is configuration rather than architecture: a console in
development, Langfuse or Phoenix self-hosted, Cloud Trace or Datadog at a
client. The example above happens to be a local Langfuse — over HTTP, since it
does not accept gRPC.

That is the `DecisionEngine` argument applied to telemetry: **you do not choose
the vendor, you choose the interface.** Wiring an observability SDK into the
decision engine would be the second lock [ADR-006](adr/006-no-agent-framework.md)
spends its length arguing against.

**With no endpoint set, nothing is installed** and the code path is the one
that ran before the module existed. Observability that changes the behaviour
it observes is worse than none.

**No customer data leaves in a span.** This is the part that is not a
convenience. An evaluation run is offline and synthetic; traces flow
continuously from production, and would otherwise carry dispute text, billing
context and our prose about both — to whichever backend someone wired up,
while this README argues you can pick a vendor by data residency. So spans
carry identifiers, token counts, classifications, verdicts and costs, and
never prose:

```text
invoke_workflow investigate_dispute
  gen_ai.operation.name              invoke_workflow
  gen_ai.conversation.id             run_79dd2173
  billing.classification             duplicate_billing
  billing.confidence                 0.95
  billing.confidence_kind            self_reported
  billing.verdict                    requires_approval
  billing.findings                   DUPLICATE_INVOICE
```

A test asserts it against the instrumentation's own source, not against
today's behaviour. And one thing the conventions deliberately do not carry is
a price: OpenTelemetry GenAI records *tokens*, because no provider exposes a
rate through an API. The arithmetic stays ours or the backend's — the same
conclusion the pricing table reaches.

### The same data, over MCP

```bash
pip install -e ".[mcp]"
python -m app.mcp.server          # stdio
```

Four tools — `find_customer`, `get_invoice`, `get_billing_context`,
`run_deterministic_checks` — every one annotated `read_only_hint`. That is
an annotation a client can show, not a guarantee the protocol enforces: what
enforces it is the paragraph below.

**Nothing there can move money.** The provider's mutating methods, a credit
note and a refund, are not reachable from the module, and that is asserted against its source rather than
against its behaviour: a call that merely is not made today is a weaker claim
than one that has no path to it.

The point is the asymmetry with our own engine. *This* agent holds no tools,
because a model that calls a tool decides its own reach at inference time
(ADR-006). Somebody else's agent is welcome to the facts and to what the
deterministic checks established — and to nothing that proposes an action.
`run_deterministic_checks` calls no model, costs nothing, and returns an empty
list when the rules found nothing, which is a result rather than a gap.

### Endpoints

```text
                                                                          who may call
POST /api/v1/disputes/investigate                                         orchestrator
GET  /api/v1/actions/{id}/review      the approval page                   approver
POST /api/v1/actions/{id}/review      a human decision                    approver
POST /api/v1/actions/{id}/approve     | reject                            approver
POST /api/v1/actions/{id}/execute     carry out what a person approved    orchestrator
GET  /api/v1/actions/{id}             one action                          either
GET  /api/v1/runs/{id}                the run, its trace, what is waiting either
GET  /api/v1/runs/{id}/view           the same, as a page                 either
GET  /health                          which implementations are wired in  anyone
```

The orchestrator presents a service key (`Authorization: Bearer`); an approver
signs in. The identity recorded against a decision is the authenticated one,
never a name in the request (ADR-009).

An approval names what it approves: the `proposal` fingerprint the page
carries, or that `GET /actions/{id}` returns, sent back as
`{"proposal": ...}`. If the proposal or its invoice changed since, or
changes before execution, the action expires instead of going through. A
request left undecided past its deadline (72 hours by default) expires
too (ADR-008).

### The audit, in SQL

```bash
sqlite3 audit.db "SELECT classification, verdict, decision_engine, cost_micros FROM agent_runs"
sqlite3 audit.db "SELECT action_id, state, approved_by, provider_action_id FROM pending_actions"
```

Without the `sqlite3` CLI — it is not installed by default on Windows — the
standard library answers the same queries, and Python is already a dependency
here:

```bash
python -m sqlite3 audit.db "SELECT action_id, state, approved_by FROM pending_actions"
```

---

## The orchestrator, and what it is not

See [n8n/README.md](../n8n/README.md) for the workflow.

n8n triggers the run and notifies the approver. It holds no financial state.
By the time its Wait node suspends, the action is already `pending_approval`
in the database with a derived idempotency key. n8n restarting, being
upgraded or disappearing loses no decision: the pending action, its approval
and its execution live in the database, and the review and run pages work
without n8n. What it can lose is its own workflow state -- a waiting
execution -- which is why that state is not where the money is decided.

The approval happens on a page the API serves, not in an n8n form. That is
what finance approval systems do, for three reasons: identity (a controller
must be able to prove *who* approved), transport (email is forwarded, quoted
and archived), and prefetching (mail scanners follow links, so a one-click
approve URL gets exercised by a scanner rather than a person).

The page knows who is deciding because the approver signed in, not because a
name was typed: the identity recorded is the authenticated one (ADR-009). In
this demonstrator, signing in is HTTP Basic against a configured list -- the
honest limit, printed on the page itself. In production that route sits
behind the company's SSO, behind the same seam.

**Not every run that needs a human needs a decision.** A dispute stopped by
the injection screen queues no action: there is nothing to authorise, only
something to read. It gets a different mail — no buttons — linking to a
read-only page of the run and its trace, where the evidence is what the trace
does *not* contain. `decision_engine_skipped` with no `decision_created` after
it is a positive record that the model was never asked, which is not the same
claim as the model having declined.

That branch was missing until a rehearsal found it, and the failure is
instructive: the flow tested `requires_human_approval`, which is true here,
and mailed an approval request with an empty action id in the link. The flag
says a human must look. It does not say there is a decision to take.

---
