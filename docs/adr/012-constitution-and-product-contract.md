# ADR-012 — The constitution, the product contract, and where factory rules live

**Status:** accepted · 2026-10-21 · adopts Spec Kit; records three governance
choices and the sources behind them

## Context

ResolveBilling is built with spec-driven development (SDD) and coding agents
from two vendors, Claude Code and Codex, under one maintainer. Adopting
GitHub Spec Kit brings a **constitution** -- the project's non-negotiable
principles, read by its planning and implementation commands (not by the bug
extension's, which is why `CLAUDE.md` now sends every agent to it) -- and
**feature directories**
(`specs/<nnn-feature>/`: spec, plan, tasks), one per piece of work.

A draft constitution written on 2026-10-17 had fallen behind the code within
days (sender identity, message masking, automated mail, uncertain checks)
and claimed more than was true ("every principle is already true"). Revising
it raised two questions that the draft left open.

- **A. Where do the rules of an agent factory belong?** A factory -- a
  deterministic controller that runs agents through contracts, gates and
  reviews -- is designed, with a responsibility matrix: four levels of human
  involvement (N1 to N4), budgets, bounded correction, stop conditions. Should
  that matrix be written into the product's constitution?
- **B. Where does the product's current contract live?** Spec Kit's feature
  directories describe one change each. Something must say what the product
  does *now*, or every new spec re-investigates the code and nobody can tell
  which old spec is still true.

Both were argued between the two agents, in writing, before the maintainer
decided: each stated its position, answered the other's strongest argument,
and said where it could move. A third question came from the maintainer on reading the
draft:

- **C. Should the constitution forbid agent loops in the product?** The
  draft carried ADR-006 over as a principle.

## Decision A — product authorities in the constitution, mechanisms in the factory

**The constitution states the authorities the product requires; the factory's
governance states how a factory meets them.**

- The constitution names a short, closed list of sensitive areas, judged by
  the effect of a change and not by the files it touches: the money-moving
  core and its policies, what a model's output may set off, trust boundaries
  and disclosure, the evaluation
  gates and the holdout policy, and normative changes to the constitution,
  an ADR or the product contract. Any such change needs the maintainer's
  explicit approval of the final diff, identified by its commit. An
  unresolved incident blocks delivery; silence is never consent.
- The factory keeps its full matrix, budgets, retries and transitions, in its
  own repository. It may add requirements and never remove the
  constitution's.
- `CONTRIBUTING.md` gives today's manual procedure. It does not link to the
  factory's repository until that repository exists.

**The argument for keeping the matrix out** (Claude Code): the constitution
belongs to the product, the factory to its own reusable repository; a rule
with two homes drifts; the matrix has never classified a real task, so
ratifying it would overstate; and tuning a retry count should not be a
constitutional amendment.

**The argument for putting it in** (Codex): the factory's rules bind only what
the factory runs. A contributor who changes the financial approval policy
without the factory must still find, in the product repository, that the
change needs a human reading of the final diff -- not agreement on a summary.
That requirement is the product's, not the factory's. "One authority per
rule" forbids two competing matrices, not a product requirement and a
mechanism that satisfies it. An untested rule calls for a maturity caveat,
not exclusion.

**Where each moved.** Codex withdrew the full matrix: budgets, retries and
operational levels are factory mechanics. Claude Code accepted that the
acceptance authority for sensitive changes is a product requirement, and
asked that "sensitive" be a closed list tied to existing principles, so the
matrix would not return through the side door. Codex added that the list is
judged by effect: a dependency or CI change can alter a trust boundary, and a
typo in an ADR does not change a decision. Two wording points came with it:
"maintainer", not "author", so code authorship and approval authority are not
confused; and changing the holdout policy is distinct from access to the
cases.

## Decision B — a living product contract by capability, filled as it is touched

**The product's current behaviour lives in `docs/contract/<capability>.md`.**
Feature directories and bug records are delivery history once merged; ADRs
hold reasons; the constitution holds invariants. The constitution records this
model, which Spec Kit's own documentation asks a project to do.

- **Named now, filled progressively.** A capability's contract is written the
  first time a change touches it, before the change is folded in, starting
  from what the ADRs, README and `evals/taxonomy.md` already say. Coverage is
  declared in `docs/contract/README.md`; for an uncovered capability the
  existing records stay authoritative. A contradiction between sources goes to
  the maintainer.
- **Same pull request, consolidated before approval.** A behaviour change
  edits the contract in the pull request that changes the code, and the
  approved diff already contains the consolidated contract. A delta written
  in a feature spec may help the work; it is never a second authority, and
  nothing is rewritten after approval.
- **A declaration, not a proof.** Every pull request says "behaviour changed"
  (capabilities and requirements affected) or "behaviour unchanged" (with a
  reason). Review judges concordance. A future check may verify that the
  declaration is present; it cannot verify that contract and code agree --
  "mapped code changed, so the contract must change" fails both ways
  (refactors touch it needlessly, a cosmetic contract edit passes a real
  change).
- **Loaded by capability.** An agent loads the contracts of the capabilities a
  change touches, and of those that depend on them -- not the whole contract.

Claude Code first proposed deciding the location at the first feature.
Spec Kit's documentation, which Codex found, changed that: the persistence
model is a project convention to record up front, and leaving it to the first
feature would let one change set the layout for all.

## Decision C — the constitution bounds what a model may set off, not how it is orchestrated

The 2026-10-17 draft carried ADR-006 into the constitution as "No agent
framework on the main pipeline": no loop, no tool, a model's output never
fed back to a model. The maintainer objected, and the objection held on
three counts.

- **It froze a choice its own record expects to revisit.** ADR-006 states
  when it would change: a refinement loop, or a flow beyond about five nodes
  with mid-run resumption. In a constitution, that change becomes a major
  amendment; the constitution itself leaves architectural boundaries to the
  ADRs.
- **It contradicted the project's aim**, an agentic dispute workflow.
- **It guarded the wrong thing.** A first rewrite, "a model proposes, it
  never acts", was wrong too, as the maintainer pointed out: once the reply
  drafter exists, text the model wrote will reach a customer with no person
  reading it, whenever the automatic-reply decision allows. The model does
  act.

The invariant that holds is therefore about bounds, not orchestration:
**what a model's output may set off is decided by code and by the
constitution, never by the model.** Money and platform writes always take a
human approval; a message goes out on its own only when the deterministic
reply decision allows it and the outgoing text has been checked; the model
writes content, code decides whether to send, to whom and about which
account; steering outputs are closed sets; tools read-only and scoped to the
dispute's customer; loops bounded. Widening those bounds joins the sensitive
areas that need the maintainer's approval of the final diff. ADR-006 stays
the record of today's orchestration -- none -- and can change by a new ADR
without touching the constitution, as long as the bounds hold.

## What practice shows (sources consulted, 2026-10-21)

No SDD tool imposes a location for a product-wide current contract; the
patterns differ in what they keep after a change ships.

- **GitHub Spec Kit** documents three models after delivery -- *flow-back*
  (feature artifacts edited in place), *flow-forward* (completed feature
  directories immutable; each change gets a new one) and *living spec*
  (`spec.md` per feature is the contract, plan and tasks regenerated). None
  is the default; the choice is to be recorded in the constitution or
  onboarding notes. Its living spec is per feature, not product-wide.
  [Spec Persistence Models](https://github.com/github/spec-kit/blob/main/docs/concepts/spec-persistence.md)
- **OpenSpec** separates `openspec/specs/<domain>/` (current behaviour, the
  source of truth) from `openspec/changes/<change>/` (proposal, design, tasks
  and spec deltas marked added, modified, removed), merged into the current
  specs and archived when a change completes. The closest model to this
  decision, adopted here as a layout without the tool.
  [Overview](https://github.com/Fission-AI/OpenSpec/blob/main/docs/overview.md)
- **Kiro (AWS)** keeps durable context in steering files
  (`.kiro/steering/product.md`, `tech.md`, `structure.md`) and work in
  `.kiro/specs/<feature>/` (requirements, design, tasks). Steering orients
  the agent; it is not an exhaustive behavioural contract.
  [Steering](https://kiro.dev/docs/steering/), [Specs](https://kiro.dev/docs/specs/)
- **SpecKit Companion**, a community extension, adds living specs per
  capability (`capabilities/<name>/<name>.spec.md`, mapped to code by globs),
  folds each feature's added/modified/removed sections into them in the same
  pull request, and detects drift. Third-party, not core Spec Kit.
  [Living Specs in SpecKit](https://dev.to/alfredoperez/living-specs-in-speckit-3a3j)
- **Birgitta Böckeler (Thoughtworks), on martinfowler.com**, distinguishes
  *spec-first* (written before the work), *spec-anchored* (kept and
  maintained as the system evolves) and *spec-as-source* (the spec is the
  maintained artifact, code derived from it), and finds most tools at the
  first level; she warns that spec-as-source risks combining model-driven
  development's inflexibility with a model's non-determinism. This decision is
  spec-anchored.
  [Understanding Spec-Driven Development: Kiro, spec-kit, and Tessl](https://www.martinfowler.com/articles/exploring-gen-ai/sdd-3-tools.html)
- **Thoughtworks Technology Radar**: teams using Spec Kit work mostly on
  existing codebases; useful constitutions hold scope, versions and structure;
  continually added context grows into instruction bloat and context rot --
  the reason contracts are loaded by capability here.
  [Spec-driven development](https://www.thoughtworks.com/en-cl/radar/techniques/spec-driven-development),
  [GitHub Spec Kit](https://www.thoughtworks.com/es/radar/languages-and-frameworks/github-spec-kit)
- **A comparison of Spec Kit and OpenSpec** notes that per-feature folders
  make the current state of the system hard to assemble.
  [intent-driven.dev](https://intent-driven.dev/knowledge/spec-kit-vs-openspec/)
- **Tessl** keeps specs and tests in the codebase as the long-term memory of
  intended behaviour; Böckeler describes each spec placed beside the code it
  generates, that code marked as generated, aiming at spec-as-source. **BMAD**
  separates the product requirements document (what to build and why) from
  the epics and stories that divide the work. Neither documents a single
  consolidated behavioural contract maintained after each change.
  [Tessl](https://tessl.io/blog/how-tessls-products-pioneer-spec-driven-development),
  [BMAD requirements](https://docs.bmad-method.org/plan/define-requirements-and-a-specification/)

These sources document mechanisms and recommendations; they do not show how
often teams actually maintain such contracts.

## Consequences

- Once the maintainer ratifies it, the constitution is at version 1.0.0
  with these two sections, and Spec Kit is merged; its commands grant no
  permission of their own.
- No contract file exists yet. The first change to touch a capability writes
  its contract, which makes that change larger than it would otherwise be.
- Until a capability is covered, two kinds of record coexist: the contract
  for covered capabilities, the older records for the rest. The coverage
  table says which is which.
- The factory, when it runs, must meet the constitution's list of sensitive
  areas with its own levels; a factory level that skipped the maintainer's
  approval of the final diff on one of them would contradict this record.
- Ruff skips only Spec Kit's vendored scripts, not `.specify/` as a whole,
  so project files placed there are still linted.

## What would change this

- Spec Kit, or another tool adopted later, shipping a product-wide contract
  model that does the same job with less manual upkeep.
- Contracts falling behind the code in practice: then a stronger mechanism
  than declaration and review is needed, or the model is wrong.
- The factory needing a product-side rule this list does not cover, or the
  list turning out to stop ordinary work for no gain.
