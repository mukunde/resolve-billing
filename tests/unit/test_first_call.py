"""The governed engine under a daily quota, the first call, and what the
runner asks before a Gemini run (specs/004-second-engines, amendment; T094)."""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

import json

import pytest

from app.decision.base import EngineUnavailable
from evals import engines, runner, state
from evals.governed import Governed
from evals.limits import RunStopped
from evals.quota import DailyQuota
from tests.unit.test_claude_engine import (  # noqa: F401 -- the fixtures are used by name
    context,
    dispute,
    findings,
)
from tests.unit.test_evaluation_limits import Vendor, _no_wait, governed, ok

PROJECT = "resolve-billing-eval"


def gemini_quota(clock=None, declared=0) -> DailyQuota:
    book = DailyQuota(PROJECT, 20, "America/Los_Angeles", now=clock or (lambda: 1_792_872_000.0))
    if declared is not None:
        book.declare(declared)
    return book


class Recording(Vendor):
    """Notes, at each send, what the quota and the ledger already held."""

    def __init__(self, script, name="gemini-3.8-flash", quota=None, **kwargs):
        super().__init__(script, name=name, **kwargs)
        self.quota = quota
        self.seen: list[int] = []

    async def _complete(self, system, user):
        if self.quota is not None:
            self.seen.append(sum(d["sent"] for d in self.quota.state()["days"].values()))
        return await super()._complete(system, user)


class TestTheGovernedEngine:
    async def test_attempts_come_from_the_configuration(self, context, findings, dispute):
        vendor = Vendor([EngineUnavailable("transport")], name="gemini-3.8-flash")
        engine = Governed(vendor, [], None, quota=gemini_quota())
        engine.begin("c1")

        with pytest.raises(RunStopped):
            await engine.decide(dispute, context, findings)
        assert vendor.calls == 1

    async def test_the_quota_counts_each_request_before_it_is_sent(
        self, context, findings, dispute
    ):
        book = gemini_quota()
        vendor = Recording([ok()], quota=book)
        engine = Governed(vendor, [], None, quota=book)
        engine.begin("c1")

        await engine.decide(dispute, context, findings)

        assert vendor.seen == [1]
        assert engine.sends == 1

    async def test_an_exhausted_quota_sends_nothing(self, context, findings, dispute):
        vendor = Vendor([ok()], name="gemini-3.8-flash")
        engine = Governed(vendor, [], None, quota=gemini_quota(declared=20))
        engine.begin("c1")

        with pytest.raises(RunStopped, match="daily quota exhausted"):
            await engine.decide(dispute, context, findings)
        assert vendor.calls == 0
        assert engine.sends == 0

    async def test_an_undeclared_day_sends_nothing(self, context, findings, dispute):
        vendor = Vendor([ok()], name="gemini-3.8-flash")
        engine = Governed(vendor, [], None, quota=gemini_quota(declared=None))
        engine.begin("c1")

        with pytest.raises(RunStopped, match="not declared"):
            await engine.decide(dispute, context, findings)
        assert vendor.calls == 0

    async def test_a_ledger_refusal_after_the_count_sends_nothing(
        self, tmp_path, context, findings, dispute
    ):
        # Mistral has no daily limit; a quota handed to it still counts first,
        # and the ledger's refusal after it sends nothing.
        vendor = Vendor([ok()])
        engine, _ = governed(tmp_path, vendor, allowance=0.001)
        engine._quota = gemini_quota()
        engine.begin("c1")

        with pytest.raises(RunStopped, match="envelope"):
            await engine.decide(dispute, context, findings)
        assert vendor.calls == 0
        assert engine.sends == 0

    async def test_a_gemini_outage_stops_the_run(self, context, findings, dispute):
        engine = Governed(
            Vendor([EngineUnavailable("server_error")], name="gemini-3.8-flash"),
            [],
            None,
            quota=gemini_quota(),
        )
        engine.begin("c1")

        with pytest.raises(RunStopped, match="outage: server_error"):
            await engine.decide(dispute, context, findings)
        assert engine.statuses == {"c1": "unavailable"}

    async def test_a_mistral_outage_still_escalates_the_case(
        self, tmp_path, context, findings, dispute
    ):
        engine, _ = governed(tmp_path, Vendor([EngineUnavailable("transport")] * 3))
        engine.begin("c1")

        with pytest.raises(EngineUnavailable):
            await engine.decide(dispute, context, findings)

    async def test_a_vendor_429_is_told_apart_from_the_local_count(
        self, context, findings, dispute
    ):
        engine = Governed(
            Vendor([EngineUnavailable("rate_limited")], name="gemini-3.8-flash"),
            [],
            None,
            quota=gemini_quota(),
        )
        engine.begin("c1")

        with pytest.raises(RunStopped, match="rate limiting persisted"):
            await engine.decide(dispute, context, findings)


# -- the runner -------------------------------------------------------------------


def run_gemini(monkeypatch, tmp_path, factory, *extra, project=PROJECT):
    monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "set-by-the-test")
    if project is None:
        monkeypatch.delenv("GEMINI_QUOTA_PROJECT", raising=False)
    else:
        monkeypatch.setenv("GEMINI_QUOTA_PROJECT", project)
    monkeypatch.setitem(engines.HOSTED_ENGINES, "gemini-3.8-flash", factory)
    monkeypatch.setattr("evals.limits.Limiter.acquire", _no_wait)
    target = tmp_path / "gemini.json"
    argv = ["runner", "--engine", "gemini-3.8-flash", "--data-terms", "unknown"]
    monkeypatch.setattr("sys.argv", [*argv, "--json", str(target), *extra])
    status = runner.main()
    written = list(tmp_path.glob("gemini-*.json"))
    result = json.loads(written[0].read_text(encoding="utf-8")) if written else None
    return status, result


class Always(Vendor):
    async def _complete(self, system, user):
        self.calls += 1
        return ok()


def always():
    return Always([], name="gemini-3.8-flash")


class TestAGeminiRun:
    def test_a_full_run_on_a_fresh_declared_day(self, tmp_path, monkeypatch):
        status, result = run_gemini(
            monkeypatch, tmp_path, always, "--gemini-requests-used-today", "0"
        )

        assert result["complete"] is True
        assert result["sends"] == 20
        quota = result["daily_quota"]
        assert quota["project"] == PROJECT
        assert quota["limit"] == 20
        assert quota["timezone"] == "America/Los_Angeles"
        assert quota["run_sends"] == 20
        assert list(quota["days"].values()) == [{"declared": 0, "sent": 20}]
        assert "dedicated project" in quota["assumptions"]
        assert result["reservation_estimator"] == "utf8-bytes-v1"
        assert result["smoke_test"] is False

    def test_a_full_run_is_refused_when_the_day_is_not_fresh(self, tmp_path, monkeypatch, capsys):
        built = []
        status, result = run_gemini(
            monkeypatch,
            tmp_path,
            lambda: built.append(1) or always(),
            "--gemini-requests-used-today",
            "1",
        )

        assert status == 2
        assert built == []
        assert result is None
        assert "full run" in capsys.readouterr().err

    def test_a_gemini_run_needs_today_declared(self, tmp_path, monkeypatch, capsys):
        status, result = run_gemini(monkeypatch, tmp_path, always)

        assert status == 2
        assert result is None
        assert "not declared" in capsys.readouterr().err

    def test_a_day_declared_earlier_needs_no_flag(self, tmp_path, monkeypatch):
        DailyQuota(PROJECT, 20, "America/Los_Angeles").declare(0)

        status, result = run_gemini(monkeypatch, tmp_path, always)

        assert result["complete"] is True

    def test_the_flag_on_a_declared_day_is_refused(self, tmp_path, monkeypatch, capsys):
        DailyQuota(PROJECT, 20, "America/Los_Angeles").declare(0)

        status, _ = run_gemini(monkeypatch, tmp_path, always, "--gemini-requests-used-today", "0")

        assert status == 2
        assert "already declared" in capsys.readouterr().err

    @pytest.mark.parametrize("project", [None, "Not_A_Project"])
    def test_every_gemini_run_names_a_valid_project(self, tmp_path, monkeypatch, project):
        built = []
        status, result = run_gemini(
            monkeypatch,
            tmp_path,
            lambda: built.append(1) or always(),
            "--first-call-only",
            "--gemini-requests-used-today",
            "0",
            project=project,
        )

        assert status == 2
        assert built == []

    def test_the_declaration_flag_is_refused_for_mistral(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
        monkeypatch.setenv("MISTRAL_API_KEY", "set-by-the-test")
        argv = ["runner", "--engine", "mistral-large-4", "--data-terms", "unknown"]
        extra = ["--json", str(tmp_path / "m.json"), "--gemini-requests-used-today", "0"]
        monkeypatch.setattr("sys.argv", [*argv, *extra])

        with pytest.raises(SystemExit):
            runner.main()

    def test_the_new_flags_are_refused_for_the_service_s_engines(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["runner", "--first-call-only"])

        with pytest.raises(SystemExit):
            runner.main()


class TestTheFirstCall:
    def test_one_request_then_the_run_stops(self, tmp_path, monkeypatch):
        vendor = always()
        status, result = run_gemini(
            monkeypatch,
            tmp_path,
            lambda: vendor,
            "--first-call-only",
            "--gemini-requests-used-today",
            "3",
        )

        assert status == 3
        assert vendor.calls == 1
        assert result["smoke_test"] is True
        assert result["complete"] is False
        assert result["sends"] == 1
        assert result["stop_reason"] == "first call only"
        assert result["daily_quota"]["run_sends"] == 1

    def test_a_failed_first_call_is_not_retried_nor_followed(self, tmp_path, monkeypatch):
        class Failing(Vendor):
            async def _complete(self, system, user):
                self.calls += 1
                raise EngineUnavailable("transport")

        vendor = Failing([], name="gemini-3.8-flash")
        status, result = run_gemini(
            monkeypatch,
            tmp_path,
            lambda: vendor,
            "--first-call-only",
            "--gemini-requests-used-today",
            "0",
        )

        assert vendor.calls == 1
        assert result["sends"] == 1
        assert result["stop_reason"] == "outage: transport"

    def test_a_refusal_before_sending_records_no_send(self, tmp_path, monkeypatch):
        vendor = Vendor([], name="gemini-3.8-flash", reserved=10**9)
        status, result = run_gemini(
            monkeypatch,
            tmp_path,
            lambda: vendor,
            "--first-call-only",
            "--gemini-requests-used-today",
            "0",
        )

        assert vendor.calls == 0
        assert result["sends"] == 0
        assert result["stop_reason"] == "not sent: oversized"

    def test_a_mistral_first_call_makes_one_attempt(self, tmp_path, monkeypatch):
        class Failing(Vendor):
            async def _complete(self, system, user):
                self.calls += 1
                raise EngineUnavailable("transport")

        vendor = Failing([])
        monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
        monkeypatch.setenv("MISTRAL_API_KEY", "set-by-the-test")
        monkeypatch.setitem(engines.HOSTED_ENGINES, "mistral-large-4", lambda: vendor)
        monkeypatch.setattr("evals.limits.Limiter.acquire", _no_wait)
        argv = ["runner", "--engine", "mistral-large-4", "--data-terms", "unknown"]
        extra = ["--json", str(tmp_path / "m.json"), "--first-call-only"]
        monkeypatch.setattr("sys.argv", [*argv, *extra, "--mistral-allowance-usd", "10"])

        runner.main()

        assert vendor.calls == 1
        (written,) = list(tmp_path.glob("m-*.json"))
        result = json.loads(written.read_text(encoding="utf-8"))
        assert result["smoke_test"] is True
        assert result["sends"] == 1
        assert "daily_quota" not in result


class TestTheDevelopmentSet:
    def test_twenty_cases_reach_the_engine(self):
        """A pinned count: the day's quota is sized on it. A change of the
        development set updates it in the same commit."""
        import asyncio

        from app.api.deps import wire
        from app.application.investigate_dispute import DisputeInvestigator
        from app.decision.mock import MockDecisionEngine
        from app.domain.models import Dispute
        from app.providers.fake.adapter import FakeBillingProvider

        reached = []

        class Counting(MockDecisionEngine):
            async def decide(self, dispute, context, findings):
                reached.append(dispute.id)
                return await super().decide(dispute, context, findings)

        async def run_all():
            provider = FakeBillingProvider()
            investigator = DisputeInvestigator(provider=provider, engine=wire(Counting(), provider))
            for case in engines.development_cases(engines.DEVELOPMENT_SET):
                await investigator.investigate(
                    Dispute(
                        id=case["id"],
                        customer_message=case["message"],
                        customer_email=case["customer_email"],
                        customer_id=case.get("customer_id"),
                        invoice_id=case["invoice_id"],
                        headers=runner.header_pairs(case.get("headers")),
                    ),
                    now=runner.EVALUATION_DATE,
                )

        asyncio.run(run_all())

        assert len(reached) == 20


def test_the_state_folder_is_the_test_s_own():
    assert "account-state" in str(state.STATE_DIR)
