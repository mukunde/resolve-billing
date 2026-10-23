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

    def test_a_subscription_saying_no_cadence_is_monthly_from_its_period_start(self):
        """The fake's convention, for fixture sets written before subscriptions
        said their cadence: monthly, anchored on the period's start."""
        from app.providers.fake.fixtures import _subscription

        raw = {
            "id": "sub_x",
            "customer_id": "cus_x",
            "state": "active",
            "plan": "pro_monthly",
            "period_start": "2026-09-01T00:00:00",
            "period_end": "2026-10-01T00:00:00",
        }
        subscription = _subscription(raw)
        assert (subscription.billing_interval, subscription.billing_interval_count) == ("month", 1)
        assert subscription.billing_anchor == subscription.current_period_start

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


LATER = datetime(2100, 1, 1, tzinfo=UTC)


async def credit(provider, invoice_id, amount_minor, key, *, allocation=None, send_by=LATER):
    """A credit note as the execution service makes one: planned, then sent."""
    if allocation is None:
        allocation = await provider.plan_write(invoice_id, "credit_note")
    return await provider.create_credit_note(
        invoice_id,
        amount_minor,
        "EUR",
        "x",
        idempotency_key=key,
        action_id=f"act_{key}",
        allocation=allocation,
        send_by=send_by,
    )


async def refund(provider, invoice_id, amount_minor, key, *, allocation="refund", send_by=LATER):
    return await provider.create_refund(
        invoice_id,
        amount_minor,
        "EUR",
        "x",
        idempotency_key=key,
        action_id=f"act_{key}",
        allocation=allocation,
        send_by=send_by,
    )


class TestIdempotentWrite:
    """The behaviour a real adapter must have, proven on the fake."""

    async def test_same_key_returns_the_original_action(self, provider):
        first = await credit(provider, "inv_002", 9900, "key-1")
        second = await credit(provider, "inv_002", 9900, "key-1")

        assert second.provider_action_id == first.provider_action_id
        assert len(provider.executed_actions) == 1

    async def test_different_keys_create_distinct_actions(self, provider):
        await credit(provider, "inv_001", 100, "a")
        await credit(provider, "inv_001", 100, "b")

        assert len(provider.executed_actions) == 2


class TestThePlatformCapsCorrections:
    """D6. The platform, not ResolveBilling, knows what an invoice can still
    give back, and refuses a write beyond it. The fake models that rule so it
    can be tested without a real platform; whether Stripe enforces the same
    is established against its sandbox (S7), not assumed. ADR-008."""

    async def test_credit_notes_cannot_exceed_the_invoice(self, provider):
        await credit(provider, "inv_002", 9900, "a")

        with pytest.raises(ProviderRejected):
            await credit(provider, "inv_002", 1, "b")

    async def test_a_refund_after_a_full_credit_note_is_refused(self, provider):
        """99 EUR credited, then 99 EUR refunded, on a 99 EUR invoice: the
        customer would get 198 EUR back. Different keys, different types --
        only a cap on the invoice stops it."""
        await credit(provider, "inv_002", 9900, "a")

        with pytest.raises(ProviderRejected):
            await refund(provider, "inv_002", 9900, "b")

        assert len(provider.executed_actions) == 1

    async def test_a_replay_still_returns_the_original_at_the_cap(self, provider):
        first = await credit(provider, "inv_002", 9900, "a")
        again = await credit(provider, "inv_002", 9900, "a")

        assert again == first

    async def test_write_against_an_unknown_invoice_is_a_refusal(self, provider):
        """The platform answered, and nothing was created: a refusal, not an
        unknown outcome. See ADR-008."""
        with pytest.raises(ProviderRejected):
            await credit(provider, "inv_nope", 100, "c", allocation="reduce_due")
        assert await provider.find_action("c", "inv_nope") == []

    async def test_created_at_is_timezone_aware(self, provider):
        action = await credit(provider, "inv_001", 100, "d")
        assert action.created_at.tzinfo is UTC


class TestAWriteIsPlannedOnce:
    """specs/002-stripe-writes, FR-010 to FR-012: how a write is allocated
    depends on the invoice's state, is decided once before the first send,
    and is never rebuilt by a retry."""

    async def test_a_credit_note_on_an_open_invoice_lowers_what_is_due(self, provider):
        assert await provider.plan_write("inv_020", "credit_note") == "reduce_due"

    async def test_a_credit_note_on_a_paid_invoice_credits_the_balance(self, provider):
        assert await provider.plan_write("inv_002", "credit_note") == "customer_balance"

    async def test_a_refund_on_a_paid_invoice_is_a_refund(self, provider):
        assert await provider.plan_write("inv_002", "refund") == "refund"

    async def test_a_refund_on_an_invoice_with_nothing_paid_is_refused(self, provider):
        with pytest.raises(ProviderRejected, match="invoice_state"):
            await provider.plan_write("inv_020", "refund")

    async def test_an_unknown_invoice_is_refused(self, provider):
        with pytest.raises(ProviderRejected):
            await provider.plan_write("inv_nope", "credit_note")

    async def test_a_write_the_invoice_no_longer_allows_is_refused(self, provider):
        """Planned on a paid invoice, sent once it is open again: refused,
        never written with another allocation."""
        with pytest.raises(ProviderRejected, match="invoice_state"):
            await credit(provider, "inv_020", 100, "k", allocation="customer_balance")
        assert provider.executed_actions == []

    async def test_nothing_is_sent_past_the_deadline(self, provider):
        from app.providers.base import NotSent

        past = datetime(2000, 1, 1, tzinfo=UTC)
        with pytest.raises(NotSent):
            await credit(provider, "inv_002", 100, "k", send_by=past)
        assert provider.executed_actions == []

    def test_the_platform_says_how_long_it_answers_a_key(self, provider):
        from datetime import timedelta

        assert provider.idempotency_window == timedelta(hours=23)


class TestWhatAWriteCarries:
    """FR-001 and FR-004: what the execution service checks a write against."""

    async def test_a_write_says_what_it_is(self, provider):
        made = await credit(provider, "inv_002", 9900, "k")

        assert made.invoice_id == "inv_002"
        assert (made.amount_minor, made.currency) == (9900, "EUR")
        assert made.action_type == "credit_note"
        assert made.allocation == "customer_balance"
        assert made.action_id == "act_k"
        assert made.idempotency_key == "k"
        assert made.test_mode is True
        assert made.status == "succeeded"
        assert made.observed_at.tzinfo is UTC

    async def test_find_action_answers_every_write_under_the_key_on_the_invoice(self, provider):
        made = await credit(provider, "inv_001", 100, "e")

        (found,) = await provider.find_action("e", "inv_001")
        assert found.provider_action_id == made.provider_action_id
        assert await provider.find_action("e", "inv_002") == []
        assert await provider.find_action("never-used", "inv_001") == []

    async def test_a_second_write_under_one_key_is_found_too(self, provider):
        made = await credit(provider, "inv_001", 100, "e")
        other = made.model_copy(update={"provider_action_id": "cn_planted"})
        provider.plant_action(other)

        found = await provider.find_action("e", "inv_001")
        assert [a.provider_action_id for a in found] == [made.provider_action_id, "cn_planted"]

    async def test_a_status_observed_later_is_the_platforms(self, provider):
        made = await refund(provider, "inv_002", 9900, "r")
        provider.set_status(made.provider_action_id, "failed")

        (seen,) = await provider.find_action("r", "inv_002")
        assert seen.status == "failed"
        assert seen.observed_at >= made.observed_at


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
