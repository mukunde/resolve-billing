"""Tax in the domain and the reconciliation every mapper runs
(specs/013-invoice-tax, FR-001, FR-002)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.enums import InvoiceStatus
from app.domain.models import Invoice, InvoiceLine, LineTax, TaxBreakdown
from app.domain.money import Money
from app.providers.base import ProviderUnsupported
from app.providers.tax import reconcile, untaxed_unless_charged

AT = datetime(2026, 9, 1, tzinfo=UTC)


def eur(minor: int, currency: str = "EUR") -> Money:
    return Money(amount_minor=minor, currency=currency)


def line(excl: int, *taxes: tuple[str, int], currency: str = "EUR", reason=None) -> InvoiceLine:
    return InvoiceLine(
        description="Plan",
        quantity=Decimal(1),
        unit_amount=eur(excl, currency),
        amount=eur(excl, currency),
        taxes=tuple(
            LineTax(amount=eur(amount, currency), rate=Decimal(rate), reason=reason)
            for rate, amount in taxes
        ),
    )


def invoice(*lines: InvoiceLine, total: int) -> Invoice:
    return Invoice(
        id="inv_t",
        customer_id="cus_t",
        subscription_id="sub_t",
        total=eur(total),
        status=InvoiceStatus.OPEN,
        issued_at=AT,
        lines=list(lines),
    )


class TestTheDomain:
    def test_an_untaxed_line_serialises_as_before(self):
        assert "taxes" not in line(10000).model_dump()

    def test_a_taxed_line_serialises_its_tax(self):
        dumped = line(10000, ("20", 2000)).model_dump(mode="json")
        assert dumped["taxes"] == [
            {"amount": {"amount_minor": 2000, "currency": "EUR"}, "rate": "20", "reason": None}
        ]

    def test_amounts_derived_from_lines(self):
        inv = invoice(line(10000, ("20", 2000)), line(5000, ("5.5", 275)), total=17275)
        assert inv.taxed
        assert inv.amount_excluding_tax == eur(15000)
        assert inv.tax_by_rate == {Decimal("5.5"): eur(275), Decimal("20"): eur(2000)}
        assert inv.tax_total == eur(2275)

    def test_one_rate_however_written_and_whatever_the_order(self):
        one = invoice(line(100, ("20.0", 20)), line(200, ("20", 40)), total=360)
        two = invoice(line(200, ("20.00", 40)), line(100, ("20", 20)), total=360)
        assert one.tax_by_rate == two.tax_by_rate == {Decimal("20"): eur(60)}

    def test_zero_tax_is_not_taxed(self):
        inv = invoice(line(10000, ("0", 0), reason="reverse_charge"), total=10000)
        assert not inv.taxed
        assert inv.tax_by_rate == {}

    def test_the_breakdown(self):
        inv = invoice(line(10000, ("20", 2000)), total=12000)
        assert TaxBreakdown.of(inv) == TaxBreakdown(
            excluding_tax=eur(10000), taxes=((Decimal("20"), eur(2000)),), total=eur(12000)
        )
        assert TaxBreakdown.of(invoice(line(10000), total=10000)) is None


class TestTheReconciliation:
    def test_an_untaxed_invoice_is_not_checked(self):
        # Today's path: an untaxed invoice whose total is not its lines' sum
        # is not refused here.
        reconcile([line(100)], eur(999))

    def test_a_consistent_taxed_invoice(self):
        reconcile(
            [line(10000, ("20", 2000)), line(5000, ("5.5", 275))],
            eur(17275),
            excluding_tax=eur(15000),
            tax_by_rate=[(Decimal("20"), eur(2000)), (Decimal("5.5"), eur(275))],
            tax_total=eur(2275),
        )

    @pytest.mark.parametrize(
        "posted",
        [
            {"excluding_tax": eur(14999)},
            {"tax_by_rate": [(Decimal("20"), eur(2275))]},
            {"tax_by_rate": [(Decimal("20"), eur(2000))]},
            {"tax_total": eur(2276)},
        ],
    )
    def test_posted_figures_that_disagree(self, posted):
        with pytest.raises(ProviderUnsupported) as refused:
            reconcile(
                [line(10000, ("20", 2000)), line(5000, ("5.5", 275))], eur(17275), **posted
            )
        assert refused.value.reason == "tax_mismatch"

    def test_a_total_that_is_not_excluding_tax_plus_tax(self):
        with pytest.raises(ProviderUnsupported, match="tax_mismatch"):
            reconcile([line(10000, ("20", 2000))], eur(12001))

    def test_tax_posted_with_no_line_carrying_it(self):
        with pytest.raises(ProviderUnsupported, match="tax_mismatch"):
            reconcile([line(10000)], eur(12000), tax_total=eur(2000))

    def test_tax_on_an_invoice_without_lines(self):
        with pytest.raises(ProviderUnsupported, match="tax_mismatch"):
            reconcile([], eur(2000), tax_by_rate=[(Decimal("20"), eur(2000))])

    def test_a_tax_in_another_currency(self):
        mixed = line(10000).model_copy(
            update={"taxes": (LineTax(amount=eur(2000, "USD"), rate=Decimal(20)),)}
        )
        with pytest.raises(ProviderUnsupported, match="tax_mismatch"):
            reconcile([mixed], eur(12000))

    def test_a_taxed_invoice_with_a_line_in_another_currency(self):
        with pytest.raises(ProviderUnsupported, match="tax_mismatch"):
            reconcile([line(10000, ("20", 2000)), line(500, currency="USD")], eur(12500))

    def test_zero_entries_are_dropped_on_an_untaxed_invoice(self):
        lines = untaxed_unless_charged([line(100, ("0", 0), reason="reverse_charge")])
        assert lines[0].taxes == ()

    def test_zero_entries_are_kept_on_a_taxed_invoice(self):
        lines = untaxed_unless_charged(
            [line(100, ("20", 20)), line(50, ("0", 0), reason="reverse_charge")]
        )
        assert lines[1].taxes[0].reason == "reverse_charge"


def test_a_breakdown_survives_its_record():
    inv = invoice(line(10000, ("20", 2000)), line(5000, ("5.5", 275)), total=17275)
    breakdown = TaxBreakdown.of(inv)
    assert TaxBreakdown.from_record(breakdown.to_record()) == breakdown
    assert breakdown.to_record()["taxes"] == [
        {"rate": "5.5", "amount_minor": 275},
        {"rate": "20", "amount_minor": 2000},
    ]


def test_an_invoice_number_is_serialised_only_when_set():
    """specs/014-pennylane-read, FR-009: the fake's and Stripe's invoices,
    which carry none, dump as before."""
    plain = invoice(line(100), total=100)
    assert "number" not in plain.model_dump()
    numbered = plain.model_copy(update={"number": "F20260042"})
    assert numbered.model_dump()["number"] == "F20260042"
