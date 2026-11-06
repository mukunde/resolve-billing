"""Rules, corrections and the fake platform on taxed invoices
(specs/013-invoice-tax, FR-004, FR-005, FR-006, FR-011a)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.enums import InvoiceStatus, PaymentStatus, RuleCode, SubscriptionStatus
from app.domain.models import Invoice, InvoiceLine, LineTax, Payment, Subscription
from app.domain.money import Money
from app.domain.rules import detect_duplicate_invoice, duplicate_payment, proration_mismatch
from app.policies.correction import correction_for
from app.providers.base import ProviderRejected, ProviderUnsupported
from app.providers.fake.adapter import FakeBillingProvider
from app.providers.fake.fixtures import FixtureSet, _invoice, _payment

SEPT = datetime(2026, 9, 1, tzinfo=UTC)
OCT = datetime(2026, 10, 1, tzinfo=UTC)
LATER = datetime(2100, 1, 1, tzinfo=UTC)


def eur(minor: int) -> Money:
    return Money(amount_minor=minor, currency="EUR")


def line(excl: int, *taxes: tuple[str, int], start=SEPT, end=OCT, unit=None) -> InvoiceLine:
    return InvoiceLine(
        description="Abonnement Pro",
        quantity=Decimal(1),
        unit_amount=eur(unit if unit is not None else excl),
        amount=eur(excl),
        period_start=start,
        period_end=end,
        taxes=tuple(LineTax(amount=eur(a), rate=Decimal(r)) for r, a in taxes),
    )


def invoice(invoice_id: str, *lines: InvoiceLine, total: int) -> Invoice:
    return Invoice(
        id=invoice_id,
        customer_id="cus_t",
        subscription_id="sub_t",
        total=eur(total),
        status=InvoiceStatus.PAID,
        issued_at=SEPT,
        lines=list(lines),
    )


class TestTheDuplicateRule:
    def test_a_taxed_duplicate_is_found(self):
        one = invoice("inv_a", line(10000, ("20", 2000)), total=12000)
        two = invoice("inv_b", line(10000, ("20", 2000)), total=12000)
        finding = detect_duplicate_invoice(two, [one, two])
        assert finding is not None and finding.code is RuleCode.DUPLICATE_INVOICE

    def test_the_same_total_with_different_amounts_excluding_tax_is_not(self):
        """US1-2: 100.00 + 20 % and 113.74 + 5.5 % are both 120.00."""
        one = invoice("inv_a", line(10000, ("20", 2000)), total=12000)
        two = invoice("inv_b", line(11374, ("5.5", 626)), total=12000)
        assert detect_duplicate_invoice(two, [one, two]) is None

    def test_equal_tax_split_differently_across_rates_is_not(self):
        """US1-3: same amount excluding tax, same total tax, other rates."""
        one = invoice(
            "inv_a", line(5000, ("20", 1000)), line(5000, ("5.5", 275)), total=11275
        )
        two = invoice(
            "inv_b", line(5000, ("10", 500)), line(5000, ("15.5", 775)), total=11275
        )
        assert detect_duplicate_invoice(two, [one, two]) is None

    def test_a_taxed_and_an_untaxed_invoice_of_the_same_total_are_not(self):
        one = invoice("inv_a", line(12000), total=12000)
        two = invoice("inv_b", line(10000, ("20", 2000)), total=12000)
        assert detect_duplicate_invoice(two, [one, two]) is None
        assert detect_duplicate_invoice(one, [one, two]) is None

    def test_the_order_of_lines_does_not_matter(self):
        one = invoice("inv_a", line(5000, ("20", 1000)), line(3000, ("5.5", 165)), total=9165)
        two = invoice("inv_b", line(3000, ("5.5", 165)), line(5000, ("20", 1000)), total=9165)
        assert detect_duplicate_invoice(two, [one, two]) is not None


class TestProrationAndPayments:
    def test_a_taxed_proration_is_measured_excluding_tax(self):
        """Half the period at a 100.00 plan is 50.00 excluding tax: right,
        whatever the 10.00 of tax on it."""
        sub = Subscription(
            id="sub_t",
            customer_id="cus_t",
            status=SubscriptionStatus.ACTIVE,
            plan_code="pro",
            current_period_start=SEPT,
            current_period_end=SEPT + timedelta(days=30),
            billing_interval="month",
            billing_interval_count=1,
            billing_anchor=SEPT,
        )
        half = line(
            5000, ("20", 1000), start=SEPT + timedelta(days=15), end=SEPT + timedelta(days=30),
            unit=10000,
        )
        assert proration_mismatch(invoice("inv_p", half, total=6000), sub) is None
        wrong = line(
            6000, ("20", 1200), start=SEPT + timedelta(days=15), end=SEPT + timedelta(days=30),
            unit=10000,
        )
        finding = proration_mismatch(invoice("inv_q", wrong, total=7200), sub)
        assert finding is not None and finding.evidence["billed_minor"] == 6000

    def test_a_payment_is_matched_including_tax(self):
        taxed = invoice("inv_t", line(4900, ("20", 980)), total=5880)
        payments = [
            Payment(id=f"py_{n}", invoice_id="inv_t", amount=eur(5880),
                    status=PaymentStatus.SUCCEEDED, received_at=SEPT)
            for n in (1, 2)
        ]
        assert duplicate_payment(taxed, payments) is not None
        assert correction_for("refund", taxed, [duplicate_payment(taxed, payments)],
                              payments).amount == eur(5880)


class TestTheCorrection:
    def test_a_whole_invoice_credit_is_the_total_including_tax(self):
        one = invoice("inv_a", line(10000, ("20", 2000)), total=12000)
        two = invoice("inv_b", line(10000, ("20", 2000)), total=12000)
        finding = detect_duplicate_invoice(two, [one, two])
        assert correction_for("credit_note", two, [finding], []).amount == eur(12000)


RAW_TAXED = {
    "id": "inv_tx",
    "customer_id": "cus_alice",
    "subscription_id": "sub_77",
    "amount": "120.00",
    "currency": "EUR",
    "state": "paid",
    "issued_at": "2026-09-01T09:00:00",
    "lines": [
        {
            "label": "Pro",
            "quantity": "1",
            "unit_amount": "100.00",
            "amount": "100.00",
            "period_start": "2026-09-01T00:00:00",
            "period_end": "2026-10-01T00:00:00",
            "taxes": [{"rate": "20", "amount": "20.00"}],
        }
    ],
}


class TestTheFakeReadsTax:
    def test_a_taxed_fixture(self):
        inv = _invoice(RAW_TAXED)
        assert inv.tax_by_rate == {Decimal("20"): eur(2000)}
        assert inv.amount_excluding_tax == eur(10000)

    def test_prices_including_tax_are_refused(self):
        with pytest.raises(ProviderUnsupported, match="tax_inclusive"):
            _invoice({**RAW_TAXED, "prices_include_tax": True})

    def test_a_total_that_does_not_reconcile_is_refused(self):
        with pytest.raises(ProviderUnsupported, match="tax_mismatch"):
            _invoice({**RAW_TAXED, "amount": "119.99"})

    def test_a_zero_tax_on_an_untaxed_fixture_is_dropped(self):
        raw = {
            **RAW_TAXED,
            "amount": "100.00",
            "lines": [
                {**RAW_TAXED["lines"][0],
                 "taxes": [{"rate": "0", "amount": "0.00", "reason": "reverse_charge"}]}
            ],
        }
        assert _invoice(raw).lines[0].taxes == ()


class TestTheFakeCapsATaxedInvoice:
    """FR-006: what a taxed invoice can give back is its total including tax,
    credit notes and refunds together."""

    @pytest.fixture
    def provider(self):
        fixtures = FixtureSet(
            customers=[],
            subscriptions=[],
            invoices=[_invoice(RAW_TAXED)],
            payments=[
                _payment({"id": "py_tx", "invoice_id": "inv_tx", "amount": "120.00",
                          "currency": "EUR", "state": "succeeded",
                          "received_at": "2026-09-02T00:00:00"})
            ],
            usage=[],
        )
        return FakeBillingProvider(fixtures)

    async def _write(self, provider, kind, amount, key):
        allocation = await provider.plan_write("inv_tx", kind)
        method = provider.create_credit_note if kind == "credit_note" else provider.create_refund
        return await method(
            "inv_tx", amount, "EUR", "x", idempotency_key=key, action_id=f"act_{key}",
            allocation=allocation, send_by=LATER,
        )

    async def test_a_credit_note_then_a_refund_stay_within_the_total(self, provider):
        await self._write(provider, "credit_note", 7000, "a")
        await self._write(provider, "refund", 5000, "b")
        with pytest.raises(ProviderRejected):
            await self._write(provider, "refund", 1, "c")
