"""The fake platform, and the mapping it performs on the way in."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.enums import InvoiceStatus, SubscriptionStatus
from app.domain.money import Money
from app.providers.base import BillingProvider
from app.providers.fake.adapter import FakeBillingProvider, NotFound

EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


@pytest.fixture
def provider() -> FakeBillingProvider:
    return FakeBillingProvider()


def test_satisfies_the_port(provider):
    assert isinstance(provider, BillingProvider)


class TestMapping:
    """Fixtures are platform-shaped; the domain is not. This is the seam."""

    async def test_decimal_strings_become_minor_units(self, provider):
        invoice = await provider.get_invoice("inv_001")
        assert invoice.total == Money(amount_minor=9900, currency="EUR")

    async def test_platform_field_names_are_translated(self, provider):
        """`amount` -> total, `state` -> status, `label` -> description."""
        invoice = await provider.get_invoice("inv_001")
        assert invoice.status is InvoiceStatus.PAID
        assert invoice.lines[0].description.startswith("Abonnement Enterprise")

    async def test_quantities_stay_decimal(self, provider):
        invoice = await provider.get_invoice("inv_011")
        assert invoice.lines[0].quantity == Decimal(47)

    async def test_usage_metric_is_carried_onto_the_line(self, provider):
        invoice = await provider.get_invoice("inv_011")
        assert invoice.lines[0].metric == "api_call_blocks_100"

    async def test_cancellation_date_is_parsed(self, provider):
        sub = await provider.get_subscription("sub_55")
        assert sub.status is SubscriptionStatus.CANCELLED
        assert sub.cancelled_at == datetime(2026, 8, 20, 16, 30, tzinfo=UTC)


class TestReads:
    async def test_lookup_by_email_is_case_insensitive(self, provider):
        assert (await provider.find_customer_by_email("Alice@ACME.fr")).id == "cus_acme"

    async def test_unknown_email_returns_none(self, provider):
        assert await provider.find_customer_by_email("nobody@example.com") is None

    async def test_unknown_invoice_raises(self, provider):
        with pytest.raises(NotFound):
            await provider.get_invoice("inv_nope")

    async def test_find_invoice_returns_the_invoice(self, provider):
        assert (await provider.find_invoice("inv_002")).customer_id == "cus_acme"

    async def test_find_invoice_returns_none_on_an_unknown_reference(self, provider):
        assert await provider.find_invoice("inv_nope") is None

    async def test_lists_every_customer_with_its_display_name(self, provider):
        customers = {c.id: c for c in await provider.list_customers()}
        assert customers["cus_acme"].display_name == "ACME SAS"
        assert "cus_nord" in customers

    async def test_invoices_are_scoped_to_the_customer(self, provider):
        invoices = await provider.list_invoices("cus_acme", since=EPOCH)
        assert {i.id for i in invoices} == {"inv_001", "inv_002"}

    async def test_payments_are_scoped_to_the_customer(self, provider):
        payments = await provider.list_payments("cus_nord")
        assert {p.id for p in payments} == {"pay_010", "pay_011"}


class TestIdempotentWrite:
    """The behaviour a real adapter must have, proven on the fake."""

    async def test_same_key_returns_the_original_action(self, provider):
        first = await provider.create_credit_note(
            "inv_002", 9900, "EUR", "doublon", idempotency_key="key-1"
        )
        second = await provider.create_credit_note(
            "inv_002", 9900, "EUR", "doublon", idempotency_key="key-1"
        )

        assert second.provider_action_id == first.provider_action_id
        assert len(provider.executed_actions) == 1

    async def test_different_keys_create_distinct_actions(self, provider):
        await provider.create_credit_note("inv_001", 100, "EUR", "x", idempotency_key="a")
        await provider.create_credit_note("inv_001", 100, "EUR", "x", idempotency_key="b")

        assert len(provider.executed_actions) == 2

    async def test_write_against_an_unknown_invoice_raises(self, provider):
        with pytest.raises(NotFound):
            await provider.create_credit_note(
                "inv_nope", 100, "EUR", "x", idempotency_key="c"
            )

    async def test_created_at_is_timezone_aware(self, provider):
        action = await provider.create_credit_note(
            "inv_001", 100, "EUR", "x", idempotency_key="d"
        )
        assert action.created_at.tzinfo is UTC
