# ResolveBilling Constitution

This constitution states what every change to ResolveBilling must respect,
whoever or whatever makes it: the maintainer, a contributor, a coding agent
or an agent factory. Its principles are obligations. Each one names the
records that currently implement it; where a rule is adopted but not yet
demonstrated in use, the text says so. The reasons behind the governance
choices below are in [ADR-012](../../docs/adr/012-constitution-and-product-contract.md).

## Core Principles

### I. Deterministic Before Probabilistic

Deterministic rules MUST run before any model call. A model is consulted only
for the ambiguity the rules leave: a higher rung is never climbed when the one
below settles the dispute.

*Implemented by:* ADR-003; README, "Design choices". *Rationale:* code is
free, instant and testable exactly; a model is none of the three.

### II. Sufficient Evidence, Then Human Approval, Before Any Financial Mutation

A correction is proposed only when the evidence supports it and its amount
can be justified by code. A check that cannot conclude MUST say so, and any
such uncertainty on the disputed invoice blocks every correction: the case
goes to a person without an amount. The engine recommends, the action policy
authorises, a person approves, and execution writes exactly the instrument
that was approved. No path moves money without a recorded human approval. An
approval holds only for the proposal and invoice that were shown, and only
until its deadline. Autonomy thresholds stay shut; a self-reported confidence
never opens them, and opening them, even on a calibrated confidence, takes a
new explicit decision of the maintainer.

*Implemented by:* ADR-004; ADR-008, including "An approval holds for what was
shown, and not forever"; `.specify/bugs/d14-silent-rules/`. *Rationale:*
swapping the engine, or the engine going wrong, must not widen what the
system is permitted to do.

### III. Provider- and Engine-Agnostic Boundaries

The domain and application layers MUST NOT name a billing platform or a model
vendor. Vendors exist only behind the `BillingProvider` and `DecisionEngine`
ports, and a platform's vocabulary only in its mapper.

*Implemented by:* ADR-001; ADR-002. *Rationale:* the engine and the platform
change for reasons unrelated to quality: residency, contract, cost, client
policy.

### IV. Money Is an Integer Count of Minor Units

Amounts are integers in minor units, with one rounding function and a
per-currency exponent table. Floats are rejected at runtime; arithmetic across
currencies raises.

*Implemented by:* ADR-005.

### V. No Agent Framework on the Main Pipeline

The product contains no agent loop: nothing routes a model's output back into
a model, and the model holds no tools. The classification and the
recommended action take values in closed sets; the explanation is free text
and the evidence references are checked against what was issued. This
constrains the product, not the tools used to develop it.

*Implemented by:* ADR-006.

### VI. Auditable by Design; the Platform Is Authoritative

Every investigation that completes leaves a run row naming the provider, the
engine and its version, the confidence and its kind, the findings with their
evidence, the authorisation verdict and its reason, and the cost -- recorded
as unknown when no price is known, never as zero. Run rows are never amended
in place; approval and execution are a separate lifecycle. No ledger is kept
here: the billing platform is the authority on amounts and on what an
invoice can still give back.

*Implemented by:* docs/engineering-notes.md, "Production considerations";
ADR-008, "The platform is authoritative on amounts and limits". *Rationale:*
amending a record erases the difference between what was decided then and
what happened since.

### VII. Untrusted Input, Explicit Trust Boundaries

The dispute message is untrusted. It is screened before any model sees it,
and what reaches a model provider is reduced: the context is pseudonymised
and the message masked -- masking, not anonymisation. The sender's address
resolves the sender to a customer; it does not authenticate them, and a
customer identifier in a request is only a claim, which an address that does
not support it turns into a case for a person, without loading the claimed
account. An unknown sender gets a result, not an error, and nothing about any
account. Sending text is authorised separately from moving money: only the
automatic-reply decision permits an automatic reply, and detected automated
mail or an unknown sender never gets one. What each channel may disclose is
limited to its recipients. Every route that acts takes an authenticated
caller, the identity recorded is the authenticated one, and such a route
refuses when its kind of caller is not configured.

*Implemented by:* ADR-007; ADR-009, including "Disclosure, channel by
channel"; ADR-011; `.specify/bugs/d9-sender-matches-customer/`,
`.specify/bugs/d10-message-masking/`.

### VIII. Measured, Not Asserted

A change is done only with the evidence its kind calls for: a behaviour
change is measured on the evaluation set, and every change keeps the existing
gates green. CI fails on any unsafe action, on a mock score below its floor,
and on any case whose checks move from the recorded baseline; the mock's
accepted errors are watched by that baseline, not hidden. A figure is
published with the size of the set it was measured on, and the runs behind
published engine figures are stored where they can be inspected.

*Implemented by:* docs/operations.md, "What CI actually enforces"; README,
"Evaluation"; `evals/results/README.md`.

### IX. Holdout Discipline

A blind measurement uses cases kept apart from development: stored outside
this repository, generated independently of the engine being measured, every
label adjudicated by the maintainer, then frozen. No blind measurement is
claimed before the set is frozen. Recusal runs both ways: a session that
tunes the system does not read the cases, and a session that has read them
does not then change the prompt, the rules or what the engine receives.
Changing the holdout policy is a change to this constitution; access to the
cases themselves is the maintainer's alone to grant. Cases that can no longer
serve as a blind test move to the development set and are replaced.

*Implemented by:* README, "Evaluation". *Rationale:* a score on cases the
system was fitted to says how well it fits them, not how it does on a
dispute it has not seen.

## Product Contract

The product's current behaviour is described by a **living contract**,
organised by capability, under `docs/contract/`. Feature directories under
`specs/` record one delivery each; bug records under `.specify/bugs/` record
one repair each; both become history once merged. ADRs hold the reasons for
durable decisions; this constitution holds the invariants.

- **Coverage is declared.** A capability's contract is written the first time
  a change touches it, before that change is folded in, and
  `docs/contract/README.md` lists which capabilities are covered. For a
  capability not yet covered, the existing records (ADRs, README, docs,
  `evals/taxonomy.md`) remain authoritative. A contradiction found between
  sources goes to the maintainer; no agent reconciles it silently.
- **Contract and code change together.** A behaviour change edits the
  contract in the same pull request, and the contract is already
  consolidated in the final diff the maintainer approves, never rewritten
  after approval.
- **Every pull request declares** "behaviour changed", with the capabilities
  and requirements affected, or "behaviour unchanged", with a reason.
  Concordance between contract and code is judged in review; a future
  automatic check may verify that the declaration exists, never that the
  contract and the code agree.

Spec Kit's documentation asks a project to record how its specs persist
after delivery; this is that record, and ADR-012 says why this model.

## Additional Constraints

- The repository is in English: code, README, ADRs, docs, comments and commit
  messages. Customer messages in evaluation cases and fixtures, and quoted
  third-party text, keep their own language.
- `PLAN.md`, `notes/`, `.env` and credentials are never committed.
- `pytest` and `ruff check` run before every commit.
- The ADRs are authoritative on architectural boundaries and are read before
  one is touched.

*Implemented by:* CLAUDE.md; `.gitignore`.

## Development Workflow

Adopted by the maintainer on 2026-10-10 and revised on 2026-10-16,
2026-10-17 and 2026-10-21. Where a practice is adopted but not yet shown in
use, it is marked *(adopted)*. CONTRIBUTING.md gives today's procedure step
by step.

- **Roles.** The maintainer owns the objectives, the business invariants, the
  architecture and acceptance: approves specs, contracts, ADRs and plans,
  adjudicates evaluation labels, approves every push and merges every pull
  request. Coding agents write the code, the money-moving core included, under
  the gates below. Today the collaboration is driven by hand; an agent factory
  with a deterministic controller is designed to run these steps and will have
  its own governance *(adopted)*. A decision the approved scope does not settle
  goes back to the maintainer.
- **Changes that need the maintainer's approval of the final diff.** Any
  change to the behaviour or the requirements of the following, judged by its
  effect and not by the files it touches, needs the maintainer's explicit
  approval of the final diff, identified by its commit:
  - the money-moving core and its policies (II, IV);
  - trust boundaries and disclosure (VII);
  - the evaluation gates and the holdout policy (VIII, IX);
  - normative changes to this constitution, to an ADR or to the product
    contract.

  A dependency or CI change that alters one of these is such a change; a
  typo in an ADR is not. Every other change still follows the review,
  verification and publication rules below; it only does not need this
  approval. How an agent factory classifies and gates work more finely is
  defined by the factory's own governance, which may add requirements and
  never remove these.
- **Every change starts from a spec or a bug record.** It names the
  observable behaviour expected, the invariants it touches, what it excludes,
  the evidence expected and the condition for acceptance. Tests derive from
  those expectations, not from the code that was generated.
- **Spec-driven development, with Spec Kit.** A feature follows specify, plan,
  tasks, implement and converge *(adopted)*. A production feature, and any
  change to the money-moving core, takes the full path, adding clarify,
  checklist and analyze. A repair follows the bug process: assess, fix, test.
  A reviewer-owned checklist is never marked satisfied by an agent. Spec Kit's
  commands grant no permission: approvals, manual publication and holdout
  recusal apply to them as to any other step.
- **Done includes operation.** A slice is not done until its security,
  observability and recovery are: malformed input, resource limits, traces
  without sensitive data, and for a money-moving slice, timeouts, idempotency
  and reconciliation (ADR-008).
- **A defect is fixed with a test shown to fail on the code before the fix.**
- **Independent verification.** A diff is reviewed by a model from another
  vendor than the one that wrote it, reading a read-only checkout of the final
  commit and not the author's conversation. The gate results that count are
  obtained on that commit independently of the author: by CI, or by the
  maintainer; results reported only by the authoring agent leave the property
  *not demonstrated*. Any change after review invalidates the review and the
  gates.
- **Publication.** Merging is the maintainer's alone. Today an agent may push
  a branch and open a pull request after the maintainer's explicit approval
  of that push; the target is that no agent holds a credential that can push,
  merge or deploy *(adopted)*. An unresolved incident blocks delivery, and an
  approval that has not been given is never assumed: silence is not consent.
- **Every factory cycle is measured** *(adopted)*. Time per phase, the
  maintainer's active time, cost, rework loops, review findings by severity,
  gate failures caught before merge, the CI result and defects found after
  merge. Each checked property is reported as passed, failed or not
  demonstrated: missing evidence is not compliance.

## Governance

This constitution supersedes any practice that conflicts with it. Amendments
are made by the maintainer, each with a version bump and a dated rationale:
MAJOR for removing or redefining a principle, MINOR for a new principle or
section or materially expanded guidance, PATCH for wording. CLAUDE.md remains
the agents' short operating rules and MUST NOT contradict this document. A
plan or review that departs from a principle states which one, and why.

**Version**: 1.0.0 | **Ratified**: TODO(RATIFICATION_DATE): set when the maintainer approves this text | **Last Amended**: TODO(RATIFICATION_DATE)
