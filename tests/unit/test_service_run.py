"""The measurement through the service's engine (specs/009-luna-service,
FR-014). No network."""

import json
from pathlib import Path

import pytest

from app.decision.base import EngineRejected, EngineUnavailable
from app.decision.hosted import Answer, configuration
from evals import engines, service_run, state
from evals.campaign import Campaign
from evals.first_calls import FirstCallsRefused
from evals.ledger import LedgerRefused
from evals.state import AccountBusy
from tests.unit.test_claude_engine import VALID_VERDICT
from tests.unit.test_evaluation_limits import _no_wait
from tests.unit.test_prepaid_runner import Scripted, verdict

LUNA = configuration("gpt-6-luna")


class Book:
    """A ledger in memory."""

    def __init__(self, total=5.0):
        self.total = total
        self.spent = {}

    def reserve(self, ticket, amount):
        if sum(self.spent.values()) + amount > self.total:
            raise LedgerRefused("beyond the envelope")
        self.spent[ticket] = amount

    def settle(self, ticket, amount):
        self.spent[ticket] = amount

    def state(self):
        return {"total": self.total, "left": self.total - sum(self.spent.values())}


@pytest.fixture(autouse=True)
def accounts(monkeypatch, tmp_path):
    monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
    for variable in ("OPENAI_CUSTOM_HEADERS", "OPENAI_LOG"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-test")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_test")
    monkeypatch.setattr("app.decision.limits.Limiter.acquire", _no_wait)
    monkeypatch.setattr("evals.state.AccountRun.wait", lambda self: 0.0)
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(service_run.FirstCalls, "admit_full_run", lambda self, config: None)
    book = Book()
    monkeypatch.setattr(service_run, "PrepaidLedger", lambda account, currency: book)
    return book


def measured(monkeypatch, tmp_path, vendor=None, argv=None):
    vendor = vendor or Scripted("gpt-6-luna")
    monkeypatch.setitem(engines.HOSTED_ENGINES, "gpt-6-luna", lambda: vendor)
    target = tmp_path / "gpt-6-luna-service.json"
    code = service_run.main(["--data-terms", "contractual", "--json", str(target), *(argv or [])])
    files = sorted(tmp_path.glob("gpt-6-luna-service-*.json"))
    return code, (json.loads(files[-1].read_text(encoding="utf-8")) if files else None)


class TestARun:
    def test_it_measures_every_case(self, monkeypatch, tmp_path):
        code, result = measured(monkeypatch, tmp_path)
        assert code in (0, 1)
        assert result["complete"] is True
        assert result["run_kind"] == "service run"
        assert result["cases_run"] == result["cases_total"] == 34
        assert result["engine"] == "gpt-6-luna"
        assert result["configuration_fingerprint"]
        assert all(attempt["outcome"] == "answered" for attempt in result["attempts"])

    def test_the_cost_is_settled(self, monkeypatch, tmp_path, accounts):
        _, result = measured(monkeypatch, tmp_path)
        assert result["cost"]["settled_amount"] > 0
        assert result["cost"]["unknown_cost_attempts"] == 0
        assert result["cost"]["complete"] is True

    def test_it_is_no_slot_of_the_campaign(self, monkeypatch, tmp_path):
        _, result = measured(monkeypatch, tmp_path)
        assert "campaign" not in result
        assert "slot" not in result
        manifest = Path("evals/comparisons/008/manifest.json")
        assert Campaign(manifest, results_dir=tmp_path).results() == {}


class TestRefusals:
    def test_another_case_file_is_refused(self, monkeypatch, tmp_path):
        code, result = measured(
            monkeypatch, tmp_path, argv=["--cases", str(tmp_path / "elsewhere.json")]
        )
        assert code == 2
        assert result is None

    def test_extra_fixtures_are_refused(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RESOLVE_EXTRA_FIXTURES_DIR", str(tmp_path))
        code, _ = measured(monkeypatch, tmp_path)
        assert code == 2

    def test_undeclared_accounts_are_refused(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENAI_ORG_ID")
        code, _ = measured(monkeypatch, tmp_path)
        assert code == 2

    def test_no_accepted_first_call_is_refused(self, monkeypatch, tmp_path):
        def refuse(self, config):
            raise FirstCallsRefused("no accepted first call")

        monkeypatch.setattr(service_run.FirstCalls, "admit_full_run", refuse)
        vendor = Scripted("gpt-6-luna")
        code, _ = measured(monkeypatch, tmp_path, vendor)
        assert code == 2
        assert vendor.calls == 0

    def test_a_second_run_on_the_account_is_refused(self, monkeypatch, tmp_path):
        class Busy:
            def __init__(self, account):
                pass

            def __enter__(self):
                raise AccountBusy("another run")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(service_run, "AccountRun", Busy)
        vendor = Scripted("gpt-6-luna")
        code, _ = measured(monkeypatch, tmp_path, vendor)
        assert code == 2
        assert vendor.calls == 0


class TestTheEnvelope:
    def test_beyond_it_nothing_is_sent(self, monkeypatch, tmp_path, accounts):
        accounts.total = 0.0
        vendor = Scripted("gpt-6-luna")
        code, result = measured(monkeypatch, tmp_path, vendor)
        assert code == 3
        assert vendor.calls == 0
        assert result["complete"] is False
        assert "ledger" in result["stop_reason"]

    def test_missing_usage_leaves_the_reservation_spent_and_stops(self, monkeypatch, tmp_path):
        vendor = Scripted(
            "gpt-6-luna", Answer(json.dumps(VALID_VERDICT), "stop", None, None, "gpt-6-luna")
        )
        code, result = measured(monkeypatch, tmp_path, vendor)
        assert code == 3
        assert "without its usage" in result["stop_reason"]
        assert result["cost"]["unknown_cost_attempts"] == 1
        assert result["cost"]["unknown_cost_reserved"] > 0

    def test_inconsistent_usage_leaves_it_spent_and_stops(self, monkeypatch, tmp_path):
        answer = Answer(json.dumps(VALID_VERDICT), "stop", 10, 5, "gpt-6-luna", reasoning_tokens=50)
        code, result = measured(monkeypatch, tmp_path, Scripted("gpt-6-luna", answer))
        assert code == 3
        assert result["cost"]["unknown_cost_attempts"] == 1

    def test_usage_above_the_reservation_settles_then_stops(self, monkeypatch, tmp_path):
        answer = verdict("gpt-6-luna", input_tokens=9_000_000, output_tokens=120)
        code, result = measured(monkeypatch, tmp_path, Scripted("gpt-6-luna", answer))
        assert code == 3
        assert "above the reservation" in result["stop_reason"]
        assert result["cost"]["unknown_cost_attempts"] == 0
        assert result["cost"]["settled_amount"] > 0


class TestStops:
    def test_a_refusal_writes_an_incomplete_result(self, monkeypatch, tmp_path):
        code, result = measured(
            monkeypatch, tmp_path, Scripted("gpt-6-luna", EngineRejected("401"))
        )
        assert code == 3
        assert result["complete"] is False
        assert "401" in result["stop_reason"]
        assert result["attempts"][0]["outcome"] == "rejected"
        assert result["cost"]["unknown_cost_attempts"] == 0

    def test_a_wrong_model_stops_it(self, monkeypatch, tmp_path):
        code, result = measured(
            monkeypatch, tmp_path, Scripted("gpt-6-luna", verdict("gpt-6-other"))
        )
        assert code == 3
        assert "another model" in result["stop_reason"]
        assert result["cost"]["unknown_cost_attempts"] == 0

    def test_an_interruption_writes_an_incomplete_result(self, monkeypatch, tmp_path):
        class Interrupted(Scripted):
            async def _complete(self, system, user):
                raise KeyboardInterrupt

        code, result = measured(monkeypatch, tmp_path, Interrupted("gpt-6-luna"))
        assert code == 3
        assert result["complete"] is False
        assert result["stop_reason"] == "interrupted"

    def test_an_outage_leaves_its_cost_unknown(self, monkeypatch, tmp_path):
        monkeypatch.setattr(service_run, "Bounds", lambda: _quick())
        code, result = measured(
            monkeypatch, tmp_path, Scripted("gpt-6-luna", EngineUnavailable("transport"))
        )
        assert result["cost"]["unknown_cost_attempts"] > 0
        assert code in (0, 1, 3)


class TestTheEdges:
    def test_an_attempt_that_never_left_costs_nothing(self):
        from app.decision.base import AttemptEnd, AttemptOutcome, AttemptStart

        book = Book()
        recorder = service_run.LedgerRecorder(book, LUNA, "run")
        recorder.started(AttemptStart("a", "i", "d", 1, "openai", "m", "t", 1000))
        recorder.finished(AttemptEnd("a", AttemptOutcome.NOT_SENT, "t", 0))
        assert recorder.open_reservations == {}
        assert recorder.cost == 0.0

    def test_a_factory_failure_writes_an_incomplete_result(self, monkeypatch, tmp_path):
        def broken():
            raise RuntimeError("no client")

        monkeypatch.setitem(engines.HOSTED_ENGINES, "gpt-6-luna", broken)
        target = tmp_path / "gpt-6-luna-service.json"
        with pytest.raises(RuntimeError):
            service_run.main(["--data-terms", "contractual", "--json", str(target)])
        result = json.loads(sorted(tmp_path.glob("gpt-6-luna-service-*.json"))[-1].read_text())
        assert result["complete"] is False
        assert "internal error" in result["stop_reason"]

    def test_a_close_that_fails_is_not_complete(self, monkeypatch, tmp_path):
        class Closing(Scripted):
            async def aclose(self):
                raise RuntimeError("no close")

        with pytest.raises(RuntimeError):
            measured(monkeypatch, tmp_path, Closing("gpt-6-luna"))
        result = json.loads(sorted(tmp_path.glob("gpt-6-luna-service-*.json"))[-1].read_text())
        assert result["complete"] is False

    def test_the_previous_runs_window_is_waited_for(self, monkeypatch, tmp_path):
        monkeypatch.setattr("evals.state.AccountRun.wait", lambda self: 0.01)
        slept = []
        real = service_run.asyncio.sleep

        async def noted(seconds):
            slept.append(seconds)
            await real(0)

        monkeypatch.setattr(service_run.asyncio, "sleep", noted)
        vendor = Scripted("gpt-6-luna")
        measured(monkeypatch, tmp_path, vendor)
        assert slept[0] == 0.01

    def test_an_unreadable_response_stops_it_as_governed_does(self, monkeypatch, tmp_path):
        unreadable = Answer(None, "other", None, None, None, ("response_unreadable",))
        code, result = measured(monkeypatch, tmp_path, Scripted("gpt-6-luna", unreadable))
        assert code == 3
        assert "without its usage" in result["stop_reason"]
        assert result["attempts"][0]["outcome"] == "unusable"


def _quick():
    from app.decision.bounded import Bounds

    return Bounds(backoff=(0.0, 0.0))
