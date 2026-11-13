"""Pennylane's payloads onto the domain (specs/014-pennylane-read, FR-002 to
FR-006, FR-008a, FR-010), and the credit notes written back
(specs/015-pennylane-credit-notes).

Pennylane's vocabulary stops here (ADR-001). Every mapping whose meaning the
documentation leaves open is a convention the maintainer approved (coordination
decision 46), written once below with its provenance; anything not listed is
refused with a reason, never approximated.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.enums import InvoiceStatus, PaymentStatus, SubscriptionStatus
from app.domain.models import (
    Customer,
    Invoice,
    InvoiceLine,
    LineTax,
    Payment,
    Subscription,
    TaxBreakdown,
    canonical_rate,
)
from app.domain.money import Money
from app.domain.rules import exactly_one_cycle
from app.providers.base import ProviderAction, ProviderRejected, ProviderUnsupported
from app.providers.tax import reconcile, untaxed_unless_charged

Payload = dict[str, Any]

# -- identifiers (FR-008a) -----------------------------------------------------

KINDS = {
    "customer": "pl_cus_",
    "invoice": "pl_inv_",
    "subscription": "pl_sub_",
    "payment": "pl_pay_",
}


def typed(kind: str, raw: Any) -> str:
    """Pennylane's integer id as a domain identifier of its kind."""
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise ProviderUnsupported("missing_field")
    return f"{KINDS[kind]}{raw}"


def raw_id(identifier: str | None, kind: str) -> int | None:
    """Back to Pennylane's integer, or None for an identifier of another kind
    or another platform -- never sent to the API as an id."""
    prefix = KINDS[kind]
    if not identifier or not identifier.startswith(prefix):
        return None
    digits = identifier[len(prefix):]
    return int(digits) if digits.isdigit() and digits[0] != "0" else None


def _ref(value: Any, kind: str) -> str | None:
    """A `{id, url}` reference as a typed id; None when absent."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProviderUnsupported("missing_field")
    return typed(kind, value.get("id"))


# -- dates (decision 46: midnight UTC, the maintainer's choice of 2026-11-12) --------


def _day(value: Any) -> date:
    if not isinstance(value, str):
        raise ProviderUnsupported("missing_field")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ProviderUnsupported("missing_field") from None


def midnight(day: date) -> datetime:
    """A calendar date as the instant it starts in UTC -- the convention, chosen
    on 2026-11-12 over Paris's midnight, which put a period's bounds an hour
    apart across a change of summer time and left the domain's UTC calendar
    unable to measure a cycle. An invoice at the window's edge may fall one day
    in or out."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def day_after(day: date) -> datetime:
    """The midnight after `day`."""
    return midnight(day + timedelta(days=1))


# -- money --------------------------------------------------------------------------


def _currency(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{3}", value):
        raise ProviderUnsupported("missing_field")
    return value.upper()


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str | int) or isinstance(value, bool):
        raise ProviderUnsupported("missing_field")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ProviderUnsupported("missing_field") from None
    if not number.is_finite():
        raise ProviderUnsupported("missing_field")
    return number


def _money(value: Any, currency: str, finer: str = "missing_field") -> Money:
    """An exact amount in minor units; one finer than the currency's minor unit
    is refused with `finer`, never rounded into representability (ADR-005)."""
    number = _decimal(value)
    money = Money.from_decimal(number, currency)
    if money.to_decimal() != number:
        raise ProviderUnsupported(finer)
    return money


def _nonzero_discount(discount: Any) -> bool:
    if discount in (None, {}):
        return False
    if not isinstance(discount, dict):
        raise ProviderUnsupported("missing_field")
    value = discount.get("value")
    return value not in (None, "") and _decimal(value) != 0


# -- customers (FR-002) ------------------------------------------------------------------


def emails(payload: Payload) -> list[str]:
    found = payload.get("emails")
    if found is None:
        return []
    if not isinstance(found, list) or not all(isinstance(e, str) for e in found):
        raise ProviderUnsupported("missing_field")
    return [e.strip() for e in found if e.strip()]


def customer(payload: Payload) -> Customer:
    addresses = emails(payload)
    if not addresses:
        raise ProviderUnsupported("missing_field")
    if len(addresses) > 1:
        # The domain holds one address; the others would escape the
        # impersonation check (ADR-007). Decision 46.
        raise ProviderUnsupported("multiple_addresses")
    name = payload.get("name")
    return Customer(
        id=typed("customer", payload.get("id")),
        email=addresses[0],
        currency=None,  # Pennylane holds none on a customer
        display_name=name if isinstance(name, str) else None,
    )


# -- invoices (FR-003, FR-004) -------------------------------------------------------------

#: Documents of the invoice list that are not invoices: left out of lists,
#: refused when named.
NOT_INVOICES = frozenset(
    {"proforma", "shipping_order", "purchasing_order", "estimate_pending",
     "estimate_accepted", "estimate_invoiced", "estimate_denied"}
)
#: Invoice statuses and what they mean (decision 46). `cancelled` is void only
#: for an invoice with no credit note against it: one with any is refused
#: (`invoice_credit`) before its status is read. Anything else: `invoice_status`.
INVOICE_STATUS = {
    "paid": InvoiceStatus.PAID,
    "upcoming": InvoiceStatus.OPEN,
    "late": InvoiceStatus.OPEN,
    "draft": InvoiceStatus.DRAFT,
    "cancelled": InvoiceStatus.VOID,  # convention, 2026-11-11
}
#: VAT codes the slice reads, as rates (metropolitan France); `exempt` charges
#: none. Every other code is refused (`tax_code`) until a later decision.
VAT_RATES = {"FR_200": Decimal(20), "FR_100": Decimal(10), "FR_55": Decimal("5.5"),
             "FR_21": Decimal("2.1")}
NO_TAX = frozenset({"exempt"})


def is_credit_note(payload: Payload) -> bool:
    """Told by the link to the invoice it credits, never by status: a linked
    credit note can carry `cancelled`."""
    return payload.get("credited_invoice") is not None


def is_invoice(payload: Payload) -> bool:
    return not is_credit_note(payload) and payload.get("status") not in NOT_INVOICES


def credited_ids(credit_notes: Iterable[Payload]) -> set[str]:
    """The typed ids of the invoices these credit notes credit."""
    return {_ref(note.get("credited_invoice"), "invoice") for note in credit_notes} - {None}


def issue_day(payload: Payload) -> date:
    return _day(payload.get("date"))


def line(payload: Payload, currency: str) -> InvoiceLine:
    if _nonzero_discount(payload.get("discount")):
        raise ProviderUnsupported("invoice_discount")
    excluding = _money(payload.get("currency_amount_before_tax"), currency)
    if excluding.amount_minor < 0:
        raise ProviderUnsupported("negative_line")
    tax = _money(payload.get("currency_tax"), currency)
    code = payload.get("vat_rate")
    if code in VAT_RATES:
        taxes = (LineTax(amount=tax, rate=VAT_RATES[code]),)
    elif code in NO_TAX:
        if tax.amount_minor != 0:
            raise ProviderUnsupported("tax_mismatch")
        taxes = (LineTax(amount=tax, rate=Decimal(0), reason=code),)
    else:
        raise ProviderUnsupported("tax_code")
    dates = payload.get("imputation_dates")
    start = end = None
    if dates is not None:
        if not isinstance(dates, dict):
            raise ProviderUnsupported("missing_field")
        if dates.get("start_date") is not None and dates.get("end_date") is not None:
            # `end_date` included (convention, 2026-11-11).
            start = midnight(_day(dates["start_date"]))
            end = day_after(_day(dates["end_date"]))
    label = payload.get("label")
    return InvoiceLine(
        description=label if isinstance(label, str) else "",
        quantity=_decimal(payload.get("quantity")),
        unit_amount=_money(payload.get("raw_currency_unit_price"), currency,
                           finer="fractional_unit_price"),
        amount=excluding,
        period_start=start,
        period_end=end,
        taxes=taxes,
    )


def invoice(payload: Payload, lines: Sequence[Payload]) -> Invoice:
    """An invoice (never a credit note or another document), with every line."""
    if not is_invoice(payload):
        raise ProviderUnsupported("invoice_status")
    status = INVOICE_STATUS.get(payload.get("status"))
    if status is None:
        raise ProviderUnsupported("invoice_status")
    if _nonzero_discount(payload.get("discount")):
        raise ProviderUnsupported("invoice_discount")
    if not lines:
        # One created from ledger entries without lines is not an empty invoice.
        raise ProviderUnsupported("missing_field")
    currency = _currency(payload.get("currency"))
    total = _money(payload.get("currency_amount"), currency)
    mapped = untaxed_unless_charged([line(item, currency) for item in lines])
    reconcile(
        mapped,
        total,
        excluding_tax=_money(payload.get("currency_amount_before_tax"), currency),
        tax_total=_money(payload.get("currency_tax"), currency),
    )
    number = payload.get("invoice_number")
    return Invoice(
        id=typed("invoice", payload.get("id")),
        customer_id=_ref(payload.get("customer"), "customer") or _missing(),
        subscription_id=_ref(payload.get("billing_subscription"), "subscription"),
        total=total,
        status=status,
        issued_at=midnight(issue_day(payload)),
        lines=mapped,
        number=number if isinstance(number, str) and number else None,
    )


def owner(payload: Payload) -> str:
    """Whose an invoice is, and nothing else read from it."""
    return _ref(payload.get("customer"), "customer") or _missing()


def _missing():
    raise ProviderUnsupported("missing_field")


# -- payments (FR-005) ---------------------------------------------------------------

#: Payment statuses (conventions of 2026-11-11, pending Pennylane's technical
#: support; each to become "confirmed by Pennylane, <date>" or be changed). The
#: maintainer kept, on 2026-11-12, the assumption that a collection still
#: confirmed or paid out was not refunded, and a refunded one in full.
PAYMENT_STATUS: dict[str, tuple[PaymentStatus, str]] = {
    "confirmed": (PaymentStatus.SUCCEEDED, "convention 2026-11-11"),
    "paid_out": (PaymentStatus.SUCCEEDED, "convention 2026-11-11"),
    "refunded": (PaymentStatus.REFUNDED, "convention 2026-11-11"),
    **{status: (PaymentStatus.FAILED, "convention 2026-11-11")
       for status in ("failed", "aborted", "error", "cancelled", "customer_approval_denied")},
    **{status: (PaymentStatus.PENDING, "convention 2026-11-11")
       for status in ("initiated", "pending", "emitted", "prepared", "submitted",
                      "pending_submission", "pending_customer_approval",
                      "resubmission_requested", "draft", "found", "not_found")},
}


def payment(payload: Payload, invoice_id: str) -> Payment:
    entry = PAYMENT_STATUS.get(payload.get("status"))
    if entry is None:  # `charged_back` and any status not listed
        raise ProviderUnsupported("payment_status")
    currency = _currency(payload.get("currency"))
    return Payment(
        id=typed("payment", payload.get("id")),
        invoice_id=invoice_id,
        amount=_money(payload.get("currency_amount"), currency),
        status=entry[0],
        received_at=None,  # Pennylane gives a payment no date
    )


# -- subscriptions (FR-006) ---------------------------------------------------------------

RULES = {"monthly": "month", "yearly": "year", "weekly": "week"}


def subscription(payload: Payload) -> Subscription:
    status = payload.get("status")
    if status == "in_progress":
        mapped, cancelled_at = SubscriptionStatus.ACTIVE, None
    elif status == "stopped":
        # `stopped_at` prevents the next renewal: the service runs until the
        # day before `next_occurrence`, included (convention, 2026-11-11).
        mapped, cancelled_at = SubscriptionStatus.CANCELLED, midnight(
            _day(payload.get("next_occurrence")))
    elif status == "finished":
        # The service ends at `finish`, that day included (convention, 2026-11-11).
        mapped, cancelled_at = SubscriptionStatus.CANCELLED, day_after(_day(payload.get("finish")))
    else:
        raise ProviderUnsupported("subscription_status")
    rule = payload.get("recurring_rule")
    if not isinstance(rule, dict):
        raise ProviderUnsupported("missing_field")
    unit = RULES.get(rule.get("rule_type"))
    count = rule.get("interval")
    if unit is None or not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ProviderUnsupported("missing_field")
    previous = _day(payload.get("prev_occurrence"))
    following = _day(payload.get("next_occurrence"))
    label = payload.get("label")
    identifier = typed("subscription", payload.get("id"))
    mapped_subscription = Subscription(
        id=identifier,
        customer_id=_ref(payload.get("customer"), "customer") or _missing(),
        status=mapped,
        plan_code=label if isinstance(label, str) and label else identifier,
        current_period_start=midnight(previous),
        current_period_end=midnight(following),
        cancelled_at=cancelled_at,
        billing_interval=unit,
        billing_interval_count=count,
        billing_anchor=midnight(_day(payload.get("start"))),
    )
    # The current period must be exactly one cycle, by the domain's own
    # calendar -- anchored, month ends clamped (decision 46, Q1).
    if not exactly_one_cycle(mapped_subscription):
        raise ProviderUnsupported("missing_field")
    return mapped_subscription


def fully_dated(invoice_: Invoice) -> bool:
    """Every line with a period that runs forwards: what an invoice of a
    cancelled subscription needs, so no check decides on partial coverage."""
    return bool(invoice_.lines) and all(
        line_.period_start is not None
        and line_.period_end is not None
        and line_.period_start < line_.period_end
        for line_ in invoice_.lines
    )


# -- references (FR-010) ------------------------------------------------------------------


def _numbers(invoices: Sequence[Invoice]) -> dict[str, str]:
    """Each number, case-folded, to the one invoice that carries it; a number
    two invoices share is left out: it designates neither."""
    by_number: dict[str, set[str]] = {}
    for item in invoices:
        if item.number:
            by_number.setdefault(item.number.casefold(), set()).add(item.id)
    return {n: next(iter(ids)) for n, ids in by_number.items() if len(ids) == 1}


def resolve_number(reference: str, invoices: Sequence[Invoice]) -> str | None:
    """The invoice whose whole number `reference` is, case-folded."""
    return _numbers(invoices).get(reference.strip().casefold())


def _folded(text: str) -> tuple[str, list[int]]:
    """`text` case-folded in full (ß as ss), and for each folded character the
    index of the original character it came from."""
    parts: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        folded = char.casefold()
        parts.append(folded)
        origin.extend([index] * len(folded))
    return "".join(parts), origin


def references(text: str, invoices: Sequence[Invoice]) -> list[tuple[int, int, str]]:
    """Where the numbers of `invoices` sit in `text`, and the invoice each
    stands for: whole numbers, compared case-folded in full, on whole
    characters, bounded by a character that is neither a letter nor a digit (in
    any script), the longest kept first where two overlap, never overlapping;
    the spans are the original text's."""
    known = _numbers(invoices)
    if not known:
        return []
    folded, origin = _folded(text)
    candidates: list[tuple[int, int, str]] = []
    for number, invoice_id in known.items():
        at = folded.find(number)
        while at != -1:
            end = at + len(number)
            whole = (at == 0 or origin[at - 1] != origin[at]) and (
                end == len(folded) or origin[end] != origin[end - 1]
            )
            if whole:
                start, stop = origin[at], origin[end - 1] + 1
                before = start == 0 or not text[start - 1].isalnum()
                after = stop == len(text) or not text[stop].isalnum()
                if before and after:
                    candidates.append((start, stop, invoice_id))
            at = folded.find(number, at + 1)
    chosen: list[tuple[int, int, str]] = []
    for start, stop, invoice_id in sorted(candidates, key=lambda c: (c[0] - c[1], c[0])):
        if all(stop <= s or start >= e for s, e, _ in chosen):
            chosen.append((start, stop, invoice_id))
    return sorted(chosen)


# -- credit notes (specs/015-pennylane-credit-notes) -----------------------------------

#: What a credit note this system writes carries as `external_reference`: the
#: prefix, then the action's 64-character key -- the only identifier Pennylane
#: holds of it (FR-013).
REFERENCE_PREFIX = "rb:"
OUR_REFERENCE = re.compile(r"rb:[0-9a-f]{64}")
#: The longest `external_reference` sent. Pennylane's own limit is to be read
#: in the write contract run; until then the reference is held to this.
REFERENCE_MAX = 255
#: Pennylane's words when linking a credit note would take the invoice's
#: credits beyond its total, seen on the sandbox on 2026-11-12 (400).
CAP_MESSAGE = "The credit notes amount is bigger than the invoice amount"
#: Fields that would carry a document out of Pennylane, on a customer or on a
#: document: none may hold anything (decision 47). Matched as parts of a
#: field's name, so a field named slightly differently still counts.
OUTWARD_FIELD_PARTS = ("electronic_billing", "e_invoicing", "einvoicing", "e-invoicing")
#: The customer's legal identifiers (SIREN, VAT number): a customer with either
#: is one the platform may address formally (decision 47).
LEGAL_IDENTIFIERS = ("reg_no", "vat_number")
#: The reserved top-level domain (RFC 6761): mail to it is never delivered.
TEST_TLD = ".test"


def reference(idempotency_key: str) -> str:
    """The `external_reference` of the action's credit note."""
    return f"{REFERENCE_PREFIX}{idempotency_key}"


def is_ours(payload: Payload) -> bool:
    """Whether a document carries a reference this system writes."""
    value = payload.get("external_reference")
    return isinstance(value, str) and OUR_REFERENCE.fullmatch(value) is not None


def credited_raw(payload: Payload) -> int | None:
    """The id of the invoice a document is linked to, or None when unlinked."""
    linked = payload.get("credited_invoice")
    if linked is None:
        return None
    if not isinstance(linked, dict):
        raise ProviderUnsupported("missing_field")
    raw = linked.get("id")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise ProviderUnsupported("missing_field")
    return raw


def is_draft(payload: Payload) -> bool:
    """Whether a document is a draft, confirmed either way: `draft: true`
    (its status `draft` or absent) is a draft; `draft: false` with any status
    but `draft` is finalised. Anything else -- the flag absent or malformed,
    or contradicting the status -- cannot be told, and is `missing_field`:
    never taken for a draft that may be deleted, nor for a final document."""
    flag, status = payload.get("draft"), payload.get("status")
    if flag is True and status in (None, "draft"):
        return True
    if flag is False and status != "draft":
        return False
    raise ProviderUnsupported("missing_field")


def partial_stage(payload: Payload) -> str | None:
    """What a document under our reference lacks to be a written credit
    note: "draft", "unlinked", or None when finalised and linked."""
    if is_draft(payload):
        return "draft"
    if credited_raw(payload) is None:
        return "unlinked"
    return None


def _unit(line_: Payload) -> str:
    unit = line_.get("unit")
    return unit if isinstance(unit, str) and unit else "piece"


def credit_lines(
    invoice_payload: Payload,
    lines: Sequence[Payload],
    amount_minor: int,
    breakdown: TaxBreakdown | None,
) -> list[Payload]:
    """The lines of the credit note, built before anything is sent.

    On a taxed invoice (an approved `breakdown`): every line of the invoice
    reversed at its own code, its quantity negative, its unit price the
    original's in cents -- the invoice as it stands must still have exactly
    the approved breakdown, credited whole (spec 013). On an untaxed one: one
    line at the invoice's no-tax code for the approved amount. Anything else
    is `invoice_state`, nothing sent. Labels are the invoice's own, never the
    dispute's (FR-004).
    """
    try:
        mapped = invoice(invoice_payload, lines)
    except ProviderUnsupported:
        raise ProviderRejected("invoice_state") from None
    now = TaxBreakdown.of(mapped)
    currency = mapped.total.currency
    if breakdown is not None:
        if now != breakdown or breakdown.total.amount_minor != amount_minor:
            raise ProviderRejected("invoice_state")
        credit: list[Payload] = []
        for item in lines:
            quantity = _decimal(item.get("quantity"))
            if quantity <= 0:
                raise ProviderRejected("invoice_state")
            try:
                price = _money(item.get("raw_currency_unit_price"), currency,
                               finer="fractional_unit_price")
            except ProviderUnsupported:
                raise ProviderRejected("invoice_state") from None
            label = item.get("label")
            credit.append({
                "label": label if isinstance(label, str) and label else "Avoir",
                "quantity": format(-quantity, "f"),
                "unit": _unit(item),
                "raw_currency_unit_price": format(price.to_decimal(), "f"),
                "vat_rate": item.get("vat_rate"),
            })
        return credit
    if now is not None:
        raise ProviderRejected("invoice_state")
    codes = {item.get("vat_rate") for item in lines}
    if len(codes) != 1 or not codes <= NO_TAX:
        raise ProviderRejected("invoice_state")
    if not 0 < amount_minor <= mapped.total.amount_minor:
        raise ProviderRejected("invoice_state")
    return [{
        "label": "Avoir",
        "quantity": "-1",
        "unit": "piece",
        "raw_currency_unit_price": format(
            Money(amount_minor=amount_minor, currency=currency).to_decimal(), "f"),
        "vat_rate": next(iter(codes)),
    }]


def _credit_amount(value: Any, currency: str) -> Money:
    """A credit note's amount, posted negative (or zero), as a positive one."""
    money = _money(value, currency)
    if money.amount_minor > 0:
        raise ProviderUnsupported("missing_field")
    return Money(amount_minor=-money.amount_minor, currency=currency)


def credit_total(doc: Payload) -> int:
    """A credit note's total including tax, positive, in minor units."""
    return _credit_amount(doc.get("currency_amount"), _currency(doc.get("currency"))).amount_minor


def observed_breakdown(doc: Payload, lines: Sequence[Payload]) -> TaxBreakdown | None:
    """What a credit note credits, as the positive amounts of a
    `TaxBreakdown`: excluding tax and total from the document, tax by rate
    from its lines, which must make the document's tax; a credit whose every
    tax is zero is None (FR-010)."""
    currency = _currency(doc.get("currency"))
    excluding = _credit_amount(doc.get("currency_amount_before_tax"), currency)
    total = _credit_amount(doc.get("currency_amount"), currency)
    tax_total = _credit_amount(doc.get("currency_tax"), currency)
    by_rate: dict[Decimal, int] = {}
    summed = 0
    for item in lines:
        tax = _credit_amount(item.get("currency_tax"), currency).amount_minor
        code = item.get("vat_rate")
        if code in VAT_RATES:
            rate = canonical_rate(VAT_RATES[code])
            by_rate[rate] = by_rate.get(rate, 0) + tax
        elif code in NO_TAX:
            if tax != 0:
                raise ProviderUnsupported("tax_mismatch")
        else:
            raise ProviderUnsupported("tax_code")
        summed += tax
    if summed != tax_total.amount_minor or excluding.amount_minor + summed != total.amount_minor:
        raise ProviderUnsupported("tax_mismatch")
    taxes = tuple(
        (rate, Money(amount_minor=amount, currency=currency))
        for rate, amount in sorted(by_rate.items())
        if amount != 0
    )
    if not taxes:
        return None
    return TaxBreakdown(excluding_tax=excluding, taxes=taxes, total=total)


def is_cap_refusal(refused: Any) -> bool:
    """Whether a refusal of a link is Pennylane's cumulative cap: a 400 with
    the message seen on the sandbox. A changed message is not recognised, and
    the refusal then goes to a person -- the safe side."""
    return getattr(refused, "status", None) == 400 and CAP_MESSAGE in str(
        getattr(refused, "detail", ""))


def _outward(payload: Payload) -> bool:
    """Whether any field that would carry a document out holds something."""
    return any(
        value not in (None, "", [], {}, False)
        for key, value in payload.items()
        if any(part in key.lower() for part in OUTWARD_FIELD_PARTS)
    )


def _test_address(address: str) -> bool:
    _, at, domain = address.strip().lower().rpartition("@")
    return bool(at) and domain.endswith(TEST_TLD) and len(domain) > len(TEST_TLD)


def outward_ready(customer_payload: Payload, addresses: Sequence[str],
                  doc: Payload | None = None, eligible: Sequence[Any] = ()) -> bool:
    """Decision 47's conditions for a document to be made and finalised: a
    customer with no SIREN, no VAT number and no electronic billing address,
    at least one address and every one under the reserved test domain; and,
    when a document is given, nothing on it that would carry it out; and no
    eligible electronic billing address, as Pennylane lists them (FR-004)."""
    if eligible:
        return False
    if any(customer_payload.get(field) not in (None, "") for field in LEGAL_IDENTIFIERS):
        return False
    if _outward(customer_payload):
        return False
    if not addresses or not all(_test_address(a) for a in addresses):
        return False
    return doc is None or not _outward(doc)


#: The allocation a credit note took, read from the status of the invoice it
#: credits: a paid one stays paid (an amount owed to the customer); an open
#: one credited is cancelled in part or in full (seen on the sandbox).
ALLOCATION_OF_STATUS = {
    "paid": "customer_balance",
    "partially_cancelled": "reduce_due",
    "cancelled": "reduce_due",
}


def _created(doc: Payload) -> datetime:
    value = doc.get("created_at")
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    return midnight(issue_day(doc))


def provider_action(
    doc: Payload,
    lines: Sequence[Payload],
    invoice_payload: Payload | None,
    *,
    observed_at: datetime,
    test_mode: bool,
) -> ProviderAction:
    """A document found under our reference, as the execution service checks
    it. Complete -- finalised, linked -- it reports its status, its allocation
    (from the credited invoice's status, when that invoice is the one read)
    and the breakdown it credits; incomplete, `partial` says what it lacks and
    status and allocation are left unread (FR-008, FR-010)."""
    value = doc.get("external_reference")
    if not isinstance(value, str) or not OUR_REFERENCE.fullmatch(value):
        raise ProviderUnsupported("missing_field")
    currency = _currency(doc.get("currency"))
    linked = credited_raw(doc)
    partial = partial_stage(doc)
    allocation = None
    if partial is None and invoice_payload is not None and linked == invoice_payload.get("id"):
        allocation = ALLOCATION_OF_STATUS.get(invoice_payload.get("status"))
    return ProviderAction(
        provider="pennylane",
        provider_action_id=typed("invoice", doc.get("id")),
        action_type="credit_note",
        idempotency_key=value[len(REFERENCE_PREFIX):],
        status="succeeded" if partial is None else None,
        created_at=_created(doc),
        invoice_id=typed("invoice", linked) if linked is not None else "",
        amount_minor=_credit_amount(doc.get("currency_amount"), currency).amount_minor,
        currency=currency,
        test_mode=test_mode,
        allocation=allocation,
        action_id=None,  # the key is the only identifier Pennylane holds (FR-013)
        observed_at=observed_at,
        breakdown=observed_breakdown(doc, lines),
        breakdown_reported=True,
        partial=partial,
    )
