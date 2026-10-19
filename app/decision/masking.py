"""Mask the customer's message before it reaches a model provider (D10).

The billing context is pseudonymised (`anonymise`); until D10 the message
travelled as written -- invoice references, addresses, the company's name, and
whatever bank or card number a customer pastes into a complaint. This masks
what can be recognised deterministically:

- invoice references the provider recognises, and any other identifier the
  context holds (subscriptions, payments, the customer): the alias the
  context already uses for it, so the message and the context speak of the
  same object -- or, for an invoice outside the context, a neutral token
  numbered per reference (`[INVOICE_REF_1]`) that never reveals its value;
- the customer's address and display name; any other address;
- IBANs (checked by their mod-97 key), card numbers (checked by Luhn),
  telephone numbers.

It is masking, not anonymisation. Names of people, and free text in general,
pass through; so do the amounts, dates and line descriptions of the context,
which are what the decision is about. ADR-009 lists it as such.

Positions are kept: every match is found on the original text, overlaps are
resolved longest-first, and replacements are applied once, from the end. A
token that already looks like one of our aliases is neutralised first, so a
message cannot pass itself off as a reference we issued.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

from app.decision.anonymise import Pseudonyms
from app.domain.models import BillingContext

#: Finds invoice references in a text: (start, end, normalised id) triples, in
#: the provider's own format. Optional: without one, only identifiers written
#: exactly as the context holds them are recognised.
ReferenceFinder = Callable[[str], list[tuple[int, int, str]]]

_ALIAS_LIKE = re.compile(r"\b(?:CUSTOMER|SUBSCRIPTION|INVOICE|PAYMENT)_\d+\b", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{2,3}[ .-]?\(?0?\)?[ .-]?[1-9]|0[1-9])(?:[ .-]?\d{2}){4}(?!\d)"
)


def mask_message(
    message: str,
    context: BillingContext,
    names: Pseudonyms,
    find_references: ReferenceFinder | None = None,
) -> str:
    """The message with what can be recognised replaced; the input untouched."""
    text = _ALIAS_LIKE.sub("[REF]", message)
    spans: list[tuple[int, int, str]] = []
    unknown_invoices: dict[str, str] = {}

    if find_references is not None:
        for start, end, invoice_id in find_references(text):
            alias = names.alias_of(invoice_id)
            if alias is None:
                number = unknown_invoices.setdefault(
                    invoice_id, f"[INVOICE_REF_{len(unknown_invoices) + 1}]"
                )
                alias = number
            spans.append((start, end, alias))

    # Identifiers written exactly as the context holds them.
    for real_id, alias in names.issued():
        for match in re.finditer(rf"(?<![\w-]){re.escape(real_id)}(?![\w-])", text, re.IGNORECASE):
            spans.append((match.start(), match.end(), alias))

    customer = context.customer
    for match in _EMAIL.finditer(text):
        is_customer = match.group(0).lower() == (customer.email or "").lower()
        spans.append((match.start(), match.end(), "[CUSTOMER_EMAIL]" if is_customer else "[EMAIL]"))
    for name in _names_of(customer.display_name):
        for match in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", _fold(text)):
            spans.append((match.start(), match.end(), "[CUSTOMER_NAME]"))

    for match in _IBAN.finditer(text):
        if _iban_valid(match.group(0)):
            spans.append((match.start(), match.end(), "[IBAN]"))
    for match in _CARD.finditer(text):
        if _luhn_valid(match.group(0)):
            spans.append((match.start(), match.end(), "[CARD]"))
    for match in _PHONE.finditer(text):
        spans.append((match.start(), match.end(), "[PHONE]"))

    return _apply(text, spans)


def _apply(text: str, spans: list[tuple[int, int, str]]) -> str:
    """Replace non-overlapping spans, longest first where they collide."""
    chosen: list[tuple[int, int, str]] = []
    for start, end, token in sorted(spans, key=lambda s: (s[0], -(s[1] - s[0]))):
        if chosen and start < chosen[-1][1]:
            continue
        chosen.append((start, end, token))
    for start, end, token in reversed(chosen):
        text = text[:start] + token + text[end:]
    return text


def _fold(text: str) -> str:
    """Lower case without accents, one character for one: positions are kept."""
    out = []
    for char in text:
        base = next(
            (c for c in unicodedata.normalize("NFKD", char) if not unicodedata.combining(c)),
            char,
        )
        lowered = base.lower()
        out.append(lowered[0] if len(lowered) == 1 else base)
    return "".join(out)


#: Legal forms dropped to find a company's name as customers write it.
_LEGAL_FORMS = re.compile(
    r"\b(?:sas|sasu|sarl|eurl|sa|sci|snc|gmbh|inc|llc|ltd)\b\.?", re.IGNORECASE
)


def _names_of(display_name: str | None) -> list[str]:
    """The display name, and the name without its legal form, folded."""
    if not display_name:
        return []
    full = _fold(display_name).strip()
    short = " ".join(_LEGAL_FORMS.sub(" ", full).split())
    names = {full}
    if len(short) >= 3:
        names.add(short)
    return sorted(names, key=len, reverse=True)


def _iban_valid(raw: str) -> bool:
    iban = raw.replace(" ", "").upper()
    if not 15 <= len(iban) <= 34:
        return False
    moved = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in moved)
    return int(digits) % 97 == 1


def _luhn_valid(raw: str) -> bool:
    digits = [int(c) for c in raw if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0
