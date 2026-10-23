"""Builds the test worlds of `scripts/stripe_worlds.py` on a Stripe test account.

specs/001-stripe-read-adapter, plan section 6. Run by the maintainer,
locally -- never by an agent -- with the secret test key in `STRIPE_SEED_KEY`:

    python -m dotenv run -- python -m scripts.stripe_seed

Anything but a secret test key is refused before the SDK is even loaded.
Every world is built on the month that ended just before its first run, with
the amounts of its fake counterpart, on its own test clock. The seed keeps a
manifest -- a customer of its own, without a clock -- naming each world's
generation, month and clock: a later run resumes the same world on the same
dates, and deleting a world's clock opens its next generation, with nothing
of the old one reused. A run completes what is missing and makes nothing
twice: every object is tagged with the step that made it and looked for
before it is made, and every creation carries an idempotency key of its
generation. The seed touches only what it made: a clock the manifest does not
name is never used, and a clock holding a customer it did not make, or a
shared object it would reuse but did not make or on other terms, is a
collision, reported and left alone.

A world is recorded -- the invoice a dispute targets and the instant to
investigate it at, on its customer -- only once its facts, read back through
the adapter's own mapping, hold; a recorded world is checked again on every
run. A world that cannot be built, or whose facts do not hold, is reported,
never dropped: removing a world from the contract needs the maintainer.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
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

#: What each target invoice totals, in cents -- its fake counterpart's
#: (SC-001 compares the whole proposal, amount included) -- and the state it
#: must be in.
TARGET_TOTALS = {
    "duplicate": 9900,
    "proration": 9900,
    "billed_after_cancellation": 4900,
    "failed_payment": 4900,
    "usage": 2350,
}
TARGET_STATUSES = {
    "duplicate": InvoiceStatus.PAID,
    "proration": InvoiceStatus.OPEN,
    "billed_after_cancellation": InvoiceStatus.PAID,
    "failed_payment": InvoiceStatus.OPEN,
    "usage": InvoiceStatus.PAID,
}

#: The usage world's blocks of 100 API calls, at 0.50 each: 23.50.
BLOCKS = 47

#: Stripe's test cards, one that pays and one that attaches then declines,
#: and the last four digits they leave on the payment method.
CARD = "pm_card_visa"
DECLINING_CARD = "pm_card_chargeCustomerFail"
LAST4 = {CARD: "4242", DECLINING_CARD: "0341"}

#: Metadata the seed sets: which step made an object, which card a customer
#: was given, where a world's one meter event stands, and which invoice a
#: subscription's step took (`none` when Stripe made none).
STEP = "rb_step"
CARD_SET = "rb_card"
USAGE_EVENT = "rb_usage_event"
TAKEN = "rb_taken_"

#: The seed's manifest: a customer of its own, without a clock, whose
#: metadata names each world's generation (`g_<world>`) and clock
#: (`c_<world>`). It has an address, so the impersonation check can list it.
MANIFEST = "rb_seed_manifest"
MANIFEST_EMAIL = f"seed-manifest@{worlds.DOMAIN}"

#: Stripe's billing reasons for the invoices a world takes.
CREATED = "subscription_create"
CYCLE = "subscription_cycle"

#: How long Stripe returns the first answer to an idempotency key: at least a
#: day. A creation the seed may have sent is re-sent with its key within it;
#: past it, the seed cannot tell, and asks the maintainer.
KEYS_REMEMBERED = 23 * 3600

#: One seed run at a time: two would each read the manifest and could open
#: one generation twice, on different months. The lock holds between runs
#: sharing this temporary directory -- one environment: Windows and WSL, say,
#: do not lock each other out, so the seed is always run from the same one.
LOCK = os.path.join(tempfile.gettempdir(), "rb-stripe-seed.lock")

#: Shared by every world, found by fixed identifiers or lookup keys and
#: tagged `shared/<kind>/<key>`; the meter, which has no metadata, by its
#: event name and checked for its configuration.
PRODUCTS = {
    "rb_pro": "Pro",
    "rb_enterprise": "Enterprise",
    "rb_addon": "Add-on",
    "rb_blocks": "API call blocks",
}
METER_EVENT = "rb_api_call_blocks"
METER = {
    "display_name": "RB API call blocks",
    "event_name": METER_EVENT,
    "default_aggregation": {"formula": "sum"},
    "customer_mapping": {"type": "by_id", "event_payload_key": "stripe_customer_id"},
    "value_settings": {"event_payload_key": "value"},
}
COUPON = {"id": "rb_ten_percent", "percent_off": 10, "duration": "forever"}
TAX_RATE = {"display_name": "TVA", "percentage": 20, "inclusive": False}
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
    """A world's month, in Unix seconds: `start` to `end`, and `previous`,
    one month before `start`, for a world that needs history."""

    start: int
    end: int
    previous: int

    @property
    def start_day(self) -> date:
        return datetime.fromtimestamp(self.start, UTC).date()

    @property
    def middle(self) -> int:
        return self.start + (self.end - self.start) // 2

    @property
    def event_day(self) -> int:
        """When usage is reported: a day before the month ends -- in the past,
        and inside the 35 days Stripe accepts meter events for."""
        return self.end - DAY

    @classmethod
    def starting(cls, day: date) -> Dates:
        return cls(
            start=_midnight(day),
            end=_midnight(_months(day, 1)),
            previous=_midnight(_months(day, -1)),
        )

    @classmethod
    def before(cls, run_at: datetime) -> Dates:
        """The month that ends just before the run (decided 2026-10-23). With
        `last` the day before the run, it starts on the same day of the month
        before `last` -- at most the 28th, so that adding a month is exact, as
        Stripe adds it -- and ends one month later."""
        last = (run_at.astimezone(UTC) - timedelta(days=1)).date()
        return cls.starting(_months(last.replace(day=min(last.day, 28)), -1))


def _months(day: date, count: int) -> date:
    months = day.year * 12 + day.month - 1 + count
    return day.replace(year=months // 12, month=months % 12 + 1)


def _midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


@dataclass(frozen=True)
class Generation:
    """One build of a world: its number -- the next one when its clock is
    deleted, so no key or object of the old build is reused -- its month,
    kept in the manifest so a run on another day resumes on the same dates,
    and when it was opened, to know whether Stripe still remembers its keys."""

    number: int
    dates: Dates
    opened: int

    def encode(self) -> str:
        return f"{self.number}@{self.dates.start_day.isoformat()}@{self.opened}"

    @classmethod
    def decode(cls, value: str) -> Generation:
        """What the manifest holds, or a Collision: a manifest the seed cannot
        read is not used."""
        try:
            number, day, opened = value.split("@")
            generation = cls(int(number), Dates.starting(date.fromisoformat(day)), int(opened))
        except ValueError:
            raise Collision(f"the manifest holds an unreadable generation: {value!r}") from None
        if generation.number < 1 or generation.dates.start_day.day > 28:
            raise Collision(f"the manifest holds an impossible generation: {value!r}")
        return generation


def clock_name(world: str, generation: Generation) -> str:
    """`rb:<world>@<first day of its month>/g<generation>`."""
    return f"rb:{world}@{generation.dates.start_day.isoformat()}/g{generation.number}"


def starts_at(name: str, dates: Dates) -> int:
    """When a world's clock starts."""
    if name in ("billed_after_cancellation", "failed_payment"):
        return dates.previous
    return dates.start


# -- what a run says ---------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    world: str
    #: "complete" (recorded earlier, and still holding), "built" or "reported".
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


class Unexpected(Exception):  # noqa: N818
    """Stripe answered, with a state the world does not expect."""


class Collision(Exception):  # noqa: N818
    """Something the seed would use or advance is not the seed's own."""


class Uncertain(Exception):  # noqa: N818
    """A write may or may not have reached Stripe: the maintainer reconciles."""


_REPORTED = (
    NotReady,
    Unexpected,
    Collision,
    Uncertain,
    NotFound,
    ProviderUnavailable,
    ProviderConfigurationError,
)


def _names(error: Exception) -> set[str]:
    return {cls.__name__ for cls in type(error).__mro__}


def _is_stripe(error: Exception) -> bool:
    return "StripeError" in _names(error)


def _missing(error: Exception) -> bool:
    return _is_stripe(error) and getattr(error, "code", None) == "resource_missing"


def _refusal(error: Exception) -> str:
    """What Stripe said, by its structured fields -- the request id finds it
    in the Dashboard's logs -- never by its message."""
    fields = (
        f"{type(error).__name__} code={getattr(error, 'code', None)} "
        f"param={getattr(error, 'param', None)} request={getattr(error, 'request_id', None)}"
    )
    if "APIConnectionError" in _names(error):
        return (
            f"no answer from Stripe ({fields}): the outcome is unknown; the next run "
            "looks for what this step may have made"
        )
    return f"Stripe refused: {fields}"


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
        self.run_at = int(run_at.timestamp())
        #: The month a world gets when its first run is this one.
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
        self.manifest: dict = {}

    async def run(self) -> list[Outcome]:
        try:
            await self._shared()
        except Exception as error:
            if not (_is_stripe(error) or isinstance(error, _REPORTED)):
                raise
            detail = f"shared objects: {_refusal(error) if _is_stripe(error) else error}"
            return [Outcome(spec.name, "reported", detail=detail) for spec in self.worlds]
        return [await self._world(spec) for spec in self.worlds]

    async def _world(self, spec: worlds.World) -> Outcome:
        try:
            clock, generation = await self._clock(spec)
            customer = await self._customer(spec, clock, generation)
            world = World(self, spec, clock, customer, generation)
            recorded = world.customer.get("metadata") or {}
            if worlds.TARGET_INVOICE in recorded:
                target = recorded[worlds.TARGET_INVOICE]
                problems = await self.checks[spec.name](world, target)
                if problems:
                    detail = "recorded, but no longer holds: " + "; ".join(problems)
                    return Outcome(spec.name, "reported", target=target, detail=detail)
                instant = datetime.fromisoformat(recorded[worlds.INVESTIGATE_AT])
                return Outcome(spec.name, "complete", target, instant)

            target = await self.builders[spec.name](world)
            problems = await self.checks[spec.name](world, target)
            if problems:
                return Outcome(spec.name, "reported", target=target, detail="; ".join(problems))
            instant = datetime.fromtimestamp(await world.now() + MINUTE, UTC)
            await self.call(
                self.v1.customers.update_async,
                world.customer_id,
                params={
                    "metadata": {
                        worlds.TARGET_INVOICE: target,
                        worlds.INVESTIGATE_AT: instant.isoformat(),
                    }
                },
            )
            return Outcome(spec.name, "built", target, instant)
        except _REPORTED as error:
            return Outcome(spec.name, "reported", detail=str(error))
        except Exception as error:
            if not _is_stripe(error):
                raise  # a defect of the seed, not something Stripe said
            return Outcome(spec.name, "reported", detail=_refusal(error))

    async def _manifest(self) -> dict:
        """The seed's own record: what makes a clock the seed's. Exactly one:
        two would each count generations, so neither can be trusted."""
        found = [
            customer
            for customer in await self.every(self.v1.customers.list_async)
            if (customer.get("metadata") or {}).get(MANIFEST) == "1"
        ]
        if len(found) > 1:
            raise Collision(f"{len(found)} seed manifests: keep one, delete the others")
        if found:
            [manifest] = found
            for spec in self.worlds:
                if self._noted_in(manifest, f"c_{spec.name}") and not self._noted_in(
                    manifest, f"g_{spec.name}"
                ):
                    raise Collision(f"the manifest names a clock for {spec.name} and no generation")
                noted = self._noted_in(manifest, f"g_{spec.name}")
                if noted:
                    Generation.decode(noted)
            return manifest
        return await self.call(
            self.v1.customers.create_async,
            params={
                "email": MANIFEST_EMAIL,
                "name": "RB seed manifest",
                "metadata": {MANIFEST: "1"},
            },
            key="manifest",
        )

    @staticmethod
    def _noted_in(manifest: dict, key: str) -> str | None:
        return (manifest.get("metadata") or {}).get(key) or None

    def _noted(self, key: str) -> str | None:
        return self._noted_in(self.manifest, key)

    async def _note(self, entries: dict[str, str]) -> None:
        self.manifest = await self.call(
            self.v1.customers.update_async, self.manifest["id"], params={"metadata": entries}
        )

    async def _clock(self, spec: worlds.World) -> tuple[dict, Generation]:
        """The world's clock as the manifest names it, or a new one. A clock
        the manifest does not name is never used, whatever it is called; one
        it names that is gone opens the next generation, on this run's month.
        """
        generation_key, clock_key = f"g_{spec.name}", f"c_{spec.name}"
        noted = self._noted(generation_key)
        generation = Generation.decode(noted) if noted else None
        clock_id = self._noted(clock_key)
        if clock_id is not None:
            clock = await self._existing_clock(clock_id)
            if clock is not None:
                if clock.get("name") != clock_name(spec.name, generation):
                    raise Collision(
                        f"the manifest's clock for {spec.name} is named {clock.get('name')!r}, "
                        f"not {clock_name(spec.name, generation)!r}"
                    )
                await self._own(clock, spec)
                return clock, generation
            generation = Generation(generation.number + 1, self.dates, self.run_at)
            await self._note({generation_key: generation.encode(), clock_key: ""})
        elif generation is None:
            generation = Generation(1, self.dates, self.run_at)
            await self._note({generation_key: generation.encode()})
        elif self.run_at - generation.opened >= KEYS_REMEMBERED:
            # Opened by a run that stopped before noting its clock, too long ago
            # for Stripe to answer the creation's key with what it made then. A
            # clock bearing its name may be that one, or someone else's: the
            # name proves nothing, so the maintainer decides.
            name = clock_name(spec.name, generation)
            clocks = await self.every(self.v1.test_helpers.test_clocks.list_async)
            if any(c.get("name") == name for c in clocks):
                raise Uncertain(
                    f"generation {generation.number} of {spec.name} may have made the clock "
                    f"{name}: delete it, or note its id as {clock_key} in the seed manifest"
                )

        # Within Stripe's memory of the key, this returns the clock an earlier
        # run made, if it made one; otherwise it makes it.
        clock = await self.call(
            self.v1.test_helpers.test_clocks.create_async,
            params={
                "frozen_time": starts_at(spec.name, generation.dates),
                "name": clock_name(spec.name, generation),
            },
            key=f"{spec.name}/g{generation.number}/clock",
        )
        await self._own(clock, spec)
        await self._note({clock_key: clock["id"]})
        return clock, generation

    async def _existing_clock(self, clock_id: str) -> dict | None:
        try:
            clock = await self.call(self.v1.test_helpers.test_clocks.retrieve_async, clock_id)
        except Exception as error:
            if not _missing(error):
                raise
            return None
        return None if clock.get("deleted") else clock

    async def _own(self, clock: dict, spec: worlds.World) -> None:
        for customer in await self.every(
            self.v1.customers.list_async, params={"test_clock": clock["id"]}
        ):
            if (customer.get("metadata") or {}).get(worlds.WORLD) != spec.name:
                raise Collision(
                    f"clock {clock['name']} holds a customer the seed did not make: "
                    "rename or delete that clock"
                )

    async def _customer(self, spec: worlds.World, clock: dict, generation: Generation) -> dict:
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
                key=f"{spec.name}/g{generation.number}/customer",
            )
        if (customer.get("metadata") or {}).get(CARD_SET) is None:
            customer = await self.card(customer, CARD, f"{spec.name}/g{generation.number}")
        return customer

    async def card(self, customer: dict, token: str, generation: str) -> dict:
        """`token`'s card as the customer's default, once: a card the customer
        already holds is reused, so an interrupted run attaches nothing twice."""
        if (customer.get("metadata") or {}).get(CARD_SET) == token:
            return customer
        held = await self.every(
            self.v1.payment_methods.list_async, params={"customer": customer["id"], "type": "card"}
        )
        method = next((m for m in held if (m.get("card") or {}).get("last4") == LAST4[token]), None)
        if method is None:
            method = await self.call(
                self.v1.payment_methods.attach_async,
                token,
                params={"customer": customer["id"]},
                key=f"{generation}/card/{token}",
            )
        return await self.call(
            self.v1.customers.update_async,
            customer["id"],
            params={
                "invoice_settings": {"default_payment_method": method["id"]},
                "metadata": {CARD_SET: token},
            },
        )

    # -- the shared objects ------------------------------------------------

    async def _shared(self) -> None:
        self.manifest = await self._manifest()
        for product, name in PRODUCTS.items():
            tag = f"shared/product/{product}"
            try:
                found = await self.call(self.v1.products.retrieve_async, product)
            except Exception as error:
                if not _missing(error):
                    raise
                found = await self.call(
                    self.v1.products.create_async,
                    params={"id": product, "name": name, "metadata": {STEP: tag}},
                    key=tag,
                )
            _own_shared(found, tag)

        meters = await self.every(self.v1.billing.meters.list_async, params={"status": "active"})
        meter = next((m for m in meters if m.get("event_name") == METER_EVENT), None)
        if meter is None:
            meter = await self.call(
                self.v1.billing.meters.create_async, params=METER, key="shared/meter"
            )
        for field in ("default_aggregation", "customer_mapping", "value_settings"):
            if meter.get(field) != METER[field]:
                raise Collision(f"the meter {METER_EVENT} exists with another {field}")
        self.meter_id = meter["id"]

        for lookup_key, spec in PRICES.items():
            tag = f"shared/price/{lookup_key}"
            if spec.get("recurring", {}).get("usage_type") == "metered":
                spec = {**spec, "recurring": {**spec["recurring"], "meter": self.meter_id}}
            found = await self.every(
                self.v1.prices.list_async, params={"lookup_keys": [lookup_key]}
            )
            if found:
                [price] = found
            else:
                price = await self.call(
                    self.v1.prices.create_async,
                    params={
                        **spec,
                        "currency": CURRENCY,
                        "lookup_key": lookup_key,
                        "metadata": {STEP: tag},
                    },
                    key=tag,
                )
            _own_shared(price, tag)
            if not _same_price(price, spec):
                raise Collision(f"the price {lookup_key} exists with other terms")
            self.prices[lookup_key] = price["id"]

        tag = "shared/coupon"
        try:
            coupon = await self.call(self.v1.coupons.retrieve_async, COUPON["id"])
        except Exception as error:
            if not _missing(error):
                raise
            coupon = await self.call(
                self.v1.coupons.create_async, params={**COUPON, "metadata": {STEP: tag}}, key=tag
            )
        _own_shared(coupon, tag)
        terms = (coupon.get("percent_off"), coupon.get("duration"), coupon.get("amount_off"))
        if terms != (COUPON["percent_off"], COUPON["duration"], None):
            raise Collision(f"the coupon {COUPON['id']} exists with other terms")

        tag = "shared/tax_rate"
        rates = await self.every(self.v1.tax_rates.list_async, params={"active": True})
        rate = next((r for r in rates if (r.get("metadata") or {}).get(STEP) == tag), None)
        if rate is None:
            rate = await self.call(
                self.v1.tax_rates.create_async,
                params={**TAX_RATE, "metadata": {STEP: tag}},
                key=tag,
            )
        if (Decimal(str(rate.get("percentage"))), rate.get("inclusive")) != (Decimal(20), False):
            raise Collision("the seed's tax rate exists with other terms")
        self.tax_rate_id = rate["id"]

    # -- the primitives --------------------------------------------------------

    async def every(self, method, *args: Any, params: dict | None = None) -> list[dict]:
        """Every object of a list, to its end: a list cut short is never taken
        for a complete one, which would let a step make its object twice."""
        params = {**(params or {}), "limit": 100}
        found: list[dict] = []
        while True:
            page = await self.call(method, *args, params=params)
            found.extend(page["data"])
            if not page.get("has_more"):
                return found
            if not page["data"]:
                raise Unexpected("a Stripe list announced more and gave nothing")
            params = {**params, "starting_after": page["data"][-1]["id"]}

    async def call(
        self, method, *args: Any, params: dict | None = None, key: str | None = None
    ) -> dict:
        """One request. `key`, on a creation, is its idempotency key: two
        runs, or one retried, make the object once."""
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        if key is not None:
            kwargs["options"] = {"idempotency_key": f"rb-seed/{key}"}
        response = await method(*args, **kwargs)
        return response.to_dict() if hasattr(response, "to_dict") else dict(response)

    async def wait_for(self, what: str, ready: Callable[[], Awaitable[bool]]) -> None:
        """Until `ready()`, looking every few seconds. Bounded as a whole,
        reads included: a read that never returns ends the wait too."""
        deadline = self.monotonic() + self.wait_within
        try:
            async with asyncio.timeout(self.wait_within):
                while not await ready():
                    if self.monotonic() >= deadline:
                        break
                    await self.sleep(self.look_every)
                else:
                    return
        except TimeoutError:
            pass
        raise NotReady(f"{what}: not ready within {self.wait_within:g}s")


def _own_shared(found: dict, tag: str) -> None:
    if (found.get("metadata") or {}).get(STEP) != tag:
        raise Collision(f"{found['id']} exists, and the seed did not make it ({tag})")


def _same_price(price: dict, spec: dict) -> bool:
    def amount(terms: dict) -> Decimal:
        return Decimal(str(terms.get("unit_amount_decimal") or terms.get("unit_amount")))

    def recurring(terms: dict | None) -> tuple | None:
        if not terms:
            return None
        return (
            terms.get("interval"),
            terms.get("interval_count") or 1,
            terms.get("usage_type") or "licensed",
            terms.get("meter"),
        )

    product = price.get("product")
    return (
        (product if isinstance(product, str) else product["id"]) == spec["product"]
        and price.get("currency") == CURRENCY
        and (price.get("billing_scheme") or "per_unit") == "per_unit"
        and price.get("transform_quantity") is None
        and amount(price) == amount(spec)
        and recurring(price.get("recurring")) == recurring(spec.get("recurring"))
    )


@dataclass
class World:
    """One world being built: its clock, its customer, its month, the steps."""

    seeder: Seeder
    spec: worlds.World
    clock: dict
    customer: dict
    generation: Generation

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def customer_id(self) -> str:
        return self.customer["id"]

    @property
    def dates(self) -> Dates:
        """The month of the world's generation: the one it was begun on."""
        return self.generation.dates

    def key(self, step: str) -> str:
        """The idempotency key of a step: its world, generation and step."""
        return f"{self.name}/g{self.generation.number}/{step}"

    @property
    def v1(self) -> Any:
        return self.seeder.v1

    def price(self, lookup_key: str) -> str:
        return self.seeder.prices[lookup_key]

    async def call(self, method, *args: Any, params: dict | None = None, key: str | None = None):
        return await self.seeder.call(method, *args, params=params, key=key)

    async def wait_for(self, what: str, ready: Callable[[], Awaitable[bool]]) -> None:
        await self.seeder.wait_for(what, ready)

    async def once(self, step: str, *, find, where: dict, create, params: dict) -> dict:
        """The object `step` made on an earlier run, or a new one, tagged."""
        tag = f"{self.name}/{step}"
        for found in await self.seeder.every(find, params=where):
            if (found.get("metadata") or {}).get(STEP) == tag:
                return found
        metadata = {**params.get("metadata", {}), worlds.WORLD: self.name, STEP: tag}
        return await self.call(create, params={**params, "metadata": metadata}, key=self.key(step))

    async def taken(
        self,
        step: str,
        subscription: dict,
        *,
        reason: str | None = None,
        optional: bool = False,
    ) -> dict | None:
        """An invoice Stripe made itself -- a first invoice, a renewal, a final
        or an arrears one -- taken as `step` once, and kept.

        The choice is kept on the subscription (`rb_taken_<step>`: the invoice,
        or `none` when Stripe made none and `optional` allows it), and the
        invoice is tagged: a later run never looks at whatever is latest by
        then. The latest invoice is taken only if no step took it yet and, when
        `reason` is given, Stripe made it for that reason.
        """
        mark = TAKEN + step
        tag = f"{self.name}/{step}"
        current = await self.call(self.v1.subscriptions.retrieve_async, subscription["id"])
        kept = (current.get("metadata") or {}).get(mark)
        if kept == "none":
            return None
        if kept:
            return await self.call(self.v1.invoices.retrieve_async, kept)
        invoices = await self.seeder.every(
            self.v1.invoices.list_async, params={"customer": self.customer_id}
        )
        for invoice in invoices:
            if (invoice.get("metadata") or {}).get(STEP) == tag:
                await self._keep(subscription, mark, invoice["id"])
                return invoice

        latest = current.get("latest_invoice")
        if latest is None:
            if not optional:
                raise Unexpected(f"subscription {subscription['id']} has no invoice for {step}")
            await self._keep(subscription, mark, "none")
            return None
        invoice = await self.call(
            self.v1.invoices.retrieve_async, latest if isinstance(latest, str) else latest["id"]
        )
        taken_as = (invoice.get("metadata") or {}).get(STEP)
        if taken_as is not None:
            raise Unexpected(f"no new invoice for {step}: the latest is {taken_as}")
        if reason is not None and invoice.get("billing_reason") != reason:
            raise Unexpected(
                f"no new invoice for {step}: the latest was made for "
                f"{invoice.get('billing_reason')}, not {reason}"
            )
        invoice = await self.call(
            self.v1.invoices.update_async,
            invoice["id"],
            params={"metadata": {worlds.WORLD: self.name, STEP: tag}},
        )
        await self._keep(subscription, mark, invoice["id"])
        return invoice

    async def _keep(self, subscription: dict, mark: str, value: str) -> None:
        await self.call(
            self.v1.subscriptions.update_async,
            subscription["id"],
            params={"metadata": {mark: value}},
        )

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

        await self.wait_for(f"clock {self.clock['name']}", settled)

    # -- the steps a world is made of -----------------------------------------

    async def card(self, token: str) -> None:
        generation = f"{self.name}/g{self.generation.number}"
        self.customer = await self.seeder.card(self.customer, token, generation)

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
        """An invoice on the subscription, taking its pending items: Stripe
        refuses `pending_invoice_items_behavior` beside `subscription`, which
        already takes that subscription's pending items (first run, T032)."""
        return await self.once(
            step,
            find=self.v1.invoices.list_async,
            where={"customer": self.customer_id},
            create=self.v1.invoices.create_async,
            params={
                "customer": self.customer_id,
                "subscription": subscription["id"],
                "auto_advance": False,
            },
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
        if invoice["status"] != "paid":
            raise Unexpected(f"invoice {invoice['id']} is {invoice['status']}, not paid")
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
        """One meter event of `value`, at the month's event day, sent once.

        Its state is kept on the customer: `sending` before the request,
        `sent` after. A run that finds `sending` -- the request may or may not
        have reached Stripe -- sends nothing: usage already aggregated settles
        it, and otherwise the maintainer is asked to check the meter. Stripe's
        own deduplication by identifier lasts a day at least, not forever.
        """
        state = (self.customer.get("metadata") or {}).get(USAGE_EVENT)
        if state == "sending":
            if not (await self.summary() or 0) > 0:
                raise Uncertain(
                    f"a meter event for {self.name} may have reached Stripe: check the meter "
                    f"{METER_EVENT} in the Dashboard; to send it again, remove {USAGE_EVENT} "
                    "from the customer's metadata"
                )
            await self._usage_state("sent")
        elif state != "sent":
            await self._usage_state("sending")
            await self.call(
                self.v1.billing.meter_events.create_async,
                params={
                    "event_name": METER_EVENT,
                    "identifier": f"rb-{self.key('usage_event')}",
                    "timestamp": self.dates.event_day,
                    "payload": {"stripe_customer_id": self.customer_id, "value": str(value)},
                },
            )
            await self._usage_state("sent")

        async def aggregated() -> bool:
            return await self.summary() == value

        await self.wait_for(f"a meter summary of {value}", aggregated)

    async def _usage_state(self, state: str) -> None:
        self.customer = await self.call(
            self.v1.customers.update_async,
            self.customer_id,
            params={"metadata": {USAGE_EVENT: state}},
        )


# -- the worlds ----------------------------------------------------------------


async def _duplicate(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_enterprise_monthly")
    await w.paid(await w.taken("first_invoice", subscription, reason=CREATED))
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
    await w.paid(await w.taken("first_invoice", subscription, reason=CREATED))
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
    await w.paid(await w.taken("first_invoice", subscription, reason=CREATED))
    await w.advance(w.dates.start - DAY)
    await w.item(
        "item", subscription=subscription, period=(w.dates.start, w.dates.end), price="rb_pro_once"
    )
    subscription = await w.cancelled(subscription)
    return (await w.paid(await w.taken("final_invoice", subscription)))["id"]


async def _failed_payment(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_pro_monthly")
    await w.paid(await w.taken("first_invoice", subscription, reason=CREATED))
    await w.advance(w.dates.start - DAY)
    await w.card(DECLINING_CARD)
    # The renewal is finalised an hour after it is made, then collected.
    await w.advance(w.dates.start + HOUR + MINUTE)
    return (await w.taken("renewal", subscription, reason=CYCLE))["id"]


async def _usage(w: World) -> str:
    base = await w.subscription("base", "rb_pro_monthly")
    await w.paid(await w.taken("base_first_invoice", base, reason=CREATED))
    metered = await w.subscription("metered", "rb_blocks_metered")
    await w.taken("metered_first_invoice", metered, reason=CREATED, optional=True)
    await w.advance(w.dates.event_day)
    await w.usage_reported(BLOCKS)
    await w.advance(w.dates.end + HOUR + MINUTE)
    return (await w.taken("arrears", metered, reason=CYCLE))["id"]


def _first_invoice_of(*prices: str, card: str | None = None, **extra: Any) -> Builder:
    """A world whose target is its subscription's first invoice."""

    async def build(w: World) -> str:
        if card is not None:
            await w.card(card)
        subscription = await w.subscription("subscription", *prices, **extra)
        return (await w.taken("first_invoice", subscription, reason=CREATED))["id"]

    return build


async def _tax(w: World) -> str:
    rate = w.seeder.tax_rate_id
    return await _first_invoice_of("rb_pro_monthly", default_tax_rates=[rate])(w)


async def _negative_line(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_pro_monthly")
    await w.paid(await w.taken("first_invoice", subscription, reason=CREATED))
    await w.advance(w.dates.start + DAY)
    period = (w.dates.start, w.dates.end)
    await w.item("item", subscription=subscription, period=period, price="rb_pro_once")
    await w.item("credit", subscription=subscription, period=period, amount=-1000)
    return (await w.finalized(await w.invoice("invoice", subscription)))["id"]


async def _partial_refund(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_pro_monthly")
    first = await w.paid(await w.taken("first_invoice", subscription, reason=CREATED))
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


async def _invoice_credit(w: World) -> str:
    subscription = await w.subscription("subscription", "rb_blocks_metered")
    await w.taken("first_invoice", subscription, reason=CREATED, optional=True)
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
    return (await w.taken("arrears", subscription, reason=CYCLE))["id"]


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
        "rb_pro_monthly", discounts=[{"coupon": COUPON["id"]}]
    ),
    "unsupported_negative_line": _negative_line,
    "unsupported_fractional_unit_price": _first_invoice_of("rb_pro_fractional"),
    "unsupported_partial_refund": _partial_refund,
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


def status_facts(target: Invoice, expected: InvoiceStatus) -> list[str]:
    return (
        [] if target.status is expected else [f"it is {target.status.value}, not {expected.value}"]
    )


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
            return (
                total_facts(target, TARGET_TOTALS[name])
                + status_facts(target, TARGET_STATUSES[name])
                + await read(w, target)
            )
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


class Busy(Exception):  # noqa: N818 -- a state, not a programming error
    """Another seed run holds the lock."""


@contextlib.contextmanager
def only_run(path: str | None = None):
    """The machine's one seed run: an exclusive lock on `path` (`LOCK` by
    default), held for the run and freed by the system if the process dies."""
    handle = open(path or LOCK, "a+b")  # noqa: SIM115 -- held for the whole run
    try:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy(path or LOCK) from None
        yield
    finally:
        handle.close()


def main() -> int:
    key = os.environ.get("STRIPE_SEED_KEY")
    try:
        check_seed_key(key)
    except ProviderConfigurationError as error:
        print(f"stripe_seed: refused ({error.category})", file=sys.stderr)
        return 2
    try:
        with only_run():
            return _run(key)
    except Busy:
        print("stripe_seed: refused (another seed run is in progress)", file=sys.stderr)
        return 2


def _run(key: str) -> int:
    try:
        import stripe

        silence(stripe)
    except ProviderConfigurationError as error:
        print(f"stripe_seed: refused ({error.category})", file=sys.stderr)
        return 2
    sdk = stripe.StripeClient(key, stripe_version=API_VERSION, max_network_retries=NETWORK_RETRIES)
    seeder = Seeder(sdk, run_at=datetime.now(UTC))
    print(f"stripe_seed: a new world is built on the month from {seeder.dates.start_day}")
    outcomes = asyncio.run(seeder.run())
    for outcome in outcomes:
        print(outcome.line())
    return 0 if all(o.state != "reported" for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
