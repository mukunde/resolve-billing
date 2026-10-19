# Bug Assessment: the customer's message reaches the model provider as written

- **Slug**: d10-message-masking
- **Created**: 2026-10-19
- **Source**: code review finding D10 ("mask the dispute message itself before
  the engine call"), reassessed against `main` `22e5932`
- **Verdict**: valid
- **Severity**: medium -- data the model provider has no need for leaves the
  system; no decision is wrong because of it

Written in the format of Spec Kit's `bug` extension, by hand: Spec Kit is not
merged into `main` yet.

## Report (summarized)

The Claude engine pseudonymises the billing context and the findings, but
sends the customer's message verbatim.

## Symptom

**Observed.** A message such as "ACME SAS here (alice@acme.fr). inv_002 is
wrong; refund to FR76 3000 6000 0112 3456 7890 189" reaches the provider
with the company's name, the address, the real invoice id and the IBAN --
while the context next to it calls the same invoice `INVOICE_2`. The model
sees two names for one object, and the provider receives what the rest of
the request was careful to withhold.

**Expected.** What can be recognised deterministically is masked before the
call, consistently with the context: the invoice a message cites carries the
alias the context gives it.

## Reproduction

1. Run the Claude engine on a dispute whose message cites `inv_002`, the
   customer's address and display name, an IBAN and a card number.
2. Capture the request handed to the SDK client.
3. Observe each of them in `<customer_message>`.

## Suspected Code Paths

- `app/decision/claude.py:decide` -- `dispute.customer_message` is
  interpolated into the prompt as is.

## Root Cause Hypothesis

Pseudonymisation (`anonymise`) was written for structured data, where every
identifier sits in a known field. The message is free text, and was left
out rather than handled imperfectly. Confidence: high.

## Proposed Remediation

**Preferred** (decided by the maintainer on 2026-10-19): deterministic
masking inside the Claude adapter, `app/decision/masking.py`.

1. **Invoice references** are recognised by the provider, which knows how
   its platform writes them (`invoice_reference_spans`, wired by
   `deps.wire`). One the context holds takes its context alias; one outside
   the context becomes `[INVOICE_REF_n]`, numbered per distinct reference,
   never revealing its value. Other context identifiers written as they are
   (subscription, payment, customer) take their aliases too.
2. **The customer's address** becomes `[CUSTOMER_EMAIL]`, any other address
   `[EMAIL]`; **the display name**, with or without its legal form, case
   and accents folded, `[CUSTOMER_NAME]`.
3. **IBANs** (mod-97 key checked), **card numbers** (Luhn checked) and
   **telephone numbers** become `[IBAN]`, `[CARD]`, `[PHONE]`. A number that
   fails its check is left alone: amounts, dates and quantities pass.
4. **A token that already looks like one of our aliases** (`INVOICE_7`)
   becomes `[REF]` first, so a message cannot pass itself off as a reference
   we issued. Only aliases we issued are put back in the engine's output.
5. Every match is found on the original text and applied once, longest
   first where two collide. The dispute itself is not modified: the audit
   and the injection screen see the message as received.

It is masking, not anonymisation: people's names other than the customer's,
and free text in general, pass through. ADR-009 says so.

**Alternatives considered**:
- A named-entity model to find people and places: rejected for now. It adds
  a dependency on the main path whose errors are neither deterministic nor
  testable case by case; the structured identifiers are the ones the
  provider can act on.
- Masking in the investigator, for every engine: rejected. The mock reads
  only findings, and a local engine would not need it; masking is the
  concern of the adapter that sends data out.
- Removing invoice references outright: rejected. "Which invoice" is what
  many disputes are about; the alias keeps the meaning and hides the value.

**Files likely to change**:
- `app/decision/masking.py` (new), `app/decision/anonymise.py`,
  `app/decision/claude.py`, `app/providers/fake/adapter.py`,
  `app/api/deps.py`, `evals/runner.py`
- `tests/unit/test_masking.py`, `docs/adr/009-trust-boundaries.md`,
  `README.md`, `docs/engineering-notes.md`

**Tests to add or update** (no model needed):
- each kind of identifier and number masked, and the context alias used for
  a known invoice in every written form the provider recognises;
- distinct tokens for distinct unknown invoices, the same token for a
  repeat;
- alias-like tokens neutralised;
- amounts, dates, quantities, and numbers failing their check left alone;
- on the request captured from a stub SDK client: no sentinel present; the
  dispute unchanged; only issued aliases restored in the decision.

## Risks & Considerations

- **The model sees less.** A message's invoice number was already redundant
  with the context's target; an unknown invoice is now a token. The
  before/after measurement below is a signal that nothing broke, not a proof
  of equivalence: the development set is small.
- **A provider's reference format is its own.** Without
  `invoice_reference_spans`, only identifiers written exactly as the context
  holds them are masked. Each real adapter must provide the hook.
- **Recognition is by pattern.** A phone number in an unusual format, or a
  card number split oddly, can pass. The list of what is masked is the list
  of what is tested.

## Measurement

Mock engine: unchanged, it does not read the message. Claude engine: one run
on `main` and one on this branch, same model, same case file, same
parameters -- to be recorded here once run.

## Acceptance

- No identifier the context holds, no customer address or display name, no
  IBAN, card or phone number in a recognised form reaches the provider.
- The development set passes in CI with no unsafe action; the Claude
  before/after run shows no regression beyond run-to-run variance.
