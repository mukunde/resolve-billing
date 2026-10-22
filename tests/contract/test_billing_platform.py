"""The billing port's contract, on every platform (spec, the ten properties).

The same assertions run against the fake, in CI, and against a seeded Stripe
test account, locally with the read-only key (see conftest). A property a
platform cannot exercise is skipped with the reason, never weakened.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.application.investigate_dispute import DisputeInvestigator
from app.decision.mock import MockDecisionEngine
from app.domain.enums import PaymentStatus, SubscriptionStatus
from app.domain.models import Dispute
from app.providers.base import (
    NotFound,
    PlatformCannotWrite,
    ProviderUnavailable,
    ProviderUnsupported,
)
from scripts import stripe_worlds as worlds
from tests.contract.conftest import EPOCH

pytestmark = pytest.mark.contract

#: Identifiers no platform holds, each in the form its platform would use.
MISSING_CUSTOMER = "cus_rbMissing0000000"
MISSING_SUBSCRIPTION = "sub_rbMissing0000000"
MISSING_INVOICE = "in_rbMissing00000000"


class TestOneAnUnknownIdentifier:
    async def test_a_get_raises_not_found(self, platform):
        provider = platform.provider
        for read, identifier in (
            (provider.get_customer, MISSING_CUSTOMER),
            (provider.get_subscription, MISSING_SUBSCRIPTION),
            (provider.get_invoice, MISSING_INVOICE),
        ):
            with pytest.raises(NotFound):
                await read(identifier)

    async def test_a_find_answers_none(self, platform):
        assert await platform.provider.find_invoice(MISSING_INVOICE) is None
        assert await platform.provider.find_customer_by_email("nobody@missing.test") is None


class TestTwoAReadThatCannotComplete:
    async def test_raises_provider_unavailable(self, platform):
        if platform.unreachable is None:
            pytest.skip(f"the {platform.name} platform cannot be made to fail a read")
        with pytest.raises(ProviderUnavailable):
            await platform.unreachable().get_customer(platform.customer_id)


class TestThreeTheAddressLookup:
    async def test_ignores_case_and_surrounding_spaces(self, platform):
        customer = await platform.provider.get_customer(platform.customer_id)
        found = await platform.provider.find_customer_by_email(f"  {customer.email.upper()}  ")
        assert found.id == customer.id


class TestFourTheWindow:
    async def test_since_is_inclusive_and_find_ignores_it(self, platform):
        provider = platform.provider
        invoice = (await provider.list_invoices(platform.customer_id, since=EPOCH))[0]
        at = invoice.issued_at

        assert invoice.id in {i.id for i in await provider.list_invoices(platform.customer_id, at)}
        later = at + timedelta(seconds=1)
        assert invoice.id not in {
            i.id for i in await provider.list_invoices(platform.customer_id, later)
        }
        assert (await provider.find_invoice(invoice.id)).id == invoice.id


class TestFiveSubscriptions:
    async def test_one_period_a_known_status_and_an_end_only_once_ended(self, platform):
        for customer_id in platform.customers:
            for subscription in await platform.provider.list_subscriptions(customer_id):
                assert subscription.current_period_start < subscription.current_period_end
                assert subscription.status in SubscriptionStatus
                ended = subscription.status is SubscriptionStatus.CANCELLED
                assert (subscription.cancelled_at is not None) == ended, subscription.id
                assert await platform.provider.get_subscription(subscription.id) == subscription


class TestSixAmounts:
    async def test_integer_minor_units_in_the_invoices_currency(self, platform):
        for customer_id in platform.customers:
            for invoice in await platform.provider.list_invoices(customer_id, since=EPOCH):
                currency = invoice.total.currency
                assert isinstance(invoice.total.amount_minor, int)
                for line in invoice.lines:
                    assert isinstance(line.amount.amount_minor, int)
                    assert line.amount.currency == currency, invoice.id
                    assert line.unit_amount.currency == currency, invoice.id


class TestSevenUsage:
    async def test_covers_the_period_asked_or_is_none(self, platform):
        subscription_id, (start, end) = platform.usage
        usage = await platform.provider.get_usage(subscription_id, (start, end))
        assert usage.subscription_id == subscription_id
        assert (usage.period_start, usage.period_end) == (start, end)

        next_period = (end, end + (end - start))
        assert await platform.provider.get_usage(subscription_id, next_period) is None


class TestEightPayments:
    async def test_carry_their_invoice_amount_and_a_known_state(self, platform):
        for customer_id in platform.customers:
            invoices = {
                i.id: i for i in await platform.provider.list_invoices(customer_id, since=EPOCH)
            }
            for payment in await platform.provider.list_payments(customer_id):
                assert payment.invoice_id in invoices, payment.id
                assert isinstance(payment.amount.amount_minor, int)
                assert payment.status in PaymentStatus


class TestNineUnsupportedData:
    @pytest.mark.parametrize("world", worlds.UNSUPPORTED, ids=lambda w: w.unsupported)
    async def test_raises_provider_unsupported_with_its_reason(self, platform, world):
        if not platform.seeded:
            pytest.skip(f"the {platform.name} platform holds only data the domain represents")
        seeded = platform.seeded[world.name]
        dispute = Dispute(
            id=f"contract_{world.name}",
            customer_message="Pouvez-vous verifier ma facture ?",
            customer_email=world.email,
            invoice_id=seeded.target_invoice,
        )
        investigator = DisputeInvestigator(provider=platform.provider, engine=MockDecisionEngine())
        with pytest.raises(ProviderUnsupported) as raised:
            await investigator.load_context(dispute, seeded.investigate_at)
        assert raised.value.reason == world.unsupported


class TestTenWrites:
    async def test_a_platform_says_whether_it_can_write(self, platform):
        assert isinstance(platform.provider.can_write, bool)

    async def test_one_that_cannot_refuses_every_write(self, platform):
        provider = platform.provider
        if provider.can_write:
            pytest.skip(f"the {platform.name} platform writes: see test_fake_provider")
        for write in (
            lambda: provider.create_credit_note("in_x", 100, "EUR", "test", "key"),
            lambda: provider.create_refund("in_x", 100, "EUR", "test", "key"),
            lambda: provider.find_action("key"),
        ):
            with pytest.raises(PlatformCannotWrite):
                await write()
