"""Gemini 3.5 Flash-Lite, and the daily count per model
(specs/004-second-engines, third amendment; T110)."""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

from dataclasses import asdict

import pytest

from app.decision.base import EngineUnavailable
from app.decision.hosted import configuration
from evals import engines, state
from evals.first_calls import fingerprint
from evals.governed import Governed
from evals.limits import RunStopped
from evals.quota import DailyQuota, QuotaRefused
from tests.unit.test_claude_engine import (  # noqa: F401 -- the fixtures are used by name
    context,
    dispute,
    findings,
)
from tests.unit.test_evaluation_limits import Vendor

PROJECT = "resolve-billing-eval"
PACIFIC = "America/Los_Angeles"
FLASH, LITE = "gemini-3.8-flash", "gemini-3.5-flash-lite"

#: The fingerprint the maintainer's accepted first call of Flash recorded on
#: 2026-10-24 (`~/.resolve-billing/first-calls.json`): adding Flash-Lite must
#: leave it exactly as it was.
FLASH_FINGERPRINT = "6fc2ab41043d7ce9690c7499a7f95dc2dfd0b2278ebcc85a022ac723c90ee5e0"


def quota(model, limit, declared=0) -> DailyQuota:
    book = DailyQuota(PROJECT, limit, PACIFIC, model=model, now=lambda: 1_792_872_000.0)
    if declared is not None:
        book.declare(declared)
    return book


class TestTheConfiguration:
    def test_flash_lite_differs_from_flash_only_where_the_amendment_says(self):
        flash = asdict(configuration(FLASH))
        lite = asdict(configuration(LITE))

        differing = {key for key in flash if flash[key] != lite[key]}

        assert differing == {
            "name",
            "model",
            "requests_per_second",
            "requests_per_day",
            "attempts",
            "stop_on_outage",
        }
        assert lite["model"] == LITE
        assert lite["requests_per_second"] == pytest.approx(15 / 60)
        assert lite["tokens_per_minute"] == 250_000
        assert lite["requests_per_day"] == 500
        assert lite["attempts"] == 3
        assert lite["stop_on_outage"] is False
        assert lite["reasoning"] == "low"

    def test_flash_is_exactly_as_its_first_call_accepted_it(self):
        assert fingerprint(configuration(FLASH)) == FLASH_FINGERPRINT

    def test_the_registry_holds_flash_lite(self):
        assert LITE in engines.evaluation_engines()
        assert engines.CREDENTIALS[LITE] == ("GEMINI_API_KEY",)


class TestTheCountPerModel:
    def test_flash_keeps_its_existing_file_and_flash_lite_counts_apart(self):
        flash = quota(FLASH, 20)
        lite = quota(LITE, 500)
        flash.take()
        lite.take()
        lite.take()

        assert flash.path == state.STATE_DIR / f"quota-{PROJECT}.json"
        assert lite.path == state.STATE_DIR / f"quota-{PROJECT}-{LITE}.json"
        assert list(flash.state()["days"].values()) == [{"declared": 0, "sent": 1}]
        assert list(lite.state()["days"].values()) == [{"declared": 0, "sent": 2}]

    def test_flash_s_spent_request_still_refuses_its_full_run_today(self):
        # The count written before Flash-Lite existed, as it stands on disk.
        flash = quota(FLASH, 20)
        flash.take()

        again = DailyQuota(PROJECT, 20, PACIFIC, model=FLASH, now=lambda: 1_792_872_000.0)
        with pytest.raises(QuotaRefused, match="full run"):
            again.admit_full_run(20)
        quota(LITE, 500).admit_full_run(60)

    @pytest.mark.parametrize("model", ["", "../x", "Gemini Flash", "x" * 70])
    def test_a_model_id_that_cannot_name_a_file_is_refused(self, model):
        with pytest.raises(QuotaRefused, match="model"):
            DailyQuota(PROJECT, 500, PACIFIC, model=model)


class TestAdmission:
    def test_flash_needs_an_untouched_day(self):
        with pytest.raises(QuotaRefused):
            quota(FLASH, 20, declared=1).admit_full_run(20 * 1)
        quota(FLASH, 20, declared=None)  # another day's state would be another test

    @pytest.mark.parametrize(("used", "admitted"), [(440, True), (441, False)])
    def test_flash_lite_needs_its_cases_times_its_attempts(self, used, admitted):
        book = quota(LITE, 500, declared=used)

        if admitted:
            book.admit_full_run(engines.ENGINE_CASES * 3)
        else:
            with pytest.raises(QuotaRefused, match="full run"):
                book.admit_full_run(engines.ENGINE_CASES * 3)


class TestOutages:
    async def test_flash_lite_retries_then_escalates_the_case(self, context, findings, dispute):
        vendor = Vendor([EngineUnavailable("transport")] * 3, name=LITE)
        engine = Governed(vendor, [], None, quota=quota(LITE, 500))
        engine.begin("c1")

        with pytest.raises(EngineUnavailable):
            await engine.decide(dispute, context, findings)
        assert vendor.calls == 3
        assert engine.statuses == {"c1": "unavailable"}

    async def test_flash_still_stops_the_run(self, context, findings, dispute):
        vendor = Vendor([EngineUnavailable("transport")], name=FLASH)
        engine = Governed(vendor, [], None, quota=quota(FLASH, 20))
        engine.begin("c1")

        with pytest.raises(RunStopped, match="outage"):
            await engine.decide(dispute, context, findings)
        assert vendor.calls == 1
