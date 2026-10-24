# Product contract

What ResolveBilling does today, by capability: the living contract the
[constitution](../../.specify/memory/constitution.md#product-contract)
describes. Each capability gets one file, `<capability>.md`, written the
first time a change touches it, before that change is folded in. The reasons
for this layout are in
[ADR-012](../adr/012-constitution-and-product-contract.md).

## Coverage

Four capabilities are covered. For the others, behaviour is described by the
existing records, which remain authoritative.

| Capability | Contract | Authoritative today |
|---|---|---|
| Billing platform | [billing-platform.md](billing-platform.md) | the contract, then ADR-001, ADR-008 |
| Intake and sender identity | not written | ADR-007, ADR-009, ADR-011 |
| Investigation and rules | not written | ADR-003, `evals/taxonomy.md` |
| Decision engine | [decision-engine.md](decision-engine.md) | the contract, then ADR-002, ADR-006, ADR-009 (model provider) |
| Money and corrections | not written | ADR-004, ADR-005, `.specify/bugs/d14-silent-rules/` |
| Approval and execution | [approval-and-execution.md](approval-and-execution.md) | the contract, then ADR-004, ADR-008 |
| Disclosure and replies | not written | ADR-009 (disclosure), ADR-011 |
| Evaluation | [evaluation.md](evaluation.md) | the contract, then README "Evaluation", `evals/taxonomy.md`, docs/operations.md |

This first split follows what exists; it can change when a contract is
written. A behaviour that spans capabilities is described in each one it
affects, and the global invariants of the constitution always apply.
