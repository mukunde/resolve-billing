"""Decision 31: money is proposed only when the code can stand behind the amount."""

from datetime import UTC, datetime

import pytest

from app.domain.enums import InvoiceStatus, PaymentStatus, RuleCode, Severity
from app.domain.models import Invoice, Payment, RuleFinding
from app.domain.money import Money
from app.policies.correction import correction_for

SEPT = datetime(2026, 9, 1, tzinfo=UTC)


def eur(minor: int) -> Money:
    return Money(amount_minor=minor, currency="EUR")


def invoice(status: InvoiceStatus = InvoiceStatus.PAID, total_minor: int = 9900) -> Invoice:
    return Invoice(
        id="inv_1",
        customer_id="cus_acme",
        subscription_id="sub_77",
        total=eur(total_minor),
        status=status,
        issued_at=SEPT,
    )


def payment(
    minor: int = 9900,
    status: PaymentStatus = PaymentStatus.SUCCEEDED,
    invoice_id: str = "inv_1",
    currency: str = "EUR",
) -> Payment:
    return Payment(
        id=f"pay_{minor}_{status.value}",
        invoice_id=invoice_id,
        amount=Money(amount_minor=minor, currency=currency),
        status=status,
    )


def finding(code: RuleCode) -> RuleFinding:
    return RuleFinding(code=code, severity=Severity.HIGH, description="", evidence={})


DUPLICATE = finding(RuleCode.DUPLICATE_INVOICE)
AFTER_CANCELLATION = finding(RuleCode.INVOICE_AFTER_CANCELLATION)
PRORATION = finding(RuleCode.PRORATION_MISMATCH)
USAGE = finding(RuleCode.SUSPICIOUS_USAGE_DELTA)


class TestWholeInvoice:
    """S5 provisional: the whole invoice is the error, so its total is the amount."""

    @pytest.mark.parametrize("found", [DUPLICATE, AFTER_CANCELLATION])
    def test_a_credit_note_is_the_invoice_total(self, found):
        correction = correction_for("credit_note", invoice(), [found], [payment()])
        assert correction.amount == eur(9900)
        assert correction.withheld is None

    def test_a_duplicate_after_cancellation_is_one_correction(self):
        """S3 counts corrections, not findings."""
        correction = correction_for(
            "credit_note", invoice(), [DUPLICATE, AFTER_CANCELLATION], [payment()]
        )
        assert correction.amount == eur(9900)

    def test_a_refund_needs_a_settled_payment_covering_it(self):
        correction = correction_for("refund", invoice(), [AFTER_CANCELLATION], [payment()])
        assert correction.amount == eur(9900)

    @pytest.mark.parametrize(
        "payments",
        [
            [],
            [payment(status=PaymentStatus.FAILED)],
            [payment(status=PaymentStatus.REFUNDED)],
            [payment(status=PaymentStatus.PENDING)],
            [payment(minor=4900)],
            [payment(invoice_id="inv_other")],
            [payment(currency="USD")],
        ],
        ids=["none", "failed", "refunded", "pending", "partial", "other-invoice", "currency"],
    )
    def test_no_refund_without_settled_money_to_return(self, payments):
        """S4: nothing to refund is a human's call, not a silent credit note."""
        correction = correction_for("refund", invoice(), [AFTER_CANCELLATION], payments)
        assert correction.amount is None
        assert "paiement" in correction.withheld


class TestExtraPayment:
    """One invoice collected twice: the extra payment is refunded."""

    def test_the_extra_payment_is_refunded(self):
        correction = correction_for("refund", invoice(), [], [payment(), payment()])
        assert correction.amount == eur(9900)

    def test_a_credit_note_does_not_correct_a_payment(self):
        correction = correction_for("credit_note", invoice(), [], [payment(), payment()])
        assert correction.amount is None
        assert "remboursement" in correction.withheld

    def test_a_refunded_second_payment_is_not_a_duplicate(self):
        payments = [payment(), payment(status=PaymentStatus.REFUNDED)]
        correction = correction_for("refund", invoice(), [], payments)
        assert correction.amount is None


class TestNoAmount:
    """Everything the code cannot put a number on goes to a human."""

    @pytest.mark.parametrize("found", [PRORATION, USAGE])
    def test_a_partial_correction_has_no_amount(self, found):
        correction = correction_for("credit_note", invoice(InvoiceStatus.OPEN), [found], [])
        assert correction.amount is None
        assert "partielle" in correction.withheld

    def test_no_finding_no_amount(self):
        """The engine alone cannot found a correction."""
        correction = correction_for("credit_note", invoice(), [], [payment()])
        assert correction.amount is None

    def test_two_distinct_corrections_have_no_amount(self):
        correction = correction_for("credit_note", invoice(), [DUPLICATE, PRORATION], [payment()])
        assert correction.amount is None
        assert "distinctes" in correction.withheld

    @pytest.mark.parametrize(
        "status", [InvoiceStatus.DRAFT, InvoiceStatus.UNCOLLECTIBLE, InvoiceStatus.VOID]
    )
    def test_an_invoice_that_cannot_be_corrected(self, status):
        correction = correction_for("credit_note", invoice(status), [DUPLICATE], [payment()])
        assert correction.amount is None
        assert correction.withheld
