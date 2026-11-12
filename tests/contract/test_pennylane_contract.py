"""The billing-platform contract on Pennylane's declared sandbox
(specs/014-pennylane-read, FR-012, SC-002 as amended).

Runs locally on the worlds `scripts/pennylane_seed.py` built, with the read-only
token and the declared sandbox's id; skipped otherwise, with the reason. CI runs
the fake only. Observations (what a payment marked paid becomes, what a
subscription generates) are printed by the seed; here the reads are checked.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.providers.base import PlatformCannotWrite, ProviderUnsupported
from app.providers.pennylane import mapping
from app.providers.pennylane.adapter import _filter
from scripts import pennylane_worlds as worlds

pytestmark = pytest.mark.contract

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@pytest.fixture
def pennylane():
    if not (os.getenv("PENNYLANE_API_TOKEN") and os.getenv("PENNYLANE_SANDBOX_COMPANY_ID")):
        pytest.skip(
            "PENNYLANE_API_TOKEN and PENNYLANE_SANDBOX_COMPANY_ID are not both set: the "
            "Pennylane contract runs locally, on the seeded sandbox"
        )
    from app.providers.pennylane.adapter import PennylaneBillingProvider
    from app.providers.pennylane.client import PennylaneReader
    from app.providers.pennylane.keys import sandbox_company_id, token

    return PennylaneBillingProvider(
        PennylaneReader(token("PENNYLANE_API_TOKEN"), sandbox_company_id())
    )


def world(name: str) -> worlds.World:
    return next(w for w in worlds.WORLDS if w.name == name)


async def _state(pennylane, spec) -> bool:
    """Whether a world is in the state its kind asks for. A refused world is
    present only when refused with its own reason; a read world is never
    refused; the subscription is present once stopped (read raw, as the mapper
    may refuse what Pennylane gives -- an observation the seed prints); the paid
    invoice once Pennylane says it is paid."""
    try:
        customer = await pennylane.find_customer_by_email(spec.email)
    except ProviderUnsupported as refusal:
        return spec.kind == "refused" and refusal.reason == spec.reason
    if customer is None:
        return False
    if spec.name == "subscription":
        raw = mapping.raw_id(customer.id, "customer")
        found = await pennylane.reader.every(
            "/billing_subscriptions", {"filter": _filter("customer_id", raw)})
        return len(found) == 1 and found[0].get("status") == "stopped"
    try:
        invoices = await pennylane.list_invoices(customer.id, since=EPOCH)
    except ProviderUnsupported as refusal:
        return spec.kind == "refused" and refusal.reason == spec.reason
    if spec.kind == "refused":
        return False  # read without its refusal: the world is not what it should be
    if spec.name == "paid_invoice":
        return len(invoices) == 1 and invoices[0].status.value == "paid"
    return len(invoices) >= 1


async def test_every_world_is_present_or_excluded(pennylane):
    """Readiness: every world, the compared ones included, is in its state or
    excluded by the maintainer (`NOT_SEEDED`); none is passed over silently."""
    missing = [
        spec.name for spec in worlds.WORLDS
        if spec.name not in worlds.NOT_SEEDED and not await _state(pennylane, spec)
    ]
    assert missing == []


async def test_a_taxed_invoice_is_read_faithfully(pennylane):
    """Against what the seed wrote (`worlds.TAXED_INVOICE`), field by field."""
    expected = worlds.TAXED_INVOICE
    customer = await pennylane.find_customer_by_email(world("taxed_invoice").email)
    (invoice,) = await pennylane.list_invoices(customer.id, since=EPOCH)
    rate = Decimal(expected["rate"])
    assert invoice.amount_excluding_tax.amount_minor == expected["excluding_tax_minor"]
    assert {r: m.amount_minor for r, m in invoice.tax_by_rate.items()} == {
        rate: expected["tax_minor"]}
    assert invoice.total.amount_minor == expected["total_minor"]
    assert invoice.issued_at == datetime.fromisoformat(expected["date"]).replace(tzinfo=UTC)
    raw = await pennylane.reader.get(
        f"/customer_invoices/{mapping.raw_id(invoice.id, 'invoice')}")
    assert invoice.number == raw["invoice_number"] and invoice.number.strip()
    (line,) = invoice.lines
    assert line.quantity == Decimal(expected["quantity"])
    assert line.unit_amount.amount_minor == expected["unit_minor"]
    assert line.amount.amount_minor == expected["excluding_tax_minor"]
    assert line.period_start == datetime.fromisoformat(expected["start_date"]).replace(tzinfo=UTC)
    assert line.period_end == (
        datetime.fromisoformat(expected["end_date"]).replace(tzinfo=UTC) + timedelta(days=1))


@pytest.mark.parametrize(
    "spec", [w for w in worlds.WORLDS if w.kind == "compared"], ids=lambda w: w.name
)
async def test_a_compared_world_matches_its_fake_counterpart(pennylane, spec):
    """Where the sandbox let the world be built (SC-002 as amended)."""
    if spec.name in worlds.NOT_SEEDED:
        pytest.skip(f"excluded by the maintainer: {worlds.NOT_SEEDED[spec.name]}")
    customer = await pennylane.find_customer_by_email(spec.email)
    if customer is None:
        pytest.fail(f"{spec.name} is not built and not excluded")
    import json

    from app.application.investigate_dispute import DisputeInvestigator
    from app.decision.mock import MockDecisionEngine
    from app.domain.models import Dispute
    from app.providers.fake.adapter import FakeBillingProvider
    from evals.engines import DEVELOPMENT_SET
    from evals.runner import EVALUATION_DATE

    case = next(c for c in json.loads(DEVELOPMENT_SET.read_bytes())
                if c["id"] == spec.counterpart)
    invoices = await pennylane.list_invoices(customer.id, since=EPOCH)
    target = max(invoices, key=lambda i: i.issued_at)

    async def outcome(provider, email, invoice_id, now):
        result = await DisputeInvestigator(provider=provider, engine=MockDecisionEngine()
                                           ).investigate(Dispute(
            id="contract", customer_message=case["message"], customer_email=email,
            invoice_id=invoice_id), now=now)
        amount = result.proposed_action.amount
        return ({f.code for f in result.findings}, result.classification,
                result.proposed_action.type, amount.amount_minor if amount else None,
                result.authorization.verdict, result.requires_human_approval)

    assert await outcome(pennylane, spec.email, target.id, datetime.now(UTC)) == await outcome(
        FakeBillingProvider(), case["customer_email"], case["invoice_id"], EVALUATION_DATE)


@pytest.mark.parametrize(
    "spec", [w for w in worlds.WORLDS if w.kind == "refused"], ids=lambda w: w.name
)
async def test_each_refused_world_gives_its_reason(pennylane, spec):
    if spec.name in worlds.NOT_SEEDED:
        pytest.skip(f"excluded by the maintainer: {worlds.NOT_SEEDED[spec.name]}")
    with pytest.raises(ProviderUnsupported) as raised:
        customer = await pennylane.find_customer_by_email(spec.email)
        if customer is None:
            pytest.fail(f"{spec.name} is not seeded: run the seed")
        await pennylane.list_invoices(customer.id, since=EPOCH)
    assert raised.value.reason == spec.reason


async def test_whose_an_invoice_is_and_nothing_more(pennylane):
    customer = await pennylane.find_customer_by_email(world("taxed_invoice").email)
    (invoice,) = await pennylane.list_invoices(customer.id, since=EPOCH)
    assert await pennylane.invoice_owner(invoice.id) == customer.id
    assert await pennylane.invoice_owner("pl_inv_999999999") is None


async def test_a_number_resolves_among_the_customer_s_invoices(pennylane):
    customer = await pennylane.find_customer_by_email(world("taxed_invoice").email)
    invoices = await pennylane.list_invoices(customer.id, since=EPOCH)
    (invoice,) = invoices
    assert pennylane.resolve_invoice_reference(invoice.number, invoices) == invoice.id


async def test_it_writes_nothing(pennylane):
    assert pennylane.can_write is False
    with pytest.raises(PlatformCannotWrite):
        await pennylane.plan_write("pl_inv_1", "credit_note")
    assert await pennylane.find_action("any-key", "pl_inv_1") == []


async def test_the_paid_invoice_reads_or_refuses_by_its_payment_status(pennylane):
    """What a payment marked paid becomes is an observation (the seed prints
    it); the adapter either maps it by the convention table or refuses it."""
    customer = await pennylane.find_customer_by_email(world("paid_invoice").email)
    try:
        payments = await pennylane.list_payments(customer.id)
    except ProviderUnsupported as refusal:
        assert refusal.reason == "payment_status"
        return
    if not payments:
        pytest.skip("Pennylane lists no payment for an invoice marked paid: an observation, "
                    "recorded in the contract, not a payment the adapter could read")
    assert all(p.received_at is None and p.invoice_id for p in payments)
