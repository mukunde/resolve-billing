"""Normalised billing objects.

Every provider maps its own payloads onto these. The rules, the application
layer and the decision engines only ever see this vocabulary -- that is the
whole point of ADR-001.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.domain.enums import (
    InvoiceStatus,
    PaymentStatus,
    RuleCode,
    Severity,
    SubscriptionStatus,
)
from app.domain.money import Money


class Customer(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    email: str
    currency: str | None = None
    display_name: str | None = None


class Subscription(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    customer_id: str
    status: SubscriptionStatus
    plan_code: str
    current_period_start: AwareDatetime
    current_period_end: AwareDatetime
    cancelled_at: AwareDatetime | None = None

    @property
    def period_seconds(self) -> int:
        """Length of the current billing period, in whole seconds."""
        return (self.current_period_end - self.current_period_start) // timedelta(seconds=1)


class InvoiceLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    description: str
    quantity: Decimal
    unit_amount: Money
    amount: Money
    period_start: AwareDatetime | None = None
    period_end: AwareDatetime | None = None
    metric: str | None = None
    """Set on usage-based lines, so metered usage can be compared to what was
    billed. ``None`` on a flat subscription line."""

    @property
    def period_seconds(self) -> int | None:
        if self.period_start is None or self.period_end is None:
            return None
        return (self.period_end - self.period_start) // timedelta(seconds=1)


class Invoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    customer_id: str
    subscription_id: str | None
    total: Money
    status: InvoiceStatus
    issued_at: AwareDatetime
    lines: list[InvoiceLine] = []

    @property
    def period_start(self) -> AwareDatetime | None:
        """Earliest period covered by any line."""
        starts = [line.period_start for line in self.lines if line.period_start is not None]
        return min(starts) if starts else None

    @property
    def period_end(self) -> AwareDatetime | None:
        """Latest period covered by any line."""
        ends = [line.period_end for line in self.lines if line.period_end is not None]
        return max(ends) if ends else None

    def overlaps_period_of(self, other: Invoice) -> bool:
        """Whether the two invoices cover any common instant.

        Returns False when either invoice carries no period at all: absence of
        data is not evidence of overlap.
        """
        if None in (self.period_start, self.period_end, other.period_start, other.period_end):
            return False
        return self.period_start < other.period_end and other.period_start < self.period_end


class Payment(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    invoice_id: str | None
    amount: Money
    status: PaymentStatus
    received_at: AwareDatetime | None = None


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    subscription_id: str
    metric: str
    quantity: Decimal
    period_start: AwareDatetime
    period_end: AwareDatetime


class Dispute(BaseModel):
    """What the customer sent us. Untrusted input, always. See threat model."""

    model_config = ConfigDict(frozen=True)

    id: str
    customer_message: str
    customer_id: str | None = None
    customer_email: str | None = None
    invoice_id: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    """Mail headers the intake forwarded, as (name, value) pairs, repeats
    kept. Untrusted like the message: they can only take an automatic reply
    away (ADR-011)."""


class BillingContext(BaseModel):
    """Everything the provider could find about the disputed situation.

    Assembled once, before any decision engine is called. Nothing downstream
    fetches more data -- which is what makes the flow loop-free (ADR-006).
    """

    model_config = ConfigDict(frozen=True)

    customer: Customer
    subscriptions: list[Subscription] = []
    invoices: list[Invoice] = []
    payments: list[Payment] = []
    usage: Usage | None = None
    target_invoice_id: str | None = None

    @property
    def target_invoice(self) -> Invoice | None:
        if self.target_invoice_id is None:
            return None
        return next((i for i in self.invoices if i.id == self.target_invoice_id), None)

    def subscription(self, subscription_id: str | None) -> Subscription | None:
        if subscription_id is None:
            return None
        return next((s for s in self.subscriptions if s.id == subscription_id), None)


class RuleFinding(BaseModel):
    """One deterministic observation about the billing situation.

    A finding is a *fact with evidence*, never a recommendation. What to do
    about it belongs to the decision engine and the action policy.
    """

    model_config = ConfigDict(frozen=True)

    code: RuleCode
    severity: Severity
    description: str
    evidence: dict
