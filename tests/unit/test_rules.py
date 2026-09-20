"""ADR-003: every rule gets a case that fires and a case that must not.

The must-not cases matter more. An agent that finds a duplicate everywhere is
worse than no agent at all -- it refunds by reflex. Scenario B of the demo
exists for exactly this reason.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain import injection as injection_module
from app.domain import rules as rules_module
from app.domain.enums import InvoiceStatus, RuleCode, Severity, SubscriptionStatus
from app.domain.models import Invoice, InvoiceLine, Subscription
from app.domain.money import Money
from app.domain.rules import (
    detect_duplicate_invoice,
    invoice_after_cancellation,
    proration_mismatch,
    suspicious_usage_delta,
    unknown_invoice,
)

SEPT = datetime(2026, 9, 1, tzinfo=UTC)
OCT = datetime(2026, 10, 1, tzinfo=UTC)


def eur(minor: int) -> Money:
    return Money(amount_minor=minor, currency="EUR")


def line(
    amount_minor: int,
    *,
    unit_minor: int | None = None,
    start: datetime | None = SEPT,
    end: datetime | None = OCT,
    description: str = "Abonnement Enterprise",
) -> InvoiceLine:
    return InvoiceLine(
        description=description,
        quantity=Decimal(1),
        unit_amount=eur(unit_minor if unit_minor is not None else amount_minor),
        amount=eur(amount_minor),
        period_start=start,
        period_end=end,
    )


def invoice(
    invoice_id: str,
    *,
    total_minor: int = 9900,
    subscription_id: str | None = "sub_77",
    status: InvoiceStatus = InvoiceStatus.PAID,
    issued_at: datetime = SEPT,
    lines: list[InvoiceLine] | None = None,
) -> Invoice:
    return Invoice(
        id=invoice_id,
        customer_id="cus_acme",
        subscription_id=subscription_id,
        total=eur(total_minor),
        status=status,
        issued_at=issued_at,
        lines=lines if lines is not None else [line(total_minor)],
    )


def subscription(
    *,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    cancelled_at: datetime | None = None,
) -> Subscription:
    return Subscription(
        id="sub_77",
        customer_id="cus_acme",
        status=status,
        plan_code="enterprise_monthly",
        current_period_start=SEPT,
        current_period_end=OCT,
        cancelled_at=cancelled_at,
    )


class TestDuplicateInvoice:
    def test_fires_on_same_period_same_amount(self):
        """Demo case A: ACME billed twice for September."""
        first = invoice("inv_001")
        second = invoice("inv_002")

        finding = detect_duplicate_invoice(second, [first, second])

        assert finding is not None
        assert finding.code is RuleCode.DUPLICATE_INVOICE
        assert finding.severity is Severity.HIGH
        assert finding.evidence["invoice_1"] == "inv_001"
        assert finding.evidence["invoice_2"] == "inv_002"

    def test_silent_on_legitimate_overage(self):
        """Demo case B: subscription plus a real usage overage.

        Two charges in one month, neither of them a duplicate. This is the
        test that keeps the agent from refunding by reflex.
        """
        subscription_invoice = invoice("inv_010", total_minor=9900)
        overage = invoice("inv_011", total_minor=4200)

        assert detect_duplicate_invoice(overage, [subscription_invoice, overage]) is None

    def test_silent_when_periods_do_not_overlap(self):
        august = invoice(
            "inv_008",
            lines=[line(9900, start=datetime(2026, 8, 1, tzinfo=UTC), end=SEPT)],
        )
        september = invoice("inv_009")

        assert detect_duplicate_invoice(september, [august, september]) is None

    def test_silent_on_a_different_subscription(self):
        other = invoice("inv_020", subscription_id="sub_88")
        target = invoice("inv_021")

        assert detect_duplicate_invoice(target, [other, target]) is None

    def test_silent_when_the_twin_was_voided(self):
        """A cancelled duplicate never took the customer's money."""
        voided = invoice("inv_030", status=InvoiceStatus.VOID)
        target = invoice("inv_031")

        assert detect_duplicate_invoice(target, [voided, target]) is None

    def test_does_not_match_itself(self):
        target = invoice("inv_040")
        assert detect_duplicate_invoice(target, [target]) is None


class TestInvoiceAfterCancellation:
    def test_fires_when_billed_past_cancellation(self):
        sub = subscription(
            status=SubscriptionStatus.CANCELLED,
            cancelled_at=datetime(2026, 8, 15, tzinfo=UTC),
        )

        finding = invoice_after_cancellation(invoice("inv_050"), sub)

        assert finding is not None
        assert finding.code is RuleCode.INVOICE_AFTER_CANCELLATION
        assert finding.evidence["cancelled_at"] == "2026-08-15T00:00:00+00:00"

    def test_silent_on_the_final_period_before_cancellation(self):
        sub = subscription(
            status=SubscriptionStatus.CANCELLED,
            cancelled_at=datetime(2026, 9, 20, tzinfo=UTC),
        )

        assert invoice_after_cancellation(invoice("inv_051"), sub) is None

    def test_silent_on_an_active_subscription(self):
        assert invoice_after_cancellation(invoice("inv_052"), subscription()) is None


class TestSuspiciousUsageDelta:
    def test_fires_beyond_tolerance(self):
        finding = suspicious_usage_delta(Decimal(1000), Decimal(5000))

        assert finding is not None
        assert finding.code is RuleCode.SUSPICIOUS_USAGE_DELTA
        assert finding.evidence["relative_delta"] == "4"

    def test_silent_within_tolerance(self):
        assert suspicious_usage_delta(Decimal(1000), Decimal(1100)) is None

    def test_fires_when_nothing_was_expected_but_something_was_billed(self):
        finding = suspicious_usage_delta(Decimal(0), Decimal(50))

        assert finding is not None
        assert finding.evidence["relative_delta"] is None

    def test_silent_when_nothing_expected_and_nothing_billed(self):
        assert suspicious_usage_delta(Decimal(0), Decimal(0)) is None


class TestProrationMismatch:
    def test_fires_when_a_half_month_is_billed_in_full(self):
        """Demo case D: plan change mid-month, full month charged."""
        half_month = line(
            9900,
            unit_minor=9900,
            start=datetime(2026, 9, 16, tzinfo=UTC),
            end=OCT,
            description="Enterprise, du 16/09 au 30/09",
        )

        finding = proration_mismatch(invoice("inv_060", lines=[half_month]), subscription())

        assert finding is not None
        assert finding.code is RuleCode.PRORATION_MISMATCH
        assert finding.evidence["expected_minor"] == 4950
        assert finding.evidence["billed_minor"] == 9900

    def test_silent_on_correct_proration(self):
        half_month = line(
            4950,
            unit_minor=9900,
            start=datetime(2026, 9, 16, tzinfo=UTC),
            end=OCT,
        )

        assert proration_mismatch(invoice("inv_061", lines=[half_month]), subscription()) is None

    def test_tolerates_one_minor_unit_of_provider_rounding(self):
        third = line(
            3300,
            unit_minor=9900,
            start=datetime(2026, 9, 21, tzinfo=UTC),
            end=OCT,  # 10 of 30 days -> 3300
        )

        assert proration_mismatch(invoice("inv_062", lines=[third]), subscription()) is None

    def test_silent_on_a_full_period_line(self):
        """Full-period lines are not prorated; a different rule's business."""
        assert proration_mismatch(invoice("inv_063"), subscription()) is None


class TestUnknownInvoice:
    def test_fires_on_an_unknown_reference(self):
        finding = unknown_invoice("inv_999", [invoice("inv_001")])

        assert finding is not None
        assert finding.code is RuleCode.UNKNOWN_INVOICE
        assert finding.evidence["known_invoice_ids"] == ["inv_001"]

    def test_silent_on_a_known_reference(self):
        assert unknown_invoice("inv_001", [invoice("inv_001")]) is None


@pytest.mark.parametrize("code", list(RuleCode))
def test_every_rule_code_is_emitted_somewhere(code):
    """No code may sit in the enum with no rule behind it.

    The audit log, the action policy and the evaluation suite all key off
    these codes; a declared-but-never-emitted one is a dead branch waiting to
    confuse whoever reads a run two months from now.
    """
    sources = "".join(
        (Path(module.__file__).read_text(encoding="utf-8"))
        for module in (rules_module, injection_module)
    )
    assert f"RuleCode.{code.name}" in sources
