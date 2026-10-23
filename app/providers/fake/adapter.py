"""A billing platform that runs offline, from JSON fixtures.

Implements :class:`BillingProvider` in full. The demo, the tests and the
evaluation suite all run against it, which is what makes them free,
reproducible and independent of anyone's API being up.

Writes are recorded in memory rather than sent anywhere, and they honour the
idempotency key: replaying the same key returns the original action instead of
issuing a second credit note. That is the behaviour a real adapter must have,
so the fake is the place to prove the application layer relies on it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from app.domain.enums import InvoiceStatus, PaymentStatus
from app.domain.models import Customer, Invoice, Payment, Subscription, Usage
from app.providers.base import (
    ALLOCATIONS,
    Allocation,
    NotFound,
    NotSent,
    ProviderAction,
    ProviderRejected,
)
from app.providers.fake.fixtures import load_all

__all__ = ["FakeBillingProvider", "NotFound"]


class FakeBillingProvider:
    """In-memory billing platform backed by ``fixtures/``."""

    name = "fake"
    can_write = True
    #: The fake keeps keys for as long as it runs; it declares the window a
    #: real platform has, so the execution service is held to it here too.
    idempotency_window = timedelta(hours=23)

    #: How this platform writes an invoice reference in free text: `inv_002`,
    #: `INV-002`, `inv002` -- digits after the prefix, so a word that happens
    #: to start with "inv" is not one (taxonomy S6).
    _INVOICE_REFERENCE = re.compile(r"\binv[_-]?(\d{3,})\b", re.IGNORECASE)

    def invoice_references(self, text: str) -> set[str]:
        """Invoice ids cited in `text`, normalised to this platform's own form.

        Optional on the port (taxonomy S6): only the platform knows its
        format, and a provider without this method simply has no
        cited-reference check.
        """
        return {invoice_id for _, _, invoice_id in self.invoice_reference_spans(text)}

    def invoice_reference_spans(self, text: str) -> list[tuple[int, int, str]]:
        """Where each reference sits in `text`, and the id it stands for --
        for masking it in place (D10). Optional on the port, like the above."""
        return [
            (match.start(), match.end(), f"inv_{match.group(1).lower()}")
            for match in self._INVOICE_REFERENCE.finditer(text)
        ]

    def __init__(self) -> None:
        data = load_all()
        self._customers = list(data.customers)
        self._subscriptions = list(data.subscriptions)
        self._invoices = list(data.invoices)
        self._payments = list(data.payments)
        self._usage = list(data.usage)
        self._actions: dict[str, ProviderAction] = {}
        # Writes put there by a test, beside those the keys produced: a second
        # write under one key, one unlike the action.
        self._planted: list[ProviderAction] = []
        #: The fake's clock, for `send_by` and observations; a test may set it.
        self.now = lambda: datetime.now(UTC)
        # What each invoice has already given back, in minor units: the
        # platform's own ledger of corrections, which caps the next one.
        self._credited: dict[str, int] = {}
        self._refunded: dict[str, int] = {}

    # -- reads -------------------------------------------------------------

    async def get_customer(self, customer_id: str) -> Customer:
        found = next((c for c in self._customers if c.id == customer_id), None)
        if found is None:
            raise NotFound(f"unknown customer {customer_id!r}")
        return found

    async def find_customer_by_email(self, email: str) -> Customer | None:
        target = email.strip().lower()
        return next((c for c in self._customers if c.email.lower() == target), None)

    async def list_customers(self) -> list[Customer]:
        return list(self._customers)

    async def get_subscription(self, subscription_id: str) -> Subscription:
        found = next((s for s in self._subscriptions if s.id == subscription_id), None)
        if found is None:
            raise NotFound(f"unknown subscription {subscription_id!r}")
        return found

    async def list_subscriptions(self, customer_id: str) -> list[Subscription]:
        return [s for s in self._subscriptions if s.customer_id == customer_id]

    async def get_invoice(self, invoice_id: str) -> Invoice:
        found = next((i for i in self._invoices if i.id == invoice_id), None)
        if found is None:
            raise NotFound(f"unknown invoice {invoice_id!r}")
        return found

    async def find_invoice(self, invoice_id: str) -> Invoice | None:
        return next((i for i in self._invoices if i.id == invoice_id), None)

    async def invoice_owner(self, invoice_id: str) -> str | None:
        # Read on its own, not through `find_invoice`: whose it is, nothing else.
        return next((i.customer_id for i in self._invoices if i.id == invoice_id), None)

    async def list_invoices(self, customer_id: str, since: datetime) -> list[Invoice]:
        return [i for i in self._invoices if i.customer_id == customer_id and i.issued_at >= since]

    async def get_usage(
        self, subscription_id: str, period: tuple[datetime, datetime]
    ) -> Usage | None:
        # Exactly that period: a record that only contains it, or sits next
        # to it, says nothing about it.
        return next(
            (
                u
                for u in self._usage
                if u.subscription_id == subscription_id and (u.period_start, u.period_end) == period
            ),
            None,
        )

    async def list_payments(self, customer_id: str) -> list[Payment]:
        invoice_ids = {i.id for i in self._invoices if i.customer_id == customer_id}
        return [p for p in self._payments if p.invoice_id in invoice_ids]

    # -- the two writes ----------------------------------------------------

    async def plan_write(self, invoice_id: str, action_type: str) -> Allocation:
        try:
            invoice = await self.get_invoice(invoice_id)
        except NotFound as error:
            raise ProviderRejected(str(error)) from error
        allowed = self._allocations(invoice) & ALLOCATIONS.get(action_type, frozenset())
        if not allowed:
            raise ProviderRejected("invoice_state")
        (allocation,) = allowed
        return allocation

    def _allocations(self, invoice: Invoice) -> frozenset[str]:
        """What the invoice's state lets a write do: lower what is due on an
        open invoice nothing was paid on; credit the balance or refund on a
        paid one. Anything else -- partly paid, draft, void, uncollectible --
        takes no write (specs/002-stripe-writes, FR-012)."""
        settled = any(
            p.invoice_id == invoice.id and p.status is PaymentStatus.SUCCEEDED
            for p in self._payments
        )
        if invoice.status is InvoiceStatus.OPEN and not settled:
            return frozenset({"reduce_due"})
        if invoice.status is InvoiceStatus.PAID:
            return frozenset({"customer_balance", "refund"})
        return frozenset()

    async def create_credit_note(
        self,
        invoice_id: str,
        amount_minor: int,
        currency: str,
        reason: str,
        idempotency_key: str,
        *,
        action_id: str,
        allocation: Allocation,
        send_by: datetime,
    ) -> ProviderAction:
        """Issue a credit note, at most once per idempotency key.

        The replay check comes first, on purpose: a retry after a timeout must
        find the original action rather than create a second one.
        """
        replayed = self._actions.get(idempotency_key)
        if replayed is not None:
            return replayed

        invoice = await self._writable(invoice_id, allocation, send_by)
        self._refuse_beyond_the_invoice(invoice, amount_minor, currency)

        action = self._made(
            "cn",
            "credit_note",
            invoice,
            amount_minor,
            currency,
            idempotency_key,
            action_id,
            allocation,
        )
        self._credited[invoice_id] = self._credited.get(invoice_id, 0) + amount_minor
        return action

    async def create_refund(
        self,
        invoice_id: str,
        amount_minor: int,
        currency: str,
        reason: str,
        idempotency_key: str,
        *,
        action_id: str,
        allocation: Allocation,
        send_by: datetime,
    ) -> ProviderAction:
        """Refund money collected on the invoice, at most once per key.

        A refund draws on a settled payment. With none large enough to cover
        it, the platform refuses rather than doing something else.
        """
        replayed = self._actions.get(idempotency_key)
        if replayed is not None:
            return replayed

        invoice = await self._writable(invoice_id, allocation, send_by)
        self._refuse_beyond_the_invoice(invoice, amount_minor, currency)

        collected = sum(
            p.amount.amount_minor
            for p in self._payments
            if p.invoice_id == invoice_id
            and p.status is PaymentStatus.SUCCEEDED
            and p.amount.currency == currency
        )
        refundable = collected - self._refunded.get(invoice_id, 0)
        if amount_minor > refundable:
            raise ProviderRejected(
                f"only {refundable} {currency} of settled payment left to refund "
                f"on invoice {invoice_id}, asked {amount_minor}"
            )

        action = self._made(
            "re",
            "refund",
            invoice,
            amount_minor,
            currency,
            idempotency_key,
            action_id,
            allocation,
        )
        self._refunded[invoice_id] = self._refunded.get(invoice_id, 0) + amount_minor
        return action

    async def _writable(self, invoice_id: str, allocation: str, send_by: datetime) -> Invoice:
        """The invoice, once it is shown to take the planned allocation, and
        only before the deadline. A bad reference is a refusal: the platform
        answered, and nothing was created."""
        try:
            invoice = await self.get_invoice(invoice_id)
        except NotFound as error:
            raise ProviderRejected(str(error)) from error
        if allocation not in self._allocations(invoice):
            raise ProviderRejected("invoice_state")
        if self.now() >= send_by:
            raise NotSent("window_passed")
        return invoice

    def _made(
        self,
        prefix: str,
        action_type: str,
        invoice: Invoice,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        action_id: str,
        allocation: str,
    ) -> ProviderAction:
        now = self.now()
        action = ProviderAction(
            provider=self.name,
            provider_action_id=f"{prefix}_{len(self._actions) + 1:03d}",
            action_type=action_type,
            idempotency_key=idempotency_key,
            status="succeeded",
            created_at=now,
            invoice_id=invoice.id,
            amount_minor=amount_minor,
            currency=currency,
            test_mode=True,
            allocation=allocation,
            action_id=action_id,
            observed_at=now,
        )
        self._actions[idempotency_key] = action
        return action

    def _refuse_beyond_the_invoice(
        self, invoice: Invoice, amount_minor: int, currency: str
    ) -> None:
        """Credit notes and refunds together never exceed the invoice.

        The fake's model of the platform's rule: once 99 EUR has been
        credited on a 99 EUR invoice, nothing more comes back from it, by
        either instrument. Whether a real platform enforces exactly this is
        checked against it (Stripe sandbox, S7), not assumed. ADR-008.
        """
        if currency != invoice.total.currency:
            raise ProviderRejected(
                f"invoice {invoice.id} is in {invoice.total.currency}, not {currency}"
            )
        given_back = self._credited.get(invoice.id, 0) + self._refunded.get(invoice.id, 0)
        left = invoice.total.amount_minor - given_back
        if amount_minor > left:
            raise ProviderRejected(
                f"only {left} {currency} left to correct on invoice {invoice.id}, "
                f"asked {amount_minor}"
            )

    async def find_action(self, idempotency_key: str, invoice_id: str) -> list[ProviderAction]:
        held = [self._actions[idempotency_key]] if idempotency_key in self._actions else []
        return [
            action.model_copy(update={"observed_at": self.now()})
            for action in held + self._planted
            if action.idempotency_key == idempotency_key and action.invoice_id == invoice_id
        ]

    # -- test and demo helpers --------------------------------------------

    @property
    def executed_actions(self) -> list[ProviderAction]:
        return list(self._actions.values())

    def plant_action(self, action: ProviderAction) -> None:
        """Hold a write the keys did not produce: a second one under a key, or
        one unlike the action, as a platform's console could."""
        self._planted.append(action)

    def set_status(self, provider_action_id: str, status: str) -> None:
        """Change what became of a write after it was made: a refund failing
        days later, a credit note voided in the console."""
        for key, action in self._actions.items():
            if action.provider_action_id == provider_action_id:
                self._actions[key] = action.model_copy(update={"status": status})

    def change_invoice(self, invoice_id: str, *, total_minor: int) -> Invoice:
        """Change an invoice behind the application's back, as someone
        working in the platform's own console would."""
        invoice = next(i for i in self._invoices if i.id == invoice_id)
        changed = invoice.model_copy(
            update={"total": invoice.total.model_copy(update={"amount_minor": total_minor})}
        )
        self._invoices[self._invoices.index(invoice)] = changed
        return changed
