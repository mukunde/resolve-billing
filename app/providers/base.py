"""The billing-provider boundary. See ADR-001.

The application layer imports :class:`BillingProvider`. It never imports
``stripe``, ``lago`` or anything else with a vendor's name on it. Adding a
platform means writing an adapter that maps its payloads onto the domain
objects -- the rules, the decision engines and the action policy do not move.

Two methods mutate anything -- a credit note and a refund -- and both take an
idempotency key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.domain.exceptions import BillingDomainError
from app.domain.models import Customer, Invoice, Payment, Subscription, Usage


class NotFound(BillingDomainError):
    """The platform holds no object under this identifier.

    What `get_customer`, `get_subscription` and `get_invoice` raise for an
    unknown identifier, on every platform; `find_*` answer `None` instead.
    """


#: Why a platform's data cannot be represented in the domain. Closed: a new
#: reason is a decision, and its tests come with it.
UNSUPPORTED_REASONS = frozenset(
    {
        "mixed_item_periods",
        "multiple_prices",
        "subscription_status",
        "cancellation_without_end",
        "invoice_tax",
        "invoice_discount",
        "negative_line",
        "fractional_unit_price",
        "partial_refund",
        "multi_invoice_payment",
        "unplaced_payment",
        "unrepresentable_period",
        "missing_field",
    }
)


class ProviderUnsupported(Exception):  # noqa: N818 -- a state, not a programming error
    """The platform answered, with data the domain cannot represent.

    Not `ProviderUnavailable` -- trying later changes nothing -- and not a
    configuration error. Nothing is decided on it: no investigation result,
    no proposal, no run row; the caller is told the data is not supported,
    by category. The message is the category alone, never the platform's
    data. See specs/001-stripe-read-adapter, FR-002.
    """

    def __init__(self, reason: str) -> None:
        if reason not in UNSUPPORTED_REASONS:
            raise ValueError(f"unknown unsupported-data reason {reason!r}")
        super().__init__(reason)
        self.reason = reason


class PlatformCannotWrite(Exception):  # noqa: N818 -- a state, not a programming error
    """A platform that cannot write was asked to write, or to reconcile.

    The execution service never asks -- it checks `can_write` first and
    refuses before reading or claiming anything -- so this is a guard against
    a caller that skipped that check, raised before any call to the platform.
    """


class ProviderRejected(BillingDomainError):
    """The platform answered, and refused the write: nothing was created.

    Adapters raise this, or a subclass, only when the platform's answer says
    so -- a validation error, an unknown invoice. Anything else, a timeout or
    a dropped connection above all, leaves the outcome unknown: the write
    may have landed with its response lost. See ADR-008.
    """


class ProviderUnavailable(Exception):  # noqa: N818 -- a state, not a programming error
    """A read could not be completed: the platform timed out, failed or is
    down (taxonomy S7).

    Adapters raise it for reads only, once their own retries are spent. The
    investigation does not guess around a missing read: it stops, decides
    nothing, and the caller is told so -- an empty answer would read as "this
    customer has no invoices".
    """


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
    #: Whether the platform can make the two writes below. A platform that
    #: cannot (a read-only slice) is never asked: execution is refused before
    #: anything is read or claimed (ADR-008).
    can_write: bool
    # Optionally, `invoice_references(text) -> set[str]`: the invoice ids cited
    # in a text, normalised to this platform's own form (taxonomy S6). Only
    # the platform knows how its references are written. Not part of the
    # protocol, so a provider without it still satisfies it and simply has no
    # cited-reference check.

    async def get_customer(self, customer_id: str) -> Customer: ...

    async def find_customer_by_email(self, email: str) -> Customer | None: ...

    async def list_customers(self) -> list[Customer]:
        """Every customer, for the impersonation check. See ADR-007.

        Cheap on the fake. A real adapter reads it from a cached index of
        names and domains rather than paging the platform on each dispute.
        """
        ...

    async def get_subscription(self, subscription_id: str) -> Subscription: ...

    async def list_subscriptions(self, customer_id: str) -> list[Subscription]: ...

    async def get_invoice(self, invoice_id: str) -> Invoice: ...

    async def find_invoice(self, invoice_id: str) -> Invoice | None: ...

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
    ) -> ProviderAction:
        """Raise :class:`ProviderRejected` when the platform says no."""
        ...

    async def create_refund(
        self,
        invoice_id: str,
        amount_minor: int,
        currency: str,
        reason: str,
        idempotency_key: str,
    ) -> ProviderAction:
        """Return money already collected on the invoice.

        Not a credit note: a credit note corrects what is owed, a refund
        moves cash back. An invoice with no settled payment to draw on is a
        refusal (:class:`ProviderRejected`), never a credit note instead.
        """
        ...

    async def find_action(self, idempotency_key: str) -> ProviderAction | None:
        """What the platform holds under this key, if anything.

        The read that resolves an unknown outcome: after a timeout, ask
        whether the write landed instead of guessing, and record its
        reference if it did. A real adapter looks the key up, or lists the
        invoice's credit notes and matches on it. See ADR-008.
        """
        ...
