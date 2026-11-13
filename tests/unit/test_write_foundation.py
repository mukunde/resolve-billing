"""The port's foundation for spec 015 (specs/015-pennylane-credit-notes, plan section 1):
what every platform declares, and what the execution service may rely on."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.infrastructure.actions import ATTENTION, INCIDENTS
from app.providers.base import PartialWrite, ProviderAction, ProviderOutcomeUnknown
from app.providers.fake.adapter import FakeBillingProvider
from app.providers.pennylane.adapter import PennylaneBillingProvider
from app.providers.stripe.adapter import StripeBillingProvider


def test_the_fake_writes_both_types():
    fake = FakeBillingProvider()
    assert fake.writes == frozenset({"credit_note", "refund"})
    assert fake.can_write is True


@pytest.mark.parametrize("cls", [StripeBillingProvider, PennylaneBillingProvider])
def test_a_read_only_platform_writes_no_type(cls):
    assert cls.writes == frozenset()
    assert cls.can_write is False


def test_stripe_with_a_writer_writes_both_types():
    provider = StripeBillingProvider(object(), writer=object())
    assert provider.writes == frozenset({"credit_note", "refund"})
    assert provider.can_write is True


def test_a_provider_action_reports_no_breakdown_and_no_partial_by_default():
    now = datetime.now(UTC)
    made = ProviderAction(
        provider="fake", provider_action_id="cn_1", action_type="credit_note",
        idempotency_key="k", status="succeeded", created_at=now, invoice_id="in_1",
        amount_minor=100, currency="EUR", test_mode=True, allocation="reduce_due",
        action_id="a", observed_at=now)
    assert made.breakdown is None
    assert made.partial is None


def test_a_partial_write_is_an_unknown_outcome_naming_its_document():
    error = PartialWrite("pl_inv_42", "linked")
    assert isinstance(error, ProviderOutcomeUnknown)
    assert (error.category, error.document_id, error.stage) == ("partial_write", "pl_inv_42",
                                                               "linked")


@pytest.mark.parametrize("attention", ["partial_write", "no_trace"])
def test_the_new_attentions_are_incidents(attention):
    assert attention in ATTENTION
    assert attention in INCIDENTS
