"""Pennylane's payloads onto the domain (specs/014-pennylane-read, SC-001).
Payload shapes are written from the v2 reference; no real data."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.domain.enums import InvoiceStatus, PaymentStatus, SubscriptionStatus
from app.domain.money import Money
from app.providers.base import ProviderUnsupported
from app.providers.pennylane import mapping


def refused(reason):
    return pytest.raises(ProviderUnsupported, match=f"^{reason}$")


def line(**changes):
    base = {
        "id": 11, "label": "Abonnement Pro", "quantity": "1",
        "raw_currency_unit_price": "100.00", "currency_amount_before_tax": "100.00",
        "currency_tax": "20.00", "currency_amount": "120.00", "vat_rate": "FR_200",
        "discount": {"type": "relative", "value": "0"},
        "imputation_dates": {"start_date": "2026-09-01", "end_date": "2026-09-30"},
    }
    return {**base, **changes}


def invoice(lines=None, **changes):
    base = {
        "id": 42, "invoice_number": "F20260042", "customer": {"id": 7, "url": "x"},
        "date": "2026-09-01", "status": "paid", "currency": "EUR",
        "currency_amount": "120.00", "currency_amount_before_tax": "100.00",
        "currency_tax": "20.00", "discount": {"type": "absolute", "value": "0"},
        "credited_invoice": None, "billing_subscription": {"id": 9},
    }
    return mapping.invoice({**base, **changes}, lines if lines is not None else [line()])


def eur(minor):
    return Money(amount_minor=minor, currency="EUR")


class TestIdentifiers:
    def test_typed_by_kind(self):
        assert mapping.typed("invoice", 42) == "pl_inv_42"
        assert mapping.typed("customer", 42) == "pl_cus_42"

    def test_back_to_the_integer_for_its_kind_only(self):
        assert mapping.raw_id("pl_inv_42", "invoice") == 42
        assert mapping.raw_id("pl_cus_42", "invoice") is None
        assert mapping.raw_id("in_42", "invoice") is None
        assert mapping.raw_id("F20260042", "invoice") is None
        assert mapping.raw_id("pl_inv_042", "invoice") is None

    @pytest.mark.parametrize("raw", [None, "42", 0, -1, True])
    def test_an_id_that_is_not_a_positive_integer(self, raw):
        with refused("missing_field"):
            mapping.typed("invoice", raw)


class TestDates:
    def test_midnight_in_paris_in_winter_and_summer(self):
        assert mapping.paris_midnight(date(2026, 1, 15)) == datetime(2026, 1, 14, 23, tzinfo=UTC)
        assert mapping.paris_midnight(date(2026, 7, 15)) == datetime(2026, 7, 14, 22, tzinfo=UTC)

    @pytest.mark.parametrize("day", [date(2026, 3, 28), date(2026, 10, 24)])
    def test_the_day_after_across_a_change_of_time(self, day):
        """28 March and 24 October 2026 are the eves of the changes."""
        after = mapping.day_after(day)
        assert after == mapping.paris_midnight(date(day.year, day.month, day.day + 1))


class TestCustomers:
    def test_one_address(self):
        found = mapping.customer({"id": 7, "name": "Vega", "emails": [" lea@vega.fr "]})
        assert (found.id, found.email, found.currency) == ("pl_cus_7", "lea@vega.fr", None)

    def test_two_addresses_are_refused(self):
        with refused("multiple_addresses"):
            mapping.customer({"id": 7, "emails": ["a@x.fr", "b@x.fr"]})

    @pytest.mark.parametrize("emails", [[], None, [" "]])
    def test_no_address(self, emails):
        with refused("missing_field"):
            mapping.customer({"id": 7, "emails": emails})


class TestInvoices:
    def test_a_taxed_invoice(self):
        read = invoice()
        assert (read.id, read.customer_id, read.subscription_id, read.number) == (
            "pl_inv_42", "pl_cus_7", "pl_sub_9", "F20260042")
        assert read.total == eur(12000)
        assert read.tax_by_rate == {Decimal(20): eur(2000)}
        assert read.issued_at == datetime(2026, 8, 31, 22, tzinfo=UTC)

    def test_the_end_date_is_included(self):
        only = invoice().lines[0]
        assert only.period_start == datetime(2026, 8, 31, 22, tzinfo=UTC)
        assert only.period_end == datetime(2026, 9, 30, 22, tzinfo=UTC)  # 1 October, Paris

    @pytest.mark.parametrize(("status", "expected"), [
        ("paid", InvoiceStatus.PAID), ("upcoming", InvoiceStatus.OPEN),
        ("late", InvoiceStatus.OPEN), ("draft", InvoiceStatus.DRAFT),
        ("cancelled", InvoiceStatus.VOID)])
    def test_the_status_table(self, status, expected):
        assert invoice(status=status).status is expected

    @pytest.mark.parametrize("status", [
        "partially_paid", "partially_cancelled", "archived", "incomplete", "something_new",
        "credit_note", "proforma", "estimate_pending"])
    def test_other_statuses_and_documents_are_refused(self, status):
        with refused("invoice_status"):
            invoice(status=status)

    def test_a_credit_note_is_never_an_invoice(self):
        with refused("invoice_status"):
            invoice(credited_invoice={"id": 41}, status="cancelled")

    def test_an_invoice_without_lines(self):
        with refused("missing_field"):
            invoice(lines=[])

    @pytest.mark.parametrize("where", ["invoice", "line"])
    def test_a_discount(self, where):
        with refused("invoice_discount"):
            if where == "invoice":
                invoice(discount={"type": "relative", "value": "10"})
            else:
                invoice([line(discount={"type": "absolute", "value": "5.00"})])

    def test_figures_that_do_not_reconcile(self):
        with refused("tax_mismatch"):
            invoice(currency_amount="121.00")

    def test_a_negative_line(self):
        with refused("negative_line"):
            invoice([line(currency_amount_before_tax="-100.00")])

    def test_a_unit_price_finer_than_a_cent(self):
        with refused("fractional_unit_price"):
            invoice([line(raw_currency_unit_price="100.005")])

    def test_a_line_without_imputation_dates_has_no_period(self):
        assert invoice([line(imputation_dates=None)]).lines[0].period_start is None


class TestVat:
    @pytest.mark.parametrize(("code", "rate", "tax"), [
        ("FR_200", "20", "20.00"), ("FR_100", "10", "10.00"),
        ("FR_55", "5.5", "5.50"), ("FR_21", "2.1", "2.10")])
    def test_the_rates_read(self, code, rate, tax):
        read = invoice([line(vat_rate=code, currency_tax=tax)],
                       currency_tax=tax, currency_amount=str(Decimal("100.00") + Decimal(tax)))
        assert read.tax_by_rate == {Decimal(rate): Money.from_decimal(Decimal(tax), "EUR")}

    def test_exempt_charges_nothing(self):
        read = invoice([line(vat_rate="exempt", currency_tax="0.00")],
                       currency_tax="0.00", currency_amount="100.00")
        assert not read.taxed
        assert read.lines[0].taxes == ()  # zero entries dropped on an untaxed invoice

    def test_exempt_with_tax_does_not_reconcile(self):
        with refused("tax_mismatch"):
            invoice([line(vat_rate="exempt")])

    @pytest.mark.parametrize("code", ["mixed", "intracom_21", "FR_85", "crossborder", None])
    def test_other_codes_are_refused(self, code):
        with refused("tax_code"):
            invoice([line(vat_rate=code)])


class TestPayments:
    @pytest.mark.parametrize(("status", "expected"), [
        ("confirmed", PaymentStatus.SUCCEEDED), ("paid_out", PaymentStatus.SUCCEEDED),
        ("refunded", PaymentStatus.REFUNDED), ("failed", PaymentStatus.FAILED),
        ("aborted", PaymentStatus.FAILED), ("error", PaymentStatus.FAILED),
        ("cancelled", PaymentStatus.FAILED), ("customer_approval_denied", PaymentStatus.FAILED),
        ("initiated", PaymentStatus.PENDING), ("pending", PaymentStatus.PENDING),
        ("emitted", PaymentStatus.PENDING), ("prepared", PaymentStatus.PENDING),
        ("submitted", PaymentStatus.PENDING), ("pending_submission", PaymentStatus.PENDING),
        ("pending_customer_approval", PaymentStatus.PENDING),
        ("resubmission_requested", PaymentStatus.PENDING), ("draft", PaymentStatus.PENDING),
        ("found", PaymentStatus.PENDING), ("not_found", PaymentStatus.PENDING)])
    def test_every_row_of_the_table(self, status, expected):
        read = mapping.payment(
            {"id": 3, "status": status, "currency": "EUR", "currency_amount": "120.00"},
            "pl_inv_42")
        assert (read.id, read.invoice_id, read.status, read.amount, read.received_at) == (
            "pl_pay_3", "pl_inv_42", expected, eur(12000), None)

    def test_every_row_says_where_it_comes_from(self):
        assert all(source for _, source in mapping.PAYMENT_STATUS.values())

    @pytest.mark.parametrize("status", ["charged_back", "something_new", None])
    def test_other_statuses_are_refused(self, status):
        with refused("payment_status"):
            mapping.payment({"id": 3, "status": status, "currency": "EUR",
                             "currency_amount": "1.00"}, "pl_inv_42")

    def test_a_payment_without_its_currency(self):
        with refused("missing_field"):
            mapping.payment({"id": 3, "status": "confirmed", "currency": None,
                             "currency_amount": "1.00"}, "pl_inv_42")


def subscription(**changes):
    base = {
        "id": 9, "status": "in_progress", "label": "Pro mensuel", "customer": {"id": 7},
        "start": "2026-01-01", "finish": None, "stopped_at": None,
        "prev_occurrence": "2026-09-01", "next_occurrence": "2026-10-01",
        "recurring_rule": {"rule_type": "monthly", "interval": 1},
    }
    return mapping.subscription({**base, **changes})


class TestSubscriptions:
    def test_in_progress(self):
        read = subscription()
        assert (read.status, read.cancelled_at, read.billing_interval,
                read.billing_interval_count, read.plan_code) == (
            SubscriptionStatus.ACTIVE, None, "month", 1, "Pro mensuel")
        assert read.current_period_start == datetime(2026, 8, 31, 22, tzinfo=UTC)
        assert read.current_period_end == datetime(2026, 9, 30, 22, tzinfo=UTC)
        assert read.billing_anchor == datetime(2025, 12, 31, 23, tzinfo=UTC)

    def test_stopped_ends_the_day_before_the_next_renewal(self):
        read = subscription(status="stopped", stopped_at="2026-09-15T10:00:00Z")
        assert read.status is SubscriptionStatus.CANCELLED
        assert read.cancelled_at == datetime(2026, 9, 30, 22, tzinfo=UTC)  # 1 October, Paris

    def test_finished_ends_at_finish_included(self):
        read = subscription(status="finished", finish="2026-10-24",
                            prev_occurrence="2026-10-01", next_occurrence="2026-11-01")
        assert read.cancelled_at == mapping.paris_midnight(date(2026, 10, 25))

    @pytest.mark.parametrize("status", ["draft", "pending", "not_started", "unknown"])
    def test_other_statuses(self, status):
        with refused("subscription_status"):
            subscription(status=status)

    def test_a_span_of_two_months_is_refused(self):
        with refused("missing_field"):
            subscription(prev_occurrence="2026-08-01", next_occurrence="2026-10-01")

    def test_every_two_months_and_weekly(self):
        assert subscription(prev_occurrence="2026-08-01",
                            recurring_rule={"rule_type": "monthly", "interval": 2}
                            ).billing_interval_count == 2
        assert subscription(prev_occurrence="2026-09-24",
                            recurring_rule={"rule_type": "weekly", "interval": 1}
                            ).billing_interval == "week"

    @pytest.mark.parametrize("rule", [None, {"rule_type": "daily", "interval": 1},
                                      {"rule_type": "monthly", "interval": 0}])
    def test_a_rule_the_slice_does_not_read(self, rule):
        with refused("missing_field"):
            subscription(recurring_rule=rule)

    def test_without_a_next_renewal(self):
        with refused("missing_field"):
            subscription(status="stopped", next_occurrence=None)


class TestReferences:
    def invoices(self, *numbers):
        return [invoice(id=n, invoice_number=number) for n, number in enumerate(numbers, 1)]

    def test_a_whole_number_case_ignored(self):
        found = mapping.references("Ma facture f2026-0042 est fausse",
                                   self.invoices("F2026-0042"))
        assert found == [(11, 21, "pl_inv_1")]

    def test_not_inside_a_longer_word(self):
        assert mapping.references("XF2026-00421", self.invoices("F2026-0042")) == []

    def test_the_longest_number_first(self):
        found = mapping.references("F2026-00421", self.invoices("F2026-0042", "F2026-00421"))
        assert found == [(0, 11, "pl_inv_2")]

    def test_a_number_two_invoices_share_designates_neither(self):
        assert mapping.references("F1", self.invoices("F1", "F1")) == []

    def test_a_number_not_among_the_invoices_is_not_recognised(self):
        assert mapping.references("F9999", self.invoices("F1")) == []


class TestPeriods:
    def test_fully_dated(self):
        assert mapping.fully_dated(invoice())
        assert not mapping.fully_dated(invoice([line(), line(id=12, imputation_dates=None)],
                                               currency_amount="240.00",
                                               currency_amount_before_tax="200.00",
                                               currency_tax="40.00"))

    def test_reversed_dates_are_not_a_period(self):
        reversed_ = invoice([line(imputation_dates={"start_date": "2026-09-30",
                                                    "end_date": "2026-09-01"})])
        assert not mapping.fully_dated(reversed_)
