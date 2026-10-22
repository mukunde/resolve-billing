"""Builds the test worlds of `scripts/stripe_worlds.py` on a Stripe test account.

specs/001-stripe-read-adapter, plan section 6. Run by the maintainer,
locally -- never by an agent -- with the secret test key in `STRIPE_SEED_KEY`:

    python -m dotenv run -- python -m scripts.stripe_seed

Anything but a secret test key is refused before the SDK is even loaded.
Every world is built on the month that ends just before the run, with the
amounts of its fake counterpart, on its own test clock. A run completes what
is missing and makes nothing twice. A world is recorded -- the invoice a
dispute targets and the instant to investigate it at, on its customer --
only once its facts, read back through the adapter's own mapping, hold. A
world that cannot be built, or whose facts do not hold, is reported, never
dropped: removing a world from the contract needs the maintainer.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.application.investigate_dispute import DisputeInvestigator
from app.decision.mock import MockDecisionEngine
from app.domain.enums import InvoiceStatus, PaymentStatus, SubscriptionStatus
from app.domain.models import Dispute, Invoice, Payment, Subscription, Usage
from app.providers.base import NotFound, ProviderUnavailable, ProviderUnsupported
from app.providers.stripe.adapter import StripeBillingProvider
from app.providers.stripe.client import API_VERSION, NETWORK_RETRIES, StripeReader, silence
from app.providers.stripe.keys import ProviderConfigurationError, check_seed_key
from scripts import stripe_worlds as worlds

MINUTE, HOUR, DAY = 60, 3600, 86400
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Every amount is in euros, as the fake counterparts are.
CURRENCY = "eur"

#: What each target invoice totals, in cents: its fake counterpart's
#: (SC-001 compares the whole proposal, amount included).
TARGET_TOTALS = {
    "duplicate": 9900,
    "proration": 9900,
    "billed_after_cancellation": 4900,
    "failed_payment": 4900,
    "usage": 2350,
}

#: The usage world's blocks of 100 API calls, at 0.50 each: 23.50.
BLOCKS = 47

#: Stripe's test cards: one that pays, one that attaches and then declines.
CARD = "pm_card_visa"
DECLINING_CARD = "pm_card_chargeCustomerFail"

#: Metadata on everything a world makes: which step made it.
STEP = "rb_step"
CARD_SET = "rb_card"

#: Shared by every world, with fixed identifiers or lookup keys.
PRODUCTS = {
    "rb_pro": "Pro",
    "rb_enterprise": "Enterprise",
    "rb_addon": "Add-on",
    "rb_blocks": "API call blocks",
}
METER_EVENT = "rb_api_call_blocks"
COUPON = "rb_ten_percent"
TAX_RATE = "shared/tax_rate"
MONTHLY = {"interval": "month"}
PRICES: dict[str, dict[str, Any]] = {
    "rb_pro_monthly": {"product": "rb_pro", "unit_amount": 4900, "recurring": MONTHLY},
    "rb_pro_yearly": {"product": "rb_pro", "unit_amount": 49000, "recurring": {"interval": "year"}},
    "rb_pro_fractional": {
        "product": "rb_pro",
        "unit_amount_decimal": "4900.5",
        "recurring": MONTHLY,
    },
    "rb_pro_once": {"product": "rb_pro", "unit_amount": 4900},
    "rb_enterprise_monthly": {
        "product": "rb_enterprise",
        "unit_amount": 9900,
        "recurring": MONTHLY,
    },
    "rb_enterprise_once": {"product": "rb_enterprise", "unit_amount": 9900},
    "rb_addon_monthly": {"product": "rb_addon", "unit_amount": 1000, "recurring": MONTHLY},
    # The meter is filled in once it exists.
    "rb_blocks_metered": {
        "product": "rb_blocks",
        "unit_amount": 50,
        "recurring": {"interval": "month", "usage_type": "metered"},
    },
}

#: How long the seed waits for Stripe -- a clock to settle, usage to be
#: aggregated -- and how often it looks. Past the bound the world is reported:
#: a delay is never read as a zero.
WAIT_WITHIN = 300
LOOK_EVERY = 5


# -- dates -------------------------------------------------------------------


@dataclass(frozen=True)
class Dates:
    """The month that ends just before the run (decided 2026-10-23), in Unix
    seconds: `start` to `end`, and `previous`, one month before `start`, for
    a world that needs history."""

    start: int
    end: int
    previous: int

    @property
    def middle(self) -> int:
        return self.start + (self.end - self.start) // 2

    @property
    def event_day(self) -> int:
        """When usage is reported: a day before the month ends -- in the past,
        and inside the 35 days Stripe accepts meter events for."""
        return self.end - DAY

    @classmethod
    def before(cls, run_at: datetime) -> Dates:
        """With `last` the day before the run, the month starts on the same day
        of the month before `last` -- at most the 28th, so that adding a month
        is exact, as Stripe adds it -- and ends one month later."""
        last = (run_at.astimezone(UTC) - timedelta(days=1)).date()
        start = _months(last.replace(day=min(last.day, 28)), -1)
        return cls(
            start=_midnight(start),
            end=_midnight(_months(start, 1)),
            previous=_midnight(_months(start, -1)),
        )


def _months(day: date, count: int) -> date:
    months = day.year * 12 + day.month - 1 + count
    return day.replace(year=months // 12, month=months % 12 + 1)


def _midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def starts_at(name: str, dates: Dates) -> int:
    """When a world's clock starts."""
    if name in ("billed_after_cancellation", "failed_payment"):
        return dates.previous
    if name == "unsupported_unrepresentable_period":
        return dates.start + 30  # off a whole minute, on purpose
    return dates.start


# -- what a run says ---------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    world: str
    #: "complete" (an earlier run recorded it), "built" or "reported".
    state: str
    target: str | None = None
    instant: datetime | None = None
    detail: str = ""

    def line(self) -> str:
        if self.state == "reported":
            return f"{self.world}: REPORTED -- {self.detail}"
        return (
            f"{self.world}: {self.state}, target {self.target}, "
            f"investigate at {self.instant.isoformat()}"
        )


class NotReady(Exception):  # noqa: N818 -- a state, not a programming error
    """Stripe had not settled within the seed's bound."""


def _is_stripe(error: Exception) -> bool:
    return "StripeError" in {cls.__name__ for cls in type(error).__mro__}


def _missing(error: Exception) -> bool:
    return _is_stripe(error) and getattr(error, "code", None) == "resource_missing"


def _refusal(error: Exception) -> str:
    """A Stripe refusal, by its structured fields -- the request id finds it in
    the Dashboard's logs -- never by its message."""
    return (
        f"Stripe refused: {type(error).__name__} code={getattr(error, 'code', None)} "
        f"param={getattr(error, 'param', None)} request={getattr(error, 'request_id', None)}"
    )


# -- the seeder ----------------------------------------------------------------

Builder = Callable[["World"], Awaitable[str]]
Check = Callable[["World", str], Awaitable[list[str]]]


class Seeder:
    def __init__(
        self,
        sdk: Any,
        *,
        run_at: datetime,
        worlds: tuple[worlds.World, ...] = worlds.WORLDS,
        builders: dict[str, Builder] | None = None,
        checks: dict[str, Check] | None = None,
        wait_within: float = WAIT_WITHIN,
        look_every: float = LOOK_EVERY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.v1 = sdk.v1
        self.dates = Dates.before(run_at)
        self.provider = StripeBillingProvider(StripeReader(sdk))
        self.worlds = worlds
        self.builders = builders if builders is not None else BUILDERS
        self.checks = checks if checks is not None else CHECKS
        self.wait_within = wait_within
        self.look_every = look_every
        self.sleep = sleep
        self.monotonic = monotonic
        self.prices: dict[str, str] = {}
        self.meter_id: str | None = None
        self.tax_rate_id: str | None = None

    async def run(self) -> list[Outcome]:
        try:
            await self._shared()
        except Exception as error:
            if not _is_stripe(error):
                raise
            detail = f"shared objects: {_refusal(error)}"
            return [Outcome(spec.name, "reported", detail=detail) for spec in self.worlds]
        return [await self._world(spec) for spec in self.worlds]

    async def _world(self, spec: worlds.World) -> Outcome:
        try:
            clock = await self._clock(spec)
            customer = await self._customer(spec, clock)
            recorded = customer.get("metadata") or {}
            if worlds.TARGET_INVOICE in recorded:
                instant = datetime.fromisoformat(recorded[worlds.INVESTIGATE_AT])
                return Outcome(spec.name, "complete", recorded[worlds.TARGET_INVOICE], instant)

            world = World(self, spec, clock, customer)
            target = await self.builders[spec.name](world)
            problems = await self.checks[spec.name](world, target)
            if problems:
                return Outcome(spec.name, "reported", target=target, detail="; ".join(problems))
            instant = datetime.fromtimestamp(await world.now() + MINUTE, UTC)
            await self.call(
                self.v1.customers.update_async,
                customer["id"],
                params={
                    "metadata": {
                        worlds.TARGET_INVOICE: target,
                        worlds.INVESTIGATE_AT: instant.isoformat(),
                    }
                },
            )
            return Outcome(spec.name, "built", target, instant)
        except (NotReady, NotFound, ProviderUnavailable, ProviderConfigurationError) as error:
            return Outcome(spec.name, "reported", detail=str(error))
        except Exception as error:
            if not _is_stripe(error):
                raise  # a defect of the seed, not something Stripe said
            return Outcome(spec.name, "reported", detail=_refusal(error))

    async def _clock(self, spec: worlds.World) -> dict:
        name = f"rb:{spec.name}"
        for clock in await self.every(self.v1.test_helpers.test_clocks.list_async):
            if clock.get("name") == name:
                return clock
        return await self.call(
            self.v1.test_helpers.test_clocks.create_async,
            params={"frozen_time": starts_at(spec.name, self.dates), "name": name},
        )

    async def _customer(self, spec: worlds.World, clock: dict) -> dict:
        customers = await self.every(
            self.v1.customers.list_async, params={"test_clock": clock["id"]}
        )
        customer = next(
            (c for c in customers if (c.get("metadata") or {}).get(worlds.WORLD) == spec.name),
            None,
        )
        if customer is None:
            customer = await self.call(
                self.v1.customers.create_async,
                params={
                    "email": spec.email,
                    "name": f"RB {spec.name}",
                    "test_clock": clock["id"],
                    "metadata": {worlds.WORLD: spec.name},
                },
            )
        if not (customer.get("invoice_settings") or {}).get("default_payment_method"):
            customer = await self.card(customer, CARD)
        return customer

    async def card(self, customer: dict, token: str) -> dict:
        """`token`'s card as the customer's default, once."""
        if (customer.get("metadata") or {}).get(CARD_SET) == token:
            return customer
        method = await self.call(
            self.v1.payment_methods.attach_async, token, params={"customer": customer["id"]}
        )
        return await self.call(
            self.v1.customers.update_async,
            customer["id"],
            params={
                "invoice_settings": {"default_payment_method": method["id"]},
                "metadata": {CARD_SET: token},
            },
        )

    async def _shared(self) -> None:
        for product, name in PRODUCTS.items():
            try:
                await self.call(self.v1.products.retrieve_async, product)
            except Exception as error:
                if not _missing(error):
                    raise
                await self.call(self.v1.products.create_async, params={"id": product, "name": name})

        meters = await self.every(self.v1.billing.meters.list_async, params={"status": "active"})
        meter = next((m for m in meters if m.get("event_name") == METER_EVENT), None)
        if meter is None:
            meter = await self.call(
                self.v1.billing.meters.create_async,
                params={
                    "display_name": "RB API call blocks",
                    "event_name": METER_EVENT,
                    "default_aggregation": {"formula": "sum"},
                    "customer_mapping": {
                        "type": "by_id",
                        "event_payload_key": "stripe_customer_id",
                    },
                    "value_settings": {"event_payload_key": "value"},
                },
            )
        self.meter_id = meter["id"]

        for lookup_key, spec in PRICES.items():
            found = await self.every(
                self.v1.prices.list_async, params={"lookup_keys": [lookup_key]}
            )
            if not found:
                params = {**spec, "currency": CURRENCY, "lookup_key": lookup_key}
                if spec.get("recurring", {}).get("usage_type") == "metered":
                    params["recurring"] = {**spec["recurring"], "meter": self.meter_id}
                found = [await self.call(self.v1.prices.create_async, params=params)]
            self.prices[lookup_key] = found[0]["id"]

        try:
            await self.call(self.v1.coupons.retrieve_async, COUPON)
        except Exception as error:
            if not _missing(error):
                raise
            await self.call(
                self.v1.coupons.create_async,
                params={"id": COUPON, "percent_off": 10, "duration": "forever"},
            )

        rates = await self.every(self.v1.tax_rates.list_async, params={"active": True})
        rate = next((r for r in rates if (r.get("metadata") or {}).get(STEP) == TAX_RATE), None)
        if rate is None:
            rate = await self.call(
                self.v1.tax_rates.create_async,
                params={
                    "display_name": "TVA",
                    "percentage": 20,
                    "inclusive": False,
                    "metadata": {STEP: TAX_RATE},
                },
            )
        self.tax_rate_id = rate["id"]

    # -- the two primitives ------------------------------------------------

    async def every(self, method, *args: Any, params: dict | None = None) -> list[dict]:
        params = {**(params or {}), "limit": 100}
        found: list[dict] = []
        while True:
            page = await self.call(method, *args, params=params)
            found.extend(page["data"])
            if not page.get("has_more") or not page["data"]:
                return found
            params = {**params, "starting_after": page["data"][-1]["id"]}

    async def call(self, method, *args: Any, params: dict | None = None) -> dict:
        response = await method(*args, params=params) if params is not None else await method(*args)
        return response.to_dict() if hasattr(response, "to_dict") else dict(response)

    async def wait_for(self, what: str, ready: Callable[[], Awaitable[bool]]) -> None:
        """Until `ready()`, looking every few seconds, within the bound."""
        deadline = self.monotonic() + self.wait_within
        while not await ready():
            if self.monotonic() >= deadline:
                raise NotReady(f"{what}: not ready within {self.wait_within:g}s")
            await self.sleep(self.look_every)


@dataclass
class World:
    """One world being built: its clock, its customer, and the steps."""

    seeder: Seeder
    spec: worlds.World
    clock: dict
    customer: dict

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def customer_id(self) -> str:
        return self.customer["id"]

    @property
    def dates(self) -> Dates:
        return self.seeder.dates

    @property
    def v1(self) -> Any:
        return self.seeder.v1

    def price(self, lookup_key: str) -> str:
        return self.seeder.prices[lookup_key]

    async def call(self, method, *args: Any, params: dict | None = None) -> dict:
        return await self.seeder.call(method, *args, params=params)

    async def wait_for(self, what: str, ready: Callable[[], Awaitable[bool]]) -> None:
        await self.seeder.wait_for(what, ready)

    async def once(self, step: str, *, find, where: dict, create, params: dict) -> dict:
        """The object `step` made on an earlier run, or a new one, tagged."""
        tag = f"{self.name}/{step}"
        for found in await self.seeder.every(find, params=where):
            if (found.get("metadata") or {}).get(STEP) == tag:
                return found
        metadata = {**params.get("metadata", {}), worlds.WORLD: self.name, STEP: tag}
        return await self.call(create, params={**params, "metadata": metadata})

    # -- the clock -----------------------------------------------------------

    async def now(self) -> int:
        clock = await self.call(self.v1.test_helpers.test_clocks.retrieve_async, self.clock["id"])
        return clock["frozen_time"]

    async def advance(self, to: int) -> None:
        """The clock at `to` at least, and settled: never moved back."""
        if await self.now() < to:
            await self.call(
                self.v1.test_helpers.test_clocks.advance_async,
                self.clock["id"],
                params={"frozen_time": to},
            )

        async def settled() -> bool:
            clock = await self.call(
                self.v1.test_helpers.test_clocks.retrieve_async, self.clock["id"]
            )
            return clock["status"] == "ready"

        await self.wait_for(f"clock rb:{self.name}", settled)

    # -- the steps a world is made of -----------------------------------------

    async def card(self, token: str) -> None:
        self.customer = await self.seeder.card(self.customer, token)

    async def subscription(self, step: str, *prices: str, **extra: Any) -> dict:
        return await self.once(
            step,
            find=self.v1.subscriptions.list_async,
            where={"customer": self.customer_id, "status": "all"},
            create=self.v1.subscriptions.create_async,
            params={
                "customer": self.customer_id,
                "items": [{"price": self.price(p)} for p in prices],
                **extra,
            },
        )

    async def item(
        self,
        step: str,
        *,
        subscription: dict,
        period: tuple[int, int],
        price: str | None = None,
        amount: int | None = None,
    ) -> dict:
        """An invoice item on the subscription, for an explicit period."""
        params: dict[str, Any] = {
            "customer": self.customer_id,
            "subscription": subscription["id"],
            "period": {"start": period[0], "end": period[1]},
        }
        if price is not None:
            params["pricing"] = {"price": self.price(price)}
        else:
            params.update(amount=amount, currency=CURRENCY)
        return await self.once(
            step,
            find=self.v1.invoice_items.list_async,
            where={"customer": self.customer_id},
            create=self.v1.invoice_items.create_async,
            params=params,
        )

    async def invoice(self, step: str, subscription: dict) -> dict:
        """An invoice on the subscription, taking its pending items."""
        return await self.once(
            step,
            find=self.v1.invoices.list_async,
            where={"customer": self.customer_id},
            create=self.v1.invoices.create_async,
            params={
                "customer": self.customer_id,
                "subscription": subscription["id"],
                "pending_invoice_items_behavior": "include",
                "auto_advance": False,
            },
        )

    async def latest_invoice(self, subscription: dict) -> dict:
        current = await self.call(self.v1.subscriptions.retrieve_async, subscription["id"])
        latest = current["latest_invoice"]
        return await self.call(
            self.v1.invoices.retrieve_async, latest if isinstance(latest, str) else latest["id"]
        )

    async def finalized(self, invoice: dict) -> dict:
        invoice = await self.call(self.v1.invoices.retrieve_async, invoice["id"])
        if invoice["status"] == "draft":
            invoice = await self.call(
                self.v1.invoices.finalize_invoice_async,
                invoice["id"],
                params={"auto_advance": False},
            )
        return invoice

    async def paid(self, invoice: dict) -> dict:
        invoice = await self.finalized(invoice)
        if invoice["status"] == "open":
            invoice = await self.call(self.v1.invoices.pay_async, invoice["id"])
        return invoice

    async def repriced(self, subscription: dict, price: str) -> dict:
        """The subscription's item moved to `price`, without proration."""
        current = await self.call(self.v1.subscriptions.retrieve_async, subscription["id"])
        item = current["items"]["data"][0]
        if item["price"]["id"] == self.price(price):
            return current
        return await self.call(
            self.v1.subscriptions.update_async,
            subscription["id"],
            params={
                "items": [{"id": item["id"], "price": self.price(price)}],
                "proration_behavior": "none",
            },
        )

    async def cancelled(self, subscription: dict) -> dict:
        """Ended now, with a final invoice that takes the pending items."""
        current = await self.call(self.v1.subscriptions.retrieve_async, subscription["id"])
        if current["status"] == "canceled":
            return current
        return await self.call(
            self.v1.subscriptions.cancel_async,
            subscription["id"],
            params={"invoice_now": True, "prorate": False},
        )

    async def summary(self) -> Decimal | None:
        """The meter's summary for the month and this customer; None when
        Stripe has none yet -- never read as zero."""
        found = await self.seeder.provider.reader.meter_summaries(
            self.seeder.meter_id, self.customer_id, self.dates.start, self.dates.end
        )
        if not found:
            return None
        return sum((Decimal(str(s["aggregated_value"])) for s in found), Decimal(0))

    async def usage_reported(self, value: int) -> None:
        """One meter event of `value`, sent at the clock's time, once: a fixed
        identifier, and nothing sent once the summary shows usage."""
        if (await self.summary() or 0) > 0:
            return
        await self.call(
            self.v1.billing.meter_events.create_async,
            params={
                "event_name": METER_EVENT,
                "identifier": f"rb-{self.name}-{self.dates.start}",
                "timestamp": await self.now(),
                "payload": {"stripe_customer_id": self.customer_id, "value": str(value)},
            },
        )

        async def aggregated() -> bool:
            return await self.summary() == value

        await self.wait_for(f"a meter summary of {value}", aggregated)


# -- the worlds ----------------------------------------------------------------


async def _duplicate(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_enterprise_monthly")
    await w.paid(await w.latest_invoice(subscription))
    await w.advance(w.dates.start + DAY)
    await w.item(
        "item",
        subscription=subscription,
        period=(w.dates.start, w.dates.end),
        price="rb_enterprise_once",
    )
    return (await w.paid(await w.invoice("invoice", subscription)))["id"]


async def _proration(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_pro_monthly")
    await w.paid(await w.latest_invoice(subscription))
    await w.advance(w.dates.middle)
    await w.repriced(subscription, "rb_enterprise_monthly")
    await w.item(
        "item",
        subscription=subscription,
        period=(w.dates.middle, w.dates.end),
        price="rb_enterprise_once",
    )
    return (await w.finalized(await w.invoice("invoice", subscription)))["id"]


async def _billed_after_cancellation(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_pro_monthly")
    await w.paid(await w.latest_invoice(subscription))
    await w.advance(w.dates.start - DAY)
    await w.item(
        "item", subscription=subscription, period=(w.dates.start, w.dates.end), price="rb_pro_once"
    )
    subscription = await w.cancelled(subscription)
    return (await w.paid(await w.latest_invoice(subscription)))["id"]


async def _failed_payment(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_pro_monthly")
    await w.paid(await w.latest_invoice(subscription))
    await w.advance(w.dates.start - DAY)
    await w.card(DECLINING_CARD)
    # The renewal is finalised an hour after it is made, then collected.
    await w.advance(w.dates.start + HOUR + MINUTE)
    return (await w.latest_invoice(subscription))["id"]


async def _usage(w: World) -> str:
    base = await w.subscription("base", "rb_pro_monthly")
    await w.paid(await w.latest_invoice(base))
    metered = await w.subscription("metered", "rb_blocks_metered")
    await w.advance(w.dates.event_day)
    await w.usage_reported(BLOCKS)
    await w.advance(w.dates.end + HOUR + MINUTE)
    return (await w.latest_invoice(metered))["id"]


def _first_invoice_of(*prices: str, card: str | None = None, **extra: Any) -> Builder:
    """A world whose target is its subscription's first invoice."""

    async def build(w: World) -> str:
        if card is not None:
            await w.card(card)
        subscription = await w.subscription("subscription", *prices, **extra)
        return (await w.latest_invoice(subscription))["id"]

    return build


async def _negative_line(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_pro_monthly")
    await w.paid(await w.latest_invoice(subscription))
    await w.advance(w.dates.start + DAY)
    period = (w.dates.start, w.dates.end)
    await w.item("item", subscription=subscription, period=period, price="rb_pro_once")
    await w.item("credit", subscription=subscription, period=period, amount=-1000)
    return (await w.finalized(await w.invoice("invoice", subscription)))["id"]


async def _partial_refund(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_pro_monthly")
    first = await w.paid(await w.latest_invoice(subscription))
    [payment] = await w.seeder.provider.reader.invoice_payments(first["id"])
    intent = payment["payment"]["payment_intent"]["id"]
    await w.once(
        "refund",
        find=w.v1.refunds.list_async,
        where={"payment_intent": intent},
        create=w.v1.refunds.create_async,
        params={"payment_intent": intent, "amount": 1000},
    )
    return first["id"]


async def _multi_invoice_payment(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_pro_monthly")
    await w.paid(await w.latest_invoice(subscription))
    await w.advance(w.dates.start + DAY)
    period = (w.dates.start, w.dates.end)
    invoices = []
    for n in (1, 2):
        await w.item(f"item_{n}", subscription=subscription, period=period, price="rb_pro_once")
        invoices.append(await w.finalized(await w.invoice(f"invoice_{n}", subscription)))
    intent = await w.once(
        "payment",
        find=w.v1.payment_intents.list_async,
        where={"customer": w.customer_id},
        create=w.v1.payment_intents.create_async,
        params={
            "customer": w.customer_id,
            "amount": 2 * 4900,
            "currency": CURRENCY,
            "payment_method": w.customer["invoice_settings"]["default_payment_method"],
            "confirm": True,
            "off_session": True,
        },
    )
    for invoice in invoices:
        attached = await w.seeder.provider.reader.invoice_payments(invoice["id"])
        if not any(p["payment"]["payment_intent"]["id"] == intent["id"] for p in attached):
            await w.call(
                w.v1.invoices.attach_payment_async,
                invoice["id"],
                params={"payment_intent": intent["id"]},
            )
    return invoices[0]["id"]


def arrears_due(name: str, dates: Dates) -> int:
    """When a metered subscription started with its world's clock has its
    first arrears invoice finalised: fixed by the dates, so a second run
    never moves the clock further."""
    return starts_at(name, dates) + (dates.end - dates.start) + HOUR + MINUTE


async def _arrears(w: World) -> str:
    """A metered subscription's invoice for its first period, once it ended."""
    subscription = await w.subscription("subscription", "rb_blocks_metered")
    await w.advance(arrears_due(w.name, w.dates))
    return (await w.latest_invoice(subscription))["id"]


async def _invoice_credit(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_blocks_metered")
    await w.once(
        "credit",
        find=w.v1.billing.credit_grants.list_async,
        where={"customer": w.customer_id},
        create=w.v1.billing.credit_grants.create_async,
        params={
            "customer": w.customer_id,
            "name": "RB billing credit",
            "category": "promotional",
            "amount": {"type": "monetary", "monetary": {"currency": CURRENCY, "value": 1000}},
            "applicability_config": {"scope": {"price_type": "metered"}},
        },
    )
    await w.advance(w.dates.event_day)
    await w.usage_reported(BLOCKS)
    await w.advance(w.dates.end + HOUR + MINUTE)
    return (await w.latest_invoice(subscription))["id"]


async def _tax(w: World) -> str:
    rate = w.seeder.tax_rate_id
    return await _first_invoice_of("rb_pro_monthly", default_tax_rates=[rate])(w)


BUILDERS: dict[str, Builder] = {
    "duplicate": _duplicate,
    "proration": _proration,
    "billed_after_cancellation": _billed_after_cancellation,
    "failed_payment": _failed_payment,
    "usage": _usage,
    "unsupported_mixed_item_periods": _first_invoice_of(
        "rb_pro_monthly", "rb_pro_yearly", billing_mode={"type": "flexible"}
    ),
    "unsupported_multiple_prices": _first_invoice_of("rb_pro_monthly", "rb_addon_monthly"),
    "unsupported_subscription_status": _first_invoice_of(
        "rb_pro_monthly", card=DECLINING_CARD, payment_behavior="default_incomplete"
    ),
    "unsupported_invoice_tax": _tax,
    "unsupported_invoice_discount": _first_invoice_of(
        "rb_pro_monthly", discounts=[{"coupon": COUPON}]
    ),
    "unsupported_negative_line": _negative_line,
    "unsupported_fractional_unit_price": _first_invoice_of("rb_pro_fractional"),
    "unsupported_partial_refund": _partial_refund,
    "unsupported_multi_invoice_payment": _multi_invoice_payment,
    "unsupported_unrepresentable_period": _arrears,
    "unsupported_invoice_credit": _invoice_credit,
}


# -- the facts each world exists for -------------------------------------------
# Pure functions on domain objects: tested on the fake counterparts
# themselves, so a Stripe world that passes them holds the same facts.


def total_facts(target: Invoice, expected_minor: int) -> list[str]:
    total = target.total
    if total.amount_minor == expected_minor and total.currency == CURRENCY.upper():
        return []
    return [f"its total is {total.amount_minor} {total.currency}, not {expected_minor}"]


def duplicate_facts(target: Invoice, invoices: list[Invoice]) -> list[str]:
    twins = [
        other
        for other in invoices
        if other.id != target.id
        and other.status is not InvoiceStatus.VOID
        and target.subscription_id is not None
        and other.subscription_id == target.subscription_id
        and other.total == target.total
        and target.overlaps_period_of(other)
    ]
    if twins:
        return []
    return ["no other live invoice on its subscription with its total and an overlapping period"]


def proration_facts(target: Invoice, subscription: Subscription) -> list[str]:
    half = [
        line
        for line in target.lines
        if line.period_seconds is not None
        and abs(2 * line.period_seconds - subscription.period_seconds) <= MINUTE
        and line.quantity == 1
        and line.amount == line.unit_amount
    ]
    return [] if half else ["no line covering half its subscription's period at the full price"]


def cancellation_facts(target: Invoice, subscription: Subscription) -> list[str]:
    ended = subscription.cancelled_at
    if subscription.status is not SubscriptionStatus.CANCELLED or ended is None:
        return ["its subscription has not ended"]
    if target.period_start is None or target.period_start <= ended:
        return ["its period does not start after its subscription ended"]
    return []


def failed_payment_facts(target: Invoice, payments: list[Payment]) -> list[str]:
    on_it = [p for p in payments if p.invoice_id == target.id]
    problems = []
    if not any(p.status is PaymentStatus.FAILED for p in on_it):
        problems.append("no failed payment on it")
    if any(p.status in (PaymentStatus.SUCCEEDED, PaymentStatus.REFUNDED) for p in on_it):
        problems.append("a payment on it was collected")
    return problems


def usage_facts(target: Invoice, usage: Usage | None) -> list[str]:
    metered = [line for line in target.lines if line.metric]
    if len(metered) != 1:
        return ["not exactly one metered line"]
    if usage is None:
        return ["no usage read for its period"]
    [line] = metered
    if (usage.metric, usage.period_start, usage.period_end) != (
        line.metric,
        line.period_start,
        line.period_end,
    ):
        return ["the usage read is not its line's metric and period"]
    if usage.quantity != line.quantity:
        return [f"usage {usage.quantity} against {line.quantity} billed"]
    return []


async def _duplicate_read(w: World, target: Invoice) -> list[str]:
    return duplicate_facts(target, await w.seeder.provider.list_invoices(w.customer_id, EPOCH))


async def _proration_read(w: World, target: Invoice) -> list[str]:
    return proration_facts(target, await w.seeder.provider.get_subscription(target.subscription_id))


async def _cancellation_read(w: World, target: Invoice) -> list[str]:
    subscription = await w.seeder.provider.get_subscription(target.subscription_id)
    return cancellation_facts(target, subscription)


async def _failed_payment_read(w: World, target: Invoice) -> list[str]:
    return failed_payment_facts(target, await w.seeder.provider.list_payments(w.customer_id))


async def _usage_read(w: World, target: Invoice) -> list[str]:
    metered = [line for line in target.lines if line.metric]
    if len(metered) != 1:
        return usage_facts(target, None)
    period = (metered[0].period_start, metered[0].period_end)
    return usage_facts(target, await w.seeder.provider.get_usage(target.subscription_id, period))


def _supported(name: str, read: Callable[[World, Invoice], Awaitable[list[str]]]) -> Check:
    async def check(w: World, target_id: str) -> list[str]:
        try:
            target = await w.seeder.provider.get_invoice(target_id)
            if target.subscription_id is None:
                return ["the target belongs to no subscription"]
            return total_facts(target, TARGET_TOTALS[name]) + await read(w, target)
        except ProviderUnsupported as error:
            return [f"the adapter refuses it: {error.reason}"]

    return check


async def _unsupported(w: World, target_id: str) -> list[str]:
    """Read as the investigation reads it, the world gives its reason."""
    reason = w.spec.unsupported
    dispute = Dispute(
        id=f"seed_{w.name}",
        customer_message="Pouvez-vous verifier ma facture ?",
        customer_email=w.spec.email,
        invoice_id=target_id,
    )
    investigator = DisputeInvestigator(provider=w.seeder.provider, engine=MockDecisionEngine())
    try:
        await investigator.load_context(
            dispute, datetime.fromtimestamp(await w.now() + MINUTE, UTC)
        )
    except ProviderUnsupported as error:
        return [] if error.reason == reason else [f"refused as {error.reason}, not {reason}"]
    return [f"read without a refusal; {reason} expected"]


CHECKS: dict[str, Check] = {
    "duplicate": _supported("duplicate", _duplicate_read),
    "proration": _supported("proration", _proration_read),
    "billed_after_cancellation": _supported("billed_after_cancellation", _cancellation_read),
    "failed_payment": _supported("failed_payment", _failed_payment_read),
    "usage": _supported("usage", _usage_read),
    **{w.name: _unsupported for w in worlds.UNSUPPORTED},
}


# -- running it ----------------------------------------------------------------


def main() -> int:
    key = os.environ.get("STRIPE_SEED_KEY")
    try:
        check_seed_key(key)
        import stripe

        silence(stripe)
    except ProviderConfigurationError as error:
        print(f"stripe_seed: refused ({error.category})", file=sys.stderr)
        return 2
    sdk = stripe.StripeClient(key, stripe_version=API_VERSION, max_network_retries=NETWORK_RETRIES)
    seeder = Seeder(sdk, run_at=datetime.now(UTC))
    start, end = (
        datetime.fromtimestamp(t, UTC).date() for t in (seeder.dates.start, seeder.dates.end)
    )
    print(f"stripe_seed: worlds on the month {start} to {end}")
    outcomes = asyncio.run(seeder.run())
    for outcome in outcomes:
        print(outcome.line())
    return 0 if all(o.state != "reported" for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
