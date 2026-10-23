"""The wait for a seeded account is bounded, and watches the seed's worlds only.

Runs in CI: an account in a few lines, no SDK and no key.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scripts import stripe_worlds as worlds
from tests.contract import conftest

SEPTEMBER = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))


class Hung:
    async def test_clocks(self):
        await asyncio.sleep(3600)


class Settled:
    """Every world on one ready clock, and a clock of someone else's still moving."""

    async def test_clocks(self):
        return [
            {"id": "clock_worlds", "status": "ready"},
            {"id": "clock_other", "status": "advancing"},
        ]

    async def customers(self, test_clock=None):
        if test_clock != "clock_worlds":
            return [{"id": "cus_other", "metadata": {}}]
        return [
            {
                "id": f"cus_{w.name}",
                "metadata": {
                    worlds.WORLD: w.name,
                    worlds.TARGET_INVOICE: f"in_{w.name}",
                    worlds.INVESTIGATE_AT: "2026-10-01T02:00:00+00:00",
                },
            }
            for w in worlds.WORLDS
        ]

    async def invoice(self, invoice_id):
        return {"id": invoice_id, "status": "open"}


class Provider:
    def __init__(self, reader):
        self.reader = reader

    async def get_invoice(self, invoice_id):
        line = SimpleNamespace(
            metric="api_call_blocks_100", period_start=SEPTEMBER[0], period_end=SEPTEMBER[1]
        )
        return SimpleNamespace(subscription_id="sub_usage", lines=[line])

    async def get_usage(self, subscription_id, period):
        return SimpleNamespace(quantity=47)


async def test_a_read_that_never_returns_fails_within_the_bound(monkeypatch):
    monkeypatch.setattr(conftest, "READY_WITHIN", 0.05)
    with pytest.raises(pytest.fail.Exception, match="not ready"):
        await conftest._ready(Provider(Hung()))


async def test_a_clock_outside_the_worlds_is_not_waited_for(monkeypatch):
    monkeypatch.setattr(conftest, "READY_WITHIN", 1)
    seeded = await conftest._ready(Provider(Settled()))
    assert set(seeded) == {w.name for w in worlds.WORLDS}
