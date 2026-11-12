"""The billing-platform contract on Pennylane's declared sandbox
(specs/014-pennylane-read, FR-012, SC-002 as amended).

Runs locally on the worlds `scripts/pennylane_seed.py` built, with the read-only
token and the declared sandbox's id; skipped otherwise, with the reason. CI runs
the fake only. Observations (what a payment marked paid becomes, what a
subscription generates) are printed by the seed; here the reads are checked.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.providers.base import PlatformCannotWrite, ProviderUnsupported
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


async def test_every_world_is_present_or_excluded(pennylane):
    """Readiness: a world the seed should have built and did not, and the
    maintainer did not exclude, fails here."""
    missing = []
    for spec in worlds.WORLDS:
        if spec.name in worlds.NOT_SEEDED or spec.kind == "compared":
            continue
        try:
            found = await pennylane.find_customer_by_email(spec.email)
        except ProviderUnsupported:
            found = True  # present, and refused: the refused worlds' own test reads why
        if not found:
            missing.append(spec.name)
    assert missing == []


async def test_a_taxed_invoice_is_read_faithfully(pennylane):
    customer = await pennylane.find_customer_by_email(world("taxed_invoice").email)
    (invoice,) = await pennylane.list_invoices(customer.id, since=EPOCH)
    expected = worlds.TAXED_INVOICE
    assert invoice.amount_excluding_tax.amount_minor == expected["excluding_tax_minor"]
    assert invoice.tax_by_rate == {
        Decimal(expected["rate"]): invoice.tax_by_rate[Decimal(expected["rate"])]
    }
    assert invoice.tax_total.amount_minor == expected["tax_minor"]
    assert invoice.total.amount_minor == expected["total_minor"]
    assert invoice.number
    (line,) = invoice.lines
    assert line.period_start is not None and line.period_end is not None
    assert line.period_start < line.period_end


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
    else:
        assert all(p.received_at is None for p in payments)
