# ResolveBilling

**Billing dispute resolution where code checks the facts, a model reads the
complaint, and a person signs for the money.**

A customer writes that they were charged twice, billed after cancelling, or
charged the full month for half of it. ResolveBilling loads their billing
records, checks them with deterministic rules, asks a language model only what
code cannot settle -- what the customer is actually disputing -- and prepares
the case for a human: findings with their evidence, a classification, and a
correction priced by code when the code can price it. Nothing moves money
without an authenticated person approving it, and every run that completes is
recorded in an audit trail.

[![evals](https://github.com/mukunde/resolve-billing/actions/workflows/evals.yml/badge.svg)](https://github.com/mukunde/resolve-billing/actions/workflows/evals.yml)

> **Status, 2026-10-19.** A working, tested demonstrator, run on fake billing
> data: no real billing platform is connected and nothing is deployed. The
> goal is a production-ready product, and the next milestone is a supervised
> pilot; [where it stands](#where-it-stands) says what is done and what is not.

![One pipeline, four disputes: a duplicate goes to a human for approval, a correct invoice proposes nothing, a mispriced proration goes to a person without an amount, and an injected instruction stops before the decision engine is ever called.](docs/workflow.svg)

<sub>Four runs through one pipeline. Case D is the newest rule: the error is
real, but the code cannot yet compute what is owed, so a person reviews and
nothing is proposed. Case F never reaches the model, and the audit says so.</sub>

---

## Why it exists

Answering a billing dispute well takes three kinds of certainty, and most of
the work is knowing which applies where.

- **Arithmetic.** Whether two invoices cover the same period for the same
  subscription, or whether a partial month was prorated correctly, is a date
  comparison and an integer calculation. Code answers it the same way every
  time, for nothing.
- **Reading.** Whether the customer is complaining about that duplicate, or
  about something else; whether the message is a dispute at all; which of the
  findings answers it. That is what a language model is good at, and what
  rules are bad at.
- **Authority.** Whether to hand back 99 €. That is neither arithmetic nor
  reading: a person decides.

**Why use a model if a person decides?** Because the person decides on a
prepared case, not a raw e-mail. Finance already works this way: the
four-eyes principle separates whoever prepares a correction from whoever
approves it. The model assists the preparer; it never replaces the approver.
A dispute that needs no money -- a correct invoice to explain, a missing
invoice reference to ask for -- gets a reply without anyone deciding
anything; a dispute that needs money reaches a person with the findings, the
evidence and the proposed correction already assembled. People also step in
when the message looks like an attack, the sender is suspected of
impersonating a customer, the model is unsure (below 0.70) or unreachable,
the evidence cannot settle the case (an invoice older than the loaded
history, amounts in different currencies, usage nobody could check), or the
message is too long to send to the model.

What is measured so far: on the original twelve cases, one model call cost
**$0.021** on average, with a **median 4.7 s**. What is not measured yet, and
can only be measured in a pilot: how much preparation time it saves, at what
quality. That is the number the product has to earn.

---

## What it does

```text
dispute → billing context → deterministic rules → decision engine
        → correction amount → action policy → human approval
        → execution, once → audit
```

1. **Billing context.** The customer is identified by their address, and
   their subscriptions, invoices, payments and usage are loaded once.
2. **Deterministic rules** run first: duplicate invoice, billing after
   cancellation, proration mismatch, usage gap, unknown invoice, unknown
   sender, suspected impersonation, prompt injection, message too long. A
   rule states a fact and shows its evidence; it never recommends.
3. **The decision engine** -- a language model behind a port -- is called only
   when the rules have not already stopped the run. It returns a
   classification from a closed set, a confidence, and the *kind* of action it
   recommends. Never an amount.
4. **The correction amount** is computed by code from the records, and only
   when the whole invoice is the error: a duplicated invoice, an invoice
   after cancellation, an invoice collected twice. A partial error goes to a
   person without an amount rather than with a wrong one.
5. **The action policy** decides what may happen: a reply or a request for
   information, a financial action awaiting approval, or human review. Refused
   money is not shown as a proposal.
6. **A person approves** on a page the API serves, signed in; the approval
   is bound to what they were shown, and expires.
7. **Execution is designed to happen once**: the action is claimed
   atomically, written with an idempotency key, and an uncertain outcome is
   reconciled with the platform rather than retried blind -- all of it tested
   against the fake provider, not yet a real one.

The classification set is the billing vocabulary, and closed:

```text
duplicate_billing           the same thing was charged twice
valid_invoice               correct; the customer has misread it
billing_after_cancellation  a period after cancellation was billed
proration_dispute           a partial period was mispriced
payment_already_received    a dunning case: the money arrived, the chase did not notice
insufficient_information    which invoice is not identifiable; ask
unknown                     it fits none of these, or it is not a dispute at all
```

`unknown` is a real answer. A dispute forced into the least bad box costs more
than one that goes to a person.

**What it is not.** The model holds no tools, starts no loop, and cannot
reach any data beyond the context it is given. "Agentic" here means a
multi-step, AI-assisted workflow with an audit trail -- not an autonomous
agent. Autonomy thresholds exist in the code and are off.

---

## Where it stands

| Area | Built and tested | Planned |
|---|---|---|
| Investigation | rules, closed classification, Claude engine and a free mock behind one port, versioned prompt (v3) | second engine (Vertex), reply drafter |
| Money | amount computed by code for whole-invoice errors; refused money shown as none; refund only against a settled payment; corrections capped at the invoice | partial amounts, specified with real billing data |
| Approval and execution | authenticated approvers, proposal fingerprint, 72 h expiry, idempotent execution, uncertain outcomes reconciled with the platform within its idempotency window, the platform's answer checked before a write is recorded, incidents left to a person ([contract](docs/contract/approval-and-execution.md)) | identity provider (OIDC) instead of a configured list |
| Intake | n8n workflow: webhook, approval mail, alert without buttons, error branch | support tool (Zendesk, Zammad) owning the case; durable workflow |
| Billing platforms | fake provider on fixtures; a second fixture directory for cases kept outside the repository; Stripe in test mode, reading and writing (a credit note; a refund as a refunding credit note, behind `STRIPE_WRITES=enabled`), its contract held on a seeded account ([contract](docs/contract/billing-platform.md)) | Pennylane; Qonto for payment evidence |
| Untrusted input | injection screen before the model; unknown and impersonating senders handled without the model; long messages read by a person; automated mail recognised by rule and never answered automatically; no automatic reply to an unknown sender, who goes to a triage mailbox; the message masked before the model | authentication results from a trusted gateway |
| Evaluation | 34 development cases in CI, case-by-case baseline, unsafe-action gate, decision-boundary taxonomy | a new blind holdout (being written outside this repository), an adversarial set, a set large enough to calibrate |
| Operations | audit in SQLite, OpenTelemetry spans, read-only MCP server, cost reconciliation | deployment, monitoring and alerting, encrypted backups |

**Measured today** (mock engine, every push): **29/34 fully correct, 0/34
unsafe actions**. The Claude engine was last measured on the original twelve
cases ([evaluation](#evaluation)).

---

## Architecture

![Architecture: a dispute arrives through n8n, the application loads the billing context, runs deterministic rules, asks a decision engine only when needed, prices a correction in code when it can, and the action policy decides whether a person must approve. Ports for billing, decisions and usage exist, the billing one with the fake and Stripe (test mode); the support tool and bank ports, and every adapter not built, are dashed as planned.](docs/architecture.svg)

<sub>What does not exist is drawn as not existing: every dashed port and
adapter is planned, not built.</sub>

The application depends on contracts, not on vendors. `investigate_dispute.py`
imports `BillingProvider`, never `stripe`, and in the HTTP application
`app/api/deps.py` is the only module that names a concrete implementation. Swapping the billing platform or
the model is an adapter, not a rewrite.

The target integration -- the support tool owns the case, a durable workflow
owns its lifecycle, ResolveBilling investigates and records -- is described in
[ADR-010](docs/adr/010-target-integration-architecture.md) and not built.

---

## Design choices

Each is a decision with a record, and each record says what would change it.

| Choice | Why | Record |
|---|---|---|
| Billing-provider agnostic | Platforms map onto one domain; the rules never see a vendor's vocabulary | [ADR-001](docs/adr/001-provider-agnostic-billing.md) |
| Decision-engine agnostic | The model can change for residency, contract, cost or latency, not only quality | [ADR-002](docs/adr/002-decision-engine-agnostic.md) |
| Deterministic before probabilistic | What code can settle is settled before the model; the model receives the findings and must not redo them; the audit records what each call cost | [ADR-003](docs/adr/003-deterministic-before-ai.md) |
| A person approves every financial action | The engine recommends, the policy authorises; a wrong engine cannot widen what is allowed | [ADR-004](docs/adr/004-human-approval-financial-actions.md) |
| Money as integer minor units | One rounding function, per-currency exponents, floats refused | [ADR-005](docs/adr/005-money-as-minor-units.md) |
| No agent framework | No loop, no tools, a closed output: a graph orchestrator would add no node | [ADR-006](docs/adr/006-no-agent-framework.md) |
| An unknown sender gets a result | Nothing about an account is said to someone nobody has verified | [ADR-007](docs/adr/007-unknown-sender.md) |
| One claim per execution; no answer is not a no | A timeout after a write is reconciled, never retried blind | [ADR-008](docs/adr/008-execution-state-machine.md) |
| Trust boundaries | Every route that acts takes an authenticated caller; the identity on a decision is the signed-in one | [ADR-009](docs/adr/009-trust-boundaries.md) |
| No automatic reply unless safe | Needing no approval is not permission to answer; detected automated mail and unknown senders never get an automatic reply | [ADR-011](docs/adr/011-automated-mail-and-automatic-replies.md) |

Why there is no RAG, no function calling, no multi-agent setup and no
framework, and the condition under which each would come in, is argued in
the [engineering notes](docs/engineering-notes.md#techniques-this-does-not-use-and-what-would-change-that).

---

## Limits

What the system does not do yet, said plainly.

- **Stripe in test mode only; no support tool or deployment.** A dispute
  can be investigated on a Stripe test account's data; an approved
  correction is written there only with `STRIPE_WRITES=enabled`, as one
  credit note for the whole amount, on an invoice open with nothing paid or
  paid in full. Data the domain cannot represent yet -- tax, discounts,
  credits, partial refunds, mixed periods -- is refused with its reason,
  never approximated. The demo, CI and the evaluation still run on fake
  data.
- **Masking is not anonymisation.** Before the model, identifiers, the
  customer's address and name, bank, card and phone numbers are masked in
  the message; other people's names and free text pass
  ([ADR-009](docs/adr/009-trust-boundaries.md)).
- **No reply is sent automatically yet.** The workflow's automatic-reply
  branch is a placeholder and no reply drafter exists; what the system
  decides is whether one *may* be sent (`automatic_reply_allowed`,
  [ADR-011](docs/adr/011-automated-mail-and-automatic-replies.md)).
- **Spam filtering is not here.** Automated mail is recognised from the
  headers the intake forwards; without headers, only the sender address is
  checked. Filtering spam belongs to the support tool in the target
  integration.
- **Decided, not yet built**: the automatic check of outgoing text, which
  comes with the reply drafter (what each channel may disclose is written
  down, channel by channel, in
  [ADR-009](docs/adr/009-trust-boundaries.md#disclosure-channel-by-channel-2026-10-19)),
  and the engine-side half of the usage-gap decision (a prompt instruction,
  which needs a measured prompt version). The decisions and their status are
  in [`evals/taxonomy.md`](evals/taxonomy.md#4-specification-questions).
- **What a good evaluation score does not cover**: approval expiry,
  concurrent execution and uncertain provider outcomes are out of reach of an
  evaluation case and are tested elsewhere; what a reply discloses is visible
  in a case but not asserted yet; sender authentication is a property the
  product does not have. Each limit, and what covers it, is listed in the
  [taxonomy](evals/taxonomy.md#3-what-the-evaluation-set-does-not-test).
- **Confidence is self-reported.** Thirty-four cases calibrate nothing, so
  the autonomy thresholds stay shut.

---

## Evaluation

```bash
python -m evals.runner --engine mock      # free, deterministic, every push
python -m evals.runner --engine claude    # real calls, on demand
```

**Development set.** 34 cases, each with a `why` field saying what it is
there to catch. They were read while the rules and the prompt were written, so
a score on them says how well the system fits them, not how it does on a
dispute it has not seen. [`evals/taxonomy.md`](evals/taxonomy.md) lists the
decision boundaries they cover, the ones they miss, and their redundancies.

**What CI enforces.** The build fails if the system would move money where
the case set says none moves, propose money without a human or to an
unverified sender, or answer a suspected impersonator. It fails again below
the mock's measured score (29), and again if any check of any case differs
from the recorded baseline -- an improvement included, until the baseline is
regenerated in the same commit.

**Blind holdout.** A holdout is written by a model of another family than
the engine, every label adjudicated by hand, never run in CI and never tuned
against. The first one lived in this repository, where every session working
on the system could read it; it has joined the development set, and a new one
is being written in a separate private repository, with its own data. Until it
is adjudicated and frozen there is no blind measurement.

**The first measurement**, on the original twelve cases, 2026-09-21. The mock
and v3 columns are as scored then; the v2 column is that run rescored against
the case set corrected the same day (its raw score was 8/12, and the
[engineering notes](docs/engineering-notes.md#how-the-evaluation-was-read)
say why):

| | mock | claude, prompt v2 | claude, prompt v3 |
|---|---|---|---|
| classification | 11/12 | 10/12 | **12/12** |
| recommended action | 9/12 | 10/12 | **12/12** |
| approval gate | 11/12 | 12/12 | **12/12** |
| fully correct | 9/12 | 10/12 | **12/12** |
| **unsafe actions** | **0/12** | **0/12** | **0/12** |

Held to today's case set with `evals.rescore`, both Claude runs count one
unsafe action: the proration case, where the system then proposed the whole
invoice as a credit note. The engine was right; the amount the code attached
was not, and the code was fixed. Twelve synthetic cases written by the
prompt's author are a consistency check, not a benchmark: the
[engineering notes](docs/engineering-notes.md#how-the-evaluation-was-read)
say what this table can and cannot be read for, and how the evaluation itself
was corrected after its first run.

---

## How it is built

The product is built with spec-driven development and AI coding agents, under
rules that keep a person in charge of every decision that matters.

- **Specification first.** A change starts from a specification or a bug
  record (GitHub Spec Kit): expected behaviour, invariants, exclusions,
  evidence, acceptance. The [constitution](.specify/memory/constitution.md)
  holds the principles every change respects; the product's current
  behaviour is to be kept, capability by capability, in a
  [living contract](docs/contract/README.md), written as each capability is
  first touched -- none is covered yet; [CONTRIBUTING](CONTRIBUTING.md) is the
  procedure.
- **Two agents from different vendors.** Claude Code and Codex write the
  code, including the core, and review each other's work; neither approves
  its own.
- **A person decides.** The maintainer owns objectives, invariants,
  architecture and acceptance, adjudicates every evaluation label, approves
  every push and merges every pull request.
- **Measured, not asserted.** A behaviour change is done only once it is
  measured on the evaluation set, and every change keeps the existing cases
  passing in CI.
- **Next: a factory.** A deterministic controller that runs these steps with
  fixed contracts, sandboxed agents and recorded evidence is designed, and
  will live in its own repository once it runs. Today the collaboration is
  driven by hand.

---

## Roadmap

**First milestone: reliable resolution end to end, in a supervised pilot.** A
dispute arrives through the support tool, is investigated with its evidence,
approved by an authenticated person, executed exactly once at the billing
platform, and traced and monitored throughout. It is declared reached only
with evidence of four failures handled: a restart during an approval, an
expired approval, a repeated delivery, and a timeout after a write.

On the way, in order:

1. **The engine-side half of the usage-gap decision**, in a measured prompt
   version; the outgoing-text check comes with the reply drafter.
2. **The new blind holdout**, generated in an isolated environment and
   adjudicated, then a reference run.
3. **Real integrations**: Stripe in test mode -- reading and writing
   done, their contract held on a seeded account -- then Pennylane as a
   second billing platform, and Qonto as evidence that a payment arrived.
4. **A demonstration company**: identity provider (OIDC), support tool,
   monitoring and alerting, secrets management, signed images, tested
   backups, on free infrastructure, each behind a port so a real company can
   substitute its own.
6. **Later**: a reply drafter as a second port, a second engine evaluated
   head to head, a durable workflow engine once a spike justifies it, partial
   correction amounts once real billing data shows their shape.

---

## Run it

```bash
pip install -e ".[api,dev,mcp,otel]"
pytest
python -m evals.runner --engine mock
```

A case file kept outside this repository brings its own data through a
second fixture directory, and `--check-only` checks the plumbing without
calling an engine, printing totals only. It checks neither the expected
decisions nor the investigation's 180-day lookback.

```bash
RESOLVE_EXTRA_FIXTURES_DIR=../holdout/fixtures python -m evals.runner --check-only --cases ../holdout/cases.json
```

The API is closed by default and answers 503 until callers are configured
(ADR-009). Starting it, calling it, the approval flow, the n8n workflow,
tracing, the MCP server, cost reconciliation and querying the audit are in
[`docs/operations.md`](docs/operations.md).

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
| [ADR-007](docs/adr/007-unknown-sender.md) | An unknown sender gets a result, not an error |
| [ADR-008](docs/adr/008-execution-state-machine.md) | Executing a financial action: one claim, and no answer is not a no |
| [ADR-009](docs/adr/009-trust-boundaries.md) | Trust boundaries: who may call, and whose identity a decision carries |
| [ADR-010](docs/adr/010-target-integration-architecture.md) | Target integration: the support tool owns the case, a durable workflow owns the lifecycle *(not built)* |
| [ADR-011](docs/adr/011-automated-mail-and-automatic-replies.md) | Automated mail, and who may receive an automatic reply |
| [ADR-012](docs/adr/012-constitution-and-product-contract.md) | The constitution, the product contract, and where factory rules live |

More: [engineering notes](docs/engineering-notes.md) (incidents, review
findings, techniques not used) · [operations](docs/operations.md) ·
[decision boundaries](evals/taxonomy.md).
