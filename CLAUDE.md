# ResolveBilling — agent instructions

- Read `docs/adr/` before touching an architectural boundary; the ADRs are
  authoritative. In particular ADR-006: no agent framework on the main
  pipeline.
- Deterministic rules run before any model call. Human approval comes
  before any financial mutation. Neither is negotiable.
- An addition is done only once it is measured on the eval suite
  (`evals/`) and the existing cases still pass in CI.
- Everything in this repository — code, README, ADRs, docs, commit
  messages — is in English.
- Run `pytest` and `ruff check` before committing.
- Never commit `PLAN.md`, `notes/`, `.env` or any credential.
