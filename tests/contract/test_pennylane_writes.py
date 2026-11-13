"""The write contract on Pennylane's declared sandbox (specs/015-pennylane-credit-notes,
SC-002).

Runs locally only, on the latest write generation the seed built
(`python -m scripts.pennylane_seed --write-run`), with writes enabled for the run
(`PENNYLANE_WRITES=enabled`, `PENNYLANE_WRITE_TOKEN`) and the read-only token; skipped
otherwise, with the reason. Each action goes through the real approval and execution
service, as `test_stripe_execution.py` does: created in a temporary store, approved with
the fingerprint it presents, executed twice. A run consumes its generation: a credited
invoice is refused afterwards, so the next run needs a fresh one.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.application.execute_action import ApprovalService
from app.domain.enums import ActionState
from app.domain.models import TaxBreakdown
from app.domain.money import ROUNDING, Money
from app.infrastructure.actions import IllegalTransition, SqliteActionStore, idempotency_key
from app.providers.base import ProviderRejected, ProviderUnsupported
from scripts import pennylane_worlds as worlds

pytestmark = pytest.mark.contract

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@pytest.fixture
async def pennylane():
    needed = ("PENNYLANE_API_TOKEN", "PENNYLANE_SANDBOX_COMPANY_ID", "PENNYLANE_WRITE_TOKEN")
    if os.getenv("PENNYLANE_WRITES") != "enabled" or not all(os.getenv(v) for v in needed):
        pytest.skip("Pennylane writes are not enabled for this run: the write contract runs "
                    "locally, on the sandbox, with PENNYLANE_WRITES=enabled and its token")
    from app.providers.pennylane.adapter import PennylaneBillingProvider
    from app.providers.pennylane.client import PennylaneReader
    from app.providers.pennylane.keys import sandbox_company_id, token, writer

    reader = PennylaneReader(token("PENNYLANE_API_TOKEN"), sandbox_company_id())
    return PennylaneBillingProvider(reader, writer=writer())


@pytest.fixture
async def generation(pennylane):
    from scripts.pennylane_seed import latest_write_generation

    found = await latest_write_generation(pennylane.reader)
    if found == 0:
        pytest.fail("no write generation: run the seed with --write-run")
    return found


@pytest.fixture
def store(tmp_path):
    return SqliteActionStore(tmp_path / "actions.db")


async def fresh(pennylane, generation, name):
    """The world's customer and its one invoice, still uncredited -- else the
    generation is spent and the seed must build another."""
    world = worlds.write_world(generation, name)
    customer = await pennylane.find_customer_by_email(world.email)
    assert customer is not None, f"{world.name} is not built: run the seed with --write-run"
    try:
        (invoice,) = await pennylane.list_invoices(customer.id, since=EPOCH)
    except ProviderUnsupported as refusal:
        pytest.fail(f"{world.name} is spent ({refusal.reason}): run the seed with --write-run")
    return customer, invoice


#: Pennylane's codes as rates, for the expected figures only.
RATES = {"FR_200": Decimal("20"), "FR_55": Decimal("5.5"), "exempt": Decimal("0")}


def expected(name: str) -> tuple[int, TaxBreakdown | None]:
    """The world's total and breakdown computed from its definition, not from
    what the mapper read: each line's tax rounded to the cent, as Pennylane did
    on the sandbox; None when no tax is charged."""
    lines, _ = worlds.WRITE_WORLDS[name]
    excluding = Decimal(0)
    taxes: dict[Decimal, Decimal] = {}
    for price, vat, quantity in lines:
        amount = Decimal(price) * quantity
        excluding += amount
        if RATES[vat]:
            tax = (amount * RATES[vat] / 100).quantize(Decimal("0.01"), ROUNDING)
            taxes[RATES[vat]] = taxes.get(RATES[vat], Decimal(0)) + tax
    total = int((excluding + sum(taxes.values())) * 100)
    if not taxes:
        return total, None

    def eur(value: Decimal) -> Money:
        return Money(amount_minor=int(value * 100), currency="EUR")

    return total, TaxBreakdown(
        excluding_tax=eur(excluding),
        taxes=tuple((rate, eur(taxes[rate])) for rate in sorted(taxes)),
        total=Money(amount_minor=total, currency="EUR"),
    )


def lines_defined(name: str) -> list[tuple[Decimal, int, int, int, Decimal]]:
    """Each line as defined: quantity, unit price and amount excluding tax, the
    tax posted, its rate (0 for none) -- in minor units, sorted."""
    out = []
    for price, vat, quantity in worlds.WRITE_WORLDS[name][0]:
        amount = Decimal(price) * quantity
        tax = (amount * RATES[vat] / 100).quantize(Decimal("0.01"), ROUNDING)
        out.append((Decimal(quantity), int(Decimal(price) * 100), int(amount * 100),
                    int(tax * 100), RATES[vat] if tax else Decimal(0)))
    return sorted(out)


def lines_read(invoice) -> list[tuple[Decimal, int, int, int, Decimal]]:
    out = []
    for line in invoice.lines:
        tax = sum(t.amount.amount_minor for t in line.taxes)
        rates = {t.rate for t in line.taxes if t.amount.amount_minor}
        out.append((line.quantity, line.unit_amount.amount_minor, line.amount.amount_minor,
                    tax, next(iter(rates)) if len(rates) == 1 else Decimal(0)))
    return sorted(out)


def as_defined(name: str, invoice) -> TaxBreakdown | None:
    """The invoice read is the world defined, line by line and in sum."""
    assert lines_read(invoice) == lines_defined(name), f"{name}: not the lines defined"
    total, breakdown = expected(name)
    assert invoice.total.amount_minor == total, f"{name}: not the world defined"
    assert TaxBreakdown.of(invoice) == breakdown, f"{name}: not the world defined"
    return breakdown


def queue(store, customer, invoice, amount=None, action_type="credit_note"):
    return store.create(
        run_id="contract", action_type=action_type, invoice_id=invoice.id,
        amount=amount or invoice.total, reason="contract", provider="pennylane",
        tax_breakdown=TaxBreakdown.of(invoice), customer_id=customer.id)


async def approved_and_executed_twice(pennylane, store, action):
    service = ApprovalService(pennylane, store)
    await service.approve(action.action_id, "contract",
                          await service.proposal_fingerprint(action))
    first = await service.execute(action.action_id)
    second = await service.execute(action.action_id)
    return first, second


async def one_complete(pennylane, action, invoice):
    (found,) = await pennylane.find_action(action.idempotency_key, invoice.id)
    assert found.partial is None and found.status == "succeeded"
    assert found.amount_minor == action.amount.amount_minor
    return found


@pytest.mark.parametrize(("name", "allocation"), [("open", "reduce_due"),
                                                  ("paid", "customer_balance")])
async def test_a_taxed_invoice_is_credited_once_through_the_service(
        pennylane, generation, store, name, allocation):
    customer, invoice = await fresh(pennylane, generation, name)
    breakdown = as_defined(name, invoice)
    assert breakdown is not None
    action = queue(store, customer, invoice)

    first, second = await approved_and_executed_twice(pennylane, store, action)

    assert first.state is ActionState.EXECUTED and first.allocation == allocation
    assert second.provider_action_id == first.provider_action_id
    found = await one_complete(pennylane, action, invoice)
    assert found.breakdown == breakdown


async def test_mixed_rates_an_exempt_line_and_rounding_edges_credit_exactly(
        pennylane, generation, store):
    customer, invoice = await fresh(pennylane, generation, "mixed")
    breakdown = as_defined("mixed", invoice)
    assert breakdown is not None and len(breakdown.taxes) == 2  # 5.5 % and 20 %
    assert len(invoice.lines) == len(worlds.WRITE_WORLDS["mixed"][0])  # the exempt one too
    action = queue(store, customer, invoice)

    first, _ = await approved_and_executed_twice(pennylane, store, action)

    assert first.state is ActionState.EXECUTED
    found = await one_complete(pennylane, action, invoice)
    assert found.breakdown == breakdown


async def test_an_untaxed_invoice_is_credited_in_part(pennylane, generation, store):
    customer, invoice = await fresh(pennylane, generation, "untaxed")
    assert as_defined("untaxed", invoice) is None
    half = Money(amount_minor=invoice.total.amount_minor // 2, currency=invoice.total.currency)
    action = queue(store, customer, invoice, amount=half)

    first, _ = await approved_and_executed_twice(pennylane, store, action)

    assert first.state is ActionState.EXECUTED and first.allocation == "reduce_due"
    found = await one_complete(pennylane, action, invoice)
    assert found.breakdown is None


async def test_a_credit_beyond_what_is_left_is_refused_at_linking_nothing_numbered_left(
        pennylane, generation):
    """The adapter never builds a credit beyond the invoice's total; Pennylane's
    own cap is reached when a correction made outside lands first. Here one of
    60 % is drafted outside ResolveBilling (with the write token, not under our
    reference); when the adapter is about to send its own link, the outside one
    is linked and finalised -- after decision 47's check -- and only then is the
    adapter's link of its 60 % sent: Pennylane refuses it for its cap, the
    adapter deletes its draft, and nothing of ours is numbered."""
    from app.providers.pennylane import mapping
    from app.providers.pennylane.keys import writer

    customer, invoice = await fresh(pennylane, generation, "cap")
    as_defined("cap", invoice)
    part = invoice.total.amount_minor * 6 // 10
    raw_invoice = mapping.raw_id(invoice.id, "invoice")
    raw_customer = mapping.raw_id(customer.id, "customer")
    outside = writer()
    day = datetime.now(UTC).date().isoformat()
    theirs = await outside.write("POST", "/customer_invoices", {
        "customer_id": raw_customer, "date": day, "deadline": day, "currency": "EUR",
        "draft": True, "external_reference": f"outside:{raw_invoice}",
        "invoice_lines": [{"label": "Avoir", "quantity": -1, "unit": "piece",
                           "raw_currency_unit_price": f"{part / 100:.2f}",
                           "vat_rate": "exempt"}]})
    landed: list[dict] = []
    ours = pennylane._writer
    send = ours.write

    async def outside_lands_first(method, path, body=None, **kwargs):
        if path == f"/customer_invoices/{raw_invoice}/link_credit_note" and not landed:
            theirs_path = f"/customer_invoices/{theirs['id']}"
            await outside.write("POST", f"/customer_invoices/{raw_invoice}/link_credit_note",
                                {"credit_note_id": theirs["id"]})
            fresh_customer = await outside.get(f"/customers/{raw_customer}")
            eligible = (await outside.get(
                f"/customers/{raw_customer}/electronic_billing_addresses")).get("items")
            assert isinstance(eligible, list) and mapping.outward_ready(
                fresh_customer, mapping.emails(fresh_customer),
                await outside.get(theirs_path), eligible)
            await outside.write("PUT", f"{theirs_path}/finalize")
            landed.append(await outside.get(theirs_path))
        return await send(method, path, body, **kwargs)

    ours.write = outside_lands_first
    amount = Money(amount_minor=part, currency=invoice.total.currency)
    key = idempotency_key("pennylane", invoice.id, "credit_note", amount)
    with pytest.raises(ProviderRejected) as refused:
        await pennylane.create_credit_note(
            invoice.id, part, invoice.total.currency, "contract", key,
            action_id="contract-cap", allocation="reduce_due", send_by=None, breakdown=None)

    (finalised,) = landed  # the outside credit landed before our link was sent
    assert finalised.get("draft") is False
    assert mapping.credited_raw(finalised) == raw_invoice
    assert "cap" in str(refused.value)
    assert await pennylane.find_action(key, invoice.id) == []


async def test_a_refund_is_refused_before_anything_is_read_or_claimed(
        pennylane, generation, store):
    """A refund on Pennylane is refused first, by its type, whatever its state:
    nothing of the platform is read, so a spent world serves as well."""
    from app.providers.pennylane import mapping

    world = worlds.write_world(generation, "cap")
    customer = await pennylane.find_customer_by_email(world.email)
    (raw,) = [d for d in await pennylane._documents(mapping.raw_id(customer.id, "customer"))
              if d.get("external_reference") == f"{world.reference}:invoice"]
    action = store.create(
        run_id="contract", action_type="refund", invoice_id=mapping.typed("invoice", raw["id"]),
        amount=Money(amount_minor=10000, currency="EUR"), reason="contract",
        provider="pennylane", customer_id=customer.id)

    with pytest.raises(IllegalTransition):
        await ApprovalService(pennylane, store).execute(action.action_id)

    held = store.get(action.action_id)
    assert held.state is ActionState.PENDING_APPROVAL and held.first_attempt_at is None
