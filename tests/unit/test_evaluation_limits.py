"""Limits, the Mistral envelope and incomplete runs, for the engines
evaluated on free tiers (specs/004-second-engines, T050)."""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

import asyncio
import contextlib
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.decision.base import EngineRejected, EngineUnavailable, RecommendedAction
from app.decision.hosted import Answer, HostedEngine, configuration
from evals import engines, runner, state
from evals.governed import Governed
from evals.ledger import Ledger, LedgerRefused
from evals.limits import Limiter, Oversized, RunStopped, limiters_for
from tests.unit.test_claude_engine import (  # noqa: F401 -- the fixtures are used by name
    VALID_VERDICT,
    context,
    dispute,
    findings,
)


@pytest.fixture(autouse=True)
def gemini_project(monkeypatch):
    """The maintainer's Gemini project, named as his `.env` names it."""
    monkeypatch.setenv("GEMINI_QUOTA_PROJECT", "resolve-billing-eval")


#: A Gemini run declares its quota day (spec 004 amendment).
FRESH_DAY = ("--gemini-requests-used-today", "0")


def fresh_quota(config):
    """The day's count a configuration with a daily limit runs against,
    on a freshly declared day; None for the others."""
    if config.requests_per_day is None:
        return None
    from evals.quota import DailyQuota

    book = DailyQuota("resolve-billing-eval", config.requests_per_day, config.quota_timezone)
    book.declare(0)
    return book


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


# -- the limiter ----------------------------------------------------------------


class TestTheLimiter:
    async def test_requests_are_spaced_by_the_request_rate(self):
        clock = Clock()
        limiter = Limiter(
            tokens_per_minute=100_000, requests_per_second=0.5, clock=clock, sleep=clock.sleep
        )

        await limiter.acquire(10)
        await limiter.acquire(10)

        assert clock.now == pytest.approx(2.0)

    async def test_tokens_wait_for_the_minute_to_free_them(self):
        clock = Clock()
        limiter = Limiter(
            tokens_per_minute=1000, requests_per_second=100, clock=clock, sleep=clock.sleep
        )

        await limiter.acquire(800)
        await limiter.acquire(800)

        assert clock.now >= 60.0
        assert limiter.waited >= 60.0

    async def test_a_request_over_the_quota_on_its_own_is_refused(self):
        clock = Clock()
        limiter = Limiter(
            tokens_per_minute=1000, requests_per_second=1, clock=clock, sleep=clock.sleep
        )

        with pytest.raises(Oversized):
            await limiter.acquire(1001)


# -- the ledger ---------------------------------------------------------------


OCTOBER = datetime(2026, 10, 24, tzinfo=UTC)
NOVEMBER = datetime(2026, 11, 1, tzinfo=UTC)


def ledger(tmp_path, when=OCTOBER, timeout=2.0):
    return Ledger(tmp_path / "envelope.json", now=lambda: when, lock_timeout=timeout)


class TestTheLedger:
    def test_the_allowance_is_declared_once_a_month(self, tmp_path):
        ledger(tmp_path).declare(10.0)

        with pytest.raises(LedgerRefused, match="already"):
            ledger(tmp_path).declare(10.0)

    def test_a_new_month_starts_a_new_ledger(self, tmp_path):
        ledger(tmp_path).declare(10.0)
        ledger(tmp_path).reserve("a", 9.0)

        november = ledger(tmp_path, when=NOVEMBER)
        november.declare(10.0)
        november.reserve("b", 9.0)

    def test_an_attempt_beyond_what_remains_is_refused(self, tmp_path):
        book = ledger(tmp_path)
        book.declare(1.0)
        book.reserve("a", 0.6)

        with pytest.raises(RunStopped, match="envelope"):
            book.reserve("b", 0.5)

    def test_settled_attempts_free_what_they_did_not_spend(self, tmp_path):
        book = ledger(tmp_path)
        book.declare(1.0)
        book.reserve("a", 0.6)
        book.settle("a", 0.1)

        book.reserve("b", 0.8)

    def test_an_unsettled_reservation_stays_spent_across_runs(self, tmp_path):
        first = ledger(tmp_path)
        first.declare(1.0)
        first.reserve("interrupted", 0.9)  # the run died before settling

        with pytest.raises(RunStopped):
            ledger(tmp_path).reserve("next", 0.2)

    def test_both_models_and_every_run_share_it(self, tmp_path):
        ledger(tmp_path).declare(1.0)
        ledger(tmp_path).reserve("mistral-large-4/case1/1", 0.5)

        with pytest.raises(RunStopped):
            ledger(tmp_path).reserve("zai-glm-5-3/case1/1", 0.6)

    def test_without_a_declared_allowance_nothing_is_reserved(self, tmp_path):
        with pytest.raises(LedgerRefused, match="allowance"):
            ledger(tmp_path).reserve("a", 0.1)

    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            '{"month": "2026-10"}',
            '{"month": "2026-10", "allowance_usd": -1, "attempts": {}}',
        ],
    )
    def test_an_unreadable_or_inconsistent_ledger_is_refused_never_reset(self, tmp_path, content):
        (tmp_path / "envelope.json").write_text(content, encoding="utf-8")

        with pytest.raises(LedgerRefused):
            ledger(tmp_path).reserve("a", 0.1)
        assert (tmp_path / "envelope.json").read_text(encoding="utf-8") == content

    def test_two_processes_at_once_never_both_spend_the_last_of_it(self, tmp_path):
        ledger(tmp_path).declare(1.0)
        outcomes: list[str] = []

        def spend(name):
            try:
                ledger(tmp_path, timeout=10.0).reserve(name, 0.6)
                outcomes.append("reserved")
            except RunStopped:
                outcomes.append("stopped")

        threads = [threading.Thread(target=spend, args=(f"t{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(outcomes) == ["reserved", "stopped"]

    def test_a_reservation_is_on_disk_before_the_attempt_is_sent(self, tmp_path):
        book = ledger(tmp_path)
        book.declare(1.0)

        book.reserve("a", 0.3)

        held = json.loads((tmp_path / "envelope.json").read_text(encoding="utf-8"))
        assert held["attempts"]["a"] == {"reserved": 0.3, "settled": None}


# -- the governed engine --------------------------------------------------------


class Vendor(HostedEngine):
    """A hosted engine whose vendor follows a script, one entry per call."""

    def __init__(self, script, name="mistral-large-4", reserved=3000):
        super().__init__(configuration(name))
        self.script = list(script)
        self.calls = 0
        self.reserved = reserved

    async def reservation(self, system, user):
        return self.reserved

    async def _complete(self, system, user):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def ok(input_tokens=900, output_tokens=120, text=None):
    return Answer(
        text=text if text is not None else json.dumps(VALID_VERDICT),
        finish="stop",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_version="m-1",
    )


def governed(tmp_path, vendor, allowance=10.0, attempts=3, deadline=3600):
    clock = Clock()
    book = None
    if vendor.config.vendor == "mistral":
        book = ledger(tmp_path)
        with contextlib.suppress(LedgerRefused):
            book.declare(allowance)
    limiters = [
        Limiter(
            tokens_per_minute=vendor.config.tokens_per_minute,
            requests_per_second=vendor.config.requests_per_second,
            clock=clock,
            sleep=clock.sleep,
        )
    ]
    return Governed(
        vendor,
        limiters,
        book,
        attempts=attempts,
        deadline=deadline,
        quota=fresh_quota(vendor.config),
        clock=clock,
        sleep=clock.sleep,
    ), clock


class TestTheGovernedEngine:
    async def test_a_decided_case(self, tmp_path, context, findings, dispute):
        engine, _ = governed(tmp_path, Vendor([ok()]))
        engine.begin("c1")

        decision = await engine.decide(dispute, context, findings)

        assert decision.recommended_action is RecommendedAction.PROPOSE_CREDIT_NOTE
        assert engine.statuses == {"c1": "decided"}
        assert engine.attempts == {"c1": 1}

    async def test_an_invalid_answer_is_counted_as_such(self, tmp_path, context, findings, dispute):
        engine, _ = governed(tmp_path, Vendor([ok(text="{not json")]))
        engine.begin("c1")

        await engine.decide(dispute, context, findings)

        assert engine.statuses == {"c1": "invalid"}

    async def test_a_request_over_the_quota_is_not_sent_and_never_a_decision(
        self, tmp_path, context, findings, dispute
    ):
        vendor = Vendor([ok()], reserved=25_000)
        engine, _ = governed(tmp_path, vendor)
        engine.begin("c1")

        decision = await engine.decide(dispute, context, findings)

        assert vendor.calls == 0
        assert engine.statuses == {"c1": "not_sent_oversized"}
        assert decision.recommended_action is RecommendedAction.ESCALATE

    async def test_rate_limiting_is_retried_then_stops_the_run(
        self, tmp_path, context, findings, dispute
    ):
        limited = [EngineUnavailable("rate_limited") for _ in range(3)]
        vendor = Vendor(limited)
        engine, _ = governed(tmp_path, vendor)
        engine.begin("c1")

        with pytest.raises(RunStopped, match="rate"):
            await engine.decide(dispute, context, findings)
        assert vendor.calls == 3

    async def test_retry_after_is_honoured(self, tmp_path, context, findings, dispute):
        limited = EngineUnavailable("rate_limited")
        limited.retry_after = 7.0
        engine, clock = governed(tmp_path, Vendor([limited, ok()]))
        engine.begin("c1")

        await engine.decide(dispute, context, findings)

        assert 7.0 in clock.slept

    async def test_a_transport_failure_past_the_attempts_is_an_outage(
        self, tmp_path, context, findings, dispute
    ):
        engine, _ = governed(tmp_path, Vendor([EngineUnavailable("transport")] * 3))
        engine.begin("c1")

        with pytest.raises(EngineUnavailable):
            await engine.decide(dispute, context, findings)
        assert engine.statuses == {"c1": "unavailable"}

    async def test_a_refusal_is_not_retried(self, tmp_path, context, findings, dispute):
        vendor = Vendor([EngineRejected("401")])
        engine, _ = governed(tmp_path, vendor)
        engine.begin("c1")

        with pytest.raises(EngineRejected):
            await engine.decide(dispute, context, findings)
        assert vendor.calls == 1

    async def test_usage_above_the_reservation_stops_the_run(
        self, tmp_path, context, findings, dispute
    ):
        engine, _ = governed(tmp_path, Vendor([ok(input_tokens=5000)], reserved=3000))
        engine.begin("c1")

        with pytest.raises(RunStopped, match="reservation"):
            await engine.decide(dispute, context, findings)

    async def test_usage_absent_stops_the_run(self, tmp_path, context, findings, dispute):
        engine, _ = governed(tmp_path, Vendor([ok(input_tokens=None)]))
        engine.begin("c1")

        with pytest.raises(RunStopped, match="usage"):
            await engine.decide(dispute, context, findings)

    async def test_the_envelope_is_charged_before_sending(
        self, tmp_path, context, findings, dispute
    ):
        vendor = Vendor([ok()])
        engine, _ = governed(tmp_path, vendor, allowance=0.001)
        engine.begin("c1")

        with pytest.raises(RunStopped, match="envelope"):
            await engine.decide(dispute, context, findings)
        assert vendor.calls == 0

    async def test_gemini_runs_without_an_envelope(self, tmp_path, context, findings, dispute):
        engine, _ = governed(tmp_path, Vendor([ok()], name="gemini-3.8-flash"))
        engine.begin("c1")

        await engine.decide(dispute, context, findings)

        assert engine.statuses == {"c1": "decided"}

    async def test_the_deadline_stops_the_run(self, tmp_path, context, findings, dispute):
        engine, clock = governed(tmp_path, Vendor([ok()]), deadline=10)
        clock.now = 11
        engine.begin("c1")

        with pytest.raises(RunStopped, match="deadline"):
            await engine.decide(dispute, context, findings)


# -- the runner -----------------------------------------------------------------


class TestTheRunner:
    def test_data_terms_are_required_and_accept_unknown(self, monkeypatch):
        monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "set-by-the-test")
        monkeypatch.setattr("sys.argv", ["runner", "--engine", "gemini-3.8-flash"])

        with pytest.raises(SystemExit):
            runner.main()
        assert "unknown" in runner.DATA_TERMS

    def test_an_interrupted_run_is_written_incomplete_and_never_overwrites(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "set-by-the-test")
        earlier = tmp_path / "gemini.json"
        earlier.write_text("earlier", encoding="utf-8")

        class Stopping(Vendor):
            async def _complete(self, system, user):
                raise KeyboardInterrupt

        monkeypatch.setitem(
            engines.HOSTED_ENGINES,
            "gemini-3.8-flash",
            lambda: Stopping([], name="gemini-3.8-flash"),
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "runner",
                "--engine",
                "gemini-3.8-flash",
                "--data-terms",
                "unknown",
                "--json",
                str(earlier),
                *FRESH_DAY,
            ],
        )

        assert runner.main() != 0
        assert earlier.read_text(encoding="utf-8") == "earlier"
        (written,) = [p for p in tmp_path.iterdir() if p != earlier]
        result = json.loads(written.read_text(encoding="utf-8"))
        assert result["complete"] is False
        assert result["stop_reason"] == "interrupted"
        assert result["not_attempted"]
        assert result["statement"].startswith("regression score on development cases")
        assert result["data_terms"] == "unknown"


class TestMoreLimits:
    async def test_mistral_s_two_models_share_one_limiter(self):
        clock = Clock()
        shared: dict = {}
        large = limiters_for(configuration("mistral-large-4"), shared, clock, clock.sleep)
        glm = limiters_for(configuration("zai-glm-5-3"), shared, clock, clock.sleep)

        assert large[1] is glm[1]
        # Alternating the models: the shared ceiling is the smaller one, for both.
        for limiters in (large, glm, large, glm):
            for limiter in limiters:
                await limiter.acquire(6000)
        assert clock.now >= 60.0

    def test_gemini_shares_with_nothing(self):
        assert len(limiters_for(configuration("gemini-3.8-flash"), {})) == 1

    async def test_a_configuration_with_uncapped_reasoning_is_never_sent(
        self, tmp_path, context, findings, dispute
    ):
        vendor = Vendor([ok()], name="gemini-3.8-flash")
        vendor.config = replace(vendor.config, reservation="uncapped")
        engine, _ = governed(tmp_path, vendor)
        engine.begin("c1")

        with pytest.raises(RunStopped, match="cap"):
            await engine.decide(dispute, context, findings)
        assert vendor.calls == 0

    async def test_a_call_outlasting_the_deadline_stops_the_run(
        self, tmp_path, context, findings, dispute
    ):
        class Hanging(Vendor):
            async def _complete(self, system, user):
                await asyncio.sleep(10)

        vendor = Hanging([], name="gemini-3.8-flash")
        engine = Governed(vendor, [], None, deadline=0.05, quota=fresh_quota(vendor.config))
        engine.begin("c1")

        with pytest.raises(RunStopped, match="deadline"):
            await engine.decide(dispute, context, findings)

    async def test_a_wait_past_the_deadline_stops_the_run(
        self, tmp_path, context, findings, dispute
    ):
        limited = EngineUnavailable("rate_limited")
        limited.retry_after = 600.0
        engine, clock = governed(tmp_path, Vendor([limited, ok()]), deadline=60)
        engine.begin("c1")

        with pytest.raises(RunStopped, match="deadline"):
            await engine.decide(dispute, context, findings)
        assert 600.0 not in clock.slept

    async def test_a_rate_limited_attempt_is_settled_at_nothing(
        self, tmp_path, context, findings, dispute
    ):
        engine, _ = governed(tmp_path, Vendor([EngineUnavailable("rate_limited"), ok()]))
        engine.begin("c1")

        await engine.decide(dispute, context, findings)

        held = json.loads((tmp_path / "envelope.json").read_text(encoding="utf-8"))
        first, second = held["attempts"].values()
        assert first["settled"] == 0.0
        assert second["settled"] == pytest.approx((900 * 1.36 + 120 * 4.18) / 1e6, abs=1e-6)

    async def test_usage_absent_leaves_the_reservation_spent(
        self, tmp_path, context, findings, dispute
    ):
        engine, _ = governed(tmp_path, Vendor([ok(output_tokens=None)]))
        engine.begin("c1")

        with pytest.raises(RunStopped):
            await engine.decide(dispute, context, findings)

        held = json.loads((tmp_path / "envelope.json").read_text(encoding="utf-8"))
        (attempt,) = held["attempts"].values()
        assert attempt["settled"] is None

    async def test_the_mistral_reservation_bounds_unicode_by_its_bytes(self):
        from app.decision.mistral import TEMPLATE_MARGIN, MistralEngine

        engine = MistralEngine(configuration("mistral-large-4"), client=object())
        text = "é" * 1000 + "日本" * 500  # 2000 characters, 5000 bytes

        reserved = await engine.reservation("", text)

        assert reserved - TEMPLATE_MARGIN - 4096 >= len(text.encode("utf-8"))

    async def test_a_mistral_engine_is_never_built_without_the_ledger(self):
        with pytest.raises(ValueError, match="ledger"):
            Governed(Vendor([]), [], None)


def run_main(monkeypatch, tmp_path, vendor_factory, *extra):
    monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "set-by-the-test")
    monkeypatch.setitem(engines.HOSTED_ENGINES, "gemini-3.8-flash", vendor_factory)
    target = tmp_path / "gemini.json"
    argv = ["runner", "--engine", "gemini-3.8-flash", "--data-terms", "unknown"]
    monkeypatch.setattr("sys.argv", [*argv, "--json", str(target), *FRESH_DAY, *extra])
    status = runner.main()
    (written,) = list(tmp_path.glob("gemini-*.json"))
    return status, json.loads(written.read_text(encoding="utf-8"))


async def _no_wait(self, tokens, remaining=None):
    if tokens > self.tokens_per_minute:
        raise Oversized(str(tokens))


class TestTheHostedRun:
    def test_a_stopped_run_is_incomplete_and_never_totalled_over_the_set(
        self, tmp_path, monkeypatch, capsys
    ):
        class Exhausted(Vendor):
            async def _complete(self, system, user):
                raise EngineUnavailable("rate_limited")

        monkeypatch.setattr("evals.governed.BACKOFF", 0.001)
        monkeypatch.setattr("evals.limits.Limiter.acquire", _no_wait)
        status, result = run_main(
            monkeypatch, tmp_path, lambda: Exhausted([], name="gemini-3.8-flash")
        )

        assert status == 3
        assert result["complete"] is False
        assert "rate limiting" in result["stop_reason"]
        assert result["stopped_at"] not in result["statuses"]
        assert len(result["outcomes"]) + 1 + len(result["not_attempted"]) == 34
        assert "/34" not in capsys.readouterr().out

    def test_a_cancelled_run_is_written_incomplete(self, tmp_path, monkeypatch):
        class Cancelled(Vendor):
            async def _complete(self, system, user):
                raise asyncio.CancelledError

        status, result = run_main(
            monkeypatch, tmp_path, lambda: Cancelled([], name="gemini-3.8-flash")
        )

        assert status != 0
        assert result["complete"] is False
        assert result["stop_reason"] == "interrupted"

    def test_a_complete_run_records_every_case_s_status(self, tmp_path, monkeypatch):
        class Always(Vendor):
            async def _complete(self, system, user):
                self.calls += 1
                return ok()

        monkeypatch.setattr("evals.limits.Limiter.acquire", _no_wait)
        status, result = run_main(
            monkeypatch, tmp_path, lambda: Always([], name="gemini-3.8-flash")
        )

        assert result["complete"] is True
        assert len(result["statuses"]) == 34
        assert set(result["statuses"].values()) <= {"decided", "rules_only"}
        assert result["configuration"]["model"] == "gemini-3.8-flash"
        assert result["cost"]["basis"] == "estimated zero under the free quota"
        assert status in (0, 1)

    def test_an_oversized_case_is_not_scored(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("evals.limits.Limiter.acquire", _no_wait)
        status, result = run_main(
            monkeypatch, tmp_path, lambda: Vendor([], name="gemini-3.8-flash", reserved=10**9)
        )

        oversized = [c for c, s in result["statuses"].items() if s == "not_sent_oversized"]
        assert oversized
        assert sorted(result["not_scored"]) == sorted(oversized)
        scored = 34 - len(oversized)
        assert f"cases                         : {scored}" in capsys.readouterr().out

    def test_the_mistral_allowance_is_for_mistral_alone(self, tmp_path, monkeypatch):
        with pytest.raises(SystemExit):
            run_main(monkeypatch, tmp_path, lambda: Vendor([]), "--mistral-allowance-usd", "10")

    def test_the_mock_s_output_is_unchanged(self, tmp_path, monkeypatch):
        target = tmp_path / "mock.json"
        monkeypatch.setattr("sys.argv", ["runner", "--json", str(target)])

        assert runner.main() == 0
        assert set(json.loads(target.read_text(encoding="utf-8"))) == {
            "engine",
            "engine_version",
            "cases_file",
            "cases_sha256",
            "ran_at",
            "outcomes",
        }

    def test_data_terms_are_refused_for_the_service_s_engines(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["runner", "--data-terms", "unknown"])

        with pytest.raises(SystemExit):
            runner.main()


# -- After review r160 -----------------------------------------------------------


class TestCountingAndWaiting:
    async def test_a_hanging_count_stops_the_run_at_the_deadline(self, context, findings, dispute):
        class HangingCount(Vendor):
            async def reservation(self, system, user):
                await asyncio.sleep(10)

        vendor = HangingCount([], name="gemini-3.8-flash")
        engine = Governed(vendor, [], None, deadline=0.05, quota=fresh_quota(vendor.config))
        engine.begin("c1")

        with pytest.raises(RunStopped, match="deadline"):
            await engine.decide(dispute, context, findings)

    async def test_a_count_outage_is_retried_then_left_unavailable(
        self, tmp_path, context, findings, dispute
    ):
        class FailingCount(Vendor):
            counts = 0

            async def reservation(self, system, user):
                self.counts += 1
                raise EngineUnavailable("transport")

        vendor = FailingCount([])
        engine, _ = governed(tmp_path, vendor)
        engine.begin("c1")

        with pytest.raises(EngineUnavailable):
            await engine.decide(dispute, context, findings)
        assert vendor.counts == 3
        assert vendor.calls == 0
        assert engine.statuses == {"c1": "unavailable"}

    async def test_a_limiter_wait_past_the_deadline_stops_the_run(self):
        clock = Clock()
        limiter = Limiter(1000, 100, clock=clock, sleep=clock.sleep)
        await limiter.acquire(800)

        with pytest.raises(RunStopped, match="deadline"):
            await limiter.acquire(800, remaining=lambda: 30.0)
        assert clock.slept == []

    async def test_network_time_leaves_the_backoff_out(self, tmp_path, context, findings, dispute):
        engine, _ = governed(tmp_path, Vendor([EngineUnavailable("server_error"), ok()]))
        engine.begin("c1")

        await engine.decide(dispute, context, findings)

        assert engine.network["c1"] == 0.0
        assert engine.waits["c1"] >= 2.0


class TestTheAccount:
    def test_the_ledger_lives_with_the_accounts_state(self):
        assert Ledger().path.parent == state.STATE_DIR

    def test_a_second_run_on_the_same_account_is_refused(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "set-by-the-test")
        built = []
        monkeypatch.setitem(
            engines.HOSTED_ENGINES, "gemini-3.8-flash", lambda: built.append(1) or Vendor([])
        )
        argv = ["runner", "--engine", "gemini-3.8-flash", "--data-terms", "unknown"]
        monkeypatch.setattr("sys.argv", [*argv, "--json", str(tmp_path / "g.json"), *FRESH_DAY])

        with state.AccountRun("google"):
            assert runner.main() == 2

        assert built == []
        assert "another run" in capsys.readouterr().err
        assert list(tmp_path.glob("g*.json")) == []

    def test_a_run_waits_out_the_previous_run_s_minute(self):
        now = [1000.0]
        with state.AccountRun("mistral", now=lambda: now[0]) as account:
            account.record_send()
        now[0] = 1020.0

        with state.AccountRun("mistral", now=lambda: now[0]) as account:
            assert account.wait() == pytest.approx(40.0)

    def test_an_unreadable_pace_waits_the_whole_minute(self):
        state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (state.STATE_DIR / "google.pace.json").write_text("{nope", encoding="utf-8")

        with state.AccountRun("google") as account:
            assert account.wait() == state.WINDOW_SECONDS

    def test_each_request_is_noted_before_it_is_sent(self, tmp_path, monkeypatch):
        noted = []

        class Noting(Vendor):
            async def _complete(self, system, user):
                noted.append((state.STATE_DIR / "google.pace.json").exists())
                return ok()

        monkeypatch.setattr("evals.limits.Limiter.acquire", _no_wait)
        run_main(monkeypatch, tmp_path, lambda: Noting([], name="gemini-3.8-flash"))

        assert noted
        assert all(noted)


class TestTheUngovernedPath:
    @pytest.mark.parametrize("with_cases", [False, True])
    async def test_the_plain_run_refuses_the_three_before_building_a_client(
        self, tmp_path, monkeypatch, with_cases
    ):
        built = []
        monkeypatch.setitem(
            engines.HOSTED_ENGINES, "mistral-large-4", lambda: built.append(1) or Vendor([])
        )
        other = tmp_path / "other.json"
        other.write_text("[]", encoding="utf-8")

        with pytest.raises(ValueError, match="hosted_run"):
            await runner.run("mistral-large-4", other, [] if with_cases else None)
        assert built == []


class TestTheRecord:
    def test_the_result_names_the_bytes_run_and_every_version(self, tmp_path, monkeypatch):
        def no_rereading(*_):
            raise AssertionError("the case file is read again after the guard")

        monkeypatch.setattr(runner, "_case_set_fingerprint", no_rereading)
        monkeypatch.setattr("evals.limits.Limiter.acquire", _no_wait)
        _, result = run_main(
            monkeypatch, tmp_path, lambda: Vendor([], name="gemini-3.8-flash", reserved=10**9)
        )

        assert result["cases_sha256"] == engines.DEVELOPMENT_SET_SHA256[:16]
        assert result["cases_file"] == "cases.json"
        assert result["configuration"]["api_version"] == "v1beta"
        assert result["masking_version"] == "masking_v1"
        assert len(result["schema_sha256"]) == 16

    def test_a_defect_is_written_incomplete_then_raised(self, tmp_path, monkeypatch):
        class Broken(Vendor):
            async def _complete(self, system, user):
                raise TypeError("a bug")

        monkeypatch.setattr("evals.limits.Limiter.acquire", _no_wait)
        with pytest.raises(TypeError):
            run_main(monkeypatch, tmp_path, lambda: Broken([], name="gemini-3.8-flash"))

        (written,) = list(tmp_path.glob("gemini-*.json"))
        result = json.loads(written.read_text(encoding="utf-8"))
        assert result["complete"] is False
        assert result["stop_reason"] == "internal error (TypeError)"
        assert "a bug" not in written.read_text(encoding="utf-8")

    def test_a_count_outage_is_not_scored_as_rules_only(self, tmp_path, monkeypatch):
        class FailingCount(Vendor):
            async def reservation(self, system, user):
                raise EngineUnavailable("transport")

        monkeypatch.setattr("evals.governed.BACKOFF", 0.001)
        _, result = run_main(
            monkeypatch, tmp_path, lambda: FailingCount([], name="gemini-3.8-flash")
        )

        # On Gemini an outage stops the run: the case is not scored, and
        # never counted as one the rules settled.
        assert result["stop_reason"] == "outage: transport"
        assert result["statuses"].get(result["stopped_at"]) is None
        assert "unavailable" not in result["statuses"].values()
        assert result["not_scored"] == []


# -- After review r161 -----------------------------------------------------------


class TestAfterR161:
    async def test_an_unreadable_mistral_answer_keeps_its_reservation_spent(
        self, tmp_path, context, findings, dispute
    ):
        pytest.importorskip("mistralai.client", reason="the `engines` extra is not installed")
        import httpx2
        from mistralai.client import errors as mistral_errors

        from app.decision.mistral import MistralEngine

        unreadable = mistral_errors.ResponseValidationError(
            "SENTINEL",
            httpx2.Response(200, request=httpx2.Request("POST", "https://api.mistral.ai")),
            ValueError("SENTINEL"),
        )

        class Chat:
            async def complete_async(self, **_):
                raise unreadable

        class Counted(MistralEngine):
            async def reservation(self, system, user):
                return 3000

        vendor = Counted(configuration("mistral-large-4"), client=type("C", (), {"chat": Chat()})())
        engine, _ = governed(tmp_path, vendor)
        engine.begin("c1")

        with pytest.raises(RunStopped, match="usage"):
            await engine.decide(dispute, context, findings)

        held = json.loads((tmp_path / "envelope.json").read_text(encoding="utf-8"))
        (attempt,) = held["attempts"].values()
        assert attempt["settled"] is None

    @pytest.mark.parametrize("status", [200, 302])
    def test_a_status_no_refusal_carries_is_not_a_refusal(self, status):
        pytest.importorskip("mistralai.client", reason="the `engines` extra is not installed")
        import httpx2
        from mistralai.client import errors as mistral_errors

        from app.decision.mistral import _translated

        response = httpx2.Response(status, request=httpx2.Request("POST", "https://x"))
        translated = _translated(mistral_errors.SDKError("SENTINEL", response, "SENTINEL"))

        assert isinstance(translated, EngineUnavailable)
        assert str(translated) == "server_error"

    async def test_counting_requests_are_paced_noted_and_counted_apart(
        self, tmp_path, context, findings, dispute
    ):
        class Remote(Vendor):
            counts_remotely = True

        noted = []
        clock = Clock()
        limiter = Limiter(100_000, 0.5, clock=clock, sleep=clock.sleep)
        vendor = Remote([ok()], name="gemini-3.8-flash")
        engine = Governed(
            vendor,
            [limiter],
            None,
            quota=fresh_quota(vendor.config),
            on_send=lambda: noted.append(clock.now),
            clock=clock,
            sleep=clock.sleep,
        )
        engine.begin("c1")

        await engine.decide(dispute, context, findings)

        assert engine.counts == {"c1": 1}
        assert engine.attempts == {"c1": 1}
        # The count, then the request, two seconds apart at 0.5 per second.
        assert noted == [0.0, 2.0]

    def test_mistral_counts_locally(self):
        from app.decision.mistral import MistralEngine

        assert MistralEngine.counts_remotely is False

    def test_an_interruption_before_the_first_case_leaves_every_case_unattempted(
        self, tmp_path, monkeypatch
    ):
        def interrupted(self):
            raise KeyboardInterrupt

        monkeypatch.setattr(state.AccountRun, "wait", interrupted)
        status, result = run_main(
            monkeypatch, tmp_path, lambda: Vendor([], name="gemini-3.8-flash")
        )

        assert status == 3
        assert result["stop_reason"] == "interrupted"
        assert len(result["not_attempted"]) == 34

    def test_a_client_that_cannot_be_built_leaves_every_case_unattempted(
        self, tmp_path, monkeypatch
    ):
        def unbuildable():
            raise RuntimeError("SENTINEL")

        with pytest.raises(RuntimeError):
            run_main(monkeypatch, tmp_path, unbuildable)

        (written,) = list(tmp_path.glob("gemini-*.json"))
        result = json.loads(written.read_text(encoding="utf-8"))
        assert result["stop_reason"] == "internal error (RuntimeError)"
        assert len(result["not_attempted"]) == 34

    @pytest.mark.parametrize("last", ['"NaN"', '"Infinity"', '"-Infinity"', "1e30"])
    def test_a_pace_that_is_not_a_time_waits_the_whole_minute(self, last):
        state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (state.STATE_DIR / "google.pace.json").write_text(
            '{"last_send": ' + last + "}", encoding="utf-8"
        )

        with state.AccountRun("google") as account:
            assert account.wait() == state.WINDOW_SECONDS
