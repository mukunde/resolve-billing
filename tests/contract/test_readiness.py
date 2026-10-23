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


SKIPPED = pytest.skip.Exception("STRIPE_API_KEY is not set")
FAILING = RuntimeError(SENTINEL)
INTERRUPTED = KeyboardInterrupt()


@pytest.mark.parametrize(
    "raised", [SKIPPED, FAILING, INTERRUPTED], ids=["skip", "platform-error", "interruption"]
)
async def test_anything_else_settles_nothing_and_is_checked_again(raised):
    """Propagated as it is -- the same exception, not one made of it -- and
    nothing kept, so the next call runs the check again."""
    once = conftest.Once()
    check = Counting(raised)

    for runs in (1, 2):
        with pytest.raises(type(raised)) as seen:
            await once(check)
        assert seen.value is raised
        assert once.kept is None
        assert check.runs == runs


async def test_what_is_kept_of_a_diagnosis_is_its_message_alone():
    once = conftest.Once()
    await_fail = Counting(lambda: pytest.fail("worlds missing from the seeded account: ['x']"))
    with pytest.raises(pytest.fail.Exception):
        await once(await_fail)

    assert once.kept == conftest.Diagnosis("worlds missing from the seeded account: ['x']")


# -- readiness, once (US1) -------------------------------------------------------


@pytest.fixture
def fresh(monkeypatch):
    """Both caches empty for the test, and the session's own restored after."""
    monkeypatch.setattr(conftest, "READINESS", conftest.Once())
    monkeypatch.setattr(conftest, "WRITE_GENERATION", conftest.Once())


class CountingHung(Hung):
    def __init__(self) -> None:
        self.looks = 0

    async def test_clocks(self):
        self.looks += 1
        return await super().test_clocks()


class OneWorldShort(Settled):
    def __init__(self) -> None:
        self.looks = 0

    async def test_clocks(self):
        self.looks += 1
        return await super().test_clocks()

    async def customers(self, test_clock=None):
        return (await super().customers(test_clock))[1:]


class Failing(Settled):
    def __init__(self) -> None:
        self.looks = 0

    async def test_clocks(self):
        self.looks += 1
        raise RuntimeError(SENTINEL)


async def test_an_account_never_ready_is_waited_for_once(fresh, monkeypatch):
    monkeypatch.setattr(conftest, "READY_WITHIN", 0.05)
    reader = CountingHung()
    provider = Provider(reader)

    for _ in range(2):
        with pytest.raises(pytest.fail.Exception, match="not ready"):
            await conftest.READINESS(lambda: conftest._readiness(provider))

    assert reader.looks == 1


async def test_a_world_missing_is_said_once_and_repeated(fresh):
    reader = OneWorldShort()
    provider = Provider(reader)
    said = []

    for _ in range(2):
        with pytest.raises(pytest.fail.Exception, match="worlds missing") as seen:
            await conftest.READINESS(lambda: conftest._readiness(provider))
        said.append(str(seen.value.msg))

    assert reader.looks == 1
    assert said[0] == said[1]
    assert conftest.READINESS.kept == conftest.Diagnosis(said[0])


async def test_a_platform_error_is_not_kept(fresh):
    reader = Failing()
    provider = Provider(reader)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await conftest.READINESS(lambda: conftest._readiness(provider))

    assert reader.looks == 2
    assert conftest.READINESS.kept is None


async def test_what_is_kept_cannot_be_changed(fresh, monkeypatch):
    monkeypatch.setattr(conftest, "READY_WITHIN", 1)
    ready = await conftest.READINESS(lambda: conftest._readiness(Provider(Settled())))

    with pytest.raises(TypeError):
        ready.seeded["duplicate"] = None
    with pytest.raises(AttributeError):
        ready.seeded["duplicate"].target_invoice = "in_other"
    with pytest.raises(AttributeError):
        ready.usage = None
    assert ready.usage == ("sub_usage", SEPTEMBER)


def test_the_fake_touches_neither_cache(fresh):
    conftest.fake_platform()
    conftest.fake_writing()

    assert conftest.READINESS.kept is None
    assert conftest.WRITE_GENERATION.kept is None


pytest_plugins = ["pytester"]

TWO_TESTS = """
import asyncio, sys, types
from types import MappingProxyType
from datetime import UTC, datetime

import pytest

import app.providers.stripe.client as client
from scripts import stripe_worlds as worlds
from tests.contract import conftest

CHECKS = []
#: Strong references, so no identity is reused between the two tests.
LOOPS, PLATFORMS = [], []


@pytest.fixture(autouse=True)
def stubbed(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "rk_test_51RbAbc")
    sdk = sys.modules.get("stripe") or types.ModuleType("stripe")
    monkeypatch.setitem(sys.modules, "stripe", sdk)
    monkeypatch.setattr(client, "connect", lambda key, **kw: object())

    async def readiness(provider):
        CHECKS.append(1)
        at = datetime(2026, 10, 1, tzinfo=UTC)
        seeded = {
            w.name: conftest.Seeded(f"cus_{w.name}", f"in_{w.name}", at) for w in worlds.WORLDS
        }
        return conftest.Readiness(MappingProxyType(seeded), ("sub_usage", (at, at)))

    monkeypatch.setattr(conftest, "_readiness", readiness)


async def record():
    LOOPS.append(asyncio.get_running_loop())
    PLATFORMS.append(await conftest.stripe_platform())


@pytest.mark.asyncio(loop_scope="function")
async def test_first():
    await record()


@pytest.mark.asyncio(loop_scope="function")
async def test_second():
    await record()
    first, second = PLATFORMS
    assert LOOPS[0] is not LOOPS[1]
    assert first.provider is not second.provider
    assert len(CHECKS) == 1
    assert first.seeded is second.seeded
    assert (first.usage, first.customers) == (second.usage, second.customers)
"""


def test_two_tests_on_their_own_loops_share_one_check_and_own_their_providers(fresh, pytester):
    """Each test builds its own provider on its own event loop; what the
    readiness check found is shared, and the check runs once. The second
    test of the sub-run asserts it, holding both loops and platforms."""
    pytester.makepyfile(test_two=TWO_TESTS)

    result = pytester.runpytest_inprocess("-q", "-o", "asyncio_default_fixture_loop_scope=function")

    result.assert_outcomes(passed=2)


# -- the write generation, once (US2) -------------------------------------------


class Account:
    """A write generation as the seed leaves it, read through a reader."""

    def __init__(self, number: str | None = "1", duplicate: bool = True) -> None:
        self.number = number
        self.duplicate = duplicate
        self.discoveries = 0
        self.notes: dict[str, list[dict]] = {}
        self.note_reads = 0

    async def customers(self, test_clock=None):
        self.discoveries += 1
        manifest = {"id": "cus_manifest", "metadata": {"rb_seed_manifest": "1"}}
        if self.number:
            manifest["metadata"]["w_last"] = self.number
        found = [
            manifest,
            {
                "id": f"cus_write{self.number}",
                "metadata": {worlds.WORLD: "write", worlds.WRITE_GENERATION: self.number},
            },
        ]
        if self.duplicate:
            found.append(
                {
                    "id": f"cus_dup{self.number}",
                    "email": f"write-duplicate-w{self.number}@{worlds.DOMAIN}",
                    "metadata": {
                        worlds.WORLD: worlds.WRITE_DUPLICATE,
                        worlds.WRITE_GENERATION: self.number,
                        worlds.TARGET_INVOICE: f"in_dup{self.number}",
                        worlds.INVESTIGATE_AT: "2026-10-23T17:43:47+00:00",
                    },
                }
            )
        return found

    async def invoices(self, customer_id):
        return [
            {"id": f"in_{purpose}{self.number}", "metadata": {worlds.PURPOSE: purpose}}
            for purpose in worlds.WRITE_PURPOSES
        ]

    async def credit_notes(self, invoice_id):
        self.note_reads += 1
        return list(self.notes.get(invoice_id, []))


async def test_the_generation_is_found_once_and_pinned(fresh):
    account = Account("1")

    first = await conftest.WRITE_GENERATION(lambda: conftest._generation(account))
    account.number = "2"  # a write run opened during the session
    again = await conftest.WRITE_GENERATION(lambda: conftest._generation(account))

    assert again is first
    assert first.number == "1"
    assert first.invoices["replay"] == "in_replay1"
    assert first.duplicate.target_invoice == "in_dup1"
    assert account.discoveries == 1
    with pytest.raises(TypeError):
        first.invoices["replay"] = "in_other"


async def test_no_write_run_is_skipped_and_not_kept(fresh):
    account = Account(number=None)

    for _ in range(2):
        with pytest.raises(pytest.skip.Exception, match="no write run"):
            await conftest.WRITE_GENERATION(lambda: conftest._generation(account))

    assert account.discoveries == 2


async def test_an_incomplete_generation_is_kept_as_its_diagnosis(fresh):
    account = Account(duplicate=False)

    for _ in range(2):
        with pytest.raises(pytest.fail.Exception, match="incomplete"):
            await conftest.WRITE_GENERATION(lambda: conftest._generation(account))

    assert account.discoveries == 1


async def test_the_two_caches_are_apart(fresh):
    await conftest.WRITE_GENERATION(lambda: conftest._generation(Account()))

    assert conftest.READINESS.kept is None


async def test_freshness_is_still_read_per_scenario(fresh, monkeypatch):
    import sys
    import types

    import app.providers.stripe.client as client

    account = Account()
    monkeypatch.setenv("STRIPE_API_KEY", "rk_test_51RbAbc")
    monkeypatch.setenv("STRIPE_WRITES", "enabled")
    monkeypatch.setitem(
        sys.modules, "stripe", sys.modules.get("stripe") or types.ModuleType("stripe")
    )
    monkeypatch.setattr(client, "connect", lambda key, **kw: account)
    monkeypatch.setattr(client, "connect_writer", lambda key, **kw: object())

    first = await conftest.stripe_writing()
    invoice = first.invoices["replay"]
    assert await first.untouched(invoice)
    account.notes[invoice] = [{"id": "cn_made"}]
    second = await conftest.stripe_writing()

    assert not await second.untouched(invoice)
    assert account.discoveries == 1
    assert account.note_reads == 2
    assert second.provider is not first.provider
