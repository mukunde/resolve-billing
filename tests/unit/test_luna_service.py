"""`gpt-6-luna` on the service path (specs/009-luna-service, FR-001, FR-004,
FR-009, FR-010). No network: the SDK on an in-memory transport, and only with
the `engines` extra.
"""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.decision.base import EngineRefused, EngineRejected
from app.decision.bounded import BoundedEngine
from app.decision.claude import ClaudeDecisionEngine
from app.decision.hosted import configuration
from app.decision.mock import MockDecisionEngine
from app.infrastructure.attempts import SqliteAttemptStore
from app.main import app
from evals import engines
from tests.integration._auth import SERVICE, SERVICE_KEY
from tests.unit.test_bounded_engine import VALID_VERDICT, Book, FakeEngine

LUNA = configuration("gpt-6-luna")
SENTINEL = "SENTINEL-vendor-text-with-a-secret"
READY = {
    "DECISION_ENGINE": "gpt-6-luna",
    "BILLING_PROVIDER": "fake",
    "RESOLVE_DATA": "synthetic",
    "OPENAI_API_KEY": "sk-test",
    "OPENAI_ORG_ID": "org-test",
    "OPENAI_PROJECT_ID": "proj_test",
}


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for name in ("OPENAI_CUSTOM_HEADERS", "OPENAI_LOG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUDIT_DB", str(tmp_path / "audit.db"))
    monkeypatch.setenv("RESOLVE_SERVICE_KEY", SERVICE_KEY)
    cached = (deps.get_engine, deps.get_attempts, deps.get_audit, deps.get_actions)
    for function in cached:
        function.cache_clear()
    yield
    for function in cached:
        function.cache_clear()


def ready(monkeypatch, **overrides):
    pytest.importorskip("openai")
    for name, value in {**READY, **overrides}.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


class TestTheSelection:
    def test_the_default_stays_the_mock(self, monkeypatch):
        monkeypatch.delenv("DECISION_ENGINE", raising=False)
        assert isinstance(deps.get_engine(), MockDecisionEngine)

    def test_luna_is_selected_by_one_variable(self, monkeypatch):
        ready(monkeypatch)
        engine = deps.get_engine()
        assert isinstance(engine, BoundedEngine)
        assert engine.name == "gpt-6-luna"

    def test_going_back_is_one_variable(self, monkeypatch):
        pytest.importorskip("anthropic")
        ready(monkeypatch)
        deps.get_engine()
        deps.get_engine.cache_clear()
        monkeypatch.setenv("DECISION_ENGINE", "claude")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert isinstance(deps.get_engine(), ClaudeDecisionEngine)

    @pytest.mark.parametrize("name", ["mock", "claude"])
    def test_the_others_never_read_the_declaration(self, name, monkeypatch):
        monkeypatch.setenv("DECISION_ENGINE", name)
        monkeypatch.setenv("BILLING_PROVIDER", "stripe")
        monkeypatch.delenv("RESOLVE_DATA", raising=False)
        assert deps.selection_refusal() is None

    def test_masking_reaches_the_inner_engine(self, monkeypatch):
        ready(monkeypatch)
        engine = deps.get_engine()
        finder = getattr(deps.get_provider(), "invoice_reference_spans", None)
        if finder is not None:
            assert engine.engine.invoice_references == finder


class TestTheRefusalAtStart:
    @pytest.mark.parametrize(
        ("overrides", "named"),
        [
            ({"BILLING_PROVIDER": "stripe"}, "BILLING_PROVIDER"),
            ({"RESOLVE_DATA": None}, "RESOLVE_DATA"),
            ({"RESOLVE_DATA": "yes"}, "RESOLVE_DATA"),
            ({"RESOLVE_DATA": "Synthetic"}, "RESOLVE_DATA"),
            ({"OPENAI_API_KEY": None}, "OPENAI_API_KEY"),
            ({"OPENAI_ORG_ID": None}, "OPENAI_ORG_ID"),
            ({"OPENAI_PROJECT_ID": None}, "OPENAI_PROJECT_ID"),
            ({"OPENAI_LOG": "debug"}, "OPENAI_LOG"),
        ],
    )
    def test_each_condition_is_named_at_start(self, overrides, named, monkeypatch):
        ready(monkeypatch, **overrides)
        with pytest.raises(RuntimeError, match=named):  # noqa: SIM117
            with TestClient(app):
                pass
        with pytest.raises(RuntimeError, match=named):
            deps.get_engine()

    def test_a_ready_selection_starts(self, monkeypatch):
        ready(monkeypatch)
        with TestClient(app) as client:
            assert client.get("/health").json()["decision_engine"] == "gpt-6-luna"


class TestTheLifespan:
    def test_two_successive_starts(self, monkeypatch):
        ready(monkeypatch)
        built = []
        with TestClient(app) as client:
            client.get("/health")
            built.append(deps.get_engine())
        assert deps.get_engine.cache_info().currsize == 0
        with TestClient(app) as client:
            client.get("/health")
            built.append(deps.get_engine())
        assert built[0] is not built[1]

    def test_twice_with_claude_too(self, monkeypatch):
        pytest.importorskip("anthropic")
        monkeypatch.setenv("DECISION_ENGINE", "claude")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        for _ in range(2):
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200

    async def test_nothing_built_nothing_closed(self):
        await deps.close_engine()
        assert deps.get_engine.cache_info().currsize == 0

    async def test_the_client_closes_on_the_way_out(self, monkeypatch):
        inner = FakeEngine()
        monkeypatch.setitem(deps.ENGINES, "fake-luna", lambda: BoundedEngine(inner, Book()))
        monkeypatch.setenv("DECISION_ENGINE", "fake-luna")
        deps.get_engine()
        await deps.close_engine()
        assert inner.closed


class TestTheEvaluationPathStays:
    def test_the_registry_holds_the_governed_engine(self):
        assert engines.evaluation_engines()["gpt-6-luna"] is engines.HOSTED_ENGINES["gpt-6-luna"]

    def test_the_runner_still_diverts_it(self):
        assert "gpt-6-luna" in engines.HOSTED

    def test_the_development_guard_holds(self):
        from pathlib import Path

        with pytest.raises(engines.OffTheDevelopmentSet):
            engines.guard("gpt-6-luna", Path("elsewhere.json"))


class TestARefusalOverHttp:
    """No vendor text anywhere it could be read (FR-004, SC-002)."""

    PAYLOAD = {
        "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
        "customer_email": "alice@acme.fr",
        "invoice_id": "inv_002",
    }

    def wired(self, monkeypatch, tmp_path, respond):
        httpx2 = pytest.importorskip("httpx2")
        from app.decision.openai_compatible import OpenAICompatibleEngine, build_client

        ready(monkeypatch)
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        store = SqliteAttemptStore(tmp_path / "audit.db")
        wrapper = BoundedEngine(
            OpenAICompatibleEngine(LUNA, build_client(LUNA, http_client=client)), store
        )
        monkeypatch.setattr(deps, "get_engine", lambda: wrapper)
        return store

    def test_a_refusal_is_a_502_with_a_category(self, monkeypatch, tmp_path, caplog):
        httpx2 = pytest.importorskip("httpx2")

        def respond(request):
            return httpx2.Response(403, json={"error": {"message": SENTINEL}})

        store = self.wired(monkeypatch, tmp_path, respond)
        http = TestClient(app, headers=SERVICE)
        with caplog.at_level(logging.DEBUG):
            response = http.post("/api/v1/disputes/investigate", json=self.PAYLOAD)
        assert response.status_code == 502
        assert response.json()["category"] == "403"
        assert SENTINEL not in response.text
        assert SENTINEL not in caplog.text
        rows = store._connection.execute("SELECT * FROM engine_attempts").fetchall()
        assert [row["outcome"] for row in rows] == ["rejected"]
        assert all(SENTINEL not in json.dumps(dict(row)) for row in rows)

    def test_exhaustion_escalates_without_vendor_text(self, monkeypatch, tmp_path, caplog):
        httpx2 = pytest.importorskip("httpx2")

        def respond(request):
            return httpx2.Response(
                503, headers={"retry-after": "0"}, json={"error": {"message": SENTINEL}}
            )

        store = self.wired(monkeypatch, tmp_path, respond)
        http = TestClient(app, headers=SERVICE)
        with caplog.at_level(logging.DEBUG):
            response = http.post("/api/v1/disputes/investigate", json=self.PAYLOAD)
        assert response.status_code == 200
        assert SENTINEL not in response.text
        assert SENTINEL not in caplog.text
        rows = store._connection.execute("SELECT outcome FROM engine_attempts").fetchall()
        assert [row["outcome"] for row in rows] == ["unavailable"] * 3

    def test_an_answer_goes_end_to_end(self, monkeypatch, tmp_path):
        httpx2 = pytest.importorskip("httpx2")

        def respond(request):
            return httpx2.Response(
                200,
                json={
                    "id": "c",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "gpt-6-luna",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(VALID_VERDICT),
                                "refusal": None,
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            )

        self.wired(monkeypatch, tmp_path, respond)
        http = TestClient(app, headers=SERVICE)
        run_id = http.post("/api/v1/disputes/investigate", json=self.PAYLOAD).json()["run_id"]
        body = http.get(f"/api/v1/runs/{run_id}").json()
        assert body["run"]["decision_engine"] == "gpt-6-luna"
        assert body["run"]["model_id"] == "gpt-6-luna"
        assert body["attempts"][0]["returned_model"] == "gpt-6-luna"


class TestClaudeKeepsItsRefusal:
    def test_a_plain_rejection_is_not_the_services_502(self):
        assert issubclass(EngineRefused, EngineRejected)
        assert not issubclass(EngineRejected, EngineRefused)
        assert EngineRejected not in app.exception_handlers
