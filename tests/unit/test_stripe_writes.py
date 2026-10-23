"""The Stripe adapter, writing (specs/002-stripe-writes).

No SDK and no network: `tests/unit/stripe_stub.py` stands for the SDK's
client, on an account built writable; it answers the one write the adapter
may make, a credit note, and fails the test on any other.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.providers.base import (
    NotSent,
    ProviderOutcomeUnknown,
    ProviderRejected,
    ProviderUnavailable,
)
from app.providers.stripe.adapter import StripeBillingProvider
from app.providers.stripe.client import StripeReader, StripeWriter
from tests.unit import stripe_payloads as stripe
from tests.unit import stripe_stub as stub

SENTINEL = "SENTINEL-platform-text"
LATER = datetime(2100, 1, 1, tzinfo=UTC)
CREATE = "v1.credit_notes.create_async"


def account(invoice: dict | None = None, **kwargs) -> stub.Account:
    held = stub.Account(writable=True, **kwargs)
    held.customers = [stripe.customer()]
    held.invoices = [invoice if invoice is not None else stripe.invoice()]
    return held


def open_invoice(**changes) -> dict:
    """Finalised, nothing paid yet."""
    made = stripe.invoice(**changes)
    return {
        **made,
        "status": "open",
        "amount_paid": 0,
        "amount_remaining": made["total"],
        **changes,
    }


def writer(held: stub.Account, budget: float = 5.0, now=None) -> StripeBillingProvider:
    sdk = stub.StubSdk(held)
    return StripeBillingProvider(
        StripeReader(sdk), writer=StripeWriter(sdk), budget=budget, now=now
    )


async def send(adapter, action_type: str, allocation: str, *, amount=4900, send_by=LATER):
    write = adapter.create_refund if action_type == "refund" else adapter.create_credit_note
    return await write(
        stripe.INVOICE,
        amount,
        "EUR",
        "a reason that must not reach Stripe",
        stripe.KEY,
        action_id=stripe.ACTION,
        allocation=allocation,
        send_by=send_by,
    )


def sent(held: stub.Account) -> list[dict]:
    return [params for _, params in held.calls_to(CREATE)]


class TestTheAdapterWritesOnlyWhenBuiltToWrite:
    def test_without_a_writer_it_cannot_write(self):
        adapter = StripeBillingProvider(StripeReader(stub.StubSdk(account())))
        assert adapter.can_write is False

    def test_with_a_writer_it_can(self):
        assert writer(account()).can_write is True

    def test_its_window_is_under_the_day_stripe_keeps_keys(self):
        assert writer(account()).idempotency_window == timedelta(hours=23)


class TestPlanning:
    """FR-010 to FR-012: the invoice's state says how a write lands."""

    async def test_a_credit_note_on_an_open_invoice_with_nothing_paid_lowers_what_is_due(self):
        adapter = writer(account(open_invoice()))
        assert await adapter.plan_write(stripe.INVOICE, "credit_note") == "reduce_due"

    async def test_a_credit_note_on_a_paid_invoice_credits_the_balance(self):
        adapter = writer(account())
        assert await adapter.plan_write(stripe.INVOICE, "credit_note") == "customer_balance"

    async def test_a_refund_on_a_paid_invoice_refunds(self):
        assert await writer(account()).plan_write(stripe.INVOICE, "refund") == "refund"

    @pytest.mark.parametrize(
        ("invoice", "action_type"),
        [
            (open_invoice(), "refund"),
            (open_invoice(amount_paid=1000, amount_remaining=3900), "credit_note"),
            (open_invoice(status="draft"), "credit_note"),
            (open_invoice(status="void"), "credit_note"),
            (open_invoice(status="uncollectible"), "credit_note"),
            (stripe.invoice(amount_paid=0), "refund"),
        ],
        ids=["refund-on-open", "partly-paid", "draft", "void", "uncollectible", "paid-out-of-band"],
    )
    async def test_any_other_state_is_refused(self, invoice, action_type):
        held = account(invoice)

        with pytest.raises(ProviderRejected, match="invoice_state"):
            await writer(held).plan_write(stripe.INVOICE, action_type)

        assert sent(held) == []


class TestTheRequest:
    """FR-010, FR-011, FR-013: one credit note, allocated as planned,
    carrying the action's key and nothing of the case."""

    async def test_a_credit_note_on_an_open_invoice_sends_only_its_amount(self):
        held = account(open_invoice())

        await send(writer(held), "credit_note", "reduce_due")

        (params,) = sent(held)
        assert params == {
            "invoice": stripe.INVOICE,
            "amount": 4900,
            "email_type": "none",
            "metadata": {
                "rb_idempotency_key": stripe.KEY,
                "rb_action_id": stripe.ACTION,
                "rb_action_type": "credit_note",
            },
            "expand": ["refunds.refund"],
        }

    async def test_a_credit_note_on_a_paid_invoice_credits_the_whole_amount(self):
        held = account()

        await send(writer(held), "credit_note", "customer_balance")

        (params,) = sent(held)
        assert params["credit_amount"] == params["amount"] == 4900
        assert "refund_amount" not in params

    async def test_a_refund_is_a_credit_note_refunding_its_whole_amount(self):
        held = account()

        await send(writer(held), "refund", "refund")

        (params,) = sent(held)
        assert params["refund_amount"] == params["amount"] == 4900
        assert "credit_amount" not in params
        assert params["metadata"]["rb_action_type"] == "refund"

    async def test_nothing_of_the_case_reaches_stripe(self):
        held = account()

        await send(writer(held), "credit_note", "customer_balance")

        (params,) = sent(held)
        assert "memo" not in params and "reason" not in params
        assert "must not reach" not in repr(params)

    async def test_the_key_is_stripes_and_the_sdk_does_not_retry_a_write(self):
        held = account()

        await send(writer(held), "credit_note", "customer_balance")

        (options,) = [options for method, options in held.options if method == CREATE]
        assert options == {"idempotency_key": stripe.KEY, "max_network_retries": 0}

    async def test_reads_keep_their_retries(self):
        held = account()

        await send(writer(held), "credit_note", "customer_balance")

        assert all(options == {} for method, options in held.options if method != CREATE)

    async def test_no_mutating_call_but_the_credit_note(self):
        held = account()

        await send(writer(held), "refund", "refund")

        assert {method for method, _, _ in held.calls} - set(stub.READS) == {CREATE}


class TestBeforeSending:
    """FR-012 and FR-005: the invoice must still take the planned
    allocation, and nothing goes out past the deadline."""

    async def test_an_allocation_the_invoice_no_longer_allows_is_refused_unsent(self):
        held = account(open_invoice())

        with pytest.raises(ProviderRejected, match="invoice_state"):
            await send(writer(held), "credit_note", "customer_balance")

        assert sent(held) == []

    async def test_a_read_that_fails_there_leaves_the_outcome_unknown(self):
        held = account()
        held.failures["v1.invoices.retrieve_async"] = stub.APIConnectionError(SENTINEL)

        with pytest.raises(ProviderOutcomeUnknown):
            await send(writer(held), "credit_note", "customer_balance")

        assert sent(held) == []

    async def test_a_read_that_ends_past_the_deadline_sends_nothing(self):
        held = account()
        held.delay = 0.05
        deadline = datetime.now(UTC) + timedelta(milliseconds=20)

        with pytest.raises(NotSent):
            await send(writer(held), "credit_note", "customer_balance", send_by=deadline)

        assert held.calls_to("v1.invoices.retrieve_async")
        assert sent(held) == []


class TestTheAnswer:
    async def test_the_answer_says_what_was_made(self):
        made = await send(writer(account()), "refund", "refund")

        assert made.provider == "stripe"
        assert made.provider_action_id.startswith("cn_")
        assert (made.invoice_id, made.amount_minor, made.currency) == (stripe.INVOICE, 4900, "EUR")
        assert (made.action_type, made.allocation, made.status) == ("refund", "refund", "succeeded")
        assert (made.idempotency_key, made.action_id) == (stripe.KEY, stripe.ACTION)
        assert made.test_mode is True

    async def test_a_key_sent_twice_answers_the_first_write(self):
        held = account()
        adapter = writer(held)

        first = await send(adapter, "credit_note", "customer_balance")
        again = await send(adapter, "credit_note", "customer_balance")

        assert again.provider_action_id == first.provider_action_id
        assert len(held.credit_notes) == 1

    async def test_an_answer_whose_refund_is_not_expanded_is_unreadable(self):
        held = account()
        held.answers[CREATE] = stripe.credit_note(
            refunds=[stripe.credit_note_refund(stripe.REFUND)]
        )

        with pytest.raises(ProviderOutcomeUnknown, match="unreadable_answer"):
            await send(writer(held), "refund", "refund")


class TestRefusalsAndUnknownOutcomes:
    """FR-020: a refusal only when Stripe's answer proves nothing was made."""

    @pytest.mark.parametrize(
        ("error", "category"),
        [
            (stub.InvalidRequestError(SENTINEL), "invalid_request"),
            (stub.InvalidRequestError(SENTINEL, code="resource_missing"), "not_found"),
            (stub.AuthenticationError(SENTINEL), "authentication"),
            (stub.PermissionError(SENTINEL), "permission"),
        ],
        ids=["invalid", "missing", "authentication", "permission"],
    )
    async def test_the_closed_list_of_refusals(self, error, category, caplog):
        held = account()
        held.failures[CREATE] = error

        with caplog.at_level(logging.DEBUG), pytest.raises(ProviderRejected) as raised:
            await send(writer(held), "credit_note", "customer_balance")

        assert str(raised.value) == category
        assert SENTINEL not in caplog.text
        assert raised.value.__cause__ is None and raised.value.__suppress_context__

    @pytest.mark.parametrize(
        ("error", "category"),
        [
            (stub.IdempotencyError(SENTINEL), "idempotency_error"),
            (stub.APIError(SENTINEL), "platform_error"),
            (stub.RateLimitError(SENTINEL), "rate_limited"),
            (stub.APIConnectionError(SENTINEL), "connection"),
            (ValueError(SENTINEL), "unexpected"),
        ],
        ids=["idempotency", "server", "rate-limit", "connection", "anything-else"],
    )
    async def test_everything_else_leaves_the_outcome_unknown(self, error, category, caplog):
        held = account()
        held.failures[CREATE] = error

        with caplog.at_level(logging.DEBUG), pytest.raises(ProviderOutcomeUnknown) as raised:
            await send(writer(held), "credit_note", "customer_balance")

        assert raised.value.category == category
        assert not isinstance(raised.value, ProviderRejected)
        assert SENTINEL not in str(raised.value) and SENTINEL not in caplog.text

    async def test_a_time_budget_spent_mid_write_leaves_the_outcome_unknown(self):
        held = account()
        held.delay = 0.05

        with pytest.raises(ProviderOutcomeUnknown, match="time_budget"):
            await send(writer(held, budget=0.08), "credit_note", "customer_balance")


LIST = "v1.credit_notes.list_async"


def ours(n: int, key: str = stripe.KEY, invoice: str = stripe.INVOICE, **changes) -> dict:
    return stripe.credit_note(
        id=f"cn_RbOurs{n:010d}",
        invoice=invoice,
        metadata={
            "rb_idempotency_key": key,
            "rb_action_id": stripe.ACTION,
            "rb_action_type": "credit_note",
        },
        **changes,
    )


class TestFindingAWriteAgain:
    """FR-001 and FR-014: every credit note of the invoice, read to the
    end, those carrying the key kept -- never Stripe's search."""

    async def test_every_page_is_read_and_only_the_keys_writes_kept(self):
        held = account(page_size=2)
        held.credit_notes = [
            ours(1, key="another"),
            ours(2),
            ours(3, key="yet-another"),
            ours(4, invoice="in_1RbOther0000001"),
        ]

        found = await writer(held).find_action(stripe.KEY, stripe.INVOICE)

        assert [made.provider_action_id for made in found] == ["cn_RbOurs0000000002"]
        pages = held.calls_to(LIST)
        assert len(pages) == 2
        assert all(params["expand"] == ["data.refunds.refund"] for _, params in pages)
        assert all(params["invoice"] == stripe.INVOICE for _, params in pages)

    async def test_two_writes_under_the_key_are_both_answered(self):
        held = account()
        held.credit_notes = [ours(1), ours(2)]

        found = await writer(held).find_action(stripe.KEY, stripe.INVOICE)

        assert len(found) == 2

    async def test_none_is_an_empty_answer(self):
        assert await writer(account()).find_action(stripe.KEY, stripe.INVOICE) == []

    async def test_a_refund_left_unexpanded_cannot_be_read(self):
        held = account()
        held.answers[LIST] = stripe.page(
            [ours(1, refunds=[stripe.credit_note_refund(stripe.REFUND)])]
        )

        (made,) = await writer(held).find_action(stripe.KEY, stripe.INVOICE)

        assert made.status is None

    async def test_a_list_cut_short_is_never_an_absence(self):
        held = account()
        held.answers[LIST] = stripe.page([], has_more=True)

        with pytest.raises(ProviderUnavailable):
            await writer(held).find_action(stripe.KEY, stripe.INVOICE)

    async def test_it_is_a_read_and_needs_no_writer(self):
        held = stub.Account()
        held.invoices = [stripe.invoice()]
        held.credit_notes = [ours(1)]
        adapter = StripeBillingProvider(StripeReader(stub.StubSdk(held)))

        (made,) = await adapter.find_action(stripe.KEY, stripe.INVOICE)

        assert made.provider_action_id == "cn_RbOurs0000000001"
        assert {method for method, _, _ in held.calls} <= set(stub.READS)


class TestAgainstTheRealSdk:
    """Where the SDK is installed: the stub's write and the error mapping
    match it."""

    def test_the_stub_write_has_the_sdks_parameter_names(self):
        sdk = pytest.importorskip("stripe")
        import inspect

        client = sdk.StripeClient("rk_test_51RbAbc")
        for path, names in stub.WRITES.items():
            method = client
            for part in path.split("."):
                method = getattr(method, part)
            assert tuple(inspect.signature(method).parameters) == names, path

    async def test_the_sdks_own_errors_map_the_same(self):
        sdk = pytest.importorskip("stripe")
        for error, expected in (
            (sdk.InvalidRequestError(SENTINEL, "amount"), "invalid_request"),
            (sdk.PermissionError(SENTINEL), "permission"),
            (sdk.IdempotencyError(SENTINEL), "idempotency_error"),
            (sdk.APIConnectionError(SENTINEL), "connection"),
        ):
            held = account()
            held.failures[CREATE] = error
            with pytest.raises((ProviderRejected, ProviderOutcomeUnknown)) as raised:
                await send(writer(held), "credit_note", "customer_balance")
            assert str(raised.value) == expected
