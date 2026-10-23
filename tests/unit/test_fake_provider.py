"""The fake platform, and the mapping it performs on the way in."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.enums import InvoiceStatus, SubscriptionStatus
from app.domain.money import Money
from app.providers.base import BillingProvider, ProviderRejected
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

    async def test_a_subscription_says_its_cadence_and_anchor(self, provider):
        subscription = await provider.get_subscription("sub_77")
        assert (subscription.billing_interval, subscription.billing_interval_count) == ("month", 1)
        assert subscription.billing_anchor == datetime(2026, 9, 1, tzinfo=UTC)

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

    async def test_the_owner_of_an_invoice_is_its_customer(self, provider):
        assert await provider.invoice_owner("inv_002") == "cus_acme"

    async def test_an_unknown_invoice_has_no_owner(self, provider):
        assert await provider.invoice_owner("inv_nope") is None

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


class TestUsageForAPeriod:
    """FR-016 as amended (option (a)): a record answers only for its own
    period, exactly -- not one it contains, not one next to it."""

    SEPTEMBER = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))

    async def test_the_record_for_exactly_that_period(self, provider):
        usage = await provider.get_usage("sub_88", self.SEPTEMBER)
        assert (usage.metric, usage.quantity) == ("api_call_blocks_100", Decimal("47"))

    async def test_nothing_for_another_period(self, provider):
        august = (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
        assert await provider.get_usage("sub_88", august) is None

    async def test_nothing_for_a_period_that_only_contains_the_record(self, provider):
        wider = (self.SEPTEMBER[0], datetime(2026, 11, 1, tzinfo=UTC))
        assert await provider.get_usage("sub_88", wider) is None

    async def test_nothing_for_another_subscription(self, provider):
        assert await provider.get_usage("sub_nope", self.SEPTEMBER) is None


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


class TestThePlatformCapsCorrections:
    """D6. The platform, not ResolveBilling, knows what an invoice can still
    give back, and refuses a write beyond it. The fake models that rule so it
    can be tested without a real platform; whether Stripe enforces the same
    is established against its sandbox (S7), not assumed. ADR-008."""

    async def test_credit_notes_cannot_exceed_the_invoice(self, provider):
        await provider.create_credit_note("inv_002", 9900, "EUR", "x", idempotency_key="a")

        with pytest.raises(ProviderRejected):
            await provider.create_credit_note("inv_002", 1, "EUR", "x", idempotency_key="b")

    async def test_a_refund_after_a_full_credit_note_is_refused(self, provider):
        """99 EUR credited, then 99 EUR refunded, on a 99 EUR invoice: the
        customer would get 198 EUR back. Different keys, different types --
        only a cap on the invoice stops it."""
        await provider.create_credit_note("inv_002", 9900, "EUR", "x", idempotency_key="a")

        with pytest.raises(ProviderRejected):
            await provider.create_refund("inv_002", 9900, "EUR", "x", idempotency_key="b")

        assert len(provider.executed_actions) == 1

    async def test_a_replay_still_returns_the_original_at_the_cap(self, provider):
        first = await provider.create_credit_note("inv_002", 9900, "EUR", "x", idempotency_key="a")
        again = await provider.create_credit_note("inv_002", 9900, "EUR", "x", idempotency_key="a")

        assert again == first

    async def test_write_against_an_unknown_invoice_is_a_refusal(self, provider):
        """The platform answered, and nothing was created: a refusal, not an
        unknown outcome. See ADR-008."""
        with pytest.raises(ProviderRejected):
            await provider.create_credit_note("inv_nope", 100, "EUR", "x", idempotency_key="c")
        assert await provider.find_action("c") is None

    async def test_find_action_returns_what_a_key_produced(self, provider):
        action = await provider.create_credit_note("inv_001", 100, "EUR", "x", idempotency_key="e")
        assert await provider.find_action("e") == action
        assert await provider.find_action("never-used") is None

    async def test_created_at_is_timezone_aware(self, provider):
        action = await provider.create_credit_note("inv_001", 100, "EUR", "x", idempotency_key="d")
        assert action.created_at.tzinfo is UTC


class TestThePortsOwnErrors:
    """The port names the error for an unknown identifier and says whether a
    platform can write (specs/001-stripe-read-adapter, FR-001 and FR-003)."""

    async def test_an_unknown_identifier_raises_the_ports_not_found(self, provider):
        from app.providers.base import NotFound as PortNotFound

        for read in (provider.get_customer, provider.get_subscription, provider.get_invoice):
            with pytest.raises(PortNotFound):
                await read("nope")

    def test_the_old_import_path_still_works(self):
        from app.providers.base import NotFound as PortNotFound

        assert NotFound is PortNotFound

    def test_the_fake_can_write(self, provider):
        assert provider.can_write is True
