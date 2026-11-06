"""What every platform's mapper checks before a taxed invoice exists
(specs/013-invoice-tax, FR-002).

No vendor here: a mapper reads its platform's figures, then hands the mapped
lines and whatever totals the platform posts to `reconcile`. The posted tax is
authoritative against its rate -- nothing is computed from a rate -- but the
posted figures must agree with each other, or the invoice is refused.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from app.domain.models import InvoiceLine, canonical_rate
from app.domain.money import Money
from app.providers.base import ProviderUnsupported


def untaxed_unless_charged(lines: list[InvoiceLine]) -> list[InvoiceLine]:
    """Drop every tax entry when none charges anything.

    An invoice whose tax is zero everywhere is an untaxed invoice, read and
    serialised exactly as before tax came into the domain (FR-008, FR-009). On
    a taxed invoice, zero entries are kept with their rate and reason.
    """
    if any(tax.amount.amount_minor != 0 for line in lines for tax in line.taxes):
        return lines
    return [line.model_copy(update={"taxes": ()}) if line.taxes else line for line in lines]


def reconcile(
    lines: list[InvoiceLine],
    total: Money,
    *,
    excluding_tax: Money | None = None,
    tax_by_rate: Iterable[tuple[Decimal, Money]] | None = None,
    tax_total: Money | None = None,
) -> None:
    """Refuse (`tax_mismatch`) a taxed invoice whose posted figures disagree.

    Only an invoice carrying tax -- on a line, or in a posted total -- is
    checked; an untaxed one keeps today's path. The posted totals are optional:
    a platform checks what it posts.
    """
    posted_rates = list(tax_by_rate) if tax_by_rate is not None else None
    posted = [m for _, m in posted_rates or ()] + ([tax_total] if tax_total else [])
    line_taxes = [tax for line in lines for tax in line.taxes]
    if not any(t.amount.amount_minor for t in line_taxes) and not any(
        m.amount_minor for m in posted
    ):
        return

    currency = total.currency
    amounts: Iterable[Money] = [
        *(line.amount for line in lines),
        *(tax.amount for tax in line_taxes),
        *posted,
        *([excluding_tax] if excluding_tax else []),
    ]
    if any(m.currency != currency for m in amounts):
        raise ProviderUnsupported("tax_mismatch")

    lines_excluding = sum((line.amount.amount_minor for line in lines), 0)
    by_rate: dict[Decimal, int] = {}
    for tax in line_taxes:
        if tax.amount.amount_minor:
            rate = canonical_rate(tax.rate)
            by_rate[rate] = by_rate.get(rate, 0) + tax.amount.amount_minor
    lines_tax = sum(by_rate.values(), 0)

    if excluding_tax is not None and excluding_tax.amount_minor != lines_excluding:
        raise ProviderUnsupported("tax_mismatch")
    if posted_rates is not None:
        posted_by_rate: dict[Decimal, int] = {}
        for rate, m in posted_rates:
            if m.amount_minor:
                key = canonical_rate(rate)
                posted_by_rate[key] = posted_by_rate.get(key, 0) + m.amount_minor
        if posted_by_rate != by_rate:
            raise ProviderUnsupported("tax_mismatch")
    if tax_total is not None and tax_total.amount_minor != lines_tax:
        raise ProviderUnsupported("tax_mismatch")
    if total.amount_minor != lines_excluding + lines_tax:
        raise ProviderUnsupported("tax_mismatch")
