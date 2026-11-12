# Implementation Plan: Pennylane as a billing platform, read-only

**Branch**: `014-pennylane-read` | **Date**: 2026-11-12 | **Spec**: [spec.md](spec.md), approved by the maintainer at `851eb33` | **Plan**: approved by the maintainer on 2026-11-12, at `db3f174`

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
- `PENNYLANE_SANDBOX_COMPANY_ID`: required. Every read method of the port -- the local
  ones too (`get_usage`, `find_action`, a find given an id of another kind) -- starts with
  one guard, inside the call's time budget: on its first use per provider instance it
  reads the identity endpoint (`GET /me`) and compares the company id, then remembers the
  verdict; a missing setting, another company or an unreadable answer →
  `ProviderConfigurationError` (`sandbox`) on that call and every later one, nothing else
  read. Writes are refused before any call, guard or not. The answer of `/me` is not
  logged. Tests: missing, mismatched and unreadable identity, each followed by every read
  method.
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
- **Invoices created from ledger entries**: one with no line, or a line without the
  fields a line needs, is refused (`missing_field`), never mapped as an empty invoice.
- **Cancelled subscriptions**: an invoice naming a subscription mapped as cancelled is
  mapped only if every line has both imputation dates, start before end; one line
  without, with one date or reversed → `missing_field`. Tests: mixed dated and undated
  lines, one missing date, reversed dates.
- **Lines**: quantity a decimal string; `raw_currency_unit_price` excluding tax, finer than
  the minor unit → `fractional_unit_price`; negative amount → `negative_line`; non-zero
  discount on line or invoice → `invoice_discount`; `imputation_dates` → period.
- **Payments**: `PAYMENT_STATUS` table, each row with its provenance
  (`"convention 2026-11-11"`), `charged_back` and unlisted → `payment_status`;
  `received_at` none.
- **Subscriptions**: `in_progress` → active; `stopped` → cancelled, ended at
  `paris_midnight(next_occurrence)` (missing → `missing_field`); `finished` → cancelled,
  ended at `paris_midnight(finish + one calendar day)` -- the day added to the date, never
  24 hours to the instant, so a daylight-saving change cannot move it; tests on both
  transitions; others `subscription_status`. Current period
  `[prev_occurrence, next_occurrence)` at Paris midnights, required, and exactly one cycle
  of `recurring_rule` (`monthly`/`yearly`/`weekly` × `interval`), else `missing_field`;
  cadence from the rule; anchor `start`; `plan_code` the label or the typed id.
- **New reasons** in `app/providers/base.py`: `invoice_status`, `payment_status`,
  `tax_code`, `multiple_addresses`; the contract's closed set and the tests that list
  them updated.

### 5. The adapter (the port)

`app/providers/pennylane/adapter.py`, `PennylaneBillingProvider`:

- `find_customer_by_email` / `list_customers`: every customer, in id order, to the end;
  the given address compared with every address of each customer, so an address that is a
  customer's second one finds that customer -- who is then refused
  (`multiple_addresses`). Test: a match on a second address.
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
- One canonical target (Q3): in `load_context`, after the window's invoices are read, the
  investigation resolves `dispute.invoice_id` once through the hook with those invoices --
  a typed id as it is, a number to its invoice's typed id, an unresolved number left as
  given -- and records the result in the context (`target_reference`). Everything that
  uses the disputed invoice reads that one value: the target among the loaded invoices,
  the ownership read for a reference outside the window (made only for a typed id of the
  platform: an unresolved number is never sent to the API as an id), `unknown_invoice`,
  and the cited-reference check's designated invoice. Every call of the hooks passes the
  loaded invoices. On the fake and Stripe the hook returns the id unchanged, so their
  investigations are as before. Tests through `load_context` and a full investigation:
  a number, a typed id, an ambiguous number, another customer's number, an unresolved
  number. Masking: `ReferenceFinder` becomes
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
- Recovery, per mutation -- before each, a read decides whether it is needed; after a
  write whose answer was lost (timeout, 5xx after sending, unreadable answer), a read
  decides whether it landed; if that read cannot tell, the seed stops and reports, and no
  uncertain write is ever sent again automatically. Durable state makes this hold across
  restarts: before each create, the seed notes its intent (world, operation, the
  identifying value it will set) in a manifest kept in the sandbox's own records -- the
  world customer's metadata, as Stripe's seed keeps its manifest -- and clears it once a
  read shows the object; a run that finds an intent noted and no object stops and reports
  instead of creating, so a fresh attempt is told from an uncertain one:
  - customer create: found by its `external_reference` (one → reuse; none and no intent
    noted → create; none with an intent noted → stop; several → stop) -- the customer's
    intent is noted in a local run file, the only state before the customer exists, and
    the same rule applies;
  - invoice create (draft): found by its `external_reference`, as above;
  - invoice finalise: the invoice's `draft` read -- false → done, true → finalise once,
    then read again;
  - subscription create: the world customer's subscriptions listed by label -- none →
    create, one → reuse, several → stop;
  - subscription stop: its status read -- stopped → done, else stop it once, read again;
  - credit note create and link: found by its `external_reference`; the link read on the
    original's credit notes -- linked → done, else link once, read again;
  - payment, where the API allows creating one: created with a unique label
    `rb:<world>:payment:<n>`, found by listing the invoice's payments and matching that
    label exactly -- one → done, none and no intent → create, none with an intent → stop,
    several → stop.
  Stub tests lose the answer of every mutation once, and make the recovery read fail or
  stay inconclusive, then check that the next run reuses, completes or stops -- never
  writes twice.
- Subscriptions: created with no invoice email (no email recipients, the mode that does
  not send) and the `offline` payment method, both read back and checked before the
  world is used; every subscription the seed created is stopped once its invoices exist,
  the stop being part of the recovery above; a stub test checks the settings sent and
  that no created subscription is left running.
- Refused worlds, each attempted and either built or reported: a credited invoice
  (`invoice_credit`), two addresses (`multiple_addresses`), a `mixed` VAT line
  (`tax_code`), a non-zero discount (`invoice_discount`), a partially paid invoice
  (`invoice_status`), a `charged_back` payment (`payment_status`), a fractional unit price
  (`fractional_unit_price`). A world the API does not let the seed build is reported with
  its category; its exclusion goes to the maintainer, and only an exclusion he approved
  enters `NOT_SEEDED`. The contract's readiness fails for a world neither present nor
  excluded.
- Output: one line per world (`built`, `complete`, `REPORTED` with the category), never
  amounts, names or payloads.

### 8. The contract on Pennylane (FR-012)

`tests/contract/conftest.py`: the `platform` fixture's parameters become fake, stripe and
pennylane; `pennylane_platform()` is skipped with the reason unless `PENNYLANE_API_TOKEN`
and `PENNYLANE_SANDBOX_COMPANY_ID` are set. Its worlds come from a catalogue of its own,
`scripts/pennylane_worlds.py` (supported worlds with their fake counterparts, refused
worlds with their reasons, `NOT_SEEDED` for exclusions the maintainer approved), found by
`external_reference`. The fixture declares capabilities: `usage` false (the usage
properties skip with the reason, Pennylane holding none), `writes` false (the write
contract skips, but the shared tests that a platform without writes refuses every write
and still answers `find_action` run on it); the tests that need Stripe's `duplicate` world
read the world from the platform's catalogue. The run is the maintainer's, or an agent's
at his request, with his tokens.

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
their results recorded in the same pull request before it is merged: SC-002 is shown on
the platform, not on stubs. The seed token is therefore needed before the merge.

## Gates

`ruff check .`; `python -m pytest -q -p no:cacheprovider`; `python -m evals.runner --engine
mock --min-fully-correct 38 --baseline evals/baselines/mock.json`; `python -m evals.coverage
--check`; spec 013's snapshot test. Codex reviews each commit's diff; the maintainer
approves the final diff.
