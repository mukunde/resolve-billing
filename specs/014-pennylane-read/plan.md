# Implementation Plan: Pennylane as a billing platform, read-only

**Branch**: `014-pennylane-read` | **Date**: 2026-11-12 | **Spec**: [spec.md](spec.md), approved by the maintainer at `851eb33` | **Plan**: draft, for the maintainer's approval

## Summary

A Pennylane adapter beside the Stripe one -- a client (HTTP, pacing, pagination, errors),
a mapper (Pennylane's vocabulary to the domain, every convention of decision 46 in one
place, each with its provenance) and the adapter (the port) -- selected by
`BILLING_PROVIDER=pennylane` and refusing any company but the declared sandbox. Two small
changes outside it: the domain `Invoice` may carry its number, and the reference hooks
receive the investigation's invoices. A seed script and the contract on Pennylane, run by
the maintainer. No change for the fake or Stripe: spec 013's snapshot, the mock baseline
and the Stripe tests hold them. One file, as plans 008 to 013.

## Technical Context

Python 3.12 (CI), 3.13 locally; `httpx` (already a dependency) for the client -- no
Pennylane SDK exists for v2. No new dependency. CI makes no network call; every
Pennylane test without network runs against a stub transport (`httpx.MockTransport`)
serving recorded-shape payloads written from the reference, never real data.

## Constitution Check

| Principle | How this plan meets it |
|---|---|
| I, II | No rule or policy moves; refusal where meaning is not established; the refund assumption the maintainer kept is stated in the contract with its effect. |
| III | Pennylane's vocabulary in `app/providers/pennylane/mapping.py` alone; typed ids keep its integers from colliding. |
| IV | Amounts through ADR-005's rounding function, in the invoice's currency. |
| VI | Run rows name the provider `pennylane`; nothing amended in place. |
| VII | Numbers resolved only among the investigation's invoices, masked before any engine; whose an invoice is, nothing more; the token never logged; only the declared sandbox read. |
| VIII | Unit and adapter tests without network; the contract on a seeded sandbox; the mock baseline and the snapshot unchanged. |
| Sensitive | A new source of evidence and of reference resolution: final-diff approval. |

## Design

### 1. Configuration and the company check (FR-001, Q2)

- `app/providers/pennylane/keys.py`: `PENNYLANE_API_TOKEN` (adapter) and
  `PENNYLANE_SEED_TOKEN` (seed) read from the environment where they are used, never
  printed; a missing token is a configuration error by category.
- `PENNYLANE_SANDBOX_COMPANY_ID`: required. Before the first read of a provider instance,
  the client reads the identity endpoint (`GET /me`) once and compares its company id;
  missing setting, another company or an unreadable answer → `ProviderConfigurationError`
  (`sandbox`), raised on every port call, nothing read. The answer of `/me` is not logged.
- `app/api/deps.py`: `PROVIDERS` gains `pennylane`; `get_provider` builds the adapter;
  writes are never wired (`can_write` false).

### 2. The client (FR-007, FR-008)

`app/providers/pennylane/client.py`, over `httpx.AsyncClient`:

- One request at a time per token, process-wide: an `asyncio.Lock` and a monotonic pacer
  allowing at most 25 requests per rolling 5 s (a deque of send instants), shared by every
  client built on the same token in the process.
- Retries: 429 (waiting `retry-after`, an integer, within the budget), 5xx and connection
  errors, with backoff; never 4xx otherwise. 404 → `NotFound` to the adapter; 401/403 →
  `ProviderConfigurationError` (`authentication`/`permission`).
- `every(path, params)`: cursor pagination to the end, the filter re-sent on each page;
  a page without `items` as a list, a `has_more` true without a new cursor, or a cursor
  seen before → `ProviderUnavailable` (malformed pagination).
- A time budget per port call (default 20 s, as Stripe's) wrapping all its requests,
  waits included; spent → `ProviderUnavailable`.
- Errors carry categories only: no URL with ids, no header, body or exception text;
  `httpx`'s and `httpcore`'s loggers are kept silent for this client.

### 3. Typed identifiers (FR-008a)

`mapping.py`: `pl_cus_<n>`, `pl_inv_<n>`, `pl_sub_<n>`, `pl_pay_<n>` on the way in;
`raw_id(typed, kind)` on the way out refuses an identifier of another kind or another
platform (`NotFound` for a get, `None` for a find) -- a `pl_cus_` id given as an invoice
is no invoice. Tests: equal integers across the four kinds through references, masking
(aliases) and restored evidence.

### 4. The mapper (FR-002 to FR-006)

`app/providers/pennylane/mapping.py`, pure functions over payload dicts:

- **Dates** (decision 46): `paris_midnight(date)` = the date at 00:00 Europe/Paris
  (`zoneinfo`), in UTC; line period `[start 00:00, end + 1 day 00:00)`; one helper, one
  test per daylight-saving edge.
- **Customers**: `emails` trimmed; zero → `missing_field` when mapped; two or more →
  `multiple_addresses`; one → `Customer(email=...)`; no currency → none.
- **Invoices**: kind first -- `credited_invoice` set → a credit note, never mapped as an
  invoice; quotes, proformas and orders (their statuses) → not invoices. Status table
  `INVOICE_STATUS` (`paid`, `upcoming`, `late`, `draft`, `cancelled` → void only with no
  credit note against it), anything else `invoice_status`. Lines mapped then tax through
  `untaxed_unless_charged` and `reconcile` (posted `currency_amount`,
  `currency_amount_before_tax`, `currency_tax`). `number` set from `invoice_number`.
- **VAT** `VAT_RATES = {"FR_200": 20, "FR_100": 10, "FR_55": 5.5, "FR_21": 2.1}` and
  `NO_TAX = {"exempt"}` (posted tax must be zero, else `tax_mismatch`); anything else
  `tax_code`.
- **Lines**: quantity a decimal string; `raw_currency_unit_price` excluding tax, finer than
  the minor unit → `fractional_unit_price`; negative amount → `negative_line`; non-zero
  discount on line or invoice → `invoice_discount`; `imputation_dates` → period.
- **Payments**: `PAYMENT_STATUS` table, each row with its provenance
  (`"convention 2026-11-11"`), `charged_back` and unlisted → `payment_status`;
  `received_at` none.
- **Subscriptions**: `in_progress` → active; `stopped` → cancelled, ended at
  `paris_midnight(next_occurrence)` (missing → `missing_field`); `finished` → cancelled,
  ended at `paris_midnight(finish) + 1 day`; others `subscription_status`. Current period
  `[prev_occurrence, next_occurrence)` at Paris midnights, required, and exactly one cycle
  of `recurring_rule` (`monthly`/`yearly`/`weekly` × `interval`), else `missing_field`;
  cadence from the rule; anchor `start`; `plan_code` the label or the typed id.
- **New reasons** in `app/providers/base.py`: `invoice_status`, `payment_status`,
  `tax_code`, `multiple_addresses`; the contract's closed set and the tests that list
  them updated.

### 5. The adapter (the port)

`app/providers/pennylane/adapter.py`, `PennylaneBillingProvider`:

- `find_customer_by_email` / `list_customers`: every customer, in id order, to the end.
- `get_invoice` / `find_invoice`: the invoice, its lines to the end, the customer's credit
  notes to the end (to refuse a credited invoice), and its subscription when it names one
  (to apply the every-line-period rule of a cancelled subscription).
- `list_invoices(customer, since)`: the customer's invoices (`filter customer_id`,
  `include_invoices_created_from_ledger_entries=true`) to the end; the customer's credit
  notes to the end; for each invoice issued at or after `since` (after the date
  conversion), mapped as above, a refused one refusing the call; older ones not mapped.
- `invoice_owner`: the invoice alone, its `customer.id` typed; nothing else read.
- `list_subscriptions` / `get_subscription`: mapped as above.
- `list_payments(customer)`: the payments of every invoice of the customer, every list to
  its end; a payment id seen under two invoices → `multi_invoice_payment`.
- `get_usage`: `None`. `can_write` false; `plan_write`, the writes and `find_action` as
  Stripe's without writes (refused before any call; `find_action` answers an empty list --
  nothing written by us exists there).
- Reads per call are bounded by the budget; an investigation's cost in requests is
  documented in the contract (customers + invoices + lines + credit notes + payments).

### 6. Numbers and the reference hooks (FR-009, FR-010, Q3)

- `Invoice.number: str | None = None`, dropped from the dump when None (the
  `model_serializer` pattern of spec 013); not in the engine payload.
- The hooks' signature becomes `invoice_references(text, invoices=())` and
  `invoice_reference_spans(text, invoices=())`. Fake and Stripe accept and ignore
  `invoices`. Pennylane's: build `{normalised number: typed id}` from the invoices given
  (a number shared by two → left out), then scan the text for whole matches --
  case-insensitive, bounded by a non-alphanumeric character, longest first, no overlap.
- Callers: the investigation passes `context.invoices` to `invoice_references` (the
  cited-reference check, `investigate_dispute.py`), and resolves `dispute.invoice_id`
  through the same hook before looking for the target among the loaded invoices (Q3): a
  typed id passes as it is, a number resolved, a number resolved to nothing left as given
  (then `UNKNOWN_INVOICE` as today). Masking: `ReferenceFinder` becomes
  `Callable[[str, Sequence[Invoice]], list[...]]`; `mask_message` calls it with
  `context.invoices`; the engines keep the finder they are given and pass the context at
  the call -- no invoices stored on an engine or a provider. `deps.wire` unchanged.
- Tests: interleaved investigations of two customers on one provider, a number inside a
  longer word, overlapping numbers, an ambiguous number, a number outside the window; and
  the fake and Stripe unchanged (the snapshot, the masking tests, the reference tests).

### 7. The seed (FR-011)

`scripts/pennylane_seed.py`, writing through its own small client (same pacing and error
categories, the seed token):

- Guard: `GET /me` company id == `PENNYLANE_SANDBOX_COMPANY_ID`, else stop, nothing written.
- Worlds, each on its own customer (`external_reference` `rb:<world>`): the taxed duplicate
  (two finalised invoices of 100.00 at `FR_200`, same imputation period, on one
  subscription's id -- or, if the API attaches invoices to a subscription only by
  generation, a subscription and its generated invoices), the taxed invoice after
  cancellation (a subscription stopped, then an invoice for the following period), and
  the refused shapes: a credited invoice, two addresses, a `mixed` VAT line, a non-zero
  discount, a partially paid invoice if the API can make one. What the API does not let it
  build is reported with its reason.
- Recovery: customers and invoices found again by `external_reference` (documented filter)
  before any write; subscriptions by listing the world's customer's, stopping with a report
  when it cannot tell; generated invoices by `billing_subscription_id`. Subscriptions with
  no email sending and an offline payment method, stopped once their invoices exist.
- Output: one line per world (`built`, `complete`, `REPORTED` with the category), never
  amounts, names or payloads.

### 8. The contract on Pennylane (FR-012)

`tests/contract/conftest.py` gains `pennylane_platform()`: skipped with the reason
unless `PENNYLANE_API_TOKEN` and `PENNYLANE_SANDBOX_COMPANY_ID` are set; it finds the
seeded worlds by `external_reference`. The platform-agnostic properties run on it; the
supported worlds compare with their fake counterparts (`taxed_duplicate_fr`,
`taxed_billed_after_cancellation_en`) as Stripe's do; each refused world its reason; the
write contract skips (no writes). The run is the maintainer's, or an agent's at his
request, with his tokens.

### 9. Documents (FR-013)

Billing-platform contract (Pennylane: reads, conventions with provenance, the refund
assumption and its effect, refusals, the company check, requests per investigation, what
is not guaranteed -- processes sharing a token, payments shared across customers);
ADR-001 dated amendment (a third platform; typed ids; hooks given the invoices);
README platform table; `docs/operations.md` (tokens, sandbox id, seed and contract
commands).

## Delivery

One pull request, commits in this order: base reasons and `Invoice.number` (snapshot
green); hooks' signature through fake, Stripe, investigation and masking (all existing
tests green); Pennylane client; mapper; adapter; wiring; seed; contract fixture;
documents. Then the seed and the contract run on the sandbox, on the maintainer's word,
their results recorded before merge -- or, if the seed token is not yet there, the PR
merged with the contract marked "to run" and a follow-up PR for the results.

## Gates

`ruff check .`; `python -m pytest -q -p no:cacheprovider`; `python -m evals.runner --engine
mock --min-fully-correct 38 --baseline evals/baselines/mock.json`; `python -m evals.coverage
--check`; spec 013's snapshot test. Codex reviews each commit's diff; the maintainer
approves the final diff.
