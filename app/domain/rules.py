"""Deterministic billing checks. See ADR-003.

The model never does what a reliable ``if`` can do. Everything computable from
dates, amounts, statuses and identifiers stays here, in code that is cheap,
instant, testable and identical on every run.

Each rule returns a :class:`RuleFinding` or ``None``. A rule states a fact and
shows its evidence; it never recommends an action.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import InvoiceStatus, RuleCode, Severity
from app.domain.injection import suspected_prompt_injection
from app.domain.models import Invoice, RuleFinding, Subscription

__all__ = [
    "detect_duplicate_invoice",
    "invoice_after_cancellation",
    "proration_mismatch",
    "suspected_prompt_injection",
    "suspicious_usage_delta",
    "unknown_invoice",
]

#: Relative gap beyond which billed usage stops matching expected usage.
DEFAULT_USAGE_TOLERANCE = Decimal("0.25")

#: Proration is allowed to differ by this many minor units, to absorb the
#: provider's own rounding without hiding a real mismatch.
DEFAULT_PRORATION_TOLERANCE_MINOR = 1


def detect_duplicate_invoice(target: Invoice, recent: list[Invoice]) -> RuleFinding | None:
    """Two live invoices charging the same subscription for the same period.

    Equal totals and overlapping periods on one subscription is the signature
    of a double charge. Voided invoices are ignored: a cancelled duplicate
    never took the customer's money.
    """
    if target.status is InvoiceStatus.VOID or target.subscription_id is None:
        return None

    for other in recent:
        if other.id == target.id:
            continue
        if other.status is InvoiceStatus.VOID:
            continue
        if other.subscription_id != target.subscription_id:
            continue
        if other.total != target.total:
            continue
        if not target.overlaps_period_of(other):
            continue

        return RuleFinding(
            code=RuleCode.DUPLICATE_INVOICE,
            severity=Severity.HIGH,
            description=(
                "Deux factures couvrent la même période d'abonnement "
                "pour un montant identique."
            ),
            evidence={
                "invoice_1": other.id,
                "invoice_2": target.id,
                "subscription_id": target.subscription_id,
                "amount_minor": target.total.amount_minor,
                "currency": target.total.currency,
                "period_start": _iso(target.period_start),
                "period_end": _iso(target.period_end),
            },
        )
    return None


def invoice_after_cancellation(invoice: Invoice, sub: Subscription) -> RuleFinding | None:
    """A subscription was billed for a period starting after its cancellation."""
    if sub.cancelled_at is None or invoice.status is InvoiceStatus.VOID:
        return None

    covered_from = invoice.period_start or invoice.issued_at
    if covered_from <= sub.cancelled_at:
        return None

    return RuleFinding(
        code=RuleCode.INVOICE_AFTER_CANCELLATION,
        severity=Severity.HIGH,
        description="Une facture couvre une période postérieure à la résiliation.",
        evidence={
            "invoice_id": invoice.id,
            "subscription_id": sub.id,
            "cancelled_at": _iso(sub.cancelled_at),
            "covered_from": _iso(covered_from),
            "amount_minor": invoice.total.amount_minor,
            "currency": invoice.total.currency,
        },
    )


def suspicious_usage_delta(
    expected: Decimal,
    billed: Decimal,
    tolerance: Decimal = DEFAULT_USAGE_TOLERANCE,
) -> RuleFinding | None:
    """Billed usage diverges from expected usage beyond `tolerance`.

    Quantities are ``Decimal``, not ``Money``: this compares consumption, not
    amounts.
    """
    if expected <= 0:
        if billed <= 0:
            return None
        relative = None
    else:
        relative = abs(billed - expected) / expected
        if relative <= tolerance:
            return None

    return RuleFinding(
        code=RuleCode.SUSPICIOUS_USAGE_DELTA,
        severity=Severity.MEDIUM,
        description="L'usage facturé s'écarte nettement de l'usage attendu.",
        evidence={
            "expected": str(expected),
            "billed": str(billed),
            "relative_delta": str(relative) if relative is not None else None,
            "tolerance": str(tolerance),
        },
    )


def proration_mismatch(
    invoice: Invoice,
    sub: Subscription,
    tolerance_minor: int = DEFAULT_PRORATION_TOLERANCE_MINOR,
) -> RuleFinding | None:
    """A partial-period line is not priced pro rata of its own period.

    Only lines covering *less* than the subscription period are checked: those
    are the ones a plan change mid-month produces, and the ones customers
    dispute.

    The ratio is the line's duration over the period's, in seconds, because
    providers prorate to the second from the moment of the change. Amounts are
    compared without their sign: a plan change is billed as a negative credit
    for the unused time on the old plan and a charge for the remaining time on
    the new one.

    Assumption: each line carries the full price of its own plan as its unit
    amount, as Stripe's proration lines do. A single net line (new plan minus
    old) carries no plan price, cannot be checked, and stays flagged.
    """
    if invoice.status is InvoiceStatus.VOID or sub.period_seconds <= 0:
        return None

    for index, line in enumerate(invoice.lines):
        line_seconds = line.period_seconds
        if line_seconds is None or line_seconds <= 0 or line_seconds >= sub.period_seconds:
            continue

        ratio = Decimal(line_seconds) / Decimal(sub.period_seconds)
        expected = abs(line.unit_amount.multiply(line.quantity).multiply(ratio))
        gap = abs(expected - abs(line.amount))
        if gap.amount_minor <= tolerance_minor:
            continue

        return RuleFinding(
            code=RuleCode.PRORATION_MISMATCH,
            severity=Severity.MEDIUM,
            description="Le montant proratisé ne correspond pas à la période couverte.",
            evidence={
                "invoice_id": invoice.id,
                "line_index": index,
                "line_description": line.description,
                "line_seconds": line_seconds,
                "period_seconds": sub.period_seconds,
                "ratio": str(ratio),
                "expected_minor": expected.amount_minor,
                "billed_minor": line.amount.amount_minor,
                "currency": line.amount.currency,
            },
        )
    return None


def unknown_invoice(invoice_id: str, invoices: list[Invoice]) -> RuleFinding | None:
    """The disputed invoice reference matches nothing we hold for this customer."""
    if any(invoice.id == invoice_id for invoice in invoices):
        return None

    return RuleFinding(
        code=RuleCode.UNKNOWN_INVOICE,
        severity=Severity.MEDIUM,
        description="La facture contestée est introuvable pour ce client.",
        evidence={
            "invoice_id": invoice_id,
            "known_invoice_ids": [invoice.id for invoice in invoices],
        },
    )


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None
