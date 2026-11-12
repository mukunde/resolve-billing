"""Pennylane's payloads onto the domain (specs/014-pennylane-read, FR-002 to
FR-006, FR-008a, FR-010).

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
from zoneinfo import ZoneInfo

from app.domain.enums import InvoiceStatus, PaymentStatus, SubscriptionStatus
from app.domain.models import Customer, Invoice, InvoiceLine, LineTax, Payment, Subscription
from app.domain.money import Money
from app.providers.base import ProviderUnsupported
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


# -- dates (decision 46, conventions of 2026-11-11) ------------------------------

PARIS = ZoneInfo("Europe/Paris")


def _day(value: Any) -> date:
    if not isinstance(value, str):
        raise ProviderUnsupported("missing_field")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ProviderUnsupported("missing_field") from None


def paris_midnight(day: date) -> datetime:
    """A calendar date as the instant it starts in Europe/Paris, in UTC
    (convention, 2026-11-11: an invoice at the window's edge may fall one day
    in or out)."""
    return datetime(day.year, day.month, day.day, tzinfo=PARIS).astimezone(UTC)


def day_after(day: date) -> datetime:
    """The midnight after `day`, the day added to the date -- never 24 hours to
    the instant, so a change of summer time cannot move it."""
    return paris_midnight(day + timedelta(days=1))


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
            start = paris_midnight(_day(dates["start_date"]))
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
        issued_at=paris_midnight(issue_day(payload)),
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


def _add(day: date, unit: str, count: int) -> date:
    if unit == "week":
        return day + timedelta(weeks=count)
    months = count * (12 if unit == "year" else 1)
    year, month = divmod(day.month - 1 + months, 12)
    year += day.year
    # A day that does not exist in the target month cannot be one cycle on.
    try:
        return date(year, month + 1, day.day)
    except ValueError:
        raise ProviderUnsupported("missing_field") from None


def subscription(payload: Payload) -> Subscription:
    status = payload.get("status")
    if status == "in_progress":
        mapped, cancelled_at = SubscriptionStatus.ACTIVE, None
    elif status == "stopped":
        # `stopped_at` prevents the next renewal: the service runs until the
        # day before `next_occurrence`, included (convention, 2026-11-11).
        mapped, cancelled_at = SubscriptionStatus.CANCELLED, paris_midnight(
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
    if _add(previous, unit, count) != following:
        # The current period must be exactly one cycle (decision 46, Q1).
        raise ProviderUnsupported("missing_field")
    label = payload.get("label")
    identifier = typed("subscription", payload.get("id"))
    return Subscription(
        id=identifier,
        customer_id=_ref(payload.get("customer"), "customer") or _missing(),
        status=mapped,
        plan_code=label if isinstance(label, str) and label else identifier,
        current_period_start=paris_midnight(previous),
        current_period_end=paris_midnight(following),
        cancelled_at=cancelled_at,
        billing_interval=unit,
        billing_interval_count=count,
        billing_anchor=paris_midnight(_day(payload.get("start"))),
    )


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


def references(text: str, invoices: Sequence[Invoice]) -> list[tuple[int, int, str]]:
    """Where the numbers of `invoices` sit in `text`, and the invoice each
    stands for: whole numbers, case ignored, bounded by a character that is
    neither a letter nor a digit, the longest first, never overlapping; a
    number two invoices share designates neither."""
    by_number: dict[str, set[str]] = {}
    for item in invoices:
        if item.number:
            by_number.setdefault(item.number.casefold(), set()).add(item.id)
    numbers = sorted(
        (n for n, ids in by_number.items() if len(ids) == 1), key=len, reverse=True
    )
    if not numbers:
        return []
    pattern = re.compile(
        r"(?<![0-9A-Za-z])(" + "|".join(re.escape(n) for n in numbers) + r")(?![0-9A-Za-z])",
        re.IGNORECASE,
    )
    return [
        (match.start(), match.end(), next(iter(by_number[match.group(1).casefold()])))
        for match in pattern.finditer(text)
    ]
