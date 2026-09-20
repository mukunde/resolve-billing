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

from datetime import UTC, datetime

from app.domain.exceptions import BillingDomainError
from app.domain.models import Customer, Invoice, Payment, Subscription, Usage
from app.providers.base import ProviderAction
from app.providers.fake.fixtures import load_all


class NotFound(BillingDomainError):
    """The platform holds no such object."""


class FakeBillingProvider:
    """In-memory billing platform backed by ``fixtures/``."""

    name = "fake"

    def __init__(self) -> None:
        data = load_all()
        self._customers = list(data.customers)
        self._subscriptions = list(data.subscriptions)
        self._invoices = list(data.invoices)
        self._payments = list(data.payments)
        self._usage = list(data.usage)
        self._actions: dict[str, ProviderAction] = {}

    # -- reads -------------------------------------------------------------

    async def get_customer(self, customer_id: str) -> Customer:
        found = next((c for c in self._customers if c.id == customer_id), None)
        if found is None:
            raise NotFound(f"unknown customer {customer_id!r}")
        return found

    async def find_customer_by_email(self, email: str) -> Customer | None:
        target = email.strip().lower()
        return next((c for c in self._customers if c.email.lower() == target), None)

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

    async def list_invoices(self, customer_id: str, since: datetime) -> list[Invoice]:
        return [
            i for i in self._invoices if i.customer_id == customer_id and i.issued_at >= since
        ]

    async def get_usage(self, subscription_id: str) -> Usage | None:
        return next((u for u in self._usage if u.subscription_id == subscription_id), None)

    async def list_payments(self, customer_id: str) -> list[Payment]:
        invoice_ids = {i.id for i in self._invoices if i.customer_id == customer_id}
        return [p for p in self._payments if p.invoice_id in invoice_ids]

    # -- the one write -----------------------------------------------------

    async def create_credit_note(
        self,
        invoice_id: str,
        amount_minor: int,
        currency: str,
        reason: str,
        idempotency_key: str,
    ) -> ProviderAction:
        """Issue a credit note, at most once per idempotency key.

        The replay check comes first, on purpose: a retry after a timeout must
        find the original action rather than create a second one.
        """
        replayed = self._actions.get(idempotency_key)
        if replayed is not None:
            return replayed

        await self.get_invoice(invoice_id)  # raises NotFound on a bad reference

        action = ProviderAction(
            provider=self.name,
            provider_action_id=f"cn_{len(self._actions) + 1:03d}",
            action_type="credit_note",
            idempotency_key=idempotency_key,
            status="succeeded",
            created_at=datetime.now(UTC),
        )
        self._actions[idempotency_key] = action
        return action

    # -- test and demo helpers --------------------------------------------

    @property
    def executed_actions(self) -> list[ProviderAction]:
        return list(self._actions.values())
