# Contributing

How a change reaches `main` today. The rules it follows are in the
[constitution](.specify/memory/constitution.md); this file is the procedure,
and it is manual: no agent factory runs these steps yet.

## 1. Start from a spec or a bug record

- **A feature** starts with Spec Kit: `/speckit-specify`, then
  `/speckit-plan`, `/speckit-tasks`, `/speckit-implement`,
  `/speckit-converge`. A change to the money-moving core, or a production
  feature, adds `/speckit-clarify`, `/speckit-checklist` and
  `/speckit-analyze`.
- **A repair** starts with a bug record in `.specify/bugs/<slug>/`
  (`/speckit-bug-assess`, then `/speckit-bug-fix`, `/speckit-bug-test`), and
  a test that fails on the code before the fix.
- **The maintainer approves** the spec, the bug record's remediation or the
  plan before code is written.

The Spec Kit commands are installed for Claude Code (`.claude/skills/`).
Another agent, Codex for instance, follows the same steps by reading those
`SKILL.md` files and the templates in `.specify/templates/`; nothing else is
installed for it. No command grants a permission the constitution does not.

## 2. Keep the product contract in step

The product's current behaviour is described under
[`docs/contract/`](docs/contract/README.md), by capability.

- If the change alters behaviour, edit the contract of each capability it
  affects **in the same pull request**. If that capability has no contract
  yet, write it first from the existing records (ADRs, README, docs,
  `evals/taxonomy.md`), then fold the change in, and add it to the coverage
  list.
- If two sources disagree, stop and ask the maintainer.

## 3. Check before committing

```bash
ruff check .
pytest
python -m evals.runner --engine mock --min-fully-correct 29 --baseline evals/baselines/mock.json
```

The floor and baseline are the ones CI enforces; a change that moves a case
regenerates the baseline in the same commit and says why. A change to how the
engine decides is also measured with the real engine, and the run is stored
in `evals/results/`.

## 4. Review by another vendor

The diff is reviewed by a model from another vendor than the one that wrote
it, on a read-only checkout of the final commit. Findings are fixed, and any
fix after review is reviewed again.

## 5. Publication

- The maintainer approves the push. An agent may push a branch and open the
  pull request only after that approval.
- The pull request says **"behaviour changed"** (with the capabilities and
  requirements affected) or **"behaviour unchanged"** (with a reason).
- A change to the money-moving core and its policies, to trust boundaries or
  disclosure, to the evaluation gates or the holdout policy, or a normative
  change to the constitution, an ADR or the contract needs the maintainer's
  explicit approval of the final diff, identified by its commit.
- CI must pass on that commit. Only the maintainer merges.

## Never

- Commit `PLAN.md`, `notes/`, `.env` or any credential.
- Read, write or run holdout cases from a session that works on the system.
- Treat silence as approval.
