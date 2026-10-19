# Bug Assessment: the customer named in a request is trusted over the sender's address

- **Slug**: d9-sender-matches-customer
- **Created**: 2026-10-19
- **Source**: code review finding D9 ("check that the customer found matches
  the sender"), reassessed against `main` `8f9e14f`
- **Verdict**: valid
- **Severity**: high -- a caller can investigate, and receive proposals about,
  an account the sender does not hold

Written in the format of Spec Kit's `bug` extension, by hand: Spec Kit is not
merged into `main` yet.

## Report (summarized)

When a dispute carries a `customer_id`, the investigation loads that customer
and ignores the sender's address entirely. The API accepts a `customer_id`
alone, or with any address.

## Symptom

**Observed.** `POST /disputes/investigate` with `customer_id=cus_acme` and
`customer_email=someone@elsewhere.test` is investigated as a message from
ACME: ACME's invoices are loaded, the engine is called, and a credit note can
be proposed for approval -- to a sender who is not ACME. With `customer_id`
alone, nobody is identified at all.

**Expected.** The sender's address identifies the sender; a `customer_id` is
only a claim, checked against it. A claim the address does not support is an
identity conflict: a person looks, nothing is proposed, nothing is said, and
the claimed account is not loaded.

## Reproduction

1. `POST /api/v1/disputes/investigate` with `message`, `customer_id:
   "cus_acme"`, `customer_email: "x@elsewhere.test"`, `invoice_id: "inv_002"`.
2. Observe `sender: known`, a `DUPLICATE_INVOICE` finding and a credit note
   proposed for approval.
3. Repeat with `customer_id` and no address: the same.

## Suspected Code Paths

- `app/application/investigate_dispute.py:_find_customer` -- `customer_id`
  wins and the address is never compared.
- `app/api/disputes.py:investigate` -- either identifier is accepted alone.

## Root Cause Hypothesis

The identifier the caller supplies was treated as an identity, when only the
sender's address is evidence of who wrote -- and even that is a declared
identity, not an authenticated one (ADR-009). Confidence: high.

## Proposed Remediation

**Preferred** (decided by the maintainer on 2026-10-19):

1. **The address is required.** The API refuses a dispute without
   `customer_email` (422). The `customer_id` stays optional.
2. **The address identifies the sender first**, through the provider's own
   lookup -- no normalisation of our own (no guessing about plus-addressing).
3. **A `customer_id` is a claim, checked against it.** When the address
   resolves to that customer, the investigation goes on as usual. In every
   other case -- the address belongs to another customer, to nobody, or the
   claimed id does not exist -- it is an **identity conflict**:
   `IDENTITY_CONFLICT`, a blocking finding; sender `suspected_impersonation`;
   human review; nothing proposed; no automatic reply; the engine is not
   called; **the claimed account is not loaded** -- not even to check that
   it exists, so the answer is the same whether it does or not.
4. The injection screen still runs first, on the raw text.

**Alternatives considered**:
- Accepting `customer_id` alone as an "unverified sender" sent to triage:
  rejected. An investigation must start from who wrote.
- Treating a conflict as an unknown sender: rejected. An address that is
  another customer's is not unknown, and calling it so would hide the claim.

**Files likely to change**:
- `app/application/investigate_dispute.py`, `app/domain/rules.py`,
  `app/domain/enums.py`, `app/policies/action_policy.py`, `app/api/disputes.py`
- tests, one development case, `docs/adr/007-unknown-sender.md`,
  `evals/taxonomy.md`, `README.md`, `docs/engineering-notes.md`

**Tests to add or update**:
- address and claim agree; address of another customer; unknown address;
  claimed id that does not exist; no address (422);
- on a conflict: no engine call, no context loaded for the claimed account,
  nothing proposed, no automatic reply, an explanation naming no account,
  the same answer whether the claimed id exists or not;
- an injection in a conflicting request is still flagged.

## Risks & Considerations

- **A colleague writing from another address of the same company, with the
  right `customer_id`,** now goes to a person -- as a colleague without the
  id already did through ADR-007's `same_domain` signal.
- **The address is declared, not authenticated.** A sender who forges a
  customer's address still passes this check; that is ADR-009's sender
  authentication limit, unchanged.
- **The audit keeps the claimed id** (`customer_id` column): it is what
  arrived, and the audit is internal (ADR-009).

## Acceptance

- No investigation loads, or proposes anything about, an account the sender's
  address does not resolve to.
- A dispute without an address is refused.
- The development set and the new case pass in CI, with no unsafe action.
