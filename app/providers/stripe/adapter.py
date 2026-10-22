"""Stripe as a billing platform, read-only.

Implements :class:`BillingProvider` on a Stripe test account: reads through
`StripeReader`, maps through the pure functions of `mapping`. It cannot
write (`can_write = False`): the execution service never asks it to, and the
two writes and `find_action` raise `PlatformCannotWrite` without any call
(ADR-008). Each port call has a time budget; spent, the read is
`ProviderUnavailable`. specs/001-stripe-read-adapter.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from app.domain.models import Customer, Invoice, Payment, Subscription, Usage
from app.providers.base import (
    NotFound,
    PlatformCannotWrite,
    ProviderAction,
    ProviderUnavailable,
    ProviderUnsupported,
)
from app.providers.stripe import mapping
from app.providers.stripe.client import StripeReader

#: Seconds one port call may take, every page and retry included.
TIME_BUDGET = 30.0


class StripeBillingProvider:
    name = "stripe"
    can_write = False

    def __init__(self, reader: StripeReader, *, budget: float = TIME_BUDGET) -> None:
        self.reader = reader
        self._budget = budget

    # -- references (taxonomy S6, D10) ------------------------------------

    def invoice_references(self, text: str) -> set[str]:
        return {invoice_id for _, _, invoice_id in mapping.references(text)}

    def invoice_reference_spans(self, text: str) -> list[tuple[int, int, str]]:
        return mapping.references(text)

    # -- customers ---------------------------------------------------------

    async def get_customer(self, customer_id: str) -> Customer:
        async with self._bounded():
            return mapping.customer(await self.reader.customer(customer_id))

    async def find_customer_by_email(self, email: str) -> Customer | None:
        """A full scan, in a fixed order, on normalised addresses (FR-011):
        Stripe's exact, case-sensitive filter and its eventually consistent
        search never prove an absence."""
        sought = email.strip().lower()
        if not sought:
            return None
        async with self._bounded():
            for payload in await self._every_customer():
                if mapping.address(payload) == sought:
                    return mapping.customer(payload)
        return None

    async def list_customers(self) -> list[Customer]:
        async with self._bounded():
            return [mapping.customer(payload) for payload in await self._every_customer()]

    async def _every_customer(self) -> list[dict]:
        """Customers without a clock, then each test clock's, each once, by
        creation then identifier."""
        found = {payload["id"]: payload for payload in await self.reader.customers()}
        for clock in await self.reader.test_clocks():
            for payload in await self.reader.customers(test_clock=clock["id"]):
                found.setdefault(payload["id"], payload)
        return sorted(found.values(), key=mapping.customer_order)

    # -- subscriptions -----------------------------------------------------

    async def get_subscription(self, subscription_id: str) -> Subscription:
        async with self._bounded():
            return mapping.subscription(await self.reader.subscription(subscription_id))

    async def list_subscriptions(self, customer_id: str) -> list[Subscription]:
        async with self._bounded():
            return [mapping.subscription(p) for p in await self.reader.subscriptions(customer_id)]

    # -- invoices ----------------------------------------------------------

    async def get_invoice(self, invoice_id: str) -> Invoice:
        async with self._bounded():
            return await _Lookups(self.reader).invoice(await self.reader.invoice(invoice_id))

    async def find_invoice(self, invoice_id: str) -> Invoice | None:
        """None only when the invoice itself is absent: a price or a meter it
        names and Stripe cannot find is a defect of its data, not an absence."""
        async with self._bounded():
            try:
                payload = await self.reader.invoice(invoice_id)
            except NotFound:
                return None
            return await _Lookups(self.reader).invoice(payload)

    async def invoice_owner(self, invoice_id: str) -> str | None:
        """Read from the invoice alone, never mapped: whatever it holds, the
        answer is whose it is."""
        async with self._bounded():
            try:
                payload = await self.reader.invoice(invoice_id, every_line=False)
            except NotFound:
                return None
            customer = payload.get("customer")
            return mapping.identifier(customer) if customer else None

    async def list_invoices(self, customer_id: str, since: datetime) -> list[Invoice]:
        """The customer's invoices issued at or after `since`. Read in full,
        but only those kept are mapped: an older invoice is not refused."""
        async with self._bounded():
            lookups = _Lookups(self.reader)
            return [
                await lookups.invoice(payload)
                for payload in await self.reader.invoices(customer_id)
                if mapping.issued_at(payload) >= since
            ]

    # -- usage and payments --------------------------------------------------

    async def get_usage(
        self, subscription_id: str, period: tuple[datetime, datetime]
    ) -> Usage | None:
        """The meter's summary over `period`, for a subscription with one
        metered item whose meter no other item of the customer uses (FR-016)."""
        async with self._bounded():
            try:
                payload = await self.reader.subscription(subscription_id)
            except NotFound:
                return None
            customer_id = mapping.identifier(payload["customer"])
            meter_id = mapping.usage_meter(
                subscription_id, await self.reader.subscriptions(customer_id)
            )
            if meter_id is None:
                return None
            start, end = mapping.summary_window(period)
            meter = await _dependency(self.reader.meter, meter_id)
            summaries = await self.reader.meter_summaries(meter_id, customer_id, start, end)
            return mapping.usage(subscription_id, meter["event_name"], period, summaries)

    async def list_payments(self, customer_id: str) -> list[Payment]:
        async with self._bounded():
            every = [
                payment
                for invoice in await self.reader.invoices(customer_id)
                for payment in await self.reader.invoice_payments(invoice["id"])
            ]
            return mapping.payments(every)

    # -- the writes this slice does not make -------------------------------

    async def create_credit_note(
        self, invoice_id: str, amount_minor: int, currency: str, reason: str, idempotency_key: str
    ) -> ProviderAction:
        raise PlatformCannotWrite("stripe: this platform is read-only")

    async def create_refund(
        self, invoice_id: str, amount_minor: int, currency: str, reason: str, idempotency_key: str
    ) -> ProviderAction:
        raise PlatformCannotWrite("stripe: this platform is read-only")

    async def find_action(self, idempotency_key: str) -> ProviderAction | None:
        raise PlatformCannotWrite("stripe: this platform is read-only")

    # -- the time budget ---------------------------------------------------

    @asynccontextmanager
    async def _bounded(self) -> AsyncIterator[None]:
        try:
            async with asyncio.timeout(self._budget):
                yield
        except TimeoutError:
            raise ProviderUnavailable("stripe: the read's time budget was spent") from None


class _Lookups:
    """Prices and meters, read once per port call (plan section 4)."""

    def __init__(self, reader: StripeReader) -> None:
        self._reader = reader
        self._prices: dict[str, dict] = {}
        self._meters: dict[str, dict] = {}

    async def invoice(self, payload: dict) -> Invoice:
        for price_id in mapping.line_price_ids(payload) - self._prices.keys():
            self._prices[price_id] = await _dependency(self._reader.price, price_id)
        for meter_id in mapping.line_meter_ids(payload, self._prices) - self._meters.keys():
            self._meters[meter_id] = await _dependency(self._reader.meter, meter_id)
        return mapping.invoice(payload, self._prices, self._meters)


async def _dependency(read, identifier: str) -> dict:
    """An object another one names. Absent, it makes that one unreadable --
    `missing_field` -- and never makes it absent itself: a `NotFound` here
    would read as "no such invoice"."""
    try:
        return await read(identifier)
    except NotFound:
        raise ProviderUnsupported("missing_field") from None
