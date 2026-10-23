"""The platforms the contract is checked against (spec FR-031).

The fake, always. Stripe only with the restricted read-only test key in
`STRIPE_API_KEY`, against an account the seed script built; skipped otherwise,
with the reason. Before any Stripe test the account must be ready, within a
bound: every test clock ready, every target invoice finalised, the usage
world's meter summary available. A world missing from the account is a
failure, not a skip: dropping one from the contract needs the maintainer.

The write contract (specs/002-stripe-writes) runs on the fake, always, and on
Stripe only with `STRIPE_WRITES=enabled` beside the key, on the latest write
generation the seed built (`--write-run`).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

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
class Diagnosis:
    """Why a check failed, in the contract's own words: never Stripe's."""

    reason: str


class Once:
    """A check made at most once per pytest process, and what it found.

    The first call runs the check. A value it returns is kept and returned
    to every later call. A `pytest.fail` -- the contract's own diagnosis: an
    account not ready within the bound, a world missing, a generation
    incomplete -- is kept as its message alone, and every later call fails
    at once with it. Anything else -- a skip, a platform error, an
    interruption -- settles nothing: it propagates, and the next call checks
    again (specs/003-contract-readiness-once, FR-001, FR-003). What a check
    returns is immutable data, never a provider or a client bound to a test's
    event loop (FR-002).
    """

    def __init__(self) -> None:
        self.kept: Any = None

    async def __call__(self, check: Callable[[], Awaitable[Any]]) -> Any:
        if isinstance(self.kept, Diagnosis):
            pytest.fail(self.kept.reason)
        if self.kept is not None:
            return self.kept
        try:
            found = await check()
        except pytest.fail.Exception as failure:
            self.kept = Diagnosis(str(failure.msg))
            raise
        self.kept = found
        return found


#: What this process found about the seeded account, and about the write
#: generation: two checks, kept apart.
READINESS = Once()
WRITE_GENERATION = Once()


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
    """The seeded worlds, once the account has settled. The whole wait is
    bounded, every read in it included: an account that never settles, or a
    read that never returns, fails."""
    waiting = ["nothing read yet"]
    try:
        async with asyncio.timeout(READY_WITHIN):
            while True:
                seeded, waiting = await _look(provider)
                if not waiting:
                    return seeded
                await asyncio.sleep(LOOK_EVERY)
    except TimeoutError:
        pytest.fail(f"the seeded account is not ready after {READY_WITHIN}s: {waiting}")


async def _look(provider) -> tuple[dict[str, Seeded], list[str]]:
    """The catalogue's worlds as the account holds them, and what is still
    settling. Only their clocks are watched: another clock on the account is
    none of the contract's business."""
    reader = provider.reader
    names = {w.name for w in worlds.WORLDS}
    by_world: dict[str, dict] = {}
    their_clocks: dict[str, dict] = {}
    for clock in await reader.test_clocks():
        for customer in await reader.customers(test_clock=clock["id"]):
            name = (customer.get("metadata") or {}).get(worlds.WORLD)
            if name in names:
                by_world[name] = customer
                their_clocks[clock["id"]] = clock
    missing = sorted(names - by_world.keys())
    if missing:
        pytest.fail(f"worlds missing from the seeded account (run the seed, or ask): {missing}")

    waiting = [
        f"clock {i} is {c['status']}" for i, c in their_clocks.items() if c["status"] != "ready"
    ]
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


# -- the write contract (specs/002-stripe-writes) ------------------------------

#: On the fake, the invoice each scenario writes on: paid in full by a
#: settled payment, or open with nothing paid, as the purpose asks.
FAKE_WRITE_INVOICES = {
    "replay": "inv_001",
    "find": "inv_010",
    "credit_paid": "inv_030",
    "credit_open": "inv_020",
    "refund": "inv_170",
    "cap": "inv_002",
    "refund_open": "inv_040",
    "execute_refund": "inv_171",
}


@dataclass
class Writing:
    name: str
    provider: object
    #: The invoice each scenario writes on, by purpose.
    invoices: dict[str, str]
    #: Whether an invoice carries no credit note yet.
    untouched: Callable[[str], Awaitable[bool]]
    #: Every credit note of an invoice, as the platform holds it (Stripe).
    credit_notes: Callable[[str], Awaitable[list[dict]]] | None = None
    #: The same platform, unable to write (Stripe).
    read_only: object | None = None
    #: The write run's duplicate dispute: its customer (Stripe).
    duplicate: dict | None = None


def fake_writing() -> Writing:
    async def untouched(invoice_id: str) -> bool:
        return True

    return Writing(
        name="fake",
        provider=FakeBillingProvider(),
        invoices=dict(FAKE_WRITE_INVOICES),
        untouched=untouched,
    )


async def stripe_writing() -> Writing:
    key = os.getenv("STRIPE_API_KEY")
    if not key or os.getenv("STRIPE_WRITES") != "enabled":
        pytest.skip(
            "STRIPE_API_KEY and STRIPE_WRITES=enabled are not both set: the write contract "
            "runs locally, with the writing key, on a fresh write run"
        )
    pytest.importorskip("stripe", reason="the Stripe SDK is the optional extra `stripe`")
    from app.providers.stripe.adapter import StripeBillingProvider
    from app.providers.stripe.client import connect, connect_writer
    from scripts.stripe_seed import MANIFEST, WRITE_LAST

    provider = StripeBillingProvider(connect(key), writer=connect_writer(key))
    reader = provider.reader
    customers = await reader.customers()
    manifests = [c for c in customers if (c.get("metadata") or {}).get(MANIFEST) == "1"]
    number = manifests[0]["metadata"].get(WRITE_LAST) if len(manifests) == 1 else None
    if not number:
        pytest.skip("no write run on this account: run the seed with --write-run")
    ours = {
        (c.get("metadata") or {}).get(worlds.WORLD): c
        for c in customers
        if (c.get("metadata") or {}).get(worlds.WRITE_GENERATION) == number
    }
    if "write" not in ours or worlds.WRITE_DUPLICATE not in ours:
        pytest.fail(f"write generation {number} is incomplete: run the seed with --write-run")
    invoices = {
        invoice["metadata"][worlds.PURPOSE]: invoice["id"]
        for invoice in await reader.invoices(ours["write"]["id"])
        if worlds.PURPOSE in (invoice.get("metadata") or {})
    }
    missing = sorted(set(worlds.WRITE_PURPOSES) - invoices.keys())
    if missing:
        pytest.fail(f"write generation {number} lacks invoices for {missing}")

    async def untouched(invoice_id: str) -> bool:
        return not await reader.credit_notes(invoice_id)

    return Writing(
        name="stripe",
        provider=provider,
        invoices=invoices,
        untouched=untouched,
        credit_notes=reader.credit_notes,
        read_only=StripeBillingProvider(connect(key)),
        duplicate=ours[worlds.WRITE_DUPLICATE],
    )


@pytest.fixture(params=["fake", "stripe"])
async def writing(request) -> Writing:
    return fake_writing() if request.param == "fake" else await stripe_writing()


@pytest.fixture
async def stripe_writer() -> Writing:
    return await stripe_writing()
