"""Attempts on record (specs/009-luna-service, FR-007, FR-007a, SC-007a)."""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

import asyncio
import sqlite3
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from app.application.investigate_dispute import DisputeInvestigator
from app.decision.base import (
    AttemptEnd,
    AttemptOutcome,
    AttemptStart,
    EngineMisidentified,
    EngineRefused,
    EngineRejected,
    EngineTelemetry,
    EngineUnavailable,
)
from app.decision.bounded import BoundedEngine
from app.infrastructure.actions import SqliteActionStore
from app.infrastructure.attempts import SqliteAttemptStore
from app.infrastructure.audit import SqliteAuditRepository
from app.providers.fake.adapter import FakeBillingProvider
from app.reconciliation.reconciler import estimated_from_audit
from tests.integration._auth import SERVICE, SERVICE_KEY
from tests.unit.test_bounded_engine import Clock, FakeEngine, answer, outage
from tests.unit.test_claude_engine import (  # noqa: F401 -- the fixtures are used by name
    context,
    dispute,
    findings,
)


def start(attempt_id="a1", invocation_id="i1", dispute_id="d1", number=1):
    return AttemptStart(
        attempt_id=attempt_id,
        invocation_id=invocation_id,
        dispute_id=dispute_id,
        number=number,
        provider="openai",
        requested_model="gpt-6-luna",
        started_at="2026-11-02T12:00:00+00:00",
        reserved_tokens=1000,
    )


def end(attempt_id="a1", outcome=AttemptOutcome.ANSWERED, **fields):
    return AttemptEnd(
        attempt_id=attempt_id,
        outcome=outcome,
        ended_at="2026-11-02T12:00:01+00:00",
        duration_ms=1000,
        **fields,
    )


@pytest.fixture
def store(tmp_path):
    held = SqliteAttemptStore(tmp_path / "audit.db")
    yield held
    held.close()


class TestTheRecord:
    def test_an_open_row_reads_unfinished(self, store):
        store.started(start())
        row = store.for_invocation("i1")[0]
        assert row["outcome"] is None
        assert row["ended_at"] is None

    def test_a_finished_row_holds_what_was_reported(self, store):
        store.started(start())
        store.finished(
            end(
                returned_model="gpt-6-luna",
                input_tokens=10,
                output_tokens=5,
                reasoning_tokens=2,
                cost_micros=7,
                pricing_as_of="a source",
            )
        )
        row = store.for_invocation("i1")[0]
        assert (row["outcome"], row["input_tokens"], row["cost_micros"]) == ("answered", 10, 7)
        assert '"reasoning_tokens": 2' in row["details_json"]

    def test_unknowns_stay_null(self, store):
        store.started(start())
        store.finished(end(outcome=AttemptOutcome.TIMEOUT))
        row = store.for_invocation("i1")[0]
        assert row["input_tokens"] is None
        assert row["cost_micros"] is None

    def test_an_ended_attempt_cannot_be_rewritten(self, store):
        store.started(start())
        store.finished(end(outcome=AttemptOutcome.TIMEOUT))
        store.finished(end(outcome=AttemptOutcome.ANSWERED))
        assert store.for_invocation("i1")[0]["outcome"] == "timeout"

    def test_it_commits_alone(self, tmp_path, store):
        store.started(start())
        other = sqlite3.connect(tmp_path / "audit.db")
        assert other.execute("SELECT COUNT(*) FROM engine_attempts").fetchone()[0] == 1
        other.close()

    def test_two_invocations_of_one_dispute_stay_apart(self, store):
        store.started(start("a1", "i1"))
        store.started(start("a2", "i2"))
        assert [r["attempt_id"] for r in store.for_invocation("i2")] == ["a2"]
        assert len(store.for_dispute("d1")) == 2

    def test_no_invocation_no_attempts(self, store):
        assert store.for_invocation(None) == []

    def test_the_schema_holds_no_room_for_text(self):
        from app.infrastructure.attempts import SCHEMA

        for word in ("message", "explanation", "content", "error_text", "prompt"):
            assert word not in SCHEMA


class TestThroughTheEngine:
    async def test_records_survive_a_refusal(self, store, dispute, context, findings):
        wrapper = BoundedEngine(FakeEngine(EngineRejected("403")), store)
        with pytest.raises(EngineRefused):
            await wrapper.decide(dispute, context, findings)
        assert [r["outcome"] for r in store.for_dispute(dispute.id)] == ["rejected"]

    async def test_records_survive_exhaustion(self, store, dispute, context, findings):
        clock = Clock()
        engine = FakeEngine(outage("transport"), outage("transport"), outage("transport"))
        wrapper = BoundedEngine(engine, store, clock=clock, sleep=clock.sleep)
        with pytest.raises(EngineUnavailable):
            await wrapper.decide(dispute, context, findings)
        assert [r["outcome"] for r in store.for_dispute(dispute.id)] == ["unavailable"] * 3

    async def test_records_survive_a_cancellation(self, store, dispute, context, findings):
        async def stall():
            await asyncio.sleep(30)

        engine = FakeEngine(stall)
        task = asyncio.create_task(BoundedEngine(engine, store).decide(dispute, context, findings))
        while not engine.sent:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [r["outcome"] for r in store.for_dispute(dispute.id)] == ["cancelled"]

    async def test_a_process_stopped_mid_attempt_stays_unfinished(self, store):
        store.started(start())
        assert all(row["outcome"] != "answered" for row in store.for_dispute("d1"))
        assert store.for_dispute("d1")[0]["outcome"] is None

    async def test_concurrent_attempts(self, store, dispute, context, findings):
        wrapper = BoundedEngine(FakeEngine(), store)
        wrapper._limiter.spacing = 0.0
        await asyncio.gather(*[wrapper.decide(dispute, context, findings) for _ in range(4)])
        assert len(store.for_dispute(dispute.id)) == 4


class Fixed:
    """An engine that always does one thing."""

    name = "gpt-6-luna"
    version = "gpt-6-luna/decision_v3"

    def __init__(self, outcome):
        self.outcome = outcome

    async def decide(self, dispute, context, findings):
        raise self.outcome


class TestTheInvestigation:
    def investigator(self, tmp_path, engine):
        audit = SqliteAuditRepository(tmp_path / "audit.db")
        return (
            DisputeInvestigator(
                provider=FakeBillingProvider(),
                engine=engine,
                audit=audit,
                actions=SqliteActionStore(tmp_path / "audit.db"),
            ),
            audit,
        )

    @staticmethod
    def a_dispute():
        from app.domain.models import Dispute

        return Dispute(
            id="dsp_test",
            customer_message="J'ai été facturé deux fois.",
            customer_email="alice@acme.fr",
            customer_id="cus_acme",
            invoice_id="inv_002",
        )

    async def test_a_wrong_model_is_its_own_escalation(self, tmp_path):
        error = EngineMisidentified("wrong_model")
        error.invocation_id = "inv-1"
        investigator, audit = self.investigator(tmp_path, Fixed(error))
        result = await investigator.investigate(self.a_dispute())
        names = [event["event"] for event in audit.events(result.run_id)]
        assert "decision_engine_identity_failed" in names
        assert "decision_engine_unavailable" not in names
        assert "decision_created" not in names
        assert "autre modèle" in result.explanation
        assert "indisponible" not in result.explanation
        assert result.recommended_action.value == "escalate"
        assert audit.run(result.run_id)["engine_invocation_id"] == "inv-1"

    async def test_an_outage_links_its_attempts(self, tmp_path):
        error = EngineUnavailable("timeout")
        error.invocation_id = "inv-2"
        investigator, audit = self.investigator(tmp_path, Fixed(error))
        result = await investigator.investigate(self.a_dispute())
        names = [event["event"] for event in audit.events(result.run_id)]
        assert "decision_engine_unavailable" in names
        assert audit.run(result.run_id)["engine_invocation_id"] == "inv-2"

    async def test_an_answer_links_its_attempts_and_its_floor(self, tmp_path):
        clock = Clock()
        store = SqliteAttemptStore(tmp_path / "audit.db")
        engine = FakeEngine(outage("transport"), answer())
        wrapper = BoundedEngine(engine, store, clock=clock, sleep=clock.sleep)
        investigator, audit = self.investigator(tmp_path, wrapper)
        result = await investigator.investigate(self.a_dispute())
        row = audit.run(result.run_id)
        assert row["unknown_cost_attempts"] == 1
        assert row["cost_micros"] > 0
        assert len(store.for_invocation(row["engine_invocation_id"])) == 2

    async def test_unknown_usage_is_null_in_the_row(self, tmp_path):
        store = SqliteAttemptStore(tmp_path / "audit.db")
        wrapper = BoundedEngine(FakeEngine(answer(tokens=None)), store)
        investigator, audit = self.investigator(tmp_path, wrapper)
        result = await investigator.investigate(self.a_dispute())
        row = audit.run(result.run_id)
        assert row["input_tokens"] is None
        assert row["output_tokens"] is None
        assert row["cost_micros"] is None

    def test_other_engines_rows_are_unchanged(self):
        telemetry = EngineTelemetry(model_id="claude", prompt_version="v3", input_tokens=5)
        assert telemetry.usage_known is True
        assert telemetry.invocation_id is None


def an_old_file(path):
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE agent_runs (
            run_id TEXT PRIMARY KEY, created_at TEXT, customer_id TEXT, model_id TEXT,
            cost_micros INTEGER, cache_read_input_tokens INTEGER, proposed_action TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO agent_runs VALUES "
        "('run_old', '2026-06-01T00:00:00+00:00', 'c', 'm', 5, 0, 'x')"
    )
    connection.commit()
    connection.close()


class TestAnOldDatabase:
    def test_it_migrates_and_keeps_its_rows(self, tmp_path):
        path = tmp_path / "audit.db"
        an_old_file(path)
        audit = SqliteAuditRepository(path)
        held = {r["name"] for r in audit._connection.execute("PRAGMA table_info(agent_runs)")}
        assert {"engine_invocation_id", "unknown_cost_attempts"} <= held
        assert audit.run("run_old")["engine_invocation_id"] is None

    def test_the_reconciler_reads_one_never_migrated(self, tmp_path):
        path = tmp_path / "audit.db"
        an_old_file(path)
        rows = estimated_from_audit(str(path), date(2026, 1, 1), date(2027, 1, 1))
        assert rows[0].lower_bound_runs == 0


class TestTheReconciler:
    async def test_a_floor_is_said_to_be_one(self, tmp_path):
        clock = Clock()
        path = tmp_path / "audit.db"
        store = SqliteAttemptStore(path)
        wrapper = BoundedEngine(
            FakeEngine(outage("transport"), answer()), store, clock=clock, sleep=clock.sleep
        )
        investigator, _ = TestTheInvestigation().investigator(tmp_path, wrapper)
        await investigator.investigate(TestTheInvestigation.a_dispute())
        today = datetime.now(UTC).date()
        rows = estimated_from_audit(str(path), today, date(today.year + 1, 1, 1))
        assert rows[0].lower_bound_runs == 1


class TestAnOutageInTheReconciler:
    async def test_it_is_a_floor_not_a_gap_in_the_price_table(self, tmp_path):
        clock = Clock()
        path = tmp_path / "audit.db"
        script = [outage("transport"), outage("transport"), outage("transport")]
        wrapper = BoundedEngine(
            FakeEngine(*script), SqliteAttemptStore(path), clock=clock, sleep=clock.sleep
        )
        investigator, _ = TestTheInvestigation().investigator(tmp_path, wrapper)
        await investigator.investigate(TestTheInvestigation.a_dispute())
        today = datetime.now(UTC).date()
        rows = estimated_from_audit(str(path), today, date(today.year + 1, 1, 1))
        assert rows[0].lower_bound_runs == 1
        assert rows[0].priced_runs == rows[0].runs == 1


class TestThePage:
    @pytest.fixture
    def page(self, tmp_path, monkeypatch):
        from app.api import deps
        from app.main import app

        monkeypatch.setenv("AUDIT_DB", str(tmp_path / "audit.db"))
        monkeypatch.setenv("RESOLVE_SERVICE_KEY", SERVICE_KEY)
        cached = (deps.get_audit, deps.get_actions, deps.get_attempts)
        for function in cached:
            function.cache_clear()
        yield TestClient(app, headers=SERVICE), deps
        for function in cached:
            function.cache_clear()

    async def _run(self, deps, script):
        clock = Clock()
        wrapper = BoundedEngine(
            FakeEngine(*script), deps.get_attempts(), clock=clock, sleep=clock.sleep
        )
        investigator = DisputeInvestigator(
            provider=FakeBillingProvider(),
            engine=wrapper,
            audit=deps.get_audit(),
            actions=deps.get_actions(),
        )
        return await investigator.investigate(TestTheInvestigation.a_dispute())

    async def test_attempts_are_shown_and_the_floor_is_said(self, page):
        http, deps = page
        result = await self._run(deps, [outage("transport"), answer()])
        html = http.get(f"/api/v1/runs/{result.run_id}/view").text
        assert "Tentatives auprès du moteur" in html
        assert "unavailable" in html
        assert "answered" in html
        assert "inconnu" in html
        assert "au moins $" in html

    async def test_an_unfinished_attempt_says_so(self, page):
        http, deps = page
        result = await self._run(deps, [answer()])
        invocation = deps.get_audit().run(result.run_id)["engine_invocation_id"]
        deps.get_attempts().started(start("open", invocation, "dsp_test", 2))
        assert "non terminée" in http.get(f"/api/v1/runs/{result.run_id}/view").text

    async def test_an_attempt_that_never_left_reads_as_zero(self, page):
        http, deps = page
        result = await self._run(deps, [answer()])
        invocation = deps.get_audit().run(result.run_id)["engine_invocation_id"]
        deps.get_attempts().started(start("gone", invocation, "dsp_test", 2))
        deps.get_attempts().finished(end("gone", AttemptOutcome.NOT_SENT, cost_micros=0))
        html = http.get(f"/api/v1/runs/{result.run_id}/view").text
        assert "not_sent" in html
        assert "$0.00000" in html
        assert "au moins" not in html

    async def test_the_api_returns_them(self, page):
        http, deps = page
        result = await self._run(deps, [answer()])
        body = http.get(f"/api/v1/runs/{result.run_id}").json()
        assert [attempt["outcome"] for attempt in body["attempts"]] == ["answered"]

    def test_a_run_of_another_engine_shows_none(self, page):
        http, _ = page
        created = http.post(
            "/api/v1/disputes/investigate",
            json={
                "message": "Je crois que j'ai été facturé deux fois pour septembre.",
                "customer_email": "alice@acme.fr",
                "invoice_id": "inv_002",
            },
        ).json()
        run_id = created["run_id"]
        assert "Tentatives auprès du moteur" not in http.get(f"/api/v1/runs/{run_id}/view").text
        assert http.get(f"/api/v1/runs/{run_id}").json()["attempts"] == []
