"""The daily request quota of an engine evaluated on a free tier
(specs/004-second-engines, amendment; T092)."""

import json
import threading
from datetime import UTC, datetime

import pytest

from evals import state
from evals.limits import RunStopped
from evals.quota import DailyQuota, QuotaRefused

PROJECT = "resolve-billing-eval"
PACIFIC = "America/Los_Angeles"


def at(*utc) -> float:
    return datetime(*utc, tzinfo=UTC).timestamp()


#: 2026-10-24, mid-afternoon in California (PDT, UTC-7).
NOON = at(2026, 10, 24, 20, 0)


class Clock:
    def __init__(self, now: float = NOON) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def quota(clock=None, limit=20, project=PROJECT, timeout=2.0) -> DailyQuota:
    return DailyQuota(
        project,
        limit,
        PACIFIC,
        model="gemini-3.8-flash",
        now=clock or Clock(),
        lock_timeout=timeout,
    )


def held() -> dict:
    return json.loads((state.STATE_DIR / f"quota-{PROJECT}.json").read_text(encoding="utf-8"))


class TestTheCount:
    def test_the_twentieth_request_is_sent_the_twenty_first_refused(self):
        book = quota()
        book.declare(0)

        for _ in range(20):
            book.take()

        with pytest.raises(RunStopped, match="daily quota exhausted"):
            book.take()
        assert held()["days"]["2026-10-24"] == {"declared": 0, "sent": 20}

    def test_requests_declared_as_already_used_count(self):
        book = quota()
        book.declare(18)
        book.take()
        book.take()

        with pytest.raises(RunStopped, match="exhausted"):
            book.take()

    def test_the_count_survives_a_restart(self):
        clock = Clock()
        quota(clock).declare(0)
        quota(clock).take()
        quota(clock).take()

        assert held()["days"]["2026-10-24"]["sent"] == 2

    def test_a_count_taken_then_interrupted_stays_counted(self):
        clock = Clock()
        book = quota(clock)
        book.declare(0)
        book.take()  # the process dies here, before the request is answered

        assert quota(clock).state()["days"]["2026-10-24"]["sent"] == 1

    def test_two_checkouts_on_one_project_share_the_count_under_one_lock(self):
        clock = Clock()
        quota(clock).declare(0)
        outcomes: list[str] = []

        def spend():
            try:
                quota(clock, timeout=30.0).take()
                outcomes.append("sent")
            except RunStopped:
                outcomes.append("stopped")

        threads = [threading.Thread(target=spend) for _ in range(25)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert outcomes.count("sent") == 20
        assert outcomes.count("stopped") == 5


class TestTheDay:
    def test_the_day_turns_at_pacific_midnight(self):
        clock = Clock(at(2026, 10, 25, 6, 59, 59))  # 23:59:59 PDT, the 24th
        book = quota(clock)
        book.declare(0)
        book.take()
        clock.now = at(2026, 10, 25, 7, 0, 0)  # midnight PDT: the 25th

        with pytest.raises(RunStopped, match="2026-10-25 not declared"):
            book.take()

    def test_daylight_saving_moves_midnight_to_eight_utc(self):
        # Standard time from 2026-11-01: midnight Pacific is 08:00 UTC.
        clock = Clock(at(2026, 11, 2, 7, 30))  # 23:30 PST, the 1st
        book = quota(clock)
        book.declare(0)
        book.take()
        assert "2026-11-01" in book.state()["days"]
        clock.now = at(2026, 11, 2, 8, 0)

        with pytest.raises(RunStopped, match="2026-11-02 not declared"):
            book.take()

    def test_pacific_dates_without_the_system_s_time_zone_database(self, monkeypatch):
        pytest.importorskip("tzdata", reason="the `engines` extra is not installed")
        import zoneinfo

        zoneinfo.reset_tzpath(to=[])
        zoneinfo.ZoneInfo.clear_cache()
        try:
            book = quota(Clock(at(2026, 10, 25, 6, 0)))  # 23:00 PDT, the 24th
            book.declare(0)
            book.take()
            assert "2026-10-24" in book.state()["days"]
        finally:
            zoneinfo.reset_tzpath()
            zoneinfo.ZoneInfo.clear_cache()


class TestTheDeclaration:
    def test_a_day_is_declared_once_and_never_lowered(self):
        book = quota()
        book.declare(5)

        with pytest.raises(QuotaRefused, match="already declared"):
            book.declare(0)
        assert held()["days"]["2026-10-24"]["declared"] == 5

    @pytest.mark.parametrize("used", [-1, 21])
    def test_a_count_outside_the_limit_is_refused(self, used):
        with pytest.raises(QuotaRefused):
            quota().declare(used)

    def test_an_undeclared_day_stops_the_run_before_sending(self):
        with pytest.raises(RunStopped, match="not declared"):
            quota().take()

    def test_a_full_run_starts_only_on_a_day_with_nothing_used(self):
        book = quota()
        book.declare(0)
        book.admit_full_run(20)

        book.take()
        with pytest.raises(QuotaRefused, match="full run"):
            book.admit_full_run(20)

    def test_a_full_run_is_refused_when_requests_were_already_used(self):
        book = quota()
        book.declare(1)

        with pytest.raises(QuotaRefused, match="full run"):
            book.admit_full_run(20)

    def test_a_full_run_is_refused_on_an_undeclared_day(self):
        with pytest.raises(QuotaRefused, match="not declared"):
            quota().admit_full_run(20)


class TestRefusals:
    def test_a_clock_gone_back_refuses_a_send_nothing_changed(self):
        clock = Clock()
        book = quota(clock)
        book.declare(0)
        book.take()
        before = (state.STATE_DIR / f"quota-{PROJECT}.json").read_bytes()
        clock.now = NOON - 60

        with pytest.raises(QuotaRefused, match="clock"):
            book.take()
        assert (state.STATE_DIR / f"quota-{PROJECT}.json").read_bytes() == before

    def test_a_clock_gone_back_refuses_an_admission(self):
        clock = Clock()
        book = quota(clock)
        book.declare(0)
        clock.now = NOON - 60

        with pytest.raises(QuotaRefused, match="clock"):
            book.admit_full_run(20)

    def test_a_clock_gone_back_refuses_a_declaration(self):
        clock = Clock()
        book = quota(clock)
        book.declare(0)
        clock.now = NOON - 86_400  # the day before, never declared

        with pytest.raises(QuotaRefused, match="clock"):
            book.declare(0)
        assert "2026-10-23" not in held()["days"]

    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            '{"project": "resolve-billing-eval"}',
            '{"project": "another-project", "days": {}, "last_seen": 0}',
            '{"project": "resolve-billing-eval", "last_seen": 0,'
            ' "days": {"2026-10-24": {"declared": -1, "sent": 0}}}',
            '{"project": "resolve-billing-eval", "last_seen": 0,'
            ' "days": {"24/10": {"declared": 0, "sent": 0}}}',
            '{"project": "resolve-billing-eval", "days": {}, "last_seen": "NaN"}',
        ],
    )
    def test_an_unreadable_or_inconsistent_state_is_refused_never_reset(self, content):
        state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = state.STATE_DIR / f"quota-{PROJECT}.json"
        path.write_text(content, encoding="utf-8")

        with pytest.raises(QuotaRefused):
            quota().take()
        assert path.read_text(encoding="utf-8") == content

    @pytest.mark.parametrize("project", ["", "Resolve", "a", "../escape", "x" * 31, "has space"])
    def test_a_project_id_that_is_not_google_s_is_refused(self, project):
        with pytest.raises(QuotaRefused, match="project"):
            quota(project=project)

    def test_the_state_says_what_it_counted(self):
        book = quota()
        book.declare(2)
        book.take()

        assert book.state() == {
            "project": PROJECT,
            "limit": 20,
            "timezone": PACIFIC,
            "days": {"2026-10-24": {"declared": 2, "sent": 1}},
        }
