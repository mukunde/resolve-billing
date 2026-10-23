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


# -- a check made once per process (specs/003-contract-readiness-once) ---------

SENTINEL = "SENTINEL-platform-text"


class Counting:
    """A check that counts its runs and does what it is told."""

    def __init__(self, outcome=None) -> None:
        self.runs = 0
        self.outcome = outcome

    async def __call__(self):
        self.runs += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            self.outcome()
        return ("kept", self.runs)


async def test_a_success_is_kept_and_shared():
    once, check = conftest.Once(), Counting()

    first = await once(check)
    second = await once(check)

    assert first == second == ("kept", 1)
    assert check.runs == 1


async def test_a_diagnosis_is_kept_and_repeated_at_once():
    once = conftest.Once()
    check = Counting(lambda: pytest.fail("the seeded account is not ready after 180s: [x]"))

    for _ in range(2):
        with pytest.raises(pytest.fail.Exception, match="not ready after 180s"):
            await once(check)

    assert check.runs == 1


@pytest.mark.parametrize(
    "outcome",
    [
        lambda: pytest.skip("STRIPE_API_KEY is not set"),
        RuntimeError(SENTINEL),
        KeyboardInterrupt(),
    ],
    ids=["skip", "platform-error", "interruption"],
)
async def test_anything_else_settles_nothing_and_is_checked_again(outcome):
    once = conftest.Once()
    check = Counting(outcome)

    for _ in range(2):
        with pytest.raises(BaseException):  # noqa: B017 -- each kind, propagated as it is
            await once(check)

    assert check.runs == 2


async def test_what_is_kept_of_a_diagnosis_is_its_message_alone():
    once = conftest.Once()
    await_fail = Counting(lambda: pytest.fail("worlds missing from the seeded account: ['x']"))
    with pytest.raises(pytest.fail.Exception):
        await once(await_fail)

    assert once.kept == conftest.Diagnosis("worlds missing from the seeded account: ['x']")
