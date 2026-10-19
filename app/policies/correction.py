"""What a financial correction is worth, or why no amount can be proposed.

The engine names the *kind* of correction. This module decides whether the
code can put a number on it. It only can when the whole invoice is the error
(`evals/taxonomy.md`, S5, provisional rule):

- a duplicated invoice, or an invoice for a period after cancellation: the
  whole invoice is corrected;
- an invoice collected at least twice, by distinct payments each equal to
  the total: the extra payment is refunded. Any other overpayment is not
  recognised here.

Everything else -- a mispriced proration, a usage gap, two distinct
corrections at once, an invoice that cannot be corrected -- goes to a human
without an amount. Proposing a figure the code cannot justify is how an
approver ends up signing a credit note for the wrong sum.

Amounts are always computed here, from the records, never by a model.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import InvoiceStatus, PaymentStatus, RuleCode
from app.domain.models import Invoice, Payment, RuleFinding
from app.domain.money import Money

#: Findings meaning the whole invoice should not exist.
WHOLE_INVOICE_FINDINGS = frozenset(
    {RuleCode.DUPLICATE_INVOICE, RuleCode.INVOICE_AFTER_CANCELLATION}
)

#: Findings meaning part of the invoice is wrong. No amount until the
#: correction-amount specification exists (S5).
PARTIAL_FINDINGS = {
    RuleCode.PRORATION_MISMATCH: "proration",
    RuleCode.SUSPICIOUS_USAGE_DELTA: "usage",
}

#: Invoice states on which no correction is proposed (S4).
NOT_CORRECTABLE = {
    InvoiceStatus.DRAFT: "facture en brouillon, pas encore émise",
    InvoiceStatus.UNCOLLECTIBLE: "facture irrécouvrable",
    InvoiceStatus.VOID: "facture annulée",
}


@dataclass(frozen=True)
class Correction:
    """An amount the code can stand behind, or the reason there is none."""

    amount: Money | None
    withheld: str | None = None


def correction_for(
    kind: str,
    invoice: Invoice,
    findings: list[RuleFinding],
    payments: list[Payment],
) -> Correction:
    """The amount of a `credit_note` or `refund` on `invoice`, if any."""
    if invoice.status in NOT_CORRECTABLE:
        return Correction(None, NOT_CORRECTABLE[invoice.status])
    if invoice.total.amount_minor <= 0:
        return Correction(None, "montant de facture nul ou négatif")

    settled = [
        p
        for p in payments
        if p.invoice_id == invoice.id and p.status is PaymentStatus.SUCCEEDED
    ]

    # Count corrections, not findings: a duplicate dated after cancellation
    # is still one invoice to cancel (S3).
    corrections: set[str] = set()
    if any(f.code in WHOLE_INVOICE_FINDINGS for f in findings):
        corrections.add("whole_invoice")
    corrections.update(PARTIAL_FINDINGS[f.code] for f in findings if f.code in PARTIAL_FINDINGS)
    if _collected_twice(invoice, settled):
        corrections.add("extra_payment")

    if not corrections:
        return Correction(None, "aucun constat ne fonde une correction chiffrable")
    if len(corrections) > 1:
        return Correction(
            None, "plusieurs corrections distinctes sur la même facture : " + ", ".join(
                sorted(corrections)
            )
        )

    (correction,) = corrections
    if correction in PARTIAL_FINDINGS.values():
        return Correction(
            None, f"correction partielle ({correction}) : montant non calculable à ce jour"
        )
    if correction == "extra_payment":
        if kind != "refund":
            return Correction(
                None, "le doublon porte sur le paiement : seul un remboursement le corrige"
            )
        return Correction(invoice.total)

    # The whole invoice is the error. A refund returns money one settled
    # payment actually brought in (S4): partial payments adding up to the
    # total are left to a human.
    if kind == "refund" and not any(_covers(p, invoice) for p in settled):
        return Correction(None, "aucun paiement encaissé ne couvre le remboursement")
    return Correction(invoice.total)


def _collected_twice(invoice: Invoice, settled: list[Payment]) -> bool:
    """At least two distinct settled payments, each the invoice's full total."""
    return len({p.id for p in settled if p.amount == invoice.total}) >= 2


def _covers(payment: Payment, invoice: Invoice) -> bool:
    return (
        payment.amount.currency == invoice.total.currency
        and payment.amount.amount_minor >= invoice.total.amount_minor
    )
