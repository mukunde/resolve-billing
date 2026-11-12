"""Pennylane behind the billing port, read-only (specs/014-pennylane-read).

Every read starts with the company check (the declared sandbox only, Q2) and
runs within one time budget; every list is read to its end; refusals come from
the mapper with their reason. Nothing is written: `can_write` is false, and the
writes refuse before any call.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from app.domain.enums import SubscriptionStatus
from app.domain.models import Customer, Invoice, Payment, Subscription, Usage
from app.providers.base import (
    NotFound,
    PlatformCannotWrite,
    ProviderAction,
    ProviderUnavailable,
    ProviderUnsupported,
)
from app.providers.pennylane import mapping
from app.providers.pennylane.client import DEFAULT_BUDGET, PennylaneReader

Payload = dict[str, Any]


def _filter(field: str, value: Any) -> str:
    return json.dumps([{"field": field, "operator": "eq", "value": value}])


class PennylaneBillingProvider:
    name = "pennylane"
    can_write = False
    writes: frozenset[str] = frozenset()
    #: Nothing is written, so no key is ever held.
    idempotency_window = timedelta(0)

    def __init__(self, reader: PennylaneReader, budget: float = DEFAULT_BUDGET) -> None:
        self.reader = reader
        self._budget = budget

    # -- references (FR-010) -------------------------------------------------

    def invoice_references(self, text: str, invoices: Sequence[Invoice] = ()) -> set[str]:
        return {invoice_id for _, _, invoice_id in mapping.references(text, invoices)}

    def invoice_reference_spans(
        self, text: str, invoices: Sequence[Invoice] = ()
    ) -> list[tuple[int, int, str]]:
        return mapping.references(text, invoices)

    def resolve_invoice_reference(self, reference: str, invoices: Sequence[Invoice]) -> str:
        """The disputed invoice (Q3): a typed id as it is; else the one loaded
        invoice whose whole number it is; else the reference as given."""
        if mapping.raw_id(reference, "invoice") is not None:
            return reference
        return mapping.resolve_number(reference, invoices) or reference

    def is_invoice_id(self, reference: str) -> bool:
        return mapping.raw_id(reference, "invoice") is not None

    # -- customers -------------------------------------------------------------

    async def get_customer(self, customer_id: str) -> Customer:
        async with self._read():
            raw = mapping.raw_id(customer_id, "customer")
            if raw is None:
                raise NotFound(f"unknown customer {customer_id!r}")
            return mapping.customer(await self.reader.get(f"/customers/{raw}"))

    async def find_customer_by_email(self, email: str) -> Customer | None:
        """The first customer, in id order, any of whose addresses is the one
        given, compared trimmed and case-folded; mapped, so a customer with two
        addresses is refused (`multiple_addresses`)."""
        wanted = email.strip().lower()
        async with self._read():
            for payload in await self._customers():
                if wanted in {address.lower() for address in mapping.emails(payload)}:
                    return mapping.customer(payload)
        return None

    async def list_customers(self) -> list[Customer]:
        async with self._read():
            return [mapping.customer(payload) for payload in await self._customers()]

    async def _customers(self) -> list[Payload]:
        found = await self.reader.every("/customers", {"sort": "id"})
        return sorted(found, key=lambda payload: _int(payload.get("id")))

    # -- subscriptions ---------------------------------------------------------------

    async def get_subscription(self, subscription_id: str) -> Subscription:
        async with self._read():
            raw = mapping.raw_id(subscription_id, "subscription")
            if raw is None:
                raise NotFound(f"unknown subscription {subscription_id!r}")
            return mapping.subscription(await self.reader.get(f"/billing_subscriptions/{raw}"))

    async def list_subscriptions(self, customer_id: str) -> list[Subscription]:
        async with self._read():
            return await self._subscriptions(customer_id)

    async def _subscriptions(self, customer_id: str) -> list[Subscription]:
        raw = mapping.raw_id(customer_id, "customer")
        if raw is None:
            return []
        found = await self.reader.every(
            "/billing_subscriptions", {"filter": _filter("customer_id", raw)}
        )
        return [mapping.subscription(payload) for payload in found]

    # -- invoices --------------------------------------------------------------------

    async def get_invoice(self, invoice_id: str) -> Invoice:
        found = await self.find_invoice(invoice_id)
        if found is None:
            raise NotFound(f"unknown invoice {invoice_id!r}")
        return found

    async def find_invoice(self, invoice_id: str) -> Invoice | None:
        async with self._read():
            raw = mapping.raw_id(invoice_id, "invoice")
            if raw is None:
                return None
            try:
                payload = await self.reader.get(f"/customer_invoices/{raw}")
            except NotFound:
                return None
            customer_raw = mapping.raw_id(mapping.owner(payload), "customer")
            documents = await self._documents(customer_raw)
            subscriptions = await self._subscriptions_of(payload, customer_raw)
            return await self._invoice(payload, mapping.credited_ids(
                d for d in documents if mapping.is_credit_note(d)), subscriptions)

    async def invoice_owner(self, invoice_id: str) -> str | None:
        """Whose an invoice is: the invoice alone is read, nothing else."""
        async with self._read():
            raw = mapping.raw_id(invoice_id, "invoice")
            if raw is None:
                return None
            try:
                payload = await self.reader.get(f"/customer_invoices/{raw}")
            except NotFound:
                return None
            return mapping.owner(payload)

    async def list_invoices(self, customer_id: str, since: datetime) -> list[Invoice]:
        """The customer's invoices issued at or after `since`; a refused one in
        that window refuses the call, an older one is not mapped. The customer's
        credit notes are all read, whatever their date."""
        async with self._read():
            raw = mapping.raw_id(customer_id, "customer")
            if raw is None:
                return []
            documents = await self._documents(raw)
            credited = mapping.credited_ids(d for d in documents if mapping.is_credit_note(d))
            recent = [
                d for d in documents
                if not mapping.is_credit_note(d)
                and d.get("status") not in mapping.NOT_INVOICES
                and mapping.midnight(mapping.issue_day(d)) >= since
            ]
            subscriptions = (
                {s.id: s for s in await self._subscriptions(customer_id)}
                if any(d.get("billing_subscription") for d in recent)
                else {}
            )
            return [await self._invoice(d, credited, subscriptions) for d in recent]

    async def _documents(self, customer_raw: int | None) -> list[Payload]:
        """Every invoice and credit note of the customer, imported ones
        included, to the end."""
        if customer_raw is None:
            return []
        return await self.reader.every(
            "/customer_invoices",
            {
                "filter": _filter("customer_id", customer_raw),
                "include_invoices_created_from_ledger_entries": "true",
            },
        )

    async def _subscriptions_of(
        self, payload: Payload, customer_raw: int | None
    ) -> dict[str, Subscription]:
        if not payload.get("billing_subscription") or customer_raw is None:
            return {}
        return {s.id: s for s in await self._subscriptions(mapping.typed("customer", customer_raw))}

    async def _invoice(
        self, payload: Payload, credited: set[str], subscriptions: dict[str, Subscription]
    ) -> Invoice:
        identifier = mapping.typed("invoice", payload.get("id"))
        if mapping.is_credit_note(payload) or payload.get("status") in mapping.NOT_INVOICES:
            raise ProviderUnsupported("invoice_status")
        if identifier in credited:
            raise ProviderUnsupported("invoice_credit")
        lines = await self.reader.every(
            f"/customer_invoices/{mapping.raw_id(identifier, 'invoice')}/invoice_lines"
        )
        invoice = mapping.invoice(payload, lines)
        subscription = subscriptions.get(invoice.subscription_id or "")
        if (
            subscription is not None
            and subscription.status is SubscriptionStatus.CANCELLED
            and not mapping.fully_dated(invoice)
        ):
            # No check decides on partial coverage, nor on the issue instant.
            raise ProviderUnsupported("missing_field")
        return invoice

    # -- payments and usage ----------------------------------------------------------

    async def list_payments(self, customer_id: str) -> list[Payment]:
        """The payments of every invoice of the customer, every list to its end;
        a payment seen under two invoices is refused."""
        async with self._read():
            raw = mapping.raw_id(customer_id, "customer")
            if raw is None:
                return []
            payments: list[Payment] = []
            seen: dict[str, str] = {}
            for document in await self._documents(raw):
                if mapping.is_credit_note(document):
                    continue
                if document.get("status") in mapping.NOT_INVOICES:
                    continue
                invoice_id = mapping.typed("invoice", document.get("id"))
                found = await self.reader.every(
                    f"/customer_invoices/{mapping.raw_id(invoice_id, 'invoice')}/payments"
                )
                for item in found:
                    paid = mapping.payment(item, invoice_id)
                    if seen.setdefault(paid.id, invoice_id) != invoice_id:
                        raise ProviderUnsupported("multi_invoice_payment")
                    payments.append(paid)
            return payments

    async def get_usage(
        self, subscription_id: str, period: tuple[datetime, datetime]
    ) -> Usage | None:
        async with self._read():
            return None  # Pennylane holds no metered usage

    # -- writes: none in this slice ----------------------------------------------------

    async def plan_write(self, invoice_id: str, action_type: str):
        raise PlatformCannotWrite("pennylane: reads only")

    async def create_credit_note(self, *args, **kwargs) -> ProviderAction:
        raise PlatformCannotWrite("pennylane: reads only")

    async def create_refund(self, *args, **kwargs) -> ProviderAction:
        raise PlatformCannotWrite("pennylane: reads only")

    async def find_action(self, idempotency_key: str, invoice_id: str) -> list[ProviderAction]:
        """Nothing was ever written by this system on Pennylane."""
        async with self._read():
            return []

    # -- the budget and the company check ----------------------------------------------

    @asynccontextmanager
    async def _read(self) -> AsyncIterator[None]:
        try:
            async with asyncio.timeout(self._budget):
                await self.reader.check_company()
                yield
        except TimeoutError:
            raise ProviderUnavailable("pennylane: the read's time budget was spent") from None


def _int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProviderUnsupported("missing_field")
    return value
