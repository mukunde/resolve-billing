# Disclosure and replies

What the system may say, to whom, through which channel, and when a reply may
go out without a person. Written on 2026-11-15 from the existing records, as
they stood, with spec 016 (specs/016-bank-evidence), the first change to
touch the capability since contracts exist: it adds the bank as a source.
Reasons are in constitution VII and III, ADR-009 ("Disclosure, channel by
channel" and "A bank as a source of evidence"), ADR-011 (automated mail and
automatic replies) and ADR-014.

ADR-009 holds the inventory: every channel, who reads it, what it carries,
and how each guard is held -- **tested**, **convention** or **to build**.
This contract states the rules the system relies on; the inventory stays
where the per-channel detail lives.

## What the system relies on

- **Text meant for the sender names only the sender's own account.** No
  identifier, name or amount of another customer. To an unknown sender or a
  suspected impersonator, nothing about any account -- not that it exists,
  not that an invoice exists (ADR-007).
- **Internal channels carry what their readers need, and no more.** Codes
  and a link where a link is enough; evidence stays behind authentication.
  Every route that shows a run, a review or an action takes an authenticated
  caller.
- **The investigation's explanation is internal prose.** The answer to
  `POST /disputes/investigate` carries the engine's explanation, or one the
  application assembles from the findings: it is not a customer reply and
  must never be forwarded to the sender as it is. No mail carries it: the
  approval mail sends codes, the amount and a link; the review and run
  pages show the explanation escaped, the review page labelling it an
  opinion, not evidence (2026-11-21).
  Execution and API-error mails still carry the API's error text as
  received; reducing those to a category is to build (ADR-009).
- **What reaches a model provider is reduced.** The context is pseudonymised,
  the message masked for the formats the masking recognises -- printed
  invoice numbers of loaded invoices included, on every platform
  (`decision-engine.md`) -- and a bank finding reaches it as its code alone.
- **Bank text goes nowhere.** Counterparty fields, account numbers, bank
  identifiers, descriptions, raw labels and references are read by the
  mapper, used by the check, then dropped. What leaves the check is its
  finding and its record, `BankCheck`, a closed type: the run row, the run
  page and the review page show that record; the investigation's answer, the
  trace, mails and n8n carry codes at most; the MCP server and the action read
  carry nothing of the bank (ADR-009).
- **Errors never echo.** A 422 never repeats the value sent; a platform or
  bank failure is a category, never the vendor's text; telemetry and this
  code's logs carry identifiers, codes and counts, never the message, the
  prompt, the context or an address.
- **An automatic reply is a separate decision.** The action policy says
  whether an action needs a person; `automatic_reply`
  (`app/policies/reply.py`), after it, says whether text may go to the sender
  unattended. The first condition that fails names the reason (a closed set);
  none failing allows:

  | In order | Reason when it fails |
  |---|---|
  | no prompt injection | `needs_a_person` |
  | not an automated response set aside | `automated_message` |
  | not a suspected impersonator | `suspected_impersonation` |
  | no automated-mail finding | `automated_message` |
  | the sender is a known customer | `unknown_sender` |
  | no bank finding and no bank check that could not conclude | `needs_a_person` |
  | the verdict is `AUTO` | `needs_a_person` |
  | the action is a `reply` or a `request_information` | `nothing_to_send` |

  A bank check that found nothing in a complete, covered read does not stop a
  reply: a request for information may still go, as it would without a bank.
- **No reply is sent today.** There is no reply drafter; the workflow's
  automatic-reply branch is gated by this decision and sends nothing yet.

## Not guaranteed today

- **The outgoing-text check is to build**, with the reply drafter: every
  outgoing text checked against the first rule, and a request for
  information after a bank read that found nothing never asserting
  non-payment nor revealing any bank data.
- **No per-customer scope.** A signed-in approver and the service caller can
  read any customer's runs.
- **Masking is by pattern**: other people's names, free text and unusual
  formats pass; pseudonymising identifiers does not anonymise the data.
- **Channels outside this code** -- the server's, n8n's and the SDKs' logs,
  n8n's execution data, the trace backend's retention -- are set by
  convention, not by tests.

## Covered by

`tests/unit/test_masking.py`, `tests/unit/test_automated_mail.py`,
`tests/unit/test_bank_foundation.py`, `tests/integration/test_bank_evidence.py`,
and the tests ADR-009's inventory names for each **tested** guard.
