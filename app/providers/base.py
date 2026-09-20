"""The billing-provider boundary. See ADR-001.

The application layer imports :class:`BillingProvider`. It never imports
``stripe``, ``lago`` or anything else with a vendor's name on it. Adding a
platform means writing an adapter that maps its payloads onto the domain
objects -- the rules, the decision engines and the action policy do not move.

Only one method mutates anything, and it takes an idempotency key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.domain.models import Customer, Invoice, Payment, Subscription, Usage


class ProviderAction(BaseModel):
    """The outcome of a write on the billing platform.

    ``provider_action_id`` is what makes a retry safe: it is recorded next to
    the idempotency key, so a timeout can be resolved by looking up what
    actually happened rather than by firing the write again.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    provider_action_id: str
    action_type: Literal["credit_note", "refund"]
    idempotency_key: str
    status: Literal["succeeded", "pending", "failed"]
    created_at: datetime


@runtime_checkable
class BillingProvider(Protocol):
    """Read the billing situation, and issue corrective writes.

    Reads are safe to retry with backoff. The single write is not, which is
    why it carries an idempotency key rather than relying on the caller to be
    careful.
    """

    name: str

    async def get_customer(self, customer_id: str) -> Customer: ...

    async def find_customer_by_email(self, email: str) -> Customer | None: ...

    async def get_subscription(self, subscription_id: str) -> Subscription: ...

    async def list_subscriptions(self, customer_id: str) -> list[Subscription]: ...

    async def get_invoice(self, invoice_id: str) -> Invoice: ...

    async def list_invoices(self, customer_id: str, since: datetime) -> list[Invoice]: ...

    async def get_usage(self, subscription_id: str) -> Usage | None: ...

    async def list_payments(self, customer_id: str) -> list[Payment]: ...

    async def create_credit_note(
        self,
        invoice_id: str,
        amount_minor: int,
        currency: str,
        reason: str,
        idempotency_key: str,
    ) -> ProviderAction: ...
