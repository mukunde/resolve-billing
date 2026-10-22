"""The seed script, without Stripe (T030).

Tested here: the key, the dates, the amounts, each world's preconditions --
checked on the fake counterparts themselves, so a Stripe world that passes
them holds the same facts -- and the machinery: one clock per world, nothing
made twice, a half-made world completed, the record written only once the
facts hold, every wait bounded. What Stripe does with each world's requests
is not simulated: the maintainer's run (T032) and the contract tests show it.
"""

from __future__ import annotations

import asyncio
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
from tests.unit.stripe_stub import APIConnectionError, InvalidRequestError

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
GENERATION = "2026-09-22"  # the month a first run on RUN gives


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


async def stripe_invoices(
    w: seed.World, subscription: dict, step: str, reason: str = seed.CYCLE
) -> None:
    """What Stripe does on its own, done by hand: an invoice on the
    subscription, made for `reason` and untagged as Stripe's own are, made its
    latest -- once."""
    made = [
        i
        for i in await w.seeder.every(w.v1.invoices.list_async, params={"customer": w.customer_id})
        if i["metadata"].get("made_by_stripe") == step
    ]
    if not made:
        made = [
            await w.call(
                w.v1.invoices.create_async,
                params={
                    "customer": w.customer_id,
                    "subscription": subscription["id"],
                    "billing_reason": reason,
                    "metadata": {"made_by_stripe": step},
                },
            )
        ]
    await w.call(
        w.v1.subscriptions.update_async,
        subscription["id"],
        params={"latest_invoice": made[0]["id"]},
    )


async def a_subscription(w: seed.World) -> dict:
    return await w.once(
        "subscription",
        find=w.v1.subscriptions.list_async,
        where={"customer": w.customer_id, "status": "all"},
        create=w.v1.subscriptions.create_async,
        params={"customer": w.customer_id, "items": [{"price": w.price("rb_pro_monthly")}]},
    )


async def renewing(w: seed.World) -> str:
    """A first invoice paid, then a renewal taken as the target."""
    subscription = await a_subscription(w)
    await stripe_invoices(w, subscription, "first", seed.CREATED)
    await w.paid(await w.taken("first_invoice", subscription, reason=seed.CREATED))
    await w.advance(w.dates.start + 3600)
    await stripe_invoices(w, subscription, "renewal", seed.CYCLE)
    return (await w.taken("renewal", subscription, reason=seed.CYCLE))["id"]


async def metered(w: seed.World) -> str:
    """No first invoice from Stripe, then the arrears taken as the target."""
    subscription = await a_subscription(w)
    await w.taken("first_invoice", subscription, reason=seed.CREATED, optional=True)
    await w.advance(w.dates.end + 3600)
    await stripe_invoices(w, subscription, "arrears", seed.CYCLE)
    return (await w.taken("arrears", subscription, reason=seed.CYCLE))["id"]


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


def seeder(
    account,
    *,
    builders=None,
    checks=None,
    ticking=None,
    wait_within=60,
    look_every=5,
    run_at=RUN,
    only=(ALPHA, BETA),
):
    ticking = ticking or Ticking()
    return seed.Seeder(
        stub.StubSdk(account),
        run_at=run_at,
        worlds=only,
        builders=builders or {w.name: simple for w in only},
        checks=checks or {w.name: holds for w in only},
        wait_within=wait_within,
        look_every=look_every,
        sleep=ticking.sleep,
        monotonic=ticking.monotonic,
    )


def customer_of(account, world):
    return next(
        c for c in account.objects["v1.customers"] if c["metadata"].get(worlds.WORLD) == world.name
    )


def clock_of(account, world):
    return next(
        c
        for c in account.objects["v1.test_helpers.test_clocks"]
        if c["name"].startswith(f"rb:{world.name}@")
    )


def clock_of_customer(account, world):
    return customer_of(account, world)["test_clock"]


def calls_to(account, method, *args):
    return [c for c in account.calls if c[0].endswith(method) and (not args or c[1] == args)]


def failing_once(account, when, error):
    """The account, failing the first call `when(path, params)` matches."""
    original = account.serve
    state = {"failed": False}

    async def serve(path, *args, params=None, options=None):
        if not state["failed"] and when(path, params or {}):
            state["failed"] = True
            raise error
        return await original(path, *args, params=params, options=options)

    account.serve = serve


class TestOneClockPerWorld:
    async def test_each_world_has_its_clock_named_with_its_month(self):
        account = stub.Account()
        await seeder(account).run()
        names = {c["name"] for c in account.objects["v1.test_helpers.test_clocks"]}
        assert names == {f"rb:alpha@{GENERATION}/g1", f"rb:beta@{GENERATION}/g1"}
        for world in (ALPHA, BETA):
            customer = customer_of(account, world)
            assert customer["test_clock"] == clock_of(account, world)["id"]
            assert customer["email"] == world.email

    async def test_the_target_and_the_instant_are_recorded_once_the_facts_hold(self):
        account = stub.Account()
        [alpha, _] = await seeder(account).run()
        metadata = customer_of(account, ALPHA)["metadata"]
        assert alpha.state == "built"
        assert metadata[worlds.TARGET_INVOICE] == alpha.target
        instant = datetime.fromisoformat(metadata[worlds.INVESTIGATE_AT])
        assert instant == datetime.fromtimestamp(clock_of(account, ALPHA)["frozen_time"] + 60, UTC)
        assert alpha.instant == instant

    async def test_every_creation_carries_its_own_idempotency_key(self):
        account = stub.Account()
        await seeder(account).run()
        keys = [
            options.get("idempotency_key")
            for (path, _, _), options in zip(account.calls, account.options, strict=True)
            if path.endswith(("create_async", "attach_async"))
        ]
        assert keys and all(k and k.startswith("rb-seed/") for k in keys)
        assert len(keys) == len(set(keys))


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
        assert len(calls_to(account, "advance_async", clock_of(account, ALPHA)["id"])) == 1

    @pytest.mark.parametrize(
        "later", [timedelta(days=1), timedelta(days=40)], ids=["next_day", "next_month"]
    )
    async def test_a_world_resumed_another_day_keeps_its_own_month(self, later):
        account = stub.Account()
        account.fail_once["v1.invoices.create_async"] = InvalidRequestError("", code="x")
        await seeder(account).run()
        first = clock_of(account, ALPHA)

        [alpha, _] = await seeder(account, run_at=RUN + later).run()
        assert alpha.state == "built"
        assert len(account.objects["v1.test_helpers.test_clocks"]) == 2
        assert clock_of(account, ALPHA)["id"] == first["id"]
        assert clock_of(account, ALPHA)["frozen_time"] == seed.Dates.before(RUN).start + 3600

    async def test_an_invoice_stripe_made_is_taken_once_and_kept(self):
        account = stub.Account()
        # Interrupted after the renewal was taken, before the world was recorded.
        failing_once(
            account,
            lambda path, params: (
                path.endswith("customers.update_async")
                and worlds.TARGET_INVOICE in params.get("metadata", {})
            ),
            InvalidRequestError("", code="rate_limit"),
        )
        run = seeder(account, builders={"alpha": renewing}, only=(ALPHA,))
        [alpha] = await run.run()
        assert alpha.state == "reported"
        [alpha] = await seeder(account, builders={"alpha": renewing}, only=(ALPHA,)).run()
        assert alpha.state == "built"
        [renewal] = account.tagged("alpha/renewal")
        [first] = account.tagged("alpha/first_invoice")
        assert alpha.target == renewal["id"] != first["id"]
        assert len(calls_to(account, "pay_async")) == 1  # the first invoice, once

    async def test_a_deleted_clock_opens_a_new_generation_with_nothing_reused(self):
        account = stub.Account()
        await seeder(account).run()
        first = clock_of(account, ALPHA)
        account.forget_clock(first["id"])

        [alpha, beta] = await seeder(account).run()  # the same day: same month
        assert (alpha.state, beta.state) == ("built", "complete")
        second = clock_of(account, ALPHA)
        assert second["id"] != first["id"]
        assert second["name"] == f"rb:alpha@{GENERATION}/g2"
        [manifest] = [
            c for c in account.objects["v1.customers"] if c["metadata"].get(seed.MANIFEST)
        ]
        assert manifest["metadata"]["g_alpha"] == f"2@{GENERATION}@{int(RUN.timestamp())}"
        assert manifest["metadata"]["c_alpha"] == second["id"]
        assert customer_of(account, ALPHA)["test_clock"] == second["id"]

    async def test_an_invoice_stripe_did_not_make_is_kept_absent(self):
        """A metered subscription with no first invoice: once noted, a later
        run never takes the arrears for it."""
        account = stub.Account()
        failing_once(
            account,
            lambda path, params: (
                path.endswith("customers.update_async")
                and worlds.TARGET_INVOICE in params.get("metadata", {})
            ),
            InvalidRequestError("", code="rate_limit"),
        )
        [alpha] = await seeder(account, builders={"alpha": metered}, only=(ALPHA,)).run()
        assert alpha.state == "reported"
        [alpha] = await seeder(account, builders={"alpha": metered}, only=(ALPHA,)).run()
        assert alpha.state == "built"
        [arrears] = account.tagged("alpha/arrears")
        assert alpha.target == arrears["id"]
        assert not account.tagged("alpha/first_invoice")
        [subscription] = account.tagged("alpha/subscription")
        assert subscription["metadata"][seed.TAKEN + "first_invoice"] == "none"

    async def test_an_interruption_before_the_arrears_are_tagged_still_takes_them(self):
        account = stub.Account()
        failing_once(
            account,
            lambda path, params: path.endswith("invoices.update_async"),
            InvalidRequestError("", code="lock_timeout"),
        )
        [alpha] = await seeder(account, builders={"alpha": metered}, only=(ALPHA,)).run()
        assert alpha.state == "reported"
        [alpha] = await seeder(account, builders={"alpha": metered}, only=(ALPHA,)).run()
        assert alpha.state == "built"
        assert [a["id"] for a in account.tagged("alpha/arrears")] == [alpha.target]
        assert not account.tagged("alpha/first_invoice")

    async def test_an_invoice_made_for_another_reason_is_not_taken(self):
        async def updated(w: seed.World) -> str:
            subscription = await a_subscription(w)
            await stripe_invoices(w, subscription, "update", "subscription_update")
            return (await w.taken("renewal", subscription, reason=seed.CYCLE))["id"]

        account = stub.Account()
        [alpha] = await seeder(account, builders={"alpha": updated}, only=(ALPHA,)).run()
        assert alpha.state == "reported"
        assert "subscription_update, not subscription_cycle" in alpha.line()

    async def test_an_invoice_already_taken_is_never_taken_again(self):
        async def no_renewal(w: seed.World) -> str:
            subscription = await a_subscription(w)
            await stripe_invoices(w, subscription, "first", seed.CREATED)
            await w.taken("first_invoice", subscription, reason=seed.CREATED)
            # Stripe made no renewal: the latest is still the first invoice.
            return (await w.taken("renewal", subscription, reason=seed.CYCLE))["id"]

        account = stub.Account()
        [alpha] = await seeder(account, builders={"alpha": no_renewal}, only=(ALPHA,)).run()
        assert alpha.state == "reported"
        assert "no new invoice for renewal" in alpha.line()

    async def test_a_card_attached_before_an_interruption_is_reused(self):
        account = stub.Account()
        failing_once(
            account,
            lambda path, params: (
                path.endswith("customers.update_async") and "invoice_settings" in params
            ),
            InvalidRequestError("", code="lock_timeout"),
        )
        [alpha] = await seeder(account, only=(ALPHA,)).run()
        assert alpha.state == "reported"
        [alpha] = await seeder(account, only=(ALPHA,)).run()
        assert alpha.state == "built"
        assert len(account.objects["v1.payment_methods"]) == 1

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

    async def test_two_runs_at_once_make_each_object_once(self):
        """The account yields on every request, so the two runs interleave:
        both look, find nothing, and create with the same keys."""
        account = stub.Account()
        await asyncio.gather(seeder(account).run(), seeder(account).run())
        assert len(account.objects["v1.test_helpers.test_clocks"]) == 2
        assert len(account.objects["v1.customers"]) == 3  # two worlds, one manifest
        for step in ("alpha/subscription", "alpha/invoice", "beta/subscription"):
            assert len(account.tagged(step)) == 1, step


def manifest_of(account):
    return next(c for c in account.objects["v1.customers"] if c["metadata"].get(seed.MANIFEST))


def noting_the_clock_fails(account):
    failing_once(
        account,
        lambda path, params: (
            path.endswith("customers.update_async") and "c_alpha" in params.get("metadata", {})
        ),
        InvalidRequestError("", code="lock_timeout"),
    )


class TestTheManifest:
    async def test_two_manifests_stop_every_world_before_anything_is_made(self):
        account = stub.Account()
        for n in (1, 2):
            account.objects["v1.customers"].append(
                {"id": f"cus_manifest{n}", "metadata": {seed.MANIFEST: "1"}}
            )
        outcomes = await seeder(account).run()
        assert [o.state for o in outcomes] == ["reported", "reported"]
        assert all("2 seed manifests" in o.line() for o in outcomes)
        assert not account.writes()

    @pytest.mark.parametrize(
        "metadata",
        [
            {"c_alpha": "clock_x"},
            {"g_alpha": "one@2026-09-22@0"},
            {"g_alpha": "0@2026-09-22@0"},
            {"g_alpha": "1@2026-09-30@0"},
        ],
        ids=["clock_without_generation", "unreadable", "number_zero", "day_past_28"],
    )
    async def test_a_manifest_the_seed_cannot_read_is_not_used(self, metadata):
        account = stub.Account()
        account.objects["v1.customers"].append(
            {"id": "cus_manifest", "metadata": {seed.MANIFEST: "1", **metadata}}
        )
        outcomes = await seeder(account).run()
        assert [o.state for o in outcomes] == ["reported", "reported"]
        assert not account.writes()

    async def test_a_clock_whose_name_is_not_its_generations_is_not_used(self):
        account = stub.Account()
        await seeder(account).run()
        clock_of(account, ALPHA)["name"] = f"rb:alpha@{GENERATION}/g7"
        [alpha, beta] = await seeder(account).run()
        assert alpha.state == "reported"
        assert "is named" in alpha.line()
        assert beta.state == "complete"

    def test_its_keys_fit_stripes_metadata_limits(self):
        keys = [seed.MANIFEST] + [f"{k}_{w.name}" for w in worlds.WORLDS for k in ("g", "c")]
        assert len(keys) <= 50
        assert max(len(k) for k in keys) <= 40
        steps = [seed.TAKEN + step for step in ("metered_first_invoice", "base_first_invoice")]
        assert max(len(k) for k in steps) <= 40


class TestAClockAStoppedRunMayHaveMade:
    async def test_within_stripes_memory_the_key_returns_the_clock_it_made(self):
        account = stub.Account()
        noting_the_clock_fails(account)
        [alpha, _] = await seeder(account).run()
        assert alpha.state == "reported"
        [alpha, _] = await seeder(account, run_at=RUN + timedelta(hours=2)).run()
        assert alpha.state == "built"
        names = [c["name"] for c in account.objects["v1.test_helpers.test_clocks"]]
        assert names.count(f"rb:alpha@{GENERATION}/g1") == 1

    async def test_past_stripes_memory_a_clock_of_that_name_is_for_the_maintainer(self):
        account = stub.Account()
        noting_the_clock_fails(account)
        await seeder(account).run()
        [alpha, _] = await seeder(account, run_at=RUN + timedelta(days=2)).run()
        assert alpha.state == "reported"
        assert "may have made the clock" in alpha.line()
        assert len(account.objects["v1.test_helpers.test_clocks"]) == 2  # alpha's, beta's

    async def test_a_homonymous_clock_is_never_adopted_even_after_a_stop(self):
        account = stub.Account()
        account.objects["v1.test_helpers.test_clocks"].append(
            {
                "id": "clock_theirs",
                "name": f"rb:alpha@{GENERATION}/g1",
                "frozen_time": 0,
                "status": "ready",
                "metadata": {},
            }
        )
        account.fail_once["v1.test_helpers.test_clocks.create_async"] = InvalidRequestError(
            "", code="rate_limit"
        )
        [alpha, _] = await seeder(account).run()
        assert alpha.state == "reported"
        [alpha, _] = await seeder(account, run_at=RUN + timedelta(hours=2)).run()
        assert alpha.state == "built"
        assert clock_of_customer(account, ALPHA) != "clock_theirs"
        assert manifest_of(account)["metadata"]["c_alpha"] != "clock_theirs"
        assert not calls_to(account, "advance_async", "clock_theirs")


class TestOneRunAtATime:
    def test_a_second_run_is_refused_while_one_holds_the_lock(self, tmp_path, monkeypatch, capsys):
        lock = tmp_path / "seed.lock"
        monkeypatch.setattr(seed, "LOCK", str(lock))
        monkeypatch.setenv("STRIPE_SEED_KEY", "sk_test_51RbAbc")
        monkeypatch.setitem(sys.modules, "stripe", None)  # the run must not get that far
        with seed.only_run(str(lock)):
            assert seed.main() == 2
        assert "another seed run is in progress" in capsys.readouterr().err

    def test_the_lock_is_free_again_once_a_run_ends(self, tmp_path):
        lock = str(tmp_path / "seed.lock")
        with seed.only_run(lock):
            pass
        with seed.only_run(lock):
            pass


class TestOnlyWhatTheSeedMade:
    @pytest.mark.parametrize("name", [f"rb:alpha@{GENERATION}", f"rb:alpha@{GENERATION}/g1"])
    async def test_a_clock_the_manifest_does_not_name_is_never_used(self, name):
        account = stub.Account()
        account.objects["v1.test_helpers.test_clocks"].append(
            {"id": "clock_empty", "name": name, "frozen_time": 0, "status": "ready", "metadata": {}}
        )
        [alpha, _] = await seeder(account).run()
        assert alpha.state == "built"
        assert clock_of_customer(account, ALPHA) != "clock_empty"
        assert not calls_to(account, "advance_async", "clock_empty")

    async def test_its_own_clock_holding_someone_elses_customer_is_left_alone(self):
        account = stub.Account()
        await seeder(account).run()
        clock = clock_of(account, ALPHA)
        customer_of(account, ALPHA)["metadata"].pop(worlds.TARGET_INVOICE)
        account.objects["v1.customers"].append(
            {"id": "cus_theirs", "test_clock": clock["id"], "metadata": {}}
        )
        advanced = len(calls_to(account, "advance_async", clock["id"]))
        [alpha, beta] = await seeder(account).run()
        assert alpha.state == "reported"
        assert "did not make" in alpha.line()
        assert len(calls_to(account, "advance_async", clock["id"])) == advanced
        assert beta.state == "complete"

    async def test_a_shared_object_the_seed_did_not_make_stops_every_world(self):
        account = stub.Account()
        account.objects["v1.products"].append({"id": "rb_pro", "name": "Pro", "metadata": {}})
        outcomes = await seeder(account).run()
        assert [o.state for o in outcomes] == ["reported", "reported"]
        assert all("rb_pro" in o.line() for o in outcomes)
        assert not account.objects["v1.test_helpers.test_clocks"]

    async def test_a_price_on_other_terms_stops_every_world(self):
        account = stub.Account()
        await seeder(account).run()
        price = next(p for p in account.objects["v1.prices"] if p["lookup_key"] == "rb_pro_monthly")
        price["unit_amount"] = 5900
        outcomes = await seeder(account).run()
        assert all("rb_pro_monthly" in o.line() for o in outcomes)

    @pytest.mark.parametrize(
        ("change", "named"),
        [
            (
                lambda a: _price(a, "rb_pro_monthly")["recurring"].update(interval_count=2),
                "rb_pro_monthly",
            ),
            (
                lambda a: _price(a, "rb_pro_monthly").update(transform_quantity={"divide_by": 2}),
                "rb_pro_monthly",
            ),
            (lambda a: a.objects["v1.coupons"][0].update(duration="once"), "rb_ten_percent"),
            (lambda a: a.objects["v1.tax_rates"][0].update(inclusive=True), "tax rate"),
            (
                lambda a: a.objects["v1.billing.meters"][0].update(
                    default_aggregation={"formula": "count"}
                ),
                "rb_api_call_blocks",
            ),
        ],
        ids=["interval_count", "transform_quantity", "coupon_duration", "tax_inclusive", "meter"],
    )
    async def test_a_shared_object_on_other_terms_stops_every_world(self, change, named):
        account = stub.Account()
        await seeder(account).run()
        change(account)
        outcomes = await seeder(account).run()
        assert [o.state for o in outcomes] == ["reported", "reported"]
        assert all(named in o.line() for o in outcomes)

    async def test_a_list_cut_short_is_never_read_as_complete(self):
        account = stub.Account()
        account.truncated.add("v1.customers.list_async")
        outcomes = await seeder(account).run()
        assert [o.state for o in outcomes] == ["reported", "reported"]
        assert not account.objects["v1.test_helpers.test_clocks"]


class TestReported:
    async def test_a_fact_that_does_not_hold_is_reported_and_not_recorded(self):
        account = stub.Account()
        [alpha, beta] = await seeder(account, checks={"alpha": never, "beta": holds}).run()
        assert alpha.state == "reported"
        assert "do not overlap" in alpha.line()
        assert worlds.TARGET_INVOICE not in customer_of(account, ALPHA)["metadata"]
        assert beta.state == "built"

    async def test_a_recorded_world_that_no_longer_holds_is_not_complete(self):
        account = stub.Account()
        await seeder(account).run()
        [alpha, beta] = await seeder(account, checks={"alpha": never, "beta": holds}).run()
        assert alpha.state == "reported"
        assert "no longer holds" in alpha.line()
        assert beta.state == "complete"

    async def test_a_clock_that_never_settles_is_reported_within_the_bound(self):
        account = stub.Account()
        account.clock_status = "advancing"
        ticking = Ticking()
        [alpha, _] = await seeder(account, ticking=ticking, wait_within=60, look_every=5).run()
        assert alpha.state == "reported"
        assert "not ready" in alpha.line()
        assert ticking.sleeps <= 2 * (60 // 5 + 1)  # two worlds, each bounded
        assert worlds.TARGET_INVOICE not in customer_of(account, ALPHA)["metadata"]

    async def test_a_read_that_never_returns_ends_the_wait_too(self):
        async def hangs(w: seed.World) -> str:
            async def forever() -> bool:
                await asyncio.sleep(3600)
                return True

            await w.wait_for("a meter summary of 47", forever)
            return "in_never"

        account = stub.Account()
        [alpha] = await seed.Seeder(
            stub.StubSdk(account),
            run_at=RUN,
            worlds=(ALPHA,),
            builders={"alpha": hangs},
            checks={"alpha": holds},
            wait_within=0.05,
        ).run()
        assert alpha.state == "reported"
        assert "a meter summary of 47: not ready" in alpha.line()

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

    def test_no_answer_is_an_unknown_outcome_not_a_refusal(self):
        detail = seed._refusal(APIConnectionError(SENTINEL))
        assert "outcome is unknown" in detail
        assert SENTINEL not in detail


class TestTheMeterEvent:
    @staticmethod
    async def reports(w: seed.World) -> str:
        await w.usage_reported(47)
        return "in_usage"

    def run(self, account):
        return seeder(account, builders={"alpha": self.reports}, only=(ALPHA,)).run()

    def unrecorded(self, account, state):
        customer = customer_of(account, ALPHA)
        customer["metadata"].pop(worlds.TARGET_INVOICE)
        customer["metadata"][seed.USAGE_EVENT] = state
        return customer

    async def test_it_is_sent_once_at_the_months_event_day_and_settled(self):
        account = stub.Account()
        [alpha] = await self.run(account)
        assert alpha.state == "built"
        [event] = account.objects["v1.billing.meter_events"]
        assert event["timestamp"] == seed.Dates.before(RUN).event_day
        assert customer_of(account, ALPHA)["metadata"][seed.USAGE_EVENT] == "sent"

    async def test_an_uncertain_sending_is_never_sent_again_blindly(self):
        account = stub.Account()
        await seeder(account, only=(ALPHA,)).run()  # the world, without its event
        self.unrecorded(account, "sending")
        [alpha] = await self.run(account)
        assert alpha.state == "reported"
        assert "may have reached Stripe" in alpha.line()
        assert not account.objects["v1.billing.meter_events"]

    async def test_an_uncertain_sending_that_stripe_aggregated_is_settled(self):
        account = stub.Account()
        await seeder(account, only=(ALPHA,)).run()
        customer = self.unrecorded(account, "sending")
        meter = account.objects["v1.billing.meters"][0]["id"]
        account.summaries[(meter, customer["id"])] = [{"aggregated_value": 47.0}]
        [alpha] = await self.run(account)
        assert alpha.state == "built"
        assert not account.objects["v1.billing.meter_events"]
        assert customer_of(account, ALPHA)["metadata"][seed.USAGE_EVENT] == "sent"


def _price(account, lookup_key):
    return next(p for p in account.objects["v1.prices"] if p["lookup_key"] == lookup_key)
