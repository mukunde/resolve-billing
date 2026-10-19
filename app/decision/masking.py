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
  telephone numbers in French national format or any international format
  written with a leading `+`.

It is masking, not anonymisation. Names of people, and free text in general,
pass through; so do the amounts, dates and line descriptions of the context,
which are what the decision is about. A long number that passes Luhn by
chance -- an order number, say -- is masked as a card: the trade-off is
deliberate, a number lost against a card number sent. ADR-009 lists what
passes.

The message is first put in Unicode NFC form, then every token that looks
like one of our aliases or placeholders becomes `[REF]`, so a message cannot
pass itself off as a reference we issued. Every match is then found on that
text; spans that overlap are merged, so a sensitive zone is masked whole, and
replaced once, from the end.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

from app.decision.anonymise import Pseudonyms
from app.domain.models import BillingContext

#: Recorded with every result the Claude engine produces, next to the prompt
#: version: a change to what is masked can change what the model decides.
MASKING_VERSION = "masking_v1"

#: Finds invoice references in a text: (start, end, normalised id) triples, in
#: the provider's own format. Optional: without one, only identifiers written
#: exactly as the context holds them are recognised.
ReferenceFinder = Callable[[str], list[tuple[int, int, str]]]

_ALIAS_LIKE = re.compile(
    r"\[(?:INVOICE_REF_\d+|CUSTOMER_EMAIL|EMAIL|CUSTOMER_NAME|IBAN|CARD|PHONE|REF)\]"
    r"|\b(?:CUSTOMER|SUBSCRIPTION|INVOICE|PAYMENT)_\d+\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_IBAN = re.compile(
    r"\b[A-Z]{2}\d{2}(?:[ -]?[A-Z0-9]{4}){2,7}(?:[ -]?[A-Z0-9]{1,4})?\b", re.IGNORECASE
)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PHONE = re.compile(
    # International: a leading +, then 7 to 15 digits, with at most two
    # separators (space, dot, dash, brackets) between two digits.
    r"(?<![\w+])\+\d(?:[ .()-]{0,2}\d){6,14}(?!\d)"
    # French national: 0, then nine digits in pairs.
    r"|(?<![\w+])0[1-9](?:[ .-]?\d{2}){4}(?!\d)"
)

#: Where two kinds of match cover the same length, which token stands.
_PRIORITY = ["[IBAN]", "[CARD]", "[CUSTOMER_EMAIL]", "[EMAIL]", "[PHONE]", "[CUSTOMER_NAME]"]


def mask_message(
    message: str,
    context: BillingContext,
    names: Pseudonyms,
    find_references: ReferenceFinder | None = None,
) -> str:
    """The message with what can be recognised replaced; the input untouched."""
    text = _ALIAS_LIKE.sub("[REF]", unicodedata.normalize("NFC", message))
    spans: list[tuple[int, int, str]] = []
    unknown_invoices: dict[str, str] = {}

    if find_references is not None:
        for start, end, invoice_id in find_references(text):
            alias = names.alias_of(invoice_id)
            if alias is None:
                alias = unknown_invoices.setdefault(
                    invoice_id, f"[INVOICE_REF_{len(unknown_invoices) + 1}]"
                )
            spans.append((start, end, alias))

    # Identifiers written exactly as the context holds them.
    for real_id, alias in names.issued():
        for match in re.finditer(rf"(?<![\w-]){re.escape(real_id)}(?![\w-])", text, re.IGNORECASE):
            spans.append((match.start(), match.end(), alias))

    customer = context.customer
    for match in _EMAIL.finditer(text):
        is_customer = match.group(0).lower() == (customer.email or "").lower()
        token = "[CUSTOMER_EMAIL]" if is_customer else "[EMAIL]"
        spans.append((match.start(), match.end(), token))

    folded, origin = _fold(text)
    for pattern in _name_patterns(customer.display_name):
        for match in pattern.finditer(folded):
            spans.append((origin[match.start()], origin[match.end() - 1] + 1, "[CUSTOMER_NAME]"))

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
    """Replace spans; overlapping ones merge into one zone, masked whole.

    The zone takes the token of its longest span -- the most specific reading
    of the zone -- so a short match starting first can never leave the tail of
    a longer one in clear.
    """
    zones: list[list] = []  # [start, end, token, length of the token's span]
    for start, end, token in sorted(spans):
        if zones and start < zones[-1][1]:
            zone = zones[-1]
            zone[1] = max(zone[1], end)
            if _outranks(end - start, token, zone[3], zone[2]):
                zone[2], zone[3] = token, end - start
        else:
            zones.append([start, end, token, end - start])
    for start, end, token, _ in reversed(zones):
        text = text[:start] + token + text[end:]
    return text


def _outranks(length: int, token: str, held_length: int, held: str) -> bool:
    if length != held_length:
        return length > held_length
    return _rank(token) < _rank(held)


def _rank(token: str) -> int:
    # Identifiers (aliases, invoice tokens) rank first: they carry meaning.
    return _PRIORITY.index(token) + 1 if token in _PRIORITY else 0


def _fold(text: str) -> tuple[str, list[int]]:
    """Lower case without accents, and where each folded character came from.

    A character may fold to several (a ligature) or to none (a combining
    mark): the map keeps every match traceable to the original text.
    """
    out: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        for piece in unicodedata.normalize("NFKD", char):
            if unicodedata.combining(piece):
                continue
            for lowered in piece.lower():
                out.append(lowered)
                origin.append(index)
    return "".join(out), origin


#: Legal forms dropped to find a company's name as customers write it.
_LEGAL_FORMS = re.compile(
    r"\b(?:sas|sasu|sarl|eurl|sa|sci|snc|gmbh|inc|llc|ltd)\b\.?", re.IGNORECASE
)


def _name_patterns(display_name: str | None) -> list[re.Pattern]:
    """The display name, and the name without its legal form, folded; the
    words may be joined by spaces, dots or dashes, or by nothing."""
    if not display_name:
        return []
    full = _fold(display_name)[0].split()
    short = _LEGAL_FORMS.sub(" ", " ".join(full)).split()
    variants = [full]
    if short != full and len("".join(short)) >= 3:
        variants.append(short)
    return [
        re.compile(r"(?<!\w)" + r"[\s.-]*".join(map(re.escape, words)) + r"(?!\w)")
        for words in variants
        if words
    ]


def _iban_valid(raw: str) -> bool:
    iban = re.sub(r"[ -]", "", raw).upper()
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
