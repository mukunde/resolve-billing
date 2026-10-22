"""The seed script, without Stripe (T030).

Tested here: the key, the dates, the amounts, each world's preconditions --
checked on the fake counterparts themselves, so a Stripe world that passes
them holds the same facts -- and the machinery: one clock per world, nothing
made twice, a half-made world completed, the record written only once the
facts hold, every wait bounded. What Stripe does with each world's requests
is not simulated: the maintainer's run (T032) and the contract tests show it.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.enums import PaymentStatus
from app.providers.base import UNSUPPORTED_REASONS
from app.providers.fake.adapter import FakeBillingProvider
from evals.runner import CASES_PATH
from scripts import stripe_seed as seed
from scripts import stripe_worlds as worlds
from tests.unit import stripe_seed_stub as stub
from tests.unit.stripe_stub import InvalidRequestError

RUN = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)
SENTINEL = "SENTINEL-platform-text"
CASES = {c["id"]: c for c in json.loads(CASES_PATH.read_text(encoding="utf-8"))}
FAKE = FakeBillingProvider()


def at(*parts) -> int:
    return int(datetime(*parts, tzinfo=UTC).timestamp())


class TestTheKey:
    @pytest.mark.parametrize(
        ("key", "category"),
        [
            (None, "missing_key"),
            ("rk_test_51RbAbc", "not_a_secret_test_key"),
            ("sk_live_51RbAbc", "live_key"),
        ],
    )
    def test_anything_but_the_secret_test_key_stops_before_the_sdk_loads(
        self, key, category, monkeypatch, capsys
    ):
        monkeypatch.setitem(sys.modules, "stripe", None)  # importing it would fail
        if key is None:
            monkeypatch.delenv("STRIPE_SEED_KEY", raising=False)
        else:
            monkeypatch.setenv("STRIPE_SEED_KEY", key)
        assert seed.main() == 2
        err = capsys.readouterr().err
        assert category in err
        assert "51RbAbc" not in err


class TestTheDates:
    """The month that ends just before the run (decided 2026-10-23)."""

    def test_the_month_before_the_run(self):
        dates = seed.Dates.before(RUN)
        assert (dates.start, dates.end) == (at(2026, 9, 22), at(2026, 10, 22))
        assert dates.previous == at(2026, 8, 22)
        assert dates.middle == dates.start + (dates.end - dates.start) // 2

    @pytest.mark.parametrize(
        ("run", "start", "end"),
        [
            (datetime(2026, 3, 31, 9, tzinfo=UTC), (2026, 2, 28), (2026, 3, 28)),
            (datetime(2026, 1, 1, 0, 5, tzinfo=UTC), (2025, 11, 28), (2025, 12, 28)),
        ],
    )
    def test_a_day_past_the_28th_is_held_at_the_28th(self, run, start, end):
        dates = seed.Dates.before(run)
        assert (dates.start, dates.end) == (at(*start), at(*end))

    def test_every_run_of_a_year_ends_before_it_with_its_events_in_stripes_window(self):
        """Meter events are sent a day before the month ends: in the past, and
        within the 35 days Stripe accepts, whichever clock it judges them by."""
        for day in range(366):
            run = datetime(2026, 1, 1, 0, 1, tzinfo=UTC) + timedelta(days=day)
            dates = seed.Dates.before(run)
            assert dates.end <= int(run.timestamp()) - 86400, run
            assert 0 < int(run.timestamp()) - dates.event_day < 35 * 86400, run
            assert datetime.fromtimestamp(dates.start, UTC).day <= 28


class TestTheClocks:
    def test_each_world_starts_when_its_story_needs(self):
        dates = seed.Dates.before(RUN)
        assert seed.starts_at("duplicate", dates) == dates.start
        assert seed.starts_at("billed_after_cancellation", dates) == dates.previous
        assert seed.starts_at("failed_payment", dates) == dates.previous
        off = seed.starts_at("unsupported_unrepresentable_period", dates)
        assert off % 60 == 30

    def test_the_arrears_are_due_a_month_and_an_hour_after_the_clock_starts(self):
        dates = seed.Dates.before(RUN)
        name = "unsupported_unrepresentable_period"
        due = seed.arrears_due(name, dates)
        assert due == seed.starts_at(name, dates) + (dates.end - dates.start) + 3600 + 60


class TestTheAmounts:
    @pytest.mark.parametrize("world", worlds.SUPPORTED, ids=lambda w: w.name)
    def test_each_target_costs_what_its_fake_counterpart_costs(self, world):
        counterpart = next(
            i for i in FAKE._invoices if i.id == CASES[world.counterpart]["invoice_id"]
        )
        assert seed.TARGET_TOTALS[world.name] == counterpart.total.amount_minor
        assert counterpart.total.currency == seed.CURRENCY.upper()


def fake(invoice_id):
    return next(i for i in FAKE._invoices if i.id == invoice_id)


def sub(subscription_id):
    return next(s for s in FAKE._subscriptions if s.id == subscription_id)


class TestThePreconditions:
    """Each check passes on the fake world its Stripe world reproduces, and
    fails when the fact it checks is taken away."""

    def test_duplicate(self):
        acme = [i for i in FAKE._invoices if i.customer_id == "cus_acme"]
        assert seed.duplicate_facts(fake("inv_002"), acme) == []
        assert seed.duplicate_facts(fake("inv_002"), [fake("inv_002")])

    def test_proration(self):
        assert seed.proration_facts(fake("inv_020"), sub("sub_99")) == []
        assert seed.proration_facts(fake("inv_001"), sub("sub_77"))

    def test_billed_after_cancellation(self):
        assert seed.cancellation_facts(fake("inv_030"), sub("sub_55")) == []
        assert seed.cancellation_facts(fake("inv_030"), sub("sub_77"))
        ended_later = sub("sub_55").model_copy(
            update={"cancelled_at": datetime(2026, 9, 15, tzinfo=UTC)}
        )
        assert seed.cancellation_facts(fake("inv_030"), ended_later)

    def test_failed_payment(self):
        payments = list(FAKE._payments)
        assert seed.failed_payment_facts(fake("inv_180"), payments) == []
        assert seed.failed_payment_facts(fake("inv_010"), payments)
        settled = [
            p.model_copy(update={"status": PaymentStatus.SUCCEEDED})
            if p.invoice_id == "inv_180"
            else p
            for p in payments
        ]
        assert seed.failed_payment_facts(fake("inv_180"), settled)

    def test_usage(self):
        record = next(u for u in FAKE._usage if u.subscription_id == "sub_88")
        assert seed.usage_facts(fake("inv_011"), record) == []
        assert seed.usage_facts(fake("inv_011"), None), "no record is never a zero"
        other = record.model_copy(update={"quantity": Decimal("46")})
        assert seed.usage_facts(fake("inv_011"), other)

    def test_total(self):
        assert seed.total_facts(fake("inv_002"), 9900) == []
        assert seed.total_facts(fake("inv_002"), 4900)


class TestTheScope:
    def test_every_world_of_the_catalogue_has_a_builder_and_a_check(self):
        names = {w.name for w in worlds.WORLDS}
        assert set(seed.BUILDERS) == names
        assert set(seed.CHECKS) == names

    def test_every_unsupported_reason_has_a_world_or_says_why_not(self):
        built = {w.unsupported for w in worlds.UNSUPPORTED}
        assert built.isdisjoint(worlds.NOT_SEEDED)
        assert built | set(worlds.NOT_SEEDED) == UNSUPPORTED_REASONS


# -- the machinery, with worlds of the tests' own ------------------------------

ALPHA, BETA = worlds.World("alpha"), worlds.World("beta")


async def simple(w: seed.World) -> str:
    subscription = await w.once(
        "subscription",
        find=w.v1.subscriptions.list_async,
        where={"customer": w.customer_id, "status": "all"},
        create=w.v1.subscriptions.create_async,
        params={"customer": w.customer_id, "items": [{"price": w.price("rb_pro_monthly")}]},
    )
    await w.advance(w.dates.start + 3600)
    invoice = await w.once(
        "invoice",
        find=w.v1.invoices.list_async,
        where={"customer": w.customer_id},
        create=w.v1.invoices.create_async,
        params={"customer": w.customer_id, "subscription": subscription["id"]},
    )
    return invoice["id"]


async def holds(w: seed.World, target: str) -> list[str]:
    return []


async def never(w: seed.World, target: str) -> list[str]:
    return ["the two invoices do not overlap"]


class Ticking:
    """A monotonic clock that a sleep moves forward: no real waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


def seeder(account, *, builders=None, checks=None, ticking=None, wait_within=60, look_every=5):
    ticking = ticking or Ticking()
    return seed.Seeder(
        stub.StubSdk(account),
        run_at=RUN,
        worlds=(ALPHA, BETA),
        builders=builders or {"alpha": simple, "beta": simple},
        checks=checks or {"alpha": holds, "beta": holds},
        wait_within=wait_within,
        look_every=look_every,
        sleep=ticking.sleep,
        monotonic=ticking.monotonic,
    )


def customer_of(account, world):
    return next(
        c for c in account.objects["v1.customers"] if c["metadata"].get(worlds.WORLD) == world.name
    )


class TestOneClockPerWorld:
    async def test_each_world_has_its_clock_and_its_customer_on_it(self):
        account = stub.Account()
        await seeder(account).run()
        clocks = {c["name"]: c for c in account.objects["v1.test_helpers.test_clocks"]}
        assert set(clocks) == {"rb:alpha", "rb:beta"}
        for world in (ALPHA, BETA):
            customer = customer_of(account, world)
            assert customer["test_clock"] == clocks[f"rb:{world.name}"]["id"]
            assert customer["email"] == world.email

    async def test_the_target_and_the_instant_are_recorded_once_the_facts_hold(self):
        account = stub.Account()
        [alpha, _] = await seeder(account).run()
        metadata = customer_of(account, ALPHA)["metadata"]
        assert alpha.state == "built"
        assert metadata[worlds.TARGET_INVOICE] == alpha.target
        clock = next(
            c for c in account.objects["v1.test_helpers.test_clocks"] if c["name"] == "rb:alpha"
        )
        instant = datetime.fromisoformat(metadata[worlds.INVESTIGATE_AT])
        assert instant == datetime.fromtimestamp(clock["frozen_time"] + 60, UTC)
        assert alpha.instant == instant


class TestNothingTwice:
    async def test_a_second_run_makes_nothing(self):
        account = stub.Account()
        await seeder(account).run()
        writes = len(account.writes())
        outcomes = await seeder(account).run()
        assert len(account.writes()) == writes
        assert [o.state for o in outcomes] == ["complete", "complete"]

    async def test_a_half_made_world_is_completed_and_nothing_is_made_twice(self):
        account = stub.Account()
        account.fail_once["v1.invoices.create_async"] = InvalidRequestError(
            SENTINEL, code="invoice_no_customer_line_items"
        )
        [alpha, beta] = await seeder(account).run()
        assert alpha.state == "reported"
        assert "invoice_no_customer_line_items" in alpha.line()
        assert SENTINEL not in alpha.line()
        assert worlds.TARGET_INVOICE not in customer_of(account, ALPHA)["metadata"]
        assert beta.state == "built"

        [alpha, beta] = await seeder(account).run()
        assert (alpha.state, beta.state) == ("built", "complete")
        for step in ("alpha/subscription", "alpha/invoice"):
            assert len(account.tagged(step)) == 1, step
        assert len(account.objects["v1.test_helpers.test_clocks"]) == 2
        # The clock went where the dates say, once: a second run never moves it on.
        clock = next(
            c for c in account.objects["v1.test_helpers.test_clocks"] if c["name"] == "rb:alpha"
        )
        advanced = [
            c for c in account.calls if c[0].endswith("advance_async") and c[1] == (clock["id"],)
        ]
        assert len(advanced) == 1

    async def test_the_shared_objects_are_made_once(self):
        account = stub.Account()
        await seeder(account).run()
        await seeder(account).run()
        prices = [p["lookup_key"] for p in account.objects["v1.prices"]]
        assert len(prices) == len(set(prices))
        assert len({p["id"] for p in account.objects["v1.products"]}) == len(
            account.objects["v1.products"]
        )
        assert len(account.objects["v1.billing.meters"]) == 1
        assert len(account.objects["v1.coupons"]) == 1
        assert len(account.objects["v1.tax_rates"]) == 1


class TestReported:
    async def test_a_fact_that_does_not_hold_is_reported_and_not_recorded(self):
        account = stub.Account()
        [alpha, beta] = await seeder(account, checks={"alpha": never, "beta": holds}).run()
        assert alpha.state == "reported"
        assert "do not overlap" in alpha.line()
        assert worlds.TARGET_INVOICE not in customer_of(account, ALPHA)["metadata"]
        assert beta.state == "built"

    async def test_a_clock_that_never_settles_is_reported_within_the_bound(self):
        account = stub.Account()
        account.clock_status = "advancing"
        ticking = Ticking()
        [alpha, _] = await seeder(account, ticking=ticking, wait_within=60, look_every=5).run()
        assert alpha.state == "reported"
        assert "not ready" in alpha.line()
        assert ticking.sleeps <= 2 * (60 // 5 + 1)  # two worlds, each bounded
        assert worlds.TARGET_INVOICE not in customer_of(account, ALPHA)["metadata"]

    async def test_usage_that_never_comes_is_waited_for_within_the_bound_never_assumed(self):
        async def waits(w: seed.World) -> str:
            async def summary() -> bool:
                return False

            await w.wait_for("a meter summary of 47", summary)
            return "in_never"

        account = stub.Account()
        [alpha, _] = await seeder(account, builders={"alpha": waits, "beta": simple}).run()
        assert alpha.state == "reported"
        assert "a meter summary of 47" in alpha.line()
        assert worlds.TARGET_INVOICE not in customer_of(account, ALPHA)["metadata"]

    def test_the_report_names_the_target_and_the_instant(self):
        instant = datetime(2026, 10, 22, 1, 1, tzinfo=UTC)
        line = seed.Outcome("alpha", "built", target="in_1", instant=instant).line()
        assert "alpha" in line and "in_1" in line and instant.isoformat() in line
