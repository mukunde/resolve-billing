# Implementation Plan: Pennylane credit notes

**Branch**: `015-pennylane-credit-notes` | **Date**: 2026-11-12 | **Spec**: [spec.md](spec.md)
(approved at `915cd57`)

**Status**: Draft, for the maintainer's approval (Codex r385 answered)

## Summary

Three changes, in an order that lets two of them run side by side once the first lands:

1. **The port** (one small commit, first): what every platform and the execution service
   agree on -- the per-type write capability, an optional window, the approved breakdown
   passed to the write, the breakdown observed on `ProviderAction`, a hook the adapter
   calls before each request, and the attention `partial_write`.
2. **The execution core and the store** (on the port): refusals by type, no-window
   semantics, the per-customer blocker, `partial_write`, the person's resolution route, the
   refund notice on the page and the API. Stripe and the fake move to the new signatures
   and behave as before.
3. **The Pennylane writer** (on the port): the write client, the credit lines and observed
   breakdown, `plan_write`, `create_credit_note`, `find_action`, the wiring behind
   `PENNYLANE_WRITES=enabled`.

Then, together: the contracts and ADR-008 amended, the write contract test, its seeded
worlds, the gates, the sandbox run (SC-002).

## Constitution check

- II (evidence, then approval, before any financial mutation): unchanged path; a Pennylane
  write happens only inside an approved, claimed execution; every uncertain state goes to a
  person. The amendment to ADR-008 (no window, `partial_write`, per-type capability) is part
  of the diff the maintainer approves.
- III (agnostic boundaries): the domain and application name no platform; Pennylane's
  vocabulary stays in its mapper. The blocker and the resolution are generic.
- IV (money): every amount in minor units; credit lines built with the one rounding
  function; Pennylane's decimal strings parsed by the mapper.
- V, VII: no model is involved; the resolution route takes an authenticated approver, POST
  only, the identity recorded the authenticated one (ADR-009).
- VI (audit; the platform authoritative): the cap is Pennylane's (refused at linking, seen
  on the sandbox); our guard stays; no ledger. Atomicity is a prerequisite to enabling
  writes (decision 47), stated in the contract.
- VIII: behaviour of the investigation unchanged (mock baseline); the new behaviour measured
  by tests without network and the sandbox write contract.

No departure from a principle.

## 1. The foundation (one commit, every suite passing)

It owns `app/providers/base.py` and, in `app/infrastructure/actions.py`, only the
`ATTENTION` set; Stripe's and the fake's signatures move here too, with defaults, so every
existing caller and test passes unchanged. After it, section 2 owns `actions.py`,
`execute_action.py`, `investigate_dispute.py` and the API; section 3 owns
`app/providers/pennylane/`, `scripts/pennylane_seed.py` and `app/api/deps.py`'s Pennylane
branch (the only API file it touches, a function section 2 does not edit).


- `writes: frozenset[str]` on `BillingProvider`; `can_write` kept, derived (`bool(writes)`)
  where adapters define it.
- `idempotency_window: timedelta | None` -- `None` meaning: the first send happens, a
  creation is never resent, an absence is never proof (FR-007).
- `create_credit_note(..., breakdown: TaxBreakdown | None, before_request: Callable[[],
  Awaitable[None]] | None, send_by: datetime | None)`: `breakdown` the approved one;
  `before_request` raises when the step deadline passed or the claim is gone (FR-011);
  `send_by` None under no window. `create_refund` takes the same keywords, ignored where
  unused.
- `ProviderAction.breakdown: TaxBreakdown | None = None` (FR-010), and
  `ProviderAction.partial: str | None = None` -- set by `find_action` for a document found
  but not complete (draft, unlinked, unfinalised), so execution can tell `partial_write`
  from "unlike".
- A new exception `PartialWrite(ProviderOutcomeUnknown)` carrying the document id and
  stage, raised by an adapter after a creation it cannot complete (FR-006).
- `ATTENTION` gains two incidents: `partial_write` (a document made, not completed) and
  `no_trace` (under no window, nothing found after an unknown outcome: the creation may
  still land). Both admit a person's status read.
- Stripe and the fake: `writes` declared (both types); the new keywords accepted with
  defaults (`breakdown=None`, `before_request=None`), ignored; `ProviderAction.breakdown`
  and `partial` default to None. No behaviour or test changes.

## 2. Execution core and store

`app/application/execute_action.py`:
- `execute()` and `reconcile()`: an action whose type is not in `provider.writes` is refused
  first, as `WritesNotSupported` is today (409, state kept) (FR-001).
- The blocker is part of the claim: the store's conditional `UPDATE` to `EXECUTING` also
  requires that no other credit-note action of the same `customer_id` is blocking:
  `resolved_at IS NULL` and (`EXECUTING`, `UNKNOWN_OUTCOME`, or attention `partial_write` /
  `no_trace`) -- one statement, so two actions of one customer cannot both claim, and a
  resolved action, though still never executed again, no longer blocks (FR-009). A refusal
  names the blocking action.
- An already credited invoice before the claim: Pennylane's `find_invoice` raises
  `ProviderUnsupported("invoice_credit")` when computing the fingerprint; as today, unsupported
  data before the claim answers 422 and leaves the action as it was, nothing written.
- `send_by = None` when the window is `None`; under no window an absence from
  `find_action` after an unknown outcome is never `FAILED`: the action stays
  `UNKNOWN_OUTCOME` with attention `no_trace` (an incident, so a person's status read can
  later settle a document that lands).
- A found `ProviderAction` with `partial` set is never success, in reconciliation and in
  both status-read paths (`_observed`, `_settled_by_a_person`): it sets or keeps
  `partial_write`, the document id and stage recorded; only a complete, alike document
  makes the action `EXECUTED`.
- `PartialWrite` → `UNKNOWN_OUTCOME`, attention `partial_write`, resolution with the
  document id, stage and the in-flight warning.
- The `before_request` hook: checks the deadline (claim instant + 2 minutes, the HTTP
  timeout counted by the adapter) and, in one conditional `UPDATE`, that the row is still
  `EXECUTING` under this claim's token while recording `last_request_at`; when either
  fails it raises `NotSent` (no document yet) and the adapter turns it into `PartialWrite`
  once a document exists.
- `_unlike()` compares `made.breakdown` with the approved one when the platform reports one
  (FR-010), after normalising all-zero tax to None.

`app/infrastructure/actions.py`:
- Columns: `customer_id` (passed at `create`; NULL for older rows, which never block),
  `claim_token` (set at each claim), `last_request_at` (each request of an attempt, durable),
  `partial_document` (the document id and stage, from `PartialWrite` or a partial
  `find_action`), `status_read_at` and `status_read_found` (every person's status read,
  empty results included), `resolved_by`, `resolved_at`, `resolution_note`.
- `record_resolution(action_id, by, note)`: one conditional `UPDATE`, only on
  `partial_write` or `no_trace`, only once, only when `status_read_at` is at least ten
  minutes after `last_request_at`; it releases the blocker; the incident stays (never
  executed again) (FR-009a).

`app/application/investigate_dispute.py`: `_queue_for_approval` passes the verified
customer's id to `create`; the store maps it.

`app/api/approvals.py`, `app/api/review.py` and templates:
- `POST /actions/{id}/resolution` (authenticated approver, POST only, identity the
  authenticated one), refused unless `record_resolution` accepts it; the page shows the
  in-flight warning and the last status read first.
- `GET /actions/{id}` and the review page: an action whose type the platform does not write
  says "cannot be executed on this platform; a person carries it out outside ResolveBilling"
  (FR-001); the approve button stays (approving records the decision), execution refuses.

Stripe and the fake: `writes` declared; new keywords accepted and ignored; behaviour and
tests unchanged. The fake learns, for tests only, to raise `PartialWrite`.

## 3. The Pennylane writer (`app/providers/pennylane/`)

- `client.py`: `PennylaneWriter(PennylaneReader)` moved from `scripts/pennylane_seed.py`
  into the app (one-shot `write`, `Uncertain` for a lost answer, `Refused(status, detail)`),
  the seed importing it from there.
- `mapping.py`: `credit_lines(invoice_payload, lines, amount_minor, breakdown)` -- on a taxed
  invoice every line reversed at its rate (quantity negative, cent price), on an untaxed one
  one line at the invoice's no-tax code for the approved amount; `observed_breakdown(doc)`
  -- the positive amounts of a credit note, all-zero tax → None; `is_cap_refusal(refused)`
  -- 400 with the cap message seen on the sandbox; `outward_ready(customer, addresses, doc)`
  -- decision 47's conditions.
- `adapter.py`: `writes = {"credit_note"}` only when a writer is given; `idempotency_window
  = None`; `plan_write` (FR-003); `create_credit_note` in FR-004's order, FR-005's two
  cleanup cases, `PartialWrite` otherwise, `before_request` before every request, never an
  email or e-invoicing call (FR-012); `find_action` by `external_reference` (FR-008),
  `test_mode` from the sandbox check (FR-013). The key's `external_reference` is
  `rb:` + the 64-character key; a longer limit refused before sending, nothing written.
- `keys.py` and `app/api/deps.py`: `PENNYLANE_WRITES=enabled` with `PENNYLANE_WRITE_TOKEN`
  builds the writer; otherwise `writes` is empty.

## 4. Docs, contract test, sandbox run

- ADR-008 amended ("A platform without a window", "Partial writes", "Write capability by
  type", the residual risk); `docs/contract/approval-and-execution.md` and
  `billing-platform.md` (Pennylane, writing; the sandbox observations; which prerequisite
  is met); `docs/operations.md`; `.env.example`; README's platform row.
- `tests/contract/test_pennylane_writes.py`: skipped unless `PENNYLANE_WRITES=enabled` and
  the write token; on worlds the seed builds for it (dated the day, drafts finalised only
  through decision 47's check): an open taxed invoice, a paid one, a mixed-rate invoice
  with an exempt line and rounding edges, an untaxed one, an invoice to cap. Each action
  created in a temporary `SqliteActionStore`, approved through `ApprovalService.approve`
  with the fingerprint it presents, executed through `ApprovalService.execute`, as
  `tests/contract/test_stripe_execution.py` does; then `find_action` asserted on the
  written credit note, and a refund asserted refused before any claim.
- The run: on the sandbox, writes enabled for that run only, recorded in the contract.

## Tests (without network) -- by requirement

| Requirement | Tests |
|---|---|
| FR-001 | per-type refusal before any read or claim, in `execute` and `reconcile`; the page and API notice; Stripe and the fake still write both |
| FR-002 | writes off → `writes` empty; a write token of another company → no write, sandbox refusal |
| FR-003 | open → `reduce_due`, paid → `customer_balance`, others `invoice_state` |
| FR-004 | the order of requests on the stub; the outward-readiness refusal before creating and before finalising; labels without the message |
| FR-005 | draft mismatch → deleted → read gone → `FAILED`; cap refusal at link → same; deletion refused, lost, or acknowledged but still listed → `partial_write` |
| FR-006 | lost / refused / unreadable answer at link (non-cap), finalise, the post-creation reads → `partial_write` with id, stage, warning |
| FR-007 | no window: first send; no resend of a creation; absence → `no_trace`, never `FAILED`; a late document then settled by a status read |
| FR-008 | `find_action`: complete alike, draft, unlinked, unfinalised, several, unlike, none |
| FR-009 | blocker in the claim: same customer blocked, other customer not, two connections claiming at once, released by `EXECUTED` or by resolving a `partial_write` or a `no_trace` (another action then claims through a separate connection), older rows never block; the investigation passes the customer |
| FR-009a | auth, POST only, identity recorded, ten-minute rule from durable times, only once, never executes, writes-off status reads work |
| FR-010 | breakdown compared exactly; all-zero tax normalised; Stripe and refunds unchanged |
| FR-011 | deadline exhausted mid-pacing or mid-retry; a stale claim's worker sends nothing; reconciliation and status reads unaffected |
| FR-012 | no email or e-invoicing path ever requested (transport log) |
| FR-013 | `test_mode` from the sandbox check; key length refused before sending |
| SC-003 | mock floor 38, baseline matched, coverage |


- Port and core: per-type refusal before anything (execute, reconcile); no window: first
  send happens, no resend of a creation, absence → person; blocker (same customer blocked,
  other customer not, released by `EXECUTED` or a resolution, older rows never block);
  `partial_write` final; resolution route (auth, POST only, ten-minute rule, once, releases
  blocker, never executes); a stale claim's worker sends nothing (`before_request`);
  breakdown comparison and normalisation; Stripe and fake suites unchanged.
- Pennylane writer against a stub transport: the happy path on open and paid invoices;
  draft mismatch → deleted → `FAILED`; cap refusal at link → deleted → `FAILED`; deletion
  refused or lost → `partial_write`; lost answer at creation, link, finalise, deletion;
  refusal at finalise; an answer that lands after a read showed nothing; `find_action`
  complete / draft / unlinked / several / unlike / none; outward-readiness refusal before
  creating and before finalising; never an email or e-invoicing path requested; the key
  length; writes off → `writes` empty.
- Gates: `ruff check .`, `pytest`, the mock floor 38 with the baseline, coverage.

## Order, and what can run side by side

1. The foundation commit, here (reviewed by Codex before anything builds on it).
2. **Side by side**, each in its own session and worktree branched from that commit, each
   reviewed by Codex on its own: the execution core and store (section 2), and the
   Pennylane writer (section 3). They touch disjoint files except `app/providers/base.py`,
   fixed in step 1.
3. Merged into this branch; then docs, the contract test and its seed (section 4), here.
4. The whole diff reviewed by Codex; the gates; the sandbox run at the maintainer's
   request; push and pull request on the maintainer's approval.

## Risks

- `customer_id` is new on actions: older pending actions carry none and never block; said in
  the contract.
- A worker paused between its hook and its request still sends once (no fencing on
  Pennylane): the accepted residual risk, recorded with the in-flight warning.
- The cap refusal is recognised by Pennylane's message text: if it changes, a cap refusal
  becomes `partial_write` (safe side), never "nothing written" by mistake.
- Pennylane may answer the prerequisites after merge: writes stay off until then.
