"""Regressions for review r366 of spec 014 (Pennylane read-only)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.application.investigate_dispute import DisputeInvestigator
from app.decision.anonymise import anonymise
from app.decision.masking import mask_message
from app.decision.mock import MockDecisionEngine
from app.domain.models import Dispute
from app.providers.base import ProviderUnavailable
from app.providers.fake.adapter import FakeBillingProvider
from app.providers.pennylane.adapter import PennylaneBillingProvider
from app.providers.pennylane.client import Pacer, PennylaneReader
from tests.unit import pennylane_stub as stub

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 15, tzinfo=UTC)


def dispute(invoice_id, email="lea@vega-conseil.fr", message="x", id_="d"):
    return Dispute(id=id_, customer_message=message, customer_email=email,
                   invoice_id=invoice_id)


async def test_a_typed_id_is_never_taken_for_a_number():
    """Finding 1: an invoice numbered `601` does not capture `pl_inv_601`."""
    account = stub.duplicate_world()
    account.invoices[0]["invoice_number"] = "601"
    investigator = DisputeInvestigator(provider=stub.provider(account),
                                       engine=MockDecisionEngine())
    context = await investigator.load_context(dispute("pl_inv_601"), NOW)
    assert context.target_invoice_id == "pl_inv_601"


async def test_the_approval_names_the_resolved_invoice(tmp_path):
    """Finding 2: a dispute naming a number queues an action on the id."""
    from app.infrastructure.actions import SqliteActionStore

    actions = SqliteActionStore(str(tmp_path / "actions.db"))
    investigator = DisputeInvestigator(provider=stub.provider(stub.duplicate_world()),
                                       engine=MockDecisionEngine(), actions=actions)
    result = await investigator.investigate(
        dispute("F20260601", message="Facture en double en septembre."), now=NOW)
    assert actions.get(result.action_id).invoice_id == "pl_inv_601"


async def test_an_unresolved_number_never_reaches_the_ownership_read(monkeypatch):
    """Finding 3: the port call itself, not the request alone."""
    provider = stub.provider(stub.duplicate_world())
    asked = []
    original = provider.invoice_owner

    async def spy(invoice_id):
        asked.append(invoice_id)
        return await original(invoice_id)

    monkeypatch.setattr(provider, "invoice_owner", spy)
    investigator = DisputeInvestigator(provider=provider, engine=MockDecisionEngine())
    await investigator.investigate(dispute("F99999"), now=NOW)
    await investigator.investigate(dispute("F99999", email="nobody@else.fr", id_="e"), now=NOW)
    assert asked == []


async def test_the_fake_keeps_its_target_semantics():
    """Finding 4: on the fake, a reference written otherwise than the id does
    not become the target, as before."""
    investigator = DisputeInvestigator(provider=FakeBillingProvider(),
                                       engine=MockDecisionEngine())
    context = await investigator.load_context(dispute("INV-002", email="alice@acme.fr"), NOW)
    assert context.target_invoice_id is None
    assert context.target_reference is None


async def test_equal_raw_ids_across_kinds_keep_apart():
    """Finding 8: a customer, a subscription, an invoice and a payment all 7."""
    account = stub.Account(
        customers=[{"id": 7, "name": "Sept", "emails": ["sept@x.fr"]}],
        invoices=[stub.invoice(7, 7, "F7", subscription=7)],
        lines={7: [stub.line(7)]},
        payments={7: [{"id": 7, "status": "confirmed", "currency": "EUR",
                       "currency_amount": "120.00"}]},
        subscriptions=[stub.subscription(7, 7)],
    )
    provider = stub.provider(account)
    investigator = DisputeInvestigator(provider=provider, engine=MockDecisionEngine())
    context = await investigator.load_context(dispute("pl_inv_7", email="sept@x.fr"), NOW)
    ids = {context.customer.id, context.subscriptions[0].id, context.invoices[0].id,
           context.payments[0].id}
    assert ids == {"pl_cus_7", "pl_sub_7", "pl_inv_7", "pl_pay_7"}
    _, names = anonymise(context, [])
    aliases = {names.alias_of(i) for i in ids}
    assert len(aliases) == 4 and None not in aliases
    masked = mask_message("F7 et pl_inv_7", context, names, provider.invoice_reference_spans)
    assert "F7" not in masked and "pl_inv_7" not in masked


async def test_interleaved_masking_through_one_engine_s_finder():
    """Finding 8: one finder, as an engine holds it, two investigations."""
    account = stub.duplicate_world()
    account.customers.append({"id": 8, "name": "Autre", "emails": ["b@autre.fr"]})
    account.invoices.append(stub.invoice(900, 8, "F20260900"))
    account.lines[900] = [stub.line(9)]
    provider = stub.provider(account)
    investigator = DisputeInvestigator(provider=provider, engine=MockDecisionEngine())
    first = await investigator.load_context(dispute("pl_inv_601"), NOW)
    other = await investigator.load_context(
        dispute("pl_inv_900", email="b@autre.fr", id_="e"), NOW)
    finder = provider.invoice_reference_spans
    _, names_other = anonymise(other, [])
    assert "F20260601" in mask_message("F20260601", other, names_other, finder)
    _, names_first = anonymise(first, [])
    assert "F20260601" not in mask_message("F20260601", first, names_first, finder)


async def test_a_spent_budget_is_unavailable():
    """Finding 8: the budget covers identity, pacing, retries and pages."""
    account = stub.duplicate_world()

    async def slow(request):
        await asyncio.sleep(0.2)
        return account.handle(request)

    reader = PennylaneReader("t", stub.SANDBOX, transport=httpx.MockTransport(slow),
                             pacer=Pacer())
    with pytest.raises(ProviderUnavailable, match="budget"):
        await PennylaneBillingProvider(reader, budget=0.3).list_invoices("pl_cus_7", since=EPOCH)


async def test_lines_and_payments_are_read_past_one_page():
    account = stub.duplicate_world()
    original = account.handle

    def paged(request):
        path = request.url.path
        if not (path.endswith("/invoice_lines") or path.endswith("/payments")):
            return original(request)
        if request.url.params.get("cursor") is None:
            return httpx.Response(200, json={"items": [], "has_more": True,
                                             "next_cursor": "next"})
        return original(request)

    account.handle = paged
    provider = stub.provider(account)
    invoices = await provider.list_invoices("pl_cus_7", since=EPOCH)
    payments = await provider.list_payments("pl_cus_7")
    assert all(i.lines for i in invoices) and len(payments) == 2


async def test_an_unreadable_identity_is_a_sandbox_refusal_remembered():
    """Finding 7."""
    from app.providers.pennylane.keys import ProviderConfigurationError

    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/me"):
            return httpx.Response(200, content=b"not json")
        return httpx.Response(200, json={})

    async def no_sleep(_):
        return None

    client = PennylaneReader("t", stub.SANDBOX, transport=httpx.MockTransport(handler),
                             pacer=Pacer(sleep=no_sleep), sleep=no_sleep)
    for _ in range(2):
        with pytest.raises(ProviderConfigurationError, match="sandbox"):
            await client.get("/x")
    assert calls == ["/api/external/v2/me"]
