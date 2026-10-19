"""Load JSON fixtures and map them onto domain objects.

This is the mapping layer an adapter needs, kept separate from the adapter
itself -- the same split a real ``StripeMapper`` would use.

The fixture format deliberately does *not* match the domain: amounts are
decimal strings in major units, statuses are called ``state``, invoice totals
are called ``amount``, invoice lines are called ``label``. That is what makes
this a real mapping rather than a rename, and it exercises the single rounding
function of ADR-005 on every load.

Known simplification: a unit price finer than the currency's minor unit
(0.005 EUR per API call) cannot be represented here. Real platforms carry a
separate high-precision unit price for that; the fixtures use block pricing
instead of pretending the problem does not exist.

A second directory can add entries: ``RESOLVE_EXTRA_FIXTURES_DIR``. It is how
cases kept outside this repository bring their own data without that data
ever entering ``fixtures/``. Its entries are appended, never substituted: an
``id`` already present is an error, because a silent override would change
what every other case sees.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.domain.enums import InvoiceStatus, PaymentStatus, SubscriptionStatus
from app.domain.models import (
    Customer,
    Invoice,
    InvoiceLine,
    Payment,
    Subscription,
    Usage,
)
from app.domain.money import Money

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures"

#: Names a directory whose fixture files add to those of ``FIXTURES_DIR``.
EXTRA_FIXTURES_ENV = "RESOLVE_EXTRA_FIXTURES_DIR"

#: Files whose entries carry an ``id``. ``usage.json`` has none: its entries
#: are only appended.
_KEYED = frozenset({"customers.json", "subscriptions.json", "invoices.json", "payments.json"})


class FixtureError(ValueError):
    """The fixtures cannot be loaded as one consistent platform."""


def extra_fixtures_dir() -> Path | None:
    """The extra directory, or None when the variable is unset or empty.

    A directory that does not exist is an error, not an empty directory: a
    typo in the path would otherwise run every case without its data.
    """
    raw = os.environ.get(EXTRA_FIXTURES_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_dir():
        raise FixtureError(f"{EXTRA_FIXTURES_ENV} names {path}, which is not a directory")
    return path


def _read(name: str, extra: Path | None = None) -> list[dict[str, Any]]:
    entries = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    if extra is None or not (extra / name).is_file():
        return entries
    added = json.loads((extra / name).read_text(encoding="utf-8"))
    if name in _KEYED:
        seen = {entry["id"] for entry in entries}
        for entry in added:
            if entry["id"] in seen:
                raise FixtureError(f"{extra / name}: id {entry['id']!r} is already defined")
            seen.add(entry["id"])
    return entries + added


def _at(raw: str | None) -> datetime | None:
    """Parse a timestamp and pin it to UTC.

    Fixtures carry no offset. The domain refuses naive datetimes, for the same
    reason it refuses floats: "2026-09-01T00:00:00" is a different instant in
    Paris and in UTC, and a billing period that slides by two hours silently
    changes which month an invoice belongs to. Normalising here keeps the
    guess at the boundary, where every other platform quirk is handled.
    """
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _money(raw: dict[str, Any], key: str, currency: str) -> Money:
    """Every amount in the fixtures crosses into the domain through here."""
    return Money.from_decimal(Decimal(raw[key]), currency)


def _customer(raw: dict[str, Any]) -> Customer:
    return Customer(
        id=raw["id"],
        email=raw["email"],
        currency=raw.get("currency"),
        display_name=raw.get("display_name"),
    )


def _subscription(raw: dict[str, Any]) -> Subscription:
    return Subscription(
        id=raw["id"],
        customer_id=raw["customer_id"],
        status=SubscriptionStatus(raw["state"]),
        plan_code=raw["plan"],
        current_period_start=_at(raw["period_start"]),
        current_period_end=_at(raw["period_end"]),
        cancelled_at=_at(raw.get("cancelled_at")),
    )


def _line(raw: dict[str, Any], currency: str) -> InvoiceLine:
    return InvoiceLine(
        description=raw["label"],
        quantity=Decimal(raw["quantity"]),
        unit_amount=_money(raw, "unit_amount", currency),
        amount=_money(raw, "amount", currency),
        period_start=_at(raw.get("period_start")),
        period_end=_at(raw.get("period_end")),
        metric=raw.get("metric"),
    )


def _invoice(raw: dict[str, Any]) -> Invoice:
    currency = raw["currency"]
    return Invoice(
        id=raw["id"],
        customer_id=raw["customer_id"],
        subscription_id=raw.get("subscription_id"),
        total=_money(raw, "amount", currency),
        status=InvoiceStatus(raw["state"]),
        issued_at=_at(raw["issued_at"]),
        lines=[_line(line, currency) for line in raw.get("lines", [])],
    )


def _payment(raw: dict[str, Any]) -> Payment:
    return Payment(
        id=raw["id"],
        invoice_id=raw.get("invoice_id"),
        amount=_money(raw, "amount", raw["currency"]),
        status=PaymentStatus(raw["state"]),
        received_at=_at(raw.get("received_at")),
    )


def _usage(raw: dict[str, Any]) -> Usage:
    return Usage(
        subscription_id=raw["subscription_id"],
        metric=raw["metric"],
        quantity=Decimal(raw["quantity"]),
        period_start=_at(raw["period_start"]),
        period_end=_at(raw["period_end"]),
    )


@lru_cache(maxsize=1)
def load_all() -> FixtureSet:
    """Parse every fixture file once, mapped into domain objects.

    Cached, so ``RESOLVE_EXTRA_FIXTURES_DIR`` is read on the first load; a
    test that changes it clears the cache.
    """
    extra = extra_fixtures_dir()
    return FixtureSet(
        customers=[_customer(raw) for raw in _read("customers.json", extra)],
        subscriptions=[_subscription(raw) for raw in _read("subscriptions.json", extra)],
        invoices=[_invoice(raw) for raw in _read("invoices.json", extra)],
        payments=[_payment(raw) for raw in _read("payments.json", extra)],
        usage=[_usage(raw) for raw in _read("usage.json", extra)],
    )


class FixtureSet:
    """The whole fake platform, in memory."""

    def __init__(
        self,
        customers: list[Customer],
        subscriptions: list[Subscription],
        invoices: list[Invoice],
        payments: list[Payment],
        usage: list[Usage],
    ) -> None:
        self.customers = customers
        self.subscriptions = subscriptions
        self.invoices = invoices
        self.payments = payments
        self.usage = usage
