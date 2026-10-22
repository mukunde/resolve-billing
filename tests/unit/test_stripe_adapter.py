"""The Stripe adapter and its client, against an account in memory.

No SDK and no network: `tests/unit/stripe_stub.py` stands for the SDK's
client, with its read methods and their parameter names, and fails on any
other. Where the SDK is installed, two tests pin the stub and the error
mapping to the real thing.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, timedelta

import pytest

from app.providers.base import (
    BillingProvider,
    NotFound,
    PlatformCannotWrite,
    ProviderUnavailable,
    ProviderUnsupported,
)
from app.providers.stripe.adapter import StripeBillingProvider
from app.providers.stripe.client import StripeReader, connect
from app.providers.stripe.keys import ProviderConfigurationError, check_read_key, check_seed_key
from tests.unit import stripe_payloads as stripe
from tests.unit import stripe_stub as stub
from tests.unit.stripe_payloads import OCT_1, SEP_1

SEPTEMBER = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SENTINEL = "SENTINEL-platform-text"


def account(**kwargs) -> stub.Account:
    """One customer on a clock, a subscription, a paid invoice and its price."""
    held = stub.Account(**kwargs)
    held.clocks = [stripe.clock()]
    held.customers = [stripe.customer()]
    held.subscriptions = [stripe.subscription()]
    held.invoices = [stripe.invoice()]
    held.invoice_payments = [stripe.invoice_payment()]
    held.prices = {stripe.PRICE: stripe.price()}
    return held


def provider(held: stub.Account, budget: float = 5.0) -> StripeBillingProvider:
    return StripeBillingProvider(StripeReader(stub.StubSdk(held)), budget=budget)


def customer(n: int, created: int, email: str, clock: str | None = stripe.CLOCK) -> dict:
    return stripe.customer(id=f"cus_Rb{n:014d}", created=created, email=email, test_clock=clock)


class TestKeys:
    """FR-021: refused before any call; the key never in a message."""

    def test_a_restricted_test_key_is_the_adapters(self):
        check_read_key("rk_test_51RbAbc")

    @pytest.mark.parametrize(
        ("key", "category"),
        [
            ("rk_live_51RbAbc", "live_key"),
            ("sk_live_51RbAbc", "live_key"),
            ("sk_test_51RbAbc", "secret_key"),
            ("pk_test_51RbAbc", "not_a_restricted_test_key"),
            ("", "missing_key"),
            (None, "missing_key"),
        ],
    )
    def test_anything_else_is_refused_by_category(self, key, category):
        with pytest.raises(ProviderConfigurationError) as raised:
            check_read_key(key)
        assert raised.value.category == category
        assert "51RbAbc" not in str(raised.value)

    def test_the_seed_key_is_the_secret_test_key_only(self):
        check_seed_key("sk_test_51RbAbc")
        for key, category in (
            ("sk_live_51RbAbc", "live_key"),
            ("rk_test_51RbAbc", "not_a_secret_test_key"),
            ("rk_live_51RbAbc", "live_key"),
        ):
            with pytest.raises(ProviderConfigurationError) as raised:
                check_seed_key(key)
            assert raised.value.category == category

    def test_connect_refuses_before_the_sdk_is_even_loaded(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "stripe", None)  # importing it would fail
        with pytest.raises(ProviderConfigurationError):
            connect("sk_live_51RbAbc")


class TestThePort:
    def test_satisfies_it_and_cannot_write(self):
        adapter = provider(account())
        assert isinstance(adapter, BillingProvider)
        assert adapter.name == "stripe"
        assert adapter.can_write is False

    async def test_writes_and_find_action_are_refused_without_a_call(self):
        held = account()
        adapter = provider(held)
        for write in (
            lambda: adapter.create_credit_note(stripe.INVOICE, 100, "EUR", "r", "key"),
            lambda: adapter.create_refund(stripe.INVOICE, 100, "EUR", "r", "key"),
            lambda: adapter.find_action("key"),
        ):
            with pytest.raises(PlatformCannotWrite):
                await write()
        assert held.calls == []


class TestCustomers:
    async def test_test_clock_customers_are_merged_once_in_a_fixed_order(self):
        held = account(page_size=1)
        held.clocks = [stripe.clock(id="clock_A"), stripe.clock(id="clock_B")]
        held.customers = [
            customer(3, created=30, email="c@x.test", clock=None),
            customer(1, created=10, email="a@x.test", clock="clock_B"),
            customer(2, created=10, email="b@x.test", clock="clock_A"),
        ]
        found = await provider(held).list_customers()
        assert [c.id for c in found] == [
            "cus_Rb00000000000001",
            "cus_Rb00000000000002",
            "cus_Rb00000000000003",
        ]
        asked = [params.get("test_clock") for _, params in held.calls_to("v1.customers.list_async")]
        assert {None, "clock_A", "clock_B"} <= set(asked)

    async def test_the_lookup_ignores_case_and_spaces_and_takes_the_first(self):
        held = account()
        held.customers = [
            customer(2, created=20, email="Same@X.test"),
            customer(1, created=20, email=" same@x.TEST "),
            customer(3, created=5, email=None),
        ]
        found = await provider(held).find_customer_by_email("  SAME@x.test ")
        assert found.id == "cus_Rb00000000000001"

    async def test_an_address_nobody_holds_is_none(self):
        assert await provider(account()).find_customer_by_email("nobody@x.test") is None

    async def test_an_empty_address_is_none_without_a_call(self):
        held = account()
        assert await provider(held).find_customer_by_email("   ") is None
        assert held.calls == []

    async def test_the_lookup_never_uses_stripes_filter_or_search(self):
        held = account()
        await provider(held).find_customer_by_email("billing@duplicate.example.test")
        for _, params in held.calls_to("v1.customers.list_async"):
            assert "email" not in params

    async def test_a_deleted_customer_is_not_found(self):
        held = account()
        held.customers = [{"id": stripe.CUSTOMER, "object": "customer", "deleted": True}]
        with pytest.raises(NotFound):
            await provider(held).get_customer(stripe.CUSTOMER)


class TestEveryList:
    async def test_nested_invoice_lines_are_read_to_the_end(self):
        held = account(page_size=1, embedded=1)
        lines = [stripe.line(id=f"il_{n}", amount=1000) for n in range(3)]
        held.invoices = [stripe.invoice(lines)]
        invoice = await provider(held).get_invoice(stripe.INVOICE)
        assert len(invoice.lines) == 3
        assert held.calls_to("v1.invoices.line_items.list_async")

    async def test_nested_subscription_items_are_read_to_the_end(self):
        held = account(page_size=1, embedded=1)
        # A third item on another period: seen only if every item is read.
        items = [
            stripe.item(id="si_1"),
            stripe.item(id="si_2"),
            stripe.item(id="si_3", end=OCT_1 + 60),
        ]
        held.subscriptions = [stripe.subscription(items)]
        with pytest.raises(ProviderUnsupported, match="mixed_item_periods"):
            await provider(held).get_subscription(stripe.SUBSCRIPTION)

    async def test_canceled_subscriptions_are_listed(self):
        held = account()
        held.subscriptions = [stripe.subscription(status="canceled", ended_at=SEP_1)]
        [found] = await provider(held).list_subscriptions(stripe.CUSTOMER)
        assert found.status.value == "cancelled"
        [(_, params)] = held.calls_to("v1.subscriptions.list_async")
        assert params["status"] == "all"

    async def test_a_page_announcing_more_without_data_is_a_cut_list(self):
        held = account()
        held.answers["v1.test_helpers.test_clocks.list_async"] = stripe.page([], has_more=True)
        with pytest.raises(ProviderUnavailable):
            await provider(held).list_customers()


class TestInvoices:
    async def test_the_window_keeps_invoices_issued_at_or_after_since(self):
        held = account()
        older = stripe.invoice(
            id="in_1RbOlder00000001",
            status_transitions={"finalized_at": SEP_1 - 86400},
            # Outside the window, and unsupported: never mapped, so no refusal.
            total_taxes=[stripe.tax()],
        )
        held.invoices = [older, stripe.invoice()]
        since = datetime.fromtimestamp(SEP_1 + 3600, UTC)
        found = await provider(held).list_invoices(stripe.CUSTOMER, since=since)
        assert [i.id for i in found] == [stripe.INVOICE]

    async def test_find_reads_by_identifier_whatever_its_age(self):
        found = await provider(account()).find_invoice(stripe.INVOICE)
        assert found.id == stripe.INVOICE

    async def test_an_unknown_invoice_is_none_for_find_and_not_found_for_get(self):
        adapter = provider(account())
        assert await adapter.find_invoice("in_1RbMissing0000001") is None
        with pytest.raises(NotFound):
            await adapter.get_invoice("in_1RbMissing0000001")

    async def test_the_owner_is_read_without_mapping_the_invoice(self):
        held = account()
        held.invoices = [stripe.invoice(total_taxes=[stripe.tax()])]  # unsupported content
        assert await provider(held).invoice_owner(stripe.INVOICE) == stripe.CUSTOMER
        assert not held.calls_to("v1.prices.retrieve_async")
        assert not held.calls_to("v1.invoices.line_items.list_async")

    async def test_an_expanded_customer_still_gives_its_identifier(self):
        held = account()
        held.invoices = [stripe.invoice(customer=stripe.customer())]
        assert await provider(held).invoice_owner(stripe.INVOICE) == stripe.CUSTOMER

    async def test_an_unknown_invoice_has_no_owner(self):
        assert await provider(account()).invoice_owner("in_1RbMissing0000001") is None

    @pytest.mark.parametrize("read", ["get_invoice", "find_invoice"])
    async def test_a_price_stripe_cannot_find_never_makes_the_invoice_absent(self, read):
        held = account()
        held.prices = {}
        with pytest.raises(ProviderUnsupported, match="missing_field"):
            await getattr(provider(held), read)(stripe.INVOICE)

    async def test_a_meter_stripe_cannot_find_never_makes_the_invoice_absent(self):
        held = account()
        held.invoices = [stripe.invoice([stripe.line(line_price=stripe.METERED_PRICE)])]
        held.prices[stripe.METERED_PRICE] = stripe.metered_price()
        with pytest.raises(ProviderUnsupported, match="missing_field"):
            await provider(held).find_invoice(stripe.INVOICE)

    async def test_a_price_is_read_once_per_call(self):
        held = account()
        held.invoices = [
            stripe.invoice([stripe.line(id="il_1"), stripe.line(id="il_2")]),
            stripe.invoice(id="in_1RbSecond0000001"),
        ]
        await provider(held).list_invoices(stripe.CUSTOMER, since=EPOCH)
        assert len(held.calls_to("v1.prices.retrieve_async")) == 1

    async def test_a_metered_line_reads_its_meter_once(self):
        held = account()
        metered = [
            stripe.line(id=f"il_{n}", line_price=stripe.METERED_PRICE, unit_amount_decimal="100")
            for n in range(2)
        ]
        held.invoices = [stripe.invoice(metered)]
        held.prices[stripe.METERED_PRICE] = stripe.metered_price()
        held.meters = {stripe.METER: stripe.meter()}
        invoice = await provider(held).get_invoice(stripe.INVOICE)
        assert {line.metric for line in invoice.lines} == {"api_call_blocks_100"}
        assert len(held.calls_to("v1.billing.meters.retrieve_async")) == 1


class TestPayments:
    async def test_every_invoices_payments_are_read_expanded(self):
        held = account()
        held.invoices = [stripe.invoice(), stripe.invoice(id="in_1RbSecond0000001")]
        held.invoice_payments = [
            stripe.invoice_payment(),
            stripe.invoice_payment(
                stripe.payment_intent(id="pi_2"), id="inpay_2", invoice="in_1RbSecond0000001"
            ),
        ]
        found = await provider(held).list_payments(stripe.CUSTOMER)
        assert {p.invoice_id for p in found} == {stripe.INVOICE, "in_1RbSecond0000001"}
        for _, params in held.calls_to("v1.invoice_payments.list_async"):
            assert params["expand"] == [stub.PAYMENT_EXPANSION]

    async def test_one_payment_across_two_invoices_is_refused(self):
        held = account()
        held.invoices = [stripe.invoice(), stripe.invoice(id="in_1RbSecond0000001")]
        held.invoice_payments = [
            stripe.invoice_payment(),
            stripe.invoice_payment(id="inpay_2", invoice="in_1RbSecond0000001"),
        ]
        with pytest.raises(ProviderUnsupported, match="multi_invoice_payment"):
            await provider(held).list_payments(stripe.CUSTOMER)


class TestUsage:
    def metered(self) -> stub.Account:
        held = account()
        held.subscriptions = [stripe.subscription([stripe.item(stripe.metered_price())])]
        held.meters = {stripe.METER: stripe.meter()}
        held.summaries = [(stripe.CUSTOMER, stripe.summary(47.0))]
        return held

    async def test_the_meters_summary_over_the_period_asked(self):
        held = self.metered()
        usage = await provider(held).get_usage(stripe.SUBSCRIPTION, SEPTEMBER)
        assert usage.metric == "api_call_blocks_100"
        assert usage.quantity == 47
        [(args, params)] = held.calls_to("v1.billing.meters.event_summaries.list_async")
        assert args == (stripe.METER,)
        assert (params["customer"], params["start_time"], params["end_time"]) == (
            stripe.CUSTOMER,
            SEP_1,
            OCT_1,
        )

    async def test_no_summary_is_none(self):
        held = self.metered()
        held.summaries = []
        assert await provider(held).get_usage(stripe.SUBSCRIPTION, SEPTEMBER) is None

    async def test_a_shared_meter_is_none_and_never_read(self):
        held = self.metered()
        other = stripe.subscription(
            [stripe.item(stripe.metered_price(), id="si_2")], id="sub_RbOther0000001"
        )
        held.subscriptions.append(other)
        assert await provider(held).get_usage(stripe.SUBSCRIPTION, SEPTEMBER) is None
        assert not held.calls_to("v1.billing.meters.event_summaries.list_async")

    async def test_an_unknown_subscription_is_none(self):
        assert await provider(self.metered()).get_usage("sub_RbMissing0000001", SEPTEMBER) is None

    async def test_a_period_off_whole_minutes_is_refused(self):
        start, end = SEPTEMBER
        with pytest.raises(ProviderUnsupported, match="unrepresentable_period"):
            await provider(self.metered()).get_usage(
                stripe.SUBSCRIPTION, (start + timedelta(seconds=30), end)
            )


class TestErrors:
    """FR-020: by category, never the platform's text."""

    @pytest.mark.parametrize(
        ("error", "expected", "category"),
        [
            (stub.APIConnectionError(SENTINEL), ProviderUnavailable, None),
            (stub.RateLimitError(SENTINEL), ProviderUnavailable, None),
            (stub.APIError(SENTINEL), ProviderUnavailable, None),
            (stub.AuthenticationError(SENTINEL), ProviderConfigurationError, "authentication"),
            (stub.PermissionError(SENTINEL), ProviderConfigurationError, "permission"),
            (stub.InvalidRequestError(SENTINEL), ProviderConfigurationError, "invalid_request"),
            (stub.InvalidRequestError(SENTINEL, code="resource_missing"), NotFound, None),
        ],
    )
    async def test_a_stripe_error_becomes_the_ports(self, error, expected, category):
        held = account()
        held.failures["v1.customers.retrieve_async"] = error
        with pytest.raises(expected) as raised:
            await provider(held).get_customer(stripe.CUSTOMER)
        assert SENTINEL not in str(raised.value)
        assert raised.value.__cause__ is None and raised.value.__suppress_context__
        if category:
            assert raised.value.category == category

    async def test_another_exception_is_not_disguised(self):
        held = account()
        held.failures["v1.customers.retrieve_async"] = KeyError("a bug of ours")
        with pytest.raises(KeyError):
            await provider(held).get_customer(stripe.CUSTOMER)

    async def test_the_time_budget_ends_a_read(self):
        held = account()
        held.delay = 0.2
        with pytest.raises(ProviderUnavailable, match="time budget"):
            await provider(held, budget=0.05).list_customers()


class TestReferences:
    def test_stripe_invoice_identifiers_only(self):
        adapter = provider(account())
        text = "Facture in_1RbWorld00000001, pas inv_002."
        assert adapter.invoice_references(text) == {"in_1RbWorld00000001"}
        assert adapter.invoice_reference_spans(text) == [(8, 27, "in_1RbWorld00000001")]


class TestTheSdkSaysNothing:
    """The SDK logs Stripe's error message before raising, and prints it when
    console logging is on: nothing Stripe answered reaches a log or stderr."""

    def canned(self, sdk, status: int, body: dict):
        class Canned(sdk.HTTPClient):
            name = "canned"

            async def request_async(self, method, url, headers, post_data=None):
                return json.dumps(body).encode(), status, {"request-id": "req_RbCanned"}

            async def close_async(self):
                return None

        return Canned()

    async def test_an_error_stripe_answered_is_in_no_log_and_not_on_stderr(
        self, caplog, capsys, monkeypatch
    ):
        sdk = pytest.importorskip("stripe")
        monkeypatch.delenv("STRIPE_LOG", raising=False)
        caplog.set_level(logging.DEBUG)
        body = {
            "error": {
                "type": "invalid_request_error",
                "code": "resource_missing",
                "message": SENTINEL,
            }
        }
        reader = connect("rk_test_51RbAbc", retries=0, http_client=self.canned(sdk, 404, body))
        with pytest.raises(NotFound):
            await reader.customer(stripe.CUSTOMER)
        assert SENTINEL not in caplog.text
        assert SENTINEL not in capsys.readouterr().err

    def test_console_logging_from_the_environment_is_refused(self, monkeypatch):
        pytest.importorskip("stripe")
        monkeypatch.setenv("STRIPE_LOG", "debug")
        with pytest.raises(ProviderConfigurationError) as raised:
            connect("rk_test_51RbAbc")
        assert raised.value.category == "sdk_logging"

    def test_console_logging_set_on_the_sdk_is_refused(self, monkeypatch):
        sdk = pytest.importorskip("stripe")
        monkeypatch.delenv("STRIPE_LOG", raising=False)
        monkeypatch.setattr(sdk, "log", "info")
        with pytest.raises(ProviderConfigurationError):
            connect("rk_test_51RbAbc")


class TestAgainstTheRealSdk:
    """Where the SDK is installed: the stub and the mapping match it."""

    def test_the_stub_reads_have_the_sdks_parameter_names(self):
        sdk = pytest.importorskip("stripe")
        import inspect

        client = sdk.StripeClient("rk_test_51RbAbc")
        for path, names in stub.READS.items():
            method = client
            for part in path.split("."):
                method = getattr(method, part)
            assert tuple(inspect.signature(method).parameters) == names, path

    async def test_the_sdks_own_errors_map_the_same(self):
        sdk = pytest.importorskip("stripe")
        for error, expected in (
            (sdk.APIConnectionError(SENTINEL), ProviderUnavailable),
            (sdk.RateLimitError(SENTINEL), ProviderUnavailable),
            (sdk.APIError(SENTINEL), ProviderUnavailable),
            (sdk.AuthenticationError(SENTINEL), ProviderConfigurationError),
            (sdk.PermissionError(SENTINEL), ProviderConfigurationError),
            (sdk.InvalidRequestError(SENTINEL, "id", code="resource_missing"), NotFound),
        ):
            held = account()
            held.failures["v1.customers.retrieve_async"] = error
            with pytest.raises(expected):
                await provider(held).get_customer(stripe.CUSTOMER)
