"""Stripe payloads to domain objects. Pure functions: no SDK, no network.

A payload is a plain dict as the API returns it at version 2026-07-29.dahlia,
its nested lists complete (the client reads them to the end). What the domain
cannot represent is refused with `ProviderUnsupported` and a reason from the
port's closed set -- never with the platform's data, and never by guessing a
value: a field the mapping needs and Stripe sends null is `missing_field`.
specs/001-stripe-read-adapter, FR-010 to FR-017 and plan section 4.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.domain.enums import InvoiceStatus, PaymentStatus, SubscriptionStatus
from app.domain.models import Customer, Invoice, InvoiceLine, Payment, Subscription, Usage
from app.domain.money import Money
from app.providers.base import ProviderUnsupported

Payload = Mapping[str, Any]

#: Subscription statuses the domain represents. `canceled` is handled apart:
#: it needs the date the subscription ended.
_SUBSCRIPTION_STATUSES = {
    "active": SubscriptionStatus.ACTIVE,
    "trialing": SubscriptionStatus.TRIALING,
    "past_due": SubscriptionStatus.PAST_DUE,
    "paused": SubscriptionStatus.PAUSED,
}

#: Payment intents whose collection is over without money: failed.
_ENDED_WITHOUT_MONEY = {"canceled"}

#: A Stripe invoice identifier in free text (taxonomy S6, D10).
_REFERENCE = re.compile(r"\bin_[A-Za-z0-9]{14,}\b")


def _required(value: Any) -> Any:
    if value is None:
        raise ProviderUnsupported("missing_field")
    return value


def identifier(value: str | Payload) -> str:
    """An identifier, whether Stripe sent the object or only its id."""
    return value if isinstance(value, str) else _required(value.get("id"))


def _at(timestamp: int | None) -> datetime:
    return datetime.fromtimestamp(_required(timestamp), UTC)


def _currency(value: str | None) -> str:
    return _required(value).upper()


def _decimal(value: Any) -> Decimal:
    """A Stripe number -- int, float, decimal string -- as a Decimal, exactly
    as written: 47.0 is 47, 2.5 stays 2.5."""
    number = Decimal(str(value))
    return number.to_integral_value() if number == number.to_integral_value() else number


# -- customers ---------------------------------------------------------------


def customer(payload: Payload) -> Customer:
    """The address as written: the lookup compares it normalised (`address`)."""
    currency = payload.get("currency")
    return Customer(
        id=payload["id"],
        email=_required(payload.get("email")),
        currency=currency.upper() if currency else None,
        display_name=payload.get("name"),
    )


def address(payload: Payload) -> str | None:
    """The address the lookup compares: trimmed, lower-cased; None matches nothing."""
    email = (payload.get("email") or "").strip().lower()
    return email or None


def customer_order(payload: Payload) -> tuple[int, str]:
    """The lookup's fixed order: creation, then identifier (FR-011)."""
    return payload["created"], payload["id"]


# -- subscriptions -----------------------------------------------------------


def subscription(payload: Payload) -> Subscription:
    """One recurring price, or several items sharing one price and one period;
    the period comes from the items (FR-012)."""
    items = payload["items"]["data"]
    if not items:
        raise ProviderUnsupported("missing_field")
    periods = {(item.get("current_period_start"), item.get("current_period_end")) for item in items}
    if len(periods) > 1:
        raise ProviderUnsupported("mixed_item_periods")
    if len({identifier(_required(item.get("price"))) for item in items}) > 1:
        raise ProviderUnsupported("multiple_prices")
    start, end = (_at(moment) for moment in periods.pop())
    if start >= end:
        raise ProviderUnsupported("unrepresentable_period")

    cancelled_at = None
    if payload["status"] == "canceled":
        # The date it ended, never the date the cancellation was asked for.
        if payload.get("ended_at") is None:
            raise ProviderUnsupported("cancellation_without_end")
        status = SubscriptionStatus.CANCELLED
        cancelled_at = _at(payload["ended_at"])
    elif payload["status"] in _SUBSCRIPTION_STATUSES:
        # A cancellation scheduled for later changes nothing yet.
        status = _SUBSCRIPTION_STATUSES[payload["status"]]
    else:
        raise ProviderUnsupported("subscription_status")

    price = items[0]["price"]
    return Subscription(
        id=payload["id"],
        customer_id=identifier(payload["customer"]),
        status=status,
        plan_code=price.get("lookup_key") or price["id"],
        current_period_start=start,
        current_period_end=end,
        cancelled_at=cancelled_at,
    )


# -- invoices ----------------------------------------------------------------


def issued_at(payload: Payload) -> datetime:
    """When the invoice was finalised; a draft, when it was created (FR-013)."""
    if payload["status"] == "draft":
        return _at(payload.get("created"))
    return _at((payload.get("status_transitions") or {}).get("finalized_at"))


def invoice(
    payload: Payload, prices: Mapping[str, Payload], meters: Mapping[str, Payload]
) -> Invoice:
    """`prices` and `meters` hold, by id, those the lines name without sending
    them (`line_price_ids`, `line_meter_ids`)."""
    try:
        status = InvoiceStatus(payload["status"])
    except ValueError:
        # A status this version does not know is a field the mapping cannot read.
        raise ProviderUnsupported("missing_field") from None
    currency = _currency(payload.get("currency"))
    if any(tax["amount"] for tax in payload.get("total_taxes") or ()):
        raise ProviderUnsupported("invoice_tax")
    if any(discount["amount"] for discount in payload.get("total_discount_amounts") or ()):
        raise ProviderUnsupported("invoice_discount")

    parent = payload.get("parent") or {}
    details = parent.get("subscription_details") or {}
    subscription_id = details.get("subscription")
    return Invoice(
        id=payload["id"],
        customer_id=identifier(_required(payload.get("customer"))),
        subscription_id=identifier(subscription_id) if subscription_id else None,
        total=Money(amount_minor=_required(payload.get("total")), currency=currency),
        status=status,
        issued_at=issued_at(payload),
        lines=[_line(line, currency, prices, meters) for line in payload["lines"]["data"]],
    )


def _line(
    payload: Payload,
    currency: str,
    prices: Mapping[str, Payload],
    meters: Mapping[str, Payload],
) -> InvoiceLine:
    amount = _required(payload.get("amount"))
    if amount < 0:
        raise ProviderUnsupported("negative_line")
    if any(tax["amount"] for tax in payload.get("taxes") or ()):
        raise ProviderUnsupported("invoice_tax")
    if any(discount["amount"] for discount in payload.get("discount_amounts") or ()):
        raise ProviderUnsupported("invoice_discount")

    pricing = _required(payload.get("pricing"))
    unit = Decimal(str(_required(pricing.get("unit_amount_decimal"))))
    if unit != unit.to_integral_value():
        raise ProviderUnsupported("fractional_unit_price")
    quantity = payload.get("quantity_decimal")
    if quantity is None:
        quantity = _required(payload.get("quantity"))
    period = _required(payload.get("period"))

    return InvoiceLine(
        # Informative only, read by the engine and never by a rule.
        description=payload.get("description") or "",
        quantity=_decimal(quantity),
        unit_amount=Money(amount_minor=int(unit), currency=currency),
        amount=Money(amount_minor=amount, currency=currency),
        period_start=_at(period.get("start")),
        period_end=_at(period.get("end")),
        metric=_metric(_line_price(payload, prices), meters),
    )


def _line_price(payload: Payload, prices: Mapping[str, Payload]) -> Payload | None:
    details = (payload.get("pricing") or {}).get("price_details") or {}
    price = details.get("price")
    if price is None:
        return None
    return prices[price] if isinstance(price, str) else price


def _meter_id(price: Payload | None) -> str | None:
    """The meter of a metered price; None for a licensed one."""
    recurring = (price or {}).get("recurring") or {}
    if recurring.get("usage_type") != "metered":
        return None
    return _required(recurring.get("meter"))


def _metric(price: Payload | None, meters: Mapping[str, Payload]) -> str | None:
    """The meter's event name: the same on the line and in the usage."""
    meter_id = _meter_id(price)
    return None if meter_id is None else meters[meter_id]["event_name"]


def line_price_ids(payload: Payload) -> set[str]:
    """Prices the invoice's lines name by id only: the adapter reads them."""
    ids = set()
    for line in payload["lines"]["data"]:
        price = ((line.get("pricing") or {}).get("price_details") or {}).get("price")
        if isinstance(price, str):
            ids.add(price)
    return ids


def line_meter_ids(payload: Payload, prices: Mapping[str, Payload]) -> set[str]:
    """Meters behind the invoice's metered lines: the adapter reads them."""
    ids = (_meter_id(_line_price(line, prices)) for line in payload["lines"]["data"])
    return {meter_id for meter_id in ids if meter_id is not None}


# -- payments ----------------------------------------------------------------


def payments(invoice_payments: Iterable[Payload]) -> list[Payment]:
    """Every invoice payment of a customer, each attached to one invoice.

    A payment intent found on invoice payments of more than one invoice is a
    payment allocated across invoices: refused (FR-015).
    """
    every = list(invoice_payments)
    invoices_by_intent: dict[str, set[str]] = defaultdict(set)
    for payload in every:
        intent = (payload.get("payment") or {}).get("payment_intent")
        if intent is not None:
            invoices_by_intent[identifier(intent)].add(identifier(payload["invoice"]))
    if any(len(invoices) > 1 for invoices in invoices_by_intent.values()):
        raise ProviderUnsupported("multi_invoice_payment")
    return [_payment(payload) for payload in every]


def _payment(payload: Payload) -> Payment:
    """Succeeded only when the collection succeeded, with what was collected;
    failed or pending with what was asked, never read as money received."""
    payment = payload.get("payment") or {}
    intent = payment.get("payment_intent")
    if payment.get("type") != "payment_intent" or intent is None:
        raise ProviderUnsupported("unplaced_payment")
    if isinstance(intent, str):
        raise ProviderUnsupported("missing_field")  # the expansion was not applied
    charge = intent.get("latest_charge")
    if isinstance(charge, str):
        raise ProviderUnsupported("missing_field")
    currency = _currency(payload.get("currency"))

    received_at = None
    if intent["status"] == "succeeded":
        charge = _required(charge)
        if charge.get("refunded"):
            status = PaymentStatus.REFUNDED
        elif charge.get("amount_refunded"):
            raise ProviderUnsupported("partial_refund")
        else:
            status = PaymentStatus.SUCCEEDED
        amount = _required(payload.get("amount_paid"))
        paid_at = (payload.get("status_transitions") or {}).get("paid_at")
        received_at = _at(paid_at) if paid_at is not None else None
    elif intent["status"] in _ENDED_WITHOUT_MONEY or (charge or {}).get("status") == "failed":
        status = PaymentStatus.FAILED
        amount = _required(payload.get("amount_requested"))
    else:
        # Not attempted yet, or in progress: nothing received so far.
        status = PaymentStatus.PENDING
        amount = _required(payload.get("amount_requested"))

    return Payment(
        id=payload["id"],
        invoice_id=identifier(payload["invoice"]),
        amount=Money(amount_minor=amount, currency=currency),
        status=status,
        received_at=received_at,
    )


# -- usage -------------------------------------------------------------------


def usage_meter(subscription_id: str, subscriptions: Iterable[Payload]) -> str | None:
    """The meter of the subscription's one metered item, when no other item
    of the customer's subscriptions uses it; otherwise None (FR-016)."""
    metered = [
        (payload["id"], meter_id)
        for payload in subscriptions
        for item in payload["items"]["data"]
        if (meter_id := _meter_id(item.get("price"))) is not None
    ]
    own = [meter_id for owner, meter_id in metered if owner == subscription_id]
    if len(own) != 1:
        return None
    shared = sum(meter_id == own[0] for _, meter_id in metered) > 1
    return None if shared else own[0]


def summary_window(period: tuple[datetime, datetime]) -> tuple[int, int]:
    """The period as meter-summary bounds, start inclusive, end exclusive.

    Stripe sums meter events by whole minutes: a period that does not fall on
    them cannot be read exactly, and is refused rather than rounded.
    """
    bounds = []
    for moment in period:
        seconds = moment.timestamp()
        if seconds % 60:
            raise ProviderUnsupported("unrepresentable_period")
        bounds.append(int(seconds))
    return bounds[0], bounds[1]


def usage(
    subscription_id: str,
    metric: str,
    period: tuple[datetime, datetime],
    summaries: Iterable[Payload],
) -> Usage | None:
    """The summaries over the period, added up. None when there are none --
    unavailable, never zero; a summary that says zero is zero."""
    found = list(summaries)
    if not found:
        return None
    return Usage(
        subscription_id=subscription_id,
        metric=metric,
        quantity=_decimal(sum(Decimal(str(s["aggregated_value"])) for s in found)),
        period_start=period[0],
        period_end=period[1],
    )


# -- references --------------------------------------------------------------


def references(text: str) -> list[tuple[int, int, str]]:
    """Stripe invoice identifiers in `text`, where each sits (FR-017)."""
    return [(match.start(), match.end(), match.group()) for match in _REFERENCE.finditer(text)]
