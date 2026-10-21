# ResolveBilling — agent instructions

- Read the constitution (`.specify/memory/constitution.md`) before any
  change; it is authoritative and these lines must not contradict it. Then
  read `docs/contract/README.md` and the contract of every capability the
  change affects, and of those that depend on it.
- Read `docs/adr/` before touching an architectural boundary; the ADRs are
  authoritative. In particular ADR-006: no agent framework on the main
  pipeline.
- Deterministic rules run before any model call. Human approval comes
  before any financial mutation. Neither is negotiable.
- A change is done only with the evidence its kind calls for: a behaviour
  change is measured on the eval suite (`evals/`), and the existing cases
  still pass in CI.
- Everything in this repository — code, README, ADRs, docs, commit
  messages — is in English. Customer messages in evaluation cases and
  fixtures, and quoted third-party text, keep their own language.
- Run `pytest` and `ruff check` before committing.
- Never commit `PLAN.md`, `notes/`, `.env` or any credential.
