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

### On a Stripe test account

The fake is the default. To investigate on a Stripe test account instead --
read-only unless writes are switched on below; then an approved correction is
made there as a credit note (specs/001-stripe-read-adapter,
specs/002-stripe-writes, [contract](contract/billing-platform.md)):

```bash
pip install -e ".[stripe]"   # the SDK pinned to its API version, and httpx
```

Two keys, created by you in the account's test mode (Developers -> API keys),
each in its own git-ignored file, and never anywhere else:

- `STRIPE_API_KEY`, in `.env`: a **restricted** key (`rk_test_...`) with
  **Read** on Customers, Subscriptions, Invoices, Payment Intents, Charges and
  Refunds, Prices, Products, Billing Meters, Billing Meter Events and Test
  Clocks, and **Credit Notes** -- Read at least, so that a person can read a
  write's status with writes switched off; existing credit notes were read
  on the test account with Write granted, which allows reading. To write, it
  needs **Write** on Credit Notes, and only that: a refund is a credit note
  that refunds, and the first write run without it was refused (403, nothing
  made) (2026-10-23). The service refuses any other kind of key before calling Stripe, and
  refuses to start the SDK with console logging on (`STRIPE_LOG`), which
  would print what Stripe answered.
- `STRIPE_SEED_KEY`, in `.env.seed` and nowhere else: the **secret** test
  key (`sk_test_...`), read only by the seed script below. It never goes in
  `.env`, which `dotenv run` loads into the service; nor in the shell the
  service starts from.

Then `BILLING_PROVIDER=stripe` selects Stripe for the API and the MCP server.
Stripe reads only, and execution there is refused (409), unless
`STRIPE_WRITES=enabled` -- that exact value -- is set beside it: then an
approved credit note, or refund, is made on Stripe as one credit note, with
no mail to the customer.

**The test worlds.** `scripts/stripe_seed.py` builds, on the account, the
worlds of `scripts/stripe_worlds.py` -- each a customer on its own test clock,
on the month that ended before the run, with the amounts of its fake
counterpart -- and records on each customer the invoice to investigate and
when:

```bash
python -m dotenv -f .env.seed run -- python -m scripts.stripe_seed
```

It prints one line per world: `built`, `complete` (an earlier run built it,
and it still holds), or `REPORTED` with why -- a Stripe refusal by its type,
code, parameter and request id (find the request in the Dashboard's logs),
never its message. Run it again to complete what is missing; it makes
nothing twice. Run it from one environment only (Windows *or* WSL): its lock
holds within one. Stripe deletes a test clock 30 days after its creation;
deleting a world's clock, or that deletion, makes the next run build the
world again on that run's month.

**The contract on Stripe.** With the seeded account and the read-only key:

```bash
python -m dotenv run -- python -m pytest tests/contract -rs -q
```

Without `STRIPE_API_KEY` the Stripe half is skipped, with the reason; CI runs
the fake half only. The account's preconditions -- every world present, its
clock ready, its target invoice finalised, the usage world's meter summary
available -- are checked once, at the start of the session, within a bound;
every later test starts from what that check found, and builds its own
connection to read Stripe for its property. They are not promised to stay
true afterwards: during a session, do not run the seed, nor change the
worlds in use. An account not ready fails the first Stripe test after the
wait, and every later one at once with the same reason; a platform error
is not kept, and the next test checks again
(specs/003-contract-readiness-once). With pytest-xdist, the check is made
once per worker. On the maintainer's account (2026-10-23), a full run -- the
read contract and, on a fresh write run, the write contract -- took 3 min 25
(80 passed, 13 skipped, all fake-only), against 9 min 19 when every test
checked the account first.

**The write contract on Stripe.** A write is permanent, so the write
properties run on invoices made for them, fresh each time. First a write
run, which opens a new write generation -- a customer with one invoice per
scenario, paid or open, and a duplicate dispute -- reusing nothing an earlier
one made:

```bash
python -m dotenv -f .env.seed run -- python -m scripts.stripe_seed --write-run
```

Then the contract with writes switched on (the key with its write
permissions):

```bash
STRIPE_WRITES=enabled python -m dotenv run -- python -m pytest tests/contract -rs -q
```

The latest write generation is found once per session and pinned: a write
run opened during the session is used by the next one. Each scenario still
checks that its invoice carries no credit note, and stops, asking for a new
write run, if one does. Run the write contract sequentially, and never two
sessions on one write generation at once. A write property skipped on
Stripe is not demonstrated: all seven must run and pass. Only the write
tests can be run, without waiting for the clocked worlds:
`tests/contract/test_billing_platform_writes.py` and
`tests/contract/test_stripe_execution.py`. A refusal reads as its category
alone (`permission`, say): Stripe's message, naming a missing permission, is
in the Dashboard's logs (Developers -> Logs).

### What CI actually enforces

The interesting assertion is not that the tests pass.

```yaml
- run: >-
    python -m evals.runner --engine mock --min-fully-correct 29
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

### The engines evaluated on free tiers

`gemini-3.8-flash`, `gemini-3.5-flash-lite`, `mistral-medium-3-5` and
`mistral-medium-3-5-reasoning` (specs/004-second-engines) run from the evaluation runner, on
`evals/cases.json` only, never in CI. Install them with the `engines`
extra:

```bash
pip install -e ".[engines]"
```

**Keys.** `GEMINI_API_KEY` (Google AI Studio) and `MISTRAL_API_KEY`
(Mistral's admin console), created by the maintainer and kept in `.env`.
Nothing else reads them; the project does not load `.env` by itself.
`GEMINI_QUOTA_PROJECT`, beside them, names the Google project the Gemini
key belongs to (its id, as AI Studio shows it): the daily quota is the
project's, and the key alone does not say which. That association is the
maintainer's declaration; the code does not verify it.

**The documented check** (the spec's assumptions), as the maintainer read it
on 2026-10-24:

| | gemini-3.8-flash | mistral-medium-3-5 and mistral-medium-3-5-reasoning |
|---|---|---|
| usable on the free level | a dedicated project, `resolve-billing-eval`, free tier, no billing; generation confirmed only by an accepted first call | $10 of API usage a month, reset on the 1st, included by default; "Activer" (Pay-As-You-Go) enables paid usage beyond it and stays off; Medium 3.5 is in the key's model list -- listed, not yet shown to answer with these parameters: confirmed only by the two accepted first calls |
| limits read in the console | 5 requests/min, 250,000 tokens/min, **20 requests/day** (per project, reset at midnight Pacific) | 20,000 tokens/min, 1.00 request/s -- the page lists only `mistral-medium-latest`, taken as `mistral-medium-3-5`'s; one limiter for both configurations |
| status | stable | Mistral Medium 3.5, v26.04 (2026-04-28), Modified MIT licence |
| data regime | for users in the EEA, Switzerland and the UK, the paid-service terms apply to free services (Google's terms, 2026-04-28): the maintainer uses it from France -- `contractual` | "Allow your API calls to train Mistral's models" is on -- `training_allowed` |
| Labs models | -- | off; Medium 3.5 is not a Labs model in the documentation, to be confirmed by the first calls |

**Refused on the free level (2026-10-24).** The first configurations planned
on Mistral, `mistral-large-4` (Public Preview) and `zai-glm-5-3` (Z.ai's
model, served by Mistral), were both refused at their first call: HTTP 403,
settled at zero locally. The maintainer's diagnostic call outside the runner
returned Mistral's reason for Large 4: "This model is not available in your
subscription tier" (`tier_not_allowed`). Their two results are kept in
`evals/results/`. Magistral Medium, considered next, is retired (its last
version on 2026-07-31); Medium 3.5 is Mistral's designated replacement.

**The maintainer's waivers (2026-10-24).** Reservations and costs are
estimates, never guarantees: for all four, the request's UTF-8 bytes plus a
512-token margin plus the output cap (`utf8-bytes-v1`). Gemini's was first
Google's count of the request; it became local the same day, since a counting
request could spend the day's quota. For the two Medium 3.5, a reasoning
effort whose billable tokens no documentation caps is accepted: the local
envelope is conservative admission accounting, not a guaranteed bound, and
what keeps the account from being billed is Mistral's own enforcement of the
free level, Pay-As-You-Go being off. Each answer's reported usage is checked
against its reservation, and a run stops when one is exceeded or usage is not
reported.

**A first call, then the run.** For each configuration, first a smoke test,
`--first-call-only`: the development set up to the first case that reaches
the engine, one attempt, then the run stops, written incomplete, its
`sends` 0 or 1. It is accepted ("first call accepted") on a valid verdict,
its usage reported and within the reservation, and a model version
returned; the acceptance is then recorded in
`~/.resolve-billing/first-calls.json`, after the result is written. A full
run is refused until its configuration's first call is accepted -- for
Mistral, both Medium 3.5 configurations' -- and again after any change to
the configuration, the prompt, the schema, the masking, the key or the
Gemini project, or if the first call's result file was moved, changed or
deleted: leave it where it was written until the full run. The record holds
a 12-character fingerprint of the key, never the key; it stays in that
private folder. `--data-terms` says
what the vendor's terms for the run rest on -- `contractual`, `opted_out`,
`training_allowed` or `unknown` -- and is required.

For Mistral, the two first calls, then the two full runs (the month's
allowance is declared once, on the month's first run, with
`--mistral-allowance-usd`):

```bash
python -m dotenv run -- python -m evals.runner --engine mistral-medium-3-5 --data-terms training_allowed --first-call-only --json evals/results/mistral-medium-3-5.json
```

```bash
python -m dotenv run -- python -m evals.runner --engine mistral-medium-3-5-reasoning --data-terms training_allowed --first-call-only --json evals/results/mistral-medium-3-5-reasoning.json
```

```bash
python -m dotenv run -- python -m evals.runner --engine mistral-medium-3-5 --data-terms training_allowed --json evals/results/mistral-medium-3-5.json
```

```bash
python -m dotenv run -- python -m evals.runner --engine mistral-medium-3-5-reasoning --data-terms training_allowed --json evals/results/mistral-medium-3-5-reasoning.json
```

For Gemini, twenty requests a day are exactly one full run (twenty cases
reach the engine), so the smoke test and the full run go on **two different
Pacific days**. On each day, declare once, before its first request, the
requests already used that day as AI Studio shows them (`0` on a fresh day):

```bash
python -m dotenv run -- python -m evals.runner --engine gemini-3.8-flash --data-terms contractual --gemini-requests-used-today 0 --first-call-only --json evals/results/gemini-3.8-flash.json
```

and, another day:

```bash
python -m dotenv run -- python -m evals.runner --engine gemini-3.8-flash --data-terms contractual --gemini-requests-used-today 0 --json evals/results/gemini-3.8-flash.json
```

Flash's first full run (2026-10-25) stopped at its first request on a Google
server error: with no room for a retry, a passing error costs the day.
**`gemini-3.5-flash-lite`** (spec 004, third amendment) allows 500 requests a
day and 15 a minute on the same project, so it makes three attempts per
case and an outage escalates the case instead of stopping the run; its
smoke test and its full run can share a day. Its first call, then its run
(declare the day once, as AI Studio shows Flash-Lite's own count):

```bash
python -m dotenv run -- python -m evals.runner --engine gemini-3.5-flash-lite --data-terms contractual --gemini-requests-used-today 0 --first-call-only --json evals/results/gemini-3.5-flash-lite.json
```

```bash
python -m dotenv run -- python -m evals.runner --engine gemini-3.5-flash-lite --data-terms contractual --json evals/results/gemini-3.5-flash-lite.json
```

**Gemini's daily quota.** Google counts per project and model, and so
does the runner: `gemini-3.8-flash` in `~/.resolve-billing/quota-<project>.json`
(the file it used first, unmoved), every other model in
`quota-<project>-<model>.json`, each under a lock every checkout of the
project shares. A day is declared once per model, never lowered; each
request is counted before it is sent and never refunded, failed ones
included. A full run starts only if the day's remaining requests cover the
20 cases that reach the engine times the configuration's attempts -- for
Flash, an untouched day; for Flash-Lite, 60 remaining. A request that would
pass the limit is not sent and the run stops ("daily quota exhausted (local
count)"), as it does on reaching a day not declared -- a run crossing Pacific
midnight -- and on a clock gone back. Flash makes one attempt per case and
an outage stops the run; Flash-Lite makes three, and an outage escalates
the case. The project is assumed
dedicated, with no other caller: neither the console nor this count can see
one.

The ledger, `~/.resolve-billing/mistral-envelope.json`, lives outside every
checkout, so both Mistral models, every run of the month and every worktree
share it; a second declaration that month is refused, and a new month starts
a new ledger, the old one kept beside it.
Each attempt reserves its maximum cost there before it is sent. If it
cannot be read, the runner refuses: inspect it, never delete it to get past
the refusal -- its unsettled reservations are attempts that may have been
billed.

**One run per account.** The same folder holds, per vendor account
(`google`, `mistral`), a run lock and the time of the account's last
request. A second run on an account is refused while one is in progress,
from this checkout or another; the lock goes with the process if it dies. A
run starts no sooner than a minute after the account's last request, so a
previous run's quota window is not counted twice.

**Reading a run.** The result is written beside the `--json` path, named by
the run's start time; nothing earlier is overwritten. `complete: false`
means the run stopped -- `stop_reason` says why, `stopped_at` where,
`not_attempted` what was left -- and the exit code is 3. Its totals are over
the cases it scored: a case whose request was too large for a quota
(`not_sent_oversized`) or whose engine could not be reached (`unavailable`)
is listed under `not_scored`, never counted as a decision of the model. A
defect in the code stops the run too: the result is written with
`internal error (<type>)`, then the error is raised as itself.
Mistral's cost in the file is an estimate; the console's usage is the
account's figure.

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
POST /api/v1/actions/{id}/status      read a write's status again         approver
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

An action says when it needs a person, in `attention`
([contract](contract/approval-and-execution.md)): `not_successful` when a
write was made and the platform's status is not success -- a refund pending
or failed, a credit note voided -- to follow up; anything else
(`window_passed`, `window_unknown`, `several_writes`, `unlike_the_action`,
`unreadable_answer`) stops everything automatic for that action, and
`execute` answers 409 with the reason. `POST /actions/{id}/status` has the
platform read again: it records the status observed and sends nothing, with
writes switched on or off; for an action left to a person, it settles it
only when the platform holds exactly one write alike.

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
