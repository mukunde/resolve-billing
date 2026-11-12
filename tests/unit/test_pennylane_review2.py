"""Regressions for review r367 of spec 014: the budget at every stage, the
subscriptions' pagination, restored evidence, a shared engine's masking."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.api.deps import wire
from app.application.investigate_dispute import DisputeInvestigator
from app.decision.anonymise import anonymise
from app.decision.masking import mask_message
from app.decision.mock import MockDecisionEngine
from app.domain.models import Dispute
from app.providers.base import ProviderUnavailable
from app.providers.pennylane.adapter import PennylaneBillingProvider
from app.providers.pennylane.client import WINDOW_REQUESTS, Pacer, PennylaneReader
from tests.unit import pennylane_stub as stub

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 15, tzinfo=UTC)


def provider_with(handler, budget=0.3, pacer=None):
    reader = PennylaneReader("t", stub.SANDBOX, transport=httpx.MockTransport(handler),
                             pacer=pacer or Pacer())
    return PennylaneBillingProvider(reader, budget=budget)


class TestTheBudget:
    async def test_spent_on_the_identity(self):
        account = stub.duplicate_world()

        async def slow_identity(request):
            if request.url.path.endswith("/me"):
                await asyncio.sleep(1.0)
            return account.handle(request)

        with pytest.raises(ProviderUnavailable, match="budget"):
            await provider_with(slow_identity).list_customers()

    async def test_spent_waiting_for_the_pacer(self):
        account = stub.duplicate_world()
        pacer = Pacer()
        for _ in range(WINDOW_REQUESTS):  # the window already full
            await pacer.wait()
        with pytest.raises(ProviderUnavailable, match="budget"):
            await provider_with(account.handle, pacer=pacer).list_customers()

    async def test_spent_on_retries(self):
        account = stub.duplicate_world()

        def limited(request):
            if request.url.path.endswith("/customers"):
                return httpx.Response(429, headers={"retry-after": "10"})
            return account.handle(request)

        with pytest.raises(ProviderUnavailable, match="budget"):
            await provider_with(limited).list_customers()

    async def test_spent_across_nested_pages(self):
        account = stub.duplicate_world()

        async def slow_lines(request):
            if request.url.path.endswith("/invoice_lines"):
                await asyncio.sleep(0.2)
            return account.handle(request)

        with pytest.raises(ProviderUnavailable, match="budget"):
            await provider_with(slow_lines).list_invoices("pl_cus_7", since=EPOCH)


async def test_subscriptions_are_read_past_one_page():
    account = stub.duplicate_world()
    original = account.handle

    def paged(request):
        if not request.url.path.endswith("/billing_subscriptions"):
            return original(request)
        if request.url.params.get("cursor") is None:
            return httpx.Response(200, json={"items": [], "has_more": True,
                                             "next_cursor": "next"})
        return original(request)

    account.handle = paged
    found = await stub.provider(account).list_subscriptions("pl_cus_7")
    assert [s.id for s in found] == ["pl_sub_9"]


async def test_equal_raw_ids_are_restored_each_to_its_own():
    """A text naming the four aliases is restored to the four typed ids."""
    account = stub.Account(
        customers=[{"id": 7, "name": "Sept", "emails": ["sept@x.fr"]}],
        invoices=[stub.invoice(7, 7, "F7", subscription=7)],
        lines={7: [stub.line(7)]},
        payments={7: [{"id": 7, "status": "confirmed", "currency": "EUR",
                       "currency_amount": "120.00"}]},
        subscriptions=[stub.subscription(7, 7)],
    )
    investigator = DisputeInvestigator(provider=stub.provider(account),
                                       engine=MockDecisionEngine())
    context = await investigator.load_context(
        Dispute(id="d", customer_message="x", customer_email="sept@x.fr",
                invoice_id="pl_inv_7"), NOW)
    _, names = anonymise(context, [])
    ids = ["pl_cus_7", "pl_sub_7", "pl_inv_7", "pl_pay_7"]
    text = " ".join(names.alias_of(i) for i in ids)
    assert names.restore(text) == " ".join(ids)


class _MaskingEngine:
    """An engine as the hosted ones are: it holds the finder `wire` gives it
    and masks each message with the context of its own call."""

    name = "masking-test"
    version = "0"

    def __init__(self):
        self.invoice_references = None
        self.sent: list[str] = []

    async def decide(self, dispute, context, findings):
        _, names = anonymise(context, findings)
        self.sent.append(mask_message(dispute.customer_message, context, names,
                                      self.invoice_references))
        return await MockDecisionEngine().decide(dispute, context, findings)


async def test_one_engine_masks_two_customers_with_their_own_invoices():
    account = stub.duplicate_world()
    account.customers.append({"id": 8, "name": "Autre", "emails": ["b@autre.fr"]})
    account.invoices.append(stub.invoice(900, 8, "F20260900"))
    account.lines[900] = [stub.line(9)]
    provider = stub.provider(account)
    engine = wire(_MaskingEngine(), provider)
    investigator = DisputeInvestigator(provider=provider, engine=engine)
    message = "Voir F20260601 et F20260900."
    await investigator.investigate(
        Dispute(id="d", customer_message=message, customer_email="lea@vega-conseil.fr",
                invoice_id="pl_inv_601"), now=NOW)
    await investigator.investigate(
        Dispute(id="e", customer_message=message, customer_email="b@autre.fr",
                invoice_id="pl_inv_900"), now=NOW)
    first, other = engine.sent
    assert "F20260601" not in first and "F20260900" in first
    assert "F20260900" not in other and "F20260601" in other
