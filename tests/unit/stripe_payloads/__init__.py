"""Hand-written Stripe objects, API version 2026-07-29.dahlia.

Written to that version's reference and to the types of the SDK pinned for it
(stripe 15.4.0); the contract run on a real account is what confirms them
(plan, Testing). Each builder returns a fresh dict, as the API sends it:
keyword arguments replace top-level fields, and nested objects are built by
their own builders. Fields the mapping ignores are kept where Stripe sends
them, so a test cannot pass on a payload thinner than the real one.
"""

from __future__ import annotations

from typing import Any

AUG_1 = 1785542400  # 2026-08-01T00:00:00Z
AUG_31 = 1788134400
SEP_1 = 1788220800
SEP_2 = 1788307200
SEP_16 = 1789516800
OCT_1 = 1790812800

CUSTOMER = "cus_RbWorld0000001"
SUBSCRIPTION = "sub_RbWorld0000001"
INVOICE = "in_1RbWorld00000001"
PRICE = "price_RbPro0000001"
METERED_PRICE = "price_RbBlocks00001"
METER = "mtr_RbBlocks000001"
CLOCK = "clock_RbWorld000001"


def _with(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    return {**base, **changes}


def page(data: list[dict], has_more: bool = False, url: str = "/v1/objects") -> dict:
    return {"object": "list", "data": data, "has_more": has_more, "url": url}


def customer(**changes) -> dict:
    return _with(
        {
            "id": CUSTOMER,
            "object": "customer",
            "address": None,
            "balance": 0,
            "created": AUG_1,
            "currency": "eur",
            "default_source": None,
            "delinquent": False,
            "description": None,
            "email": "  Billing@Duplicate.Example.test ",
            "invoice_prefix": "RBW0001",
            "livemode": False,
            "metadata": {},
            "name": "ACME SAS",
            "phone": None,
            "preferred_locales": [],
            "test_clock": CLOCK,
        },
        changes,
    )


def clock(**changes) -> dict:
    return _with(
        {
            "id": CLOCK,
            "object": "test_helpers.test_clock",
            "created": AUG_1,
            "deletes_after": OCT_1 + 30 * 86400,
            "frozen_time": SEP_2,
            "livemode": False,
            "name": "rb duplicate",
            "status": "ready",
            "status_details": {},
        },
        changes,
    )


def price(**changes) -> dict:
    return _with(
        {
            "id": PRICE,
            "object": "price",
            "active": True,
            "billing_scheme": "per_unit",
            "created": AUG_1,
            "currency": "eur",
            "livemode": False,
            "lookup_key": "pro_monthly",
            "metadata": {},
            "nickname": None,
            "product": "prod_RbPro0000001",
            "recurring": {
                "interval": "month",
                "interval_count": 1,
                "meter": None,
                "trial_period_days": None,
                "usage_type": "licensed",
            },
            "tax_behavior": "unspecified",
            "tiers_mode": None,
            "transform_quantity": None,
            "type": "recurring",
            "unit_amount": 4900,
            "unit_amount_decimal": "4900",
        },
        changes,
    )


def metered_price(**changes) -> dict:
    return price(
        **_with(
            {
                "id": METERED_PRICE,
                "lookup_key": None,
                "product": "prod_RbBlocks00001",
                "recurring": {
                    "interval": "month",
                    "interval_count": 1,
                    "meter": METER,
                    "trial_period_days": None,
                    "usage_type": "metered",
                },
                "unit_amount": 100,
                "unit_amount_decimal": "100",
            },
            changes,
        )
    )


def meter(**changes) -> dict:
    return _with(
        {
            "id": METER,
            "object": "billing.meter",
            "created": AUG_1,
            "customer_mapping": {"event_payload_key": "stripe_customer_id", "type": "by_id"},
            "default_aggregation": {"formula": "sum"},
            "display_name": "API call blocks",
            "event_name": "api_call_blocks_100",
            "event_time_window": None,
            "livemode": False,
            "status": "active",
            "status_transitions": {"deactivated_at": None},
            "updated": AUG_1,
            "value_settings": {"event_payload_key": "value"},
        },
        changes,
    )


def summary(aggregated_value: float = 47.0, **changes) -> dict:
    return _with(
        {
            "id": "mtrsum_RbBlocks000001",
            "object": "billing.meter_event_summary",
            "aggregated_value": aggregated_value,
            "end_time": OCT_1,
            "livemode": False,
            "meter": METER,
            "start_time": SEP_1,
        },
        changes,
    )


def item(item_price: dict | None = None, start: int = SEP_1, end: int = OCT_1, **changes) -> dict:
    return _with(
        {
            "id": "si_RbWorld0000001",
            "object": "subscription_item",
            "created": SEP_1,
            "current_period_end": end,
            "current_period_start": start,
            "discounts": [],
            "metadata": {},
            "price": item_price if item_price is not None else price(),
            "quantity": 1,
            "subscription": SUBSCRIPTION,
            "tax_rates": [],
        },
        changes,
    )


def subscription(items: list[dict] | None = None, **changes) -> dict:
    data = items if items is not None else [item()]
    return _with(
        {
            "id": SUBSCRIPTION,
            "object": "subscription",
            "billing_cycle_anchor": SEP_1,
            "cancel_at": None,
            "cancel_at_period_end": False,
            "canceled_at": None,
            "cancellation_details": {"comment": None, "feedback": None, "reason": None},
            "collection_method": "charge_automatically",
            "created": SEP_1,
            "currency": "eur",
            "customer": CUSTOMER,
            "discounts": [],
            "ended_at": None,
            "items": page(data, url=f"/v1/subscription_items?subscription={SUBSCRIPTION}"),
            "latest_invoice": INVOICE,
            "livemode": False,
            "metadata": {},
            "start_date": SEP_1,
            "status": "active",
            "test_clock": CLOCK,
            "trial_end": None,
            "trial_start": None,
        },
        changes,
    )


def line(
    amount: int = 4900,
    unit_amount_decimal: str | None = "4900",
    line_price: str | dict | None = PRICE,
    start: int = SEP_1,
    end: int = OCT_1,
    **changes,
) -> dict:
    return _with(
        {
            "id": "il_RbWorld0000001",
            "object": "line_item",
            "amount": amount,
            "currency": "eur",
            "description": "1 x Pro (at EUR 49.00 / month)",
            "discount_amounts": [],
            "discountable": True,
            "discounts": [],
            "invoice": INVOICE,
            "livemode": False,
            "metadata": {},
            "parent": {
                "invoice_item_details": None,
                "subscription_item_details": {
                    "invoice_item": None,
                    "proration": False,
                    "proration_details": {"credited_items": None},
                    "subscription": SUBSCRIPTION,
                    "subscription_item": "si_RbWorld0000001",
                },
                "type": "subscription_item_details",
            },
            "period": {"end": end, "start": start},
            "pretax_credit_amounts": [],
            "pricing": {
                "price_details": {"price": line_price, "product": "prod_RbPro0000001"},
                "type": "price_details",
                "unit_amount_decimal": unit_amount_decimal,
            },
            "quantity": 1,
            "quantity_decimal": "1",
            "subscription": SUBSCRIPTION,
            "subtotal": amount,
            "taxes": [],
        },
        changes,
    )


def invoice(lines: list[dict] | None = None, **changes) -> dict:
    data = lines if lines is not None else [line()]
    total = sum(item["amount"] for item in data)
    return _with(
        {
            "id": INVOICE,
            "object": "invoice",
            "amount_due": total,
            "amount_paid": total,
            "amount_remaining": 0,
            "attempt_count": 1,
            "billing_reason": "subscription_cycle",
            "collection_method": "charge_automatically",
            "created": SEP_1,
            "currency": "eur",
            "customer": CUSTOMER,
            "customer_email": "billing@duplicate.example.test",
            "discounts": [],
            "livemode": False,
            "lines": page(data, url=f"/v1/invoices/{INVOICE}/lines"),
            "metadata": {},
            "number": "RBW0001-0001",
            "parent": {
                "quote_details": None,
                "subscription_details": {"metadata": {}, "subscription": SUBSCRIPTION},
                "type": "subscription_details",
            },
            "period_end": SEP_1,
            "period_start": SEP_1,
            "status": "paid",
            "status_transitions": {
                "finalized_at": SEP_1 + 3600,
                "marked_uncollectible_at": None,
                "paid_at": SEP_1 + 7200,
                "voided_at": None,
            },
            "subtotal": total,
            "subtotal_excluding_tax": total,
            "test_clock": CLOCK,
            "total": total,
            "total_discount_amounts": [],
            "total_excluding_tax": total,
            "total_pretax_credit_amounts": [],
            "total_taxes": [],
        },
        changes,
    )


def tax(amount: int = 980, behavior: str = "exclusive") -> dict:
    return {
        "amount": amount,
        "tax_behavior": behavior,
        "tax_rate_details": {"tax_rate": "txr_RbVat20000001"},
        "taxability_reason": "standard_rated",
        "taxable_amount": 4900,
        "type": "tax_rate_details",
    }


def discount_amount(amount: int = 490) -> dict:
    return {"amount": amount, "discount": "di_RbTenPercent001"}


def pretax_credit(amount: int = 490, kind: str = "discount") -> dict:
    return {
        "amount": amount,
        "credit_balance_transaction": "cbtxn_Rb0000001" if kind != "discount" else None,
        "discount": "di_RbTenPercent001" if kind == "discount" else None,
        "type": kind,
    }


def charge(**changes) -> dict:
    return _with(
        {
            "id": "ch_RbWorld0000001",
            "object": "charge",
            "amount": 4900,
            "amount_captured": 4900,
            "amount_refunded": 0,
            "captured": True,
            "created": SEP_1 + 7200,
            "currency": "eur",
            "failure_code": None,
            "livemode": False,
            "paid": True,
            "refunded": False,
            "status": "succeeded",
        },
        changes,
    )


#: Stands for "a succeeded charge" where None means "no charge yet".
_SUCCEEDED_CHARGE = object()


def payment_intent(
    latest_charge: dict | str | None | object = _SUCCEEDED_CHARGE, **changes
) -> dict:
    return _with(
        {
            "id": "pi_RbWorld0000001",
            "object": "payment_intent",
            "amount": 4900,
            "amount_received": 4900,
            "created": SEP_1 + 3600,
            "currency": "eur",
            "customer": CUSTOMER,
            "latest_charge": charge() if latest_charge is _SUCCEEDED_CHARGE else latest_charge,
            "livemode": False,
            "status": "succeeded",
        },
        changes,
    )


def invoice_payment(intent: dict | str | None = None, **changes) -> dict:
    return _with(
        {
            "id": "inpay_RbWorld0000001",
            "object": "invoice_payment",
            "amount_paid": 4900,
            "amount_requested": 4900,
            "created": SEP_1 + 3600,
            "currency": "eur",
            "invoice": INVOICE,
            "is_default": True,
            "livemode": False,
            "payment": {
                "payment_intent": intent if intent is not None else payment_intent(),
                "type": "payment_intent",
            },
            "status": "paid",
            "status_transitions": {"canceled_at": None, "paid_at": SEP_1 + 7200},
        },
        changes,
    )
