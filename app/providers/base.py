"""The billing-provider boundary. See ADR-001.

The application layer imports :class:`BillingProvider`. It never imports
``stripe``, ``lago`` or anything else with a vendor's name on it. Adding a
platform means writing an adapter that maps its payloads onto the domain
objects -- the rules, the decision engines and the action policy do not move.

Two methods mutate anything -- a credit note and a refund -- and both take an
idempotency key.
"""

from __future__ import annotations

from datetime import datetime, timedelta
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
        "invoice_credit",
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


class NotSent(ProviderRejected):
    """Nothing was sent: the deadline for sending had passed by the time the
    adapter was ready to send (specs/002-stripe-writes, FR-005)."""


class ProviderOutcomeUnknown(Exception):  # noqa: N818 -- a state, not a programming error
    """A write was, or may have been, sent, and what became of it is unknown.

    What an adapter raises for anything after the request left that is not a
    refusal: no answer, an answer it cannot read, an idempotency conflict, a
    time budget spent. Never `ProviderUnavailable`, which is for reads. The
    message is the category alone (specs/002-stripe-writes, FR-020).
    """

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class ProviderUnavailable(Exception):  # noqa: N818 -- a state, not a programming error
    """A read could not be completed: the platform timed out, failed or is
    down (taxonomy S7).

    Adapters raise it for reads only, once their own retries are spent. The
    investigation does not guess around a missing read: it stops, decides
    nothing, and the caller is told so -- an empty answer would read as "this
    customer has no invoices".
    """


#: How a write lands on the invoice: what is due lowered, the customer's
#: balance credited, or money paid back.
Allocation = Literal["reduce_due", "customer_balance", "refund"]

#: The allocations each type of write may take.
ALLOCATIONS: dict[str, frozenset[str]] = {
    "credit_note": frozenset({"reduce_due", "customer_balance"}),
    "refund": frozenset({"refund"}),
}


class ProviderAction(BaseModel):
    """A write on the billing platform, as the platform holds it.

    ``provider_action_id`` is what makes a retry safe: it is recorded next to
    the idempotency key, so a timeout can be resolved by looking up what
    actually happened rather than by firing the write again. The rest is
    what the execution service checks the write against before recording it
    (specs/002-stripe-writes, FR-001 and FR-004). `allocation` and `status`
    are None when the platform's answer does not say them: an answer that
    cannot be read, never a guess. `status` is an observation, made at
    `observed_at`, not the current state.
    """

    model_config = ConfigDict(frozen=True)

    provider: str
    provider_action_id: str
    action_type: Literal["credit_note", "refund"]
    idempotency_key: str
    status: Literal["succeeded", "pending", "failed"] | None
    created_at: datetime
    invoice_id: str
    amount_minor: int
    currency: str
    test_mode: bool
    allocation: Allocation | None
    action_id: str | None
    observed_at: datetime


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
    #: How long after a first attempt the platform still answers a key with
    #: what it made: every send, and every absence taken as proof, falls
    #: within it (specs/002-stripe-writes, FR-005).
    idempotency_window: timedelta
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

    async def invoice_owner(self, invoice_id: str) -> str | None:
        """The customer an invoice belongs to, or None when there is no such
        invoice. Only the owner is extracted -- nothing of the content is
        mapped and nothing further is read -- so what the invoice bills never
        makes it refuse: the check on an invoice that may be another
        customer's learns whose it is, and nothing more. An owner the
        platform cannot name is `ProviderUnsupported("missing_field")`
        (ADR-001, 2026-10-22)."""
        ...

    async def list_invoices(self, customer_id: str, since: datetime) -> list[Invoice]: ...

    async def get_usage(
        self, subscription_id: str, period: tuple[datetime, datetime]
    ) -> Usage | None:
        """The subscription's metered usage over `period` (start, end), or None.

        The period is the one the invoice's metered lines cover: on a platform
        that bills usage in arrears, not the subscription's current one
        (ADR-001, 2026-10-22). None when the platform holds nothing for
        exactly that period -- unavailable, never zero.
        """
        ...

    async def list_payments(self, customer_id: str) -> list[Payment]: ...

    async def plan_write(self, invoice_id: str, action_type: str) -> Allocation:
        """How a write of this type would land on the invoice as it stands.

        Read once, before an action's first send; the execution service keeps
        the answer and passes it to every send, so a retry never rebuilds it.
        An invoice in a state the platform does not write on is
        ``ProviderRejected("invoice_state")`` (specs/002-stripe-writes,
        FR-012).
        """
        ...

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
        """Raise :class:`ProviderRejected` when the platform says no.

        The write takes the allocation planned, and refuses
        (``"invoice_state"``) an invoice that no longer allows it, never
        writing another. Past `send_by`, nothing is sent: :class:`NotSent`.
        Anything after the request left that is not a refusal is
        :class:`ProviderOutcomeUnknown`.
        """
        ...

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
        """Return money already collected on the invoice.

        Not a credit note: a credit note corrects what is owed, a refund
        moves cash back. An invoice with no settled payment to draw on is a
        refusal (:class:`ProviderRejected`), never a credit note instead.
        Otherwise as `create_credit_note`.
        """
        ...

    async def find_action(self, idempotency_key: str, invoice_id: str) -> list[ProviderAction]:
        """Every write the platform holds under this key on this invoice.

        The read that resolves an unknown outcome: after a timeout, ask
        whether the write landed instead of guessing, and record its
        reference if it did. None, one or more: two is an incident, never
        "the first one found". A list cut short is `ProviderUnavailable`,
        never an absence. See ADR-008.
        """
        ...
