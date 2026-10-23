# Implementation Plan: Stripe as a billing platform, writing

**Branch**: `002-stripe-writes` | **Date**: 2026-10-23 | **Spec**: [spec.md](spec.md), approved by the maintainer at commit `c51e175`

**Input**: Feature specification from `specs/002-stripe-writes/spec.md`

## Summary

Stripe, in test mode, makes the two writes the domain has -- a credit note,
and a refund through a credit note -- when `STRIPE_WRITES=enabled`. The port
changes so that a write can be checked and found again: the writes take the
action's identifier, `find_action` takes the invoice and answers every write
under the key, and a `ProviderAction` carries what the checks read. The
execution service (ADR-008) gains what the spec requires around a write: a
first-attempt instant recorded with the claim and a window checked before
every send, a refusal that never settles an earlier unknown outcome, the
platform's answer checked before `EXECUTED`, the platform's status recorded
as a dated observation that a person can have read again, and an action that
says when it needs a person. The seed builds fresh worlds to write on; write
contract tests run against the fake in CI and against Stripe locally.

Before the change is folded in, the capability *Approval and execution* gets
its contract, written from the existing records (constitution, "Product
Contract").

## Technical Context

**Language/Version**: Python 3.12 (CI), 3.13 locally.

**Primary Dependencies**: `stripe` 15.4.0, API version `2026-07-29.dahlia`,
both pinned already (specs/001). The SDK's credit note service takes
`invoice`, `amount`, `credit_amount`, `refund_amount`, `out_of_band_amount`,
`email_type`, `metadata`; a credit note carries `status` (`issued`, `void`),
`livemode`, `metadata`, `refunds` (each with `amount_refunded`, `refund`,
`type`), `pre_payment_amount`, `post_payment_amount`; a refund's status is
`pending`, `requires_action`, `succeeded`, `failed` or `canceled`. Request
options take `idempotency_key` and `max_network_retries` per request.

**Storage**: the action table gains columns (section 3); added by the
store's existing migration.

**Testing**: pytest. Execution tests on the fake and on test doubles (CI);
Stripe adapter tests on the stub client (CI, no SDK, no network); write
contract tests on the fake (CI) and on Stripe with the writing key on fresh
seeded worlds (local, run by the maintainer).

**Target Platform**: the existing service; Stripe in test mode only.

**Constraints**: test mode only; nothing sent without `STRIPE_WRITES=enabled`;
no Stripe name outside the adapter, the wiring, the seed and Stripe tests;
no key and no Stripe text in any output; no write but the credit note.

**Scale/Scope**: one action at a time per invoice; a handful of write worlds
per contract run.

## Constitution Check

| Principle | How this plan meets it |
|---|---|
| I. Deterministic before probabilistic | Untouched: no rule or engine changes. |
| II. Evidence, then human approval, before money | Nothing is sent but an approved action, unexpired, on the invoice shown (fingerprint), within the invoice and its currency (ADR-008's guard), within the window; writing is off unless configured. Execution writes exactly the approved instrument: a refund stays a refund. |
| III. Provider-agnostic boundaries | The port additions are vendor-neutral (an action identifier, the invoice, a window the platform declares, fields any platform's write has). Stripe's vocabulary stays in the adapter. |
| IV. Money in minor units | Amounts cross the port as integer minor units; Stripe's are minor units already. |
| V. A model acts within bounds | Untouched. Nothing a model wrote reaches Stripe (FR-013). |
| VI. Auditable; the platform is authoritative | Every outcome, recovery and incident is said in the action's `resolution`; the platform's status is an observation with its instant. No ledger: Stripe's caps decide, and the contract run measures them. |
| VII. Untrusted input, trust boundaries | No customer text reaches Stripe; Stripe sends no mail (`email_type=none`). The status read is an authenticated person's call. |
| VIII. Measured | The mock baseline stays matched (no decision changes); the write properties and SC-002 are measured on the test account. |
| Sensitive areas | The money-moving core (II, IV) and a normative change to ADR-008 and the product contract: **final-diff approval** before merge. |

No violation; no complexity to justify.

## Design

### 1. The Approval and execution contract, first

`docs/contract/approval-and-execution.md`, written from ADR-004 and ADR-008
as they stand -- states, the claim, the three outcomes, reconciliation, the
guards, expiry, a platform that cannot write -- in a commit of its own, before
anything changes; `docs/contract/README.md` lists it. The change is then
folded into it with the code (section 9).

### 2. The port (`app/providers/base.py`; ADR-001 and ADR-008 amended)

- **The writes take the action's identifier**:
  `create_credit_note(invoice_id, amount_minor, currency, reason,
  idempotency_key, action_id)`, likewise `create_refund`. `reason` stays on
  the port for platforms that record one; Stripe ignores it (FR-013).
- **`find_action(idempotency_key, invoice_id) -> list[ProviderAction]`**:
  every write under the key on that invoice, in the platform's order; an
  empty list is "none". A list cut short is `ProviderUnavailable`.
- **`ProviderAction`** gains what the checks read (FR-001, FR-004):
  `invoice_id`, `amount_minor`, `currency`, `test_mode: bool`,
  `allocation` (`reduce_due` | `customer_balance` | `refund`),
  `action_id: str | None` (what the platform holds), `observed_at`. `status`
  keeps `succeeded` | `pending` | `failed`.
- **`idempotency_window: timedelta`**: how long after a first attempt the
  platform still answers a key with what it made. The execution service
  reads it; the number is the platform's (Stripe: 23 hours; the fake models
  the same). Vendor-neutral, so the service never names Stripe.
- **`ProviderOutcomeUnknown(Exception)`**: what an adapter raises when a
  write was, or may have been, sent and its answer is unknown -- never
  `ProviderUnavailable`, which is for reads (FR-020). The execution service
  still treats any exception but `ProviderRejected` as unknown, as a
  defence.

### 3. Execution (`app/application/execute_action.py`, `app/infrastructure/actions.py`)

**New columns** on `pending_actions`, added by `_migrate`:

| column | meaning |
|---|---|
| `first_attempt_at` | set in the claim's own `UPDATE`, only when null; never reset (FR-005) |
| `uncertain_since` | set when an attempt's outcome first becomes unknown; never cleared (FR-006) |
| `provider_status`, `provider_status_at` | the platform's status of the write, and when it was observed (FR-003, FR-015) |
| `attention` | why the action needs a person, from a closed set; null otherwise |

`attention` takes `window_passed`, `several_writes`, `unlike_the_action`,
`unreadable_answer`, `not_successful` (a write made whose status is not
success). It is not a state: the state machine of ADR-008 keeps its states
and transitions; an action needing a person stays `UNKNOWN_OUTCOME` (nothing
proved) or `EXECUTED` (made, to follow up). The store gains
`annotate(action_id, expected_state, attention, resolution)`: a conditional
`UPDATE` that changes no state, for an action that stays where it is.

**The window, checked before every send.** `execute()` computes
`now - first_attempt_at` right before the claim, on the stored value -- a
restart reads the same. At or past `provider.idempotency_window`: nothing is
claimed or sent; the action is annotated `window_passed` and `execute()`
raises `NeedsAPerson` (an `IllegalTransition`, so 409). The service takes a
clock (`now`) for the tests.

**Same parameters on every send.** The request is a pure function of the
action (invoice, type, amount, currency, key, identifier) and of the
invoice's status, which the approved fingerprint pins: `execute()` checks the
fingerprint before every claim, retries included, and a changed invoice
expires the action. A test pins that two sends of one action make the same
request.

**Outcomes of a send:**

- *An answer*: checked against the action (below). Alike: `EXECUTED` with the
  reference and the status observed; a status but success also annotates
  `not_successful` and says it in `resolution`. Unlike, incomplete or
  unreadable: `UNKNOWN_OUTCOME`, `uncertain_since` set, annotated
  (`unlike_the_action` or `unreadable_answer`), and `NeedsAPerson`.
- *`ProviderRejected`*, with `uncertain_since` null: `FAILED`, as today.
  With `uncertain_since` set (FR-006): `UNKNOWN_OUTCOME`, then reconciled at
  once; the refusal is kept in `failure_reason`.
- *Anything else*: `UNKNOWN_OUTCOME`, `uncertain_since` set if null; 504, as
  today.

**Reconciliation** (an `UNKNOWN_OUTCOME`, or a claim abandoned past 5
minutes, which first becomes `UNKNOWN_OUTCOME` with `uncertain_since` set):
`find_action(key, invoice_id)`, then:

| found | within the window | past it |
|---|---|---|
| one, alike | `EXECUTED`, status observed | the same |
| one, unlike; or several | stays unknown, annotated | the same |
| none | `FAILED` (retried by the next `execute()`, within the window) | stays unknown, annotated `window_passed` |

Reconciliation never writes. A read that fails leaves the state as it was.

**The check** (`_unlike(action, found) -> str | None`, pure): the invoice,
the type, the amount, the currency, `test_mode` true, an allocation the type
allows (a refund: `refund`; a credit note: `reduce_due` or
`customer_balance`), and, where the platform holds them, the key and the
action identifier. A missing field is `unreadable_answer`.

**Reading the status again** (FR-003): `ApprovalService.refresh_status(id)`,
for an `EXECUTED` action: `find_action`, the write with the recorded
reference, its status and the instant observed recorded. Nothing is sent;
`not_successful` is set or cleared by what is observed. Its write gone or
unlike: annotated, reported. A platform that cannot write: 409, as for
execute.

### 4. The API (`app/api/approvals.py`)

- `ActionOut` gains `provider_status`, `provider_status_at`, `attention` and
  `first_attempt_at`.
- `POST /api/v1/actions/{id}/status`: reads the status again; an approver's
  call (a person), 409 unless `EXECUTED`.
- `NeedsAPerson` answers 409 with the category, ahead of the catch-all;
  the 504 message no longer promises that the next call settles everything:
  "reconciled on the next execute, or put to a person".

### 5. The fake (`app/providers/fake/adapter.py`)

Follows the port: writes take `action_id` and record it; `find_action(key,
invoice_id)` answers a list, filtered by invoice; `ProviderAction` filled
(`test_mode` true, the allocation from the invoice's status and the type);
`idempotency_window` 23 hours. Its caps are unchanged. Test helpers model a
lost answer, a second write under one key, a write unlike the action and a
status that changes.

### 6. The Stripe adapter (`app/providers/stripe/`)

**`client.py`.** `StripeReader` stays read-only. A `StripeWriter`, built only
when writes are enabled, has one method, `create_credit_note(params,
idempotency_key)`, sent with **`max_network_retries=0`**: the SDK retries a
409 and any 5xx on its own, so one attempt of ours could otherwise be
several requests, and a refusal of the last would hide an earlier one that
landed (FR-006). Each attempt is one request, whose outcome the execution
service sees. Reads keep their retries. Errors on a write are classified by
`_write_outcome`:

| Stripe answers | the port's |
|---|---|
| `InvalidRequestError` (400, 404), not an idempotency error | `ProviderRejected("invalid_request")`, `("not_found")` |
| `AuthenticationError` (401), `PermissionError` (403) | `ProviderRejected("authentication")`, `("permission")` |
| `IdempotencyError`, a 409, a rate limit, a connection error, a 5xx, anything else; the time budget spent | `ProviderOutcomeUnknown(category)` |

Categories only; `from None`, so no Stripe text reaches a traceback.

**`mapping.py`**, pure: `credit_note_params(invoice, action)` -- the
allocation from the invoice's status and the type (FR-010, FR-011), the
metadata (`rb_idempotency_key`, `rb_action_id`, `rb_action_type`), `email_type`
`none`, no `memo`, no `reason` -- and `provider_action(credit_note)`: type
from the metadata, allocation from `refunds` and the amounts
(`pre_payment_amount` → `reduce_due`; `post_payment_amount` refunded →
`refund`, credited → `customer_balance`; anything else unreadable), status
per FR-015 from the credit note's status and its refunds' statuses
(`refunds.refund` expanded), `test_mode` from `livemode`.

**`adapter.py`.** `can_write` is true only when built with a writer.

- *The state check, before sending* (FR-012), on the raw invoice: open with
  nothing paid (`amount_paid` 0) takes a credit note only; paid in full
  (`amount_remaining` 0, `amount_paid` equal to the total) takes either; any
  other state is `ProviderRejected("invoice_state")`. A read failing there
  is `ProviderOutcomeUnknown`.
- *The write*: one `credit_notes.create` with the action's key; a refund is
  a credit note whose whole amount is `refund_amount` (FR-011). No other
  mutating call exists in the module (FR-016); the stub fails on any.
- *`find_action`*: the invoice's credit notes listed to the end
  (`invoice=<id>`, `refunds.refund` expanded), those whose metadata holds the
  key mapped. Never search (FR-014).
- `idempotency_window` = 23 hours.

### 7. Wiring and configuration

- `app/api/deps.py`: with `BILLING_PROVIDER=stripe`, `STRIPE_WRITES=enabled`
  (read there only, compared exactly) builds the writer on the same
  restricted test key; anything else, or absent, keeps the slice read-only
  (FR-002, SC-004).
- The restricted key's write permissions are **established on the test
  account by the maintainer** (credit notes; whatever a refunding credit
  note needs besides) and recorded in `docs/operations.md` and
  `.env.example` (FR-021). No agent sees the key.

### 8. Write worlds and the contract tests

- **`scripts/stripe_seed.py --write-run`** opens a new write generation `w<n>`
  (noted in the manifest as `w_<n>`) and builds, without a test clock, one
  customer `rb:write/w<n>` with one invoice per purpose, tagged
  `rb_purpose=<purpose>`: paid in full by a test card, or open with nothing
  paid, one per property below that writes. For SC-002 it also builds a fresh
  duplicate world, as the read-only catalogue does, under the generation's
  own name and keys. Same rules as the seed: test key only, keys per step,
  the lock, lists read to their end, reported refusals.
- **The contract tests never write on an invoice written before**: before
  each write, the test lists the invoice's credit notes and skips, asking
  for a new `--write-run`, if any exists (FR-032).
- **`tests/contract/test_billing_platform_writes.py`**, one test per write
  property (spec, 1 to 7), on the fake in CI (fresh in memory) and on Stripe
  when `STRIPE_API_KEY` and `STRIPE_WRITES=enabled` are set, else skipped
  with the reason. Property 1 also measures a key answered twice within the
  run; the day-long retention is not measured (spec, Assumptions).
- **`tests/contract/test_stripe_execution.py`** (SC-002): on the fresh
  duplicate world, investigate, approve the proposal as shown, execute twice,
  and read Stripe: one credit note for the approved amount. A second test
  executes a refund through the execution service on a paid write invoice.

### 9. Documents

- `docs/contract/approval-and-execution.md`: the change folded in (the
  window, refusals after an unknown outcome, the answer checked, statuses as
  observations, attention, the status read).
- `docs/contract/billing-platform.md`: the port changes; Stripe writing;
  the write properties as measured.
- ADR-008 amended (dated section): `find_action` by invoice, the window,
  FR-006, the answer checked, attention, the status read; its "to verify"
  on Stripe's caps answered by the contract run. ADR-001: the port changes.
- `docs/operations.md`, `.env.example`, README (what Stripe does now).

## Tests (what proves each requirement)

| Requirement | Where | Runs |
|---|---|---|
| FR-001, FR-004 (port, the check) | `tests/unit/test_execute_action.py`, `tests/unit/test_fake_provider.py` | CI |
| FR-003, FR-015 (status observed, read again) | `tests/unit/test_execute_action.py`, `tests/integration/test_approval_flow.py`, `tests/unit/test_stripe_writes.py` | CI |
| FR-005, FR-006 (window, refusal after unknown) | `tests/unit/test_execute_action.py`: no send at or past the window, after a restart (a new store on the same file), from `FAILED` and from `UNKNOWN_OUTCOME`; landed, answer lost, retry refused | CI |
| FR-002, SC-004 (switched on) | `tests/unit/test_provider_wiring.py` | CI |
| FR-010 to FR-016, FR-020 | `tests/unit/test_stripe_writes.py` on the stub client: parameters, allocation, metadata, no `memo`, `email_type`, no retries on a write, every error class, the state check, pages and a page cut short, two matches, statuses, no other mutating call | CI, no SDK |
| FR-031, SC-001, SC-002 | `tests/contract/` | fake in CI; Stripe locally |
| FR-032 | `tests/unit/test_stripe_seed.py` (stub): a new generation per `--write-run`, purposes, keys | CI |
| SC-003 | `evals/` baseline, coverage check | CI |

## Project Structure

```text
specs/002-stripe-writes/          spec.md, plan.md, tasks.md
app/providers/base.py             port changes, ProviderOutcomeUnknown, idempotency_window
app/providers/fake/adapter.py
app/providers/stripe/             client.py (StripeWriter), mapping.py, adapter.py
app/infrastructure/actions.py     columns, annotate, NeedsAPerson
app/application/execute_action.py
app/api/approvals.py, app/api/deps.py
scripts/stripe_seed.py, scripts/stripe_worlds.py
tests/unit/ tests/integration/ tests/contract/
docs/contract/approval-and-execution.md (new), docs/contract/billing-platform.md, docs/contract/README.md
docs/adr/001-*.md, docs/adr/008-*.md, docs/operations.md, .env.example, README.md
```

**Structure Decision**: the existing layout; no new package.

## Research notes (2026-10-23)

- From the SDK 15.4.0 sources: the credit note create parameters and
  object fields listed under Technical Context; the list takes `invoice`;
  request options take `idempotency_key` and `max_network_retries`; the SDK
  retries on its own a 409, a 5xx, a connection error, or when Stripe says
  `Stripe-Should-Retry: true`; `IdempotencyError` comes on a 400 or 404 whose
  type is `idempotency_error`.
- From Stripe's documentation, not measured: keys are kept at least 24
  hours; a result is saved once the endpoint starts executing, so a request
  failing validation saves nothing.
- To measure in the contract run: that a refunding credit note on a paid
  invoice creates the refund itself; Stripe's cap on credit notes and refunds
  together; a refund's status on the credit note right after creation.

## Complexity Tracking

None.
