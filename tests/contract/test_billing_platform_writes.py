"""The write contract, on every platform that writes (specs/002-stripe-writes).

The seven write properties, against the fake in CI and against a Stripe test
account locally, with the writing key, on the latest write generation the
seed built. Each scenario writes on its own invoice, and first checks that it
carries no credit note: a scenario that would write on an invoice an earlier
run wrote on stops, asking for a new write run. On Stripe a property that
did not run is not demonstrated (SC-001).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.providers.base import PlatformCannotWrite, ProviderRejected

pytestmark = pytest.mark.contract


async def fresh(writing, purpose: str) -> str:
    invoice = writing.invoices[purpose]
    if not await writing.untouched(invoice):
        pytest.skip(
            f"invoice {invoice} ({purpose}) already carries a credit note: run the seed "
            "with --write-run for a fresh generation"
        )
    return invoice


def key_for(invoice: str, purpose: str, step: str = "write") -> str:
    """A key no earlier run used: the invoice is new with each generation."""
    return f"rb-contract/{invoice}/{purpose}/{step}"


async def send(writing, action_type: str, invoice: str, key: str, amount: int | None = None):
    provider = writing.provider
    total = (await provider.get_invoice(invoice)).total
    allocation = await provider.plan_write(invoice, action_type)
    write = provider.create_refund if action_type == "refund" else provider.create_credit_note
    return await write(
        invoice,
        total.amount_minor if amount is None else amount,
        total.currency,
        "contract",
        key,
        action_id=f"act_contract_{action_type}",
        allocation=allocation,
        send_by=datetime.now(UTC) + timedelta(hours=1),
    )


class TestOneAKeyMakesOneWrite:
    async def test_a_write_made_twice_under_one_key_is_made_once(self, writing):
        invoice = await fresh(writing, "replay")
        key = key_for(invoice, "replay")

        first = await send(writing, "credit_note", invoice, key)
        again = await send(writing, "credit_note", invoice, key)

        assert again.provider_action_id == first.provider_action_id
        assert len(await writing.provider.find_action(key, invoice)) == 1


class TestTwoAWriteIsFoundAgain:
    async def test_after_the_fact_with_what_reconciliation_checks(self, writing):
        invoice = await fresh(writing, "find")
        key = key_for(invoice, "find")
        made = await send(writing, "credit_note", invoice, key)

        (found,) = await writing.provider.find_action(key, invoice)

        fields = ("provider_action_id", "invoice_id", "action_type", "amount_minor")
        assert [getattr(found, f) for f in fields] == [getattr(made, f) for f in fields]
        assert (found.currency, found.allocation, found.idempotency_key) == (
            made.currency,
            made.allocation,
            made.idempotency_key,
        )
        assert (found.action_id, found.test_mode) == ("act_contract_credit_note", True)
        assert await writing.provider.find_action(key_for(invoice, "find", "never"), invoice) == []


class TestThreeWhereACreditNoteLands:
    async def test_on_a_paid_invoice_it_credits_the_balance(self, writing):
        invoice = await fresh(writing, "credit_paid")

        made = await send(writing, "credit_note", invoice, key_for(invoice, "credit_paid"))

        assert made.allocation == "customer_balance"
        if writing.credit_notes is not None:
            (note,) = await writing.credit_notes(invoice)
            assert note["customer_balance_transaction"] is not None
            assert note["refunds"] == []

    async def test_on_an_open_unpaid_invoice_it_lowers_what_is_due(self, writing):
        invoice = await fresh(writing, "credit_open")

        made = await send(writing, "credit_note", invoice, key_for(invoice, "credit_open"))

        assert made.allocation == "reduce_due"
        if writing.credit_notes is not None:
            (note,) = await writing.credit_notes(invoice)
            assert note["pre_payment_amount"] == made.amount_minor
            raw = await writing.provider.reader.invoice(invoice, every_line=False)
            assert raw["amount_remaining"] == raw["total"] - made.amount_minor


class TestFourARefundRefundsThePayment:
    async def test_a_refunding_credit_note_refunds_the_invoices_payment(self, writing):
        invoice = await fresh(writing, "refund")

        made = await send(writing, "refund", invoice, key_for(invoice, "refund"))

        assert (made.action_type, made.allocation) == ("refund", "refund")
        assert made.status in ("succeeded", "pending")
        if writing.credit_notes is not None:
            (note,) = await writing.credit_notes(invoice)
            assert sum(r["amount_refunded"] for r in note["refunds"]) == made.amount_minor


class TestFiveNeverMoreThanTheInvoice:
    async def test_credit_notes_and_refunds_together_stay_within_it(self, writing):
        invoice = await fresh(writing, "cap")
        await send(writing, "credit_note", invoice, key_for(invoice, "cap", "credit"))
        beyond = key_for(invoice, "cap", "refund")

        with pytest.raises(ProviderRejected):
            await send(writing, "refund", invoice, beyond)

        assert await writing.provider.find_action(beyond, invoice) == []
        if writing.credit_notes is not None:
            assert len(await writing.credit_notes(invoice)) == 1


class TestSixARefundNeedsMoneyCollected:
    async def test_a_refund_on_an_invoice_with_nothing_paid_is_refused_unsent(self, writing):
        invoice = await fresh(writing, "refund_open")

        with pytest.raises(ProviderRejected, match="invoice_state"):
            await send(writing, "refund", invoice, key_for(invoice, "refund_open"))

        assert await writing.untouched(invoice)


class TestSevenAPlatformThatCannotWrite:
    async def test_says_so_and_writes_nothing(self, writing):
        if writing.read_only is None:
            pytest.skip(f"the {writing.name} platform always writes: see test_fake_provider")
        invoice = await fresh(writing, "refund_open")
        provider = writing.read_only
        sent = {
            "action_id": "act_contract",
            "allocation": "reduce_due",
            "send_by": datetime.now(UTC) + timedelta(hours=1),
        }

        assert provider.can_write is False
        for write in (
            lambda: provider.plan_write(invoice, "credit_note"),
            lambda: provider.create_credit_note(invoice, 100, "EUR", "x", "k", **sent),
            lambda: provider.create_refund(invoice, 100, "EUR", "x", "k", **sent),
        ):
            with pytest.raises(PlatformCannotWrite):
                await write()
        assert await writing.untouched(invoice)
