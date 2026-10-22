"""The platforms the contract is checked against (spec FR-031).

The fake, always. Stripe only with the restricted read-only test key in
`STRIPE_API_KEY`, against an account the seed script built; skipped otherwise,
with the reason. Before any Stripe test the account must be ready, within a
bound: every test clock ready, every target invoice finalised, the usage
world's meter summary available. A world missing from the account is a
failure, not a skip: dropping one from the contract needs the maintainer.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.providers.fake.adapter import FakeBillingProvider
from scripts import stripe_worlds as worlds

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
SEPTEMBER = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))

#: How long the tests wait for a seeded account to be ready, and how often
#: they look. Waiting is bounded: an account that never settles fails.
READY_WITHIN = 180
LOOK_EVERY = 5

#: Nothing listens there: a read sent to it cannot complete.
UNREACHABLE = "http://127.0.0.1:9"


@dataclass(frozen=True)
class Seeded:
    """A world as the seed recorded it on its customer."""

    customer_id: str
    target_invoice: str
    investigate_at: datetime


@dataclass
class Platform:
    name: str
    provider: object
    #: Customers whose every object is checked against the properties.
    customers: list[str]
    #: A customer with invoices and a subscription, for the single-object checks.
    customer_id: str
    #: A subscription and the period its usage record covers exactly.
    usage: tuple[str, tuple[datetime, datetime]]
    #: The same platform behind an address that never answers, or None when
    #: the platform cannot be made to fail (the fake).
    unreachable: Callable[[], object] | None = None
    #: Seeded worlds by name (Stripe only).
    seeded: dict[str, Seeded] = field(default_factory=dict)


def fake_platform() -> Platform:
    provider = FakeBillingProvider()
    return Platform(
        name="fake",
        provider=provider,
        customers=[c.id for c in provider._customers],
        customer_id="cus_acme",
        usage=("sub_88", SEPTEMBER),
    )


async def stripe_platform() -> Platform:
    key = os.getenv("STRIPE_API_KEY")
    if not key:
        pytest.skip(
            "STRIPE_API_KEY is not set: the Stripe contract runs locally, "
            "with the restricted read-only test key"
        )
    pytest.importorskip("stripe", reason="the Stripe SDK is the optional extra `stripe`")
    from app.providers.stripe.adapter import StripeBillingProvider
    from app.providers.stripe.client import connect

    provider = StripeBillingProvider(connect(key))
    seeded = await _ready(provider)
    usage_world = seeded["usage"]
    target = await provider.get_invoice(usage_world.target_invoice)
    metered = next(line for line in target.lines if line.metric)
    return Platform(
        name="stripe",
        provider=provider,
        customers=[seeded[w.name].customer_id for w in worlds.SUPPORTED],
        customer_id=seeded["duplicate"].customer_id,
        usage=(target.subscription_id, (metered.period_start, metered.period_end)),
        unreachable=lambda: StripeBillingProvider(connect(key, api_base=UNREACHABLE, retries=0)),
        seeded=seeded,
    )


async def _ready(provider) -> dict[str, Seeded]:
    """The seeded worlds, once the account has settled; fails past the bound."""
    deadline = time.monotonic() + READY_WITHIN
    while True:
        seeded, waiting = await _look(provider)
        if not waiting:
            return seeded
        if time.monotonic() > deadline:
            pytest.fail(f"the seeded account is not ready after {READY_WITHIN}s: {waiting}")
        await asyncio.sleep(LOOK_EVERY)


async def _look(provider) -> tuple[dict[str, Seeded], list[str]]:
    reader = provider.reader
    clocks = await reader.test_clocks()
    customers = [c for clock in clocks for c in await reader.customers(test_clock=clock["id"])]
    by_world = {
        c["metadata"][worlds.WORLD]: c for c in customers if worlds.WORLD in (c["metadata"] or {})
    }
    missing = [w.name for w in worlds.WORLDS if w.name not in by_world]
    if missing:
        pytest.fail(f"worlds missing from the seeded account (run the seed, or ask): {missing}")

    waiting = [f"clock {c['id']} is {c['status']}" for c in clocks if c["status"] != "ready"]
    seeded = {}
    for name, customer in by_world.items():
        metadata = customer["metadata"]
        seeded[name] = Seeded(
            customer_id=customer["id"],
            target_invoice=metadata[worlds.TARGET_INVOICE],
            investigate_at=datetime.fromisoformat(metadata[worlds.INVESTIGATE_AT]),
        )
        invoice = await reader.invoice(metadata[worlds.TARGET_INVOICE])
        if invoice["status"] == "draft":
            waiting.append(f"world {name}: target invoice not finalised")
    if not waiting:
        target = await provider.get_invoice(seeded["usage"].target_invoice)
        metered = next((line for line in target.lines if line.metric), None)
        period = (metered.period_start, metered.period_end) if metered else None
        if period is None or await provider.get_usage(target.subscription_id, period) is None:
            waiting.append("world usage: no meter summary for the invoiced period yet")
    return seeded, waiting


@pytest.fixture(params=["fake", "stripe"])
async def platform(request) -> Platform:
    return fake_platform() if request.param == "fake" else await stripe_platform()


@pytest.fixture
async def stripe_account() -> Platform:
    return await stripe_platform()
