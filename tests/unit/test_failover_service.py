"""The composite on the service path and in the measurement
(specs/010-haiku-fallback, FR-001, FR-009, FR-010, FR-012). No network: the
SDKs on in-memory transports, and only with the `engines` and `claude` extras.
"""

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.decision.bounded import BoundedEngine
from app.decision.failover import PRIMARY_BOUNDS, SECONDARY_BOUNDS, FailoverEngine
from app.decision.hosted import configuration
from app.decision.mock import MockDecisionEngine
from app.main import app
from evals import engines, runner, service_run, state
from tests.integration._auth import SERVICE, SERVICE_KEY
from tests.unit.test_bounded_engine import VALID_VERDICT
from tests.unit.test_evaluation_limits import _no_wait
from tests.unit.test_prepaid_runner import Scripted
from tests.unit.test_service_run import Book

LUNA = configuration("gpt-6-luna")
HAIKU = configuration("claude-haiku-5-5")
SENTINEL = "SENTINEL-vendor-text-with-a-secret"
READY = {
    "DECISION_ENGINE": deps.FAILOVER,
    "BILLING_PROVIDER": "fake",
    "RESOLVE_DATA": "synthetic",
    "OPENAI_API_KEY": "sk-test",
    "OPENAI_ORG_ID": "org-test",
    "OPENAI_PROJECT_ID": "proj_test",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "RESOLVE_ANTHROPIC_ORG_ID": "anth-org",
}
PAYLOAD = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for name in (
        "OPENAI_CUSTOM_HEADERS",
        "OPENAI_LOG",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_LOG",
    ):
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
    pytest.importorskip("anthropic")
    for name, value in {**READY, **overrides}.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


class TestTheSelection:
    def test_the_composite_is_selected_by_one_variable(self, monkeypatch):
        ready(monkeypatch)
        engine = deps.get_engine()
        assert isinstance(engine, FailoverEngine)
        assert engine.name == "gpt-6-luna+claude-haiku-5-5"
        assert "failover_v1" in engine.version
        assert engine.primary.engine.config.name == "gpt-6-luna"
        assert engine.secondary.engine.config.name == "claude-haiku-5-5"

    def test_haiku_stays_pinned_whatever_claude_model_says(self, monkeypatch):
        ready(monkeypatch, CLAUDE_MODEL="claude-opus-5")
        assert deps.get_engine().secondary.engine.config.model == "claude-haiku-5-5"

    def test_the_fingerprints_are_the_manifests(self):
        from evals.first_calls import fingerprint

        manifest = json.loads(
            Path("evals/comparisons/008/manifest.json").read_text(encoding="utf-8")
        )
        for config in (LUNA, HAIKU):
            assert fingerprint(config) == manifest["candidates"][config.name]["fingerprint"]

    def test_the_others_stay(self, monkeypatch):
        monkeypatch.delenv("DECISION_ENGINE", raising=False)
        assert isinstance(deps.get_engine(), MockDecisionEngine)
        deps.get_engine.cache_clear()
        ready(monkeypatch, DECISION_ENGINE="gpt-6-luna", ANTHROPIC_API_KEY=None)
        assert isinstance(deps.get_engine(), BoundedEngine)

    def test_masking_reaches_both_legs(self, monkeypatch):
        ready(monkeypatch)
        engine = deps.get_engine()
        finder = getattr(deps.get_provider(), "invoice_reference_spans", None)
        if finder is not None:
            assert engine.primary.engine.invoice_references == finder
            assert engine.secondary.engine.invoice_references == finder


class TestTheRefusalAtStart:
    @pytest.mark.parametrize(
        ("overrides", "named"),
        [
            ({"BILLING_PROVIDER": "stripe"}, "BILLING_PROVIDER"),
            ({"RESOLVE_DATA": None}, "RESOLVE_DATA"),
            ({"OPENAI_API_KEY": None}, "OPENAI_API_KEY"),
            ({"ANTHROPIC_API_KEY": None}, "ANTHROPIC_API_KEY"),
            ({"RESOLVE_ANTHROPIC_ORG_ID": None}, "RESOLVE_ANTHROPIC_ORG_ID"),
            ({"ANTHROPIC_LOG": "debug"}, "ANTHROPIC_LOG"),
            ({"OPENAI_LOG": "debug"}, "OPENAI_LOG"),
        ],
    )
    def test_each_condition_is_named(self, overrides, named, monkeypatch):
        ready(monkeypatch, **overrides)
        with pytest.raises(RuntimeError, match=named), TestClient(app):
            pass

    def test_two_successive_starts(self, monkeypatch):
        ready(monkeypatch)
        for _ in range(2):
            with TestClient(app) as client:
                assert client.get("/health").json()["decision_engine"] == deps.FAILOVER


class TestThePlainRunner:
    def test_it_leaves_the_composite_out(self):
        assert deps.FAILOVER in engines.SERVICE_ONLY
        assert deps.FAILOVER not in engines.evaluation_engines()

    def test_it_refuses_before_a_case_or_a_client(self, monkeypatch, tmp_path):
        built = []
        monkeypatch.setitem(deps.ENGINES, deps.FAILOVER, lambda: built.append(1))
        monkeypatch.setattr(
            "sys.argv", ["runner", "--engine", deps.FAILOVER, "--cases", str(tmp_path / "none")]
        )
        with pytest.raises(SystemExit):
            runner.main()
        assert built == []


def completion(model):
    return {
        "id": "c",
        "object": "chat.completion",
        "created": 1,
        "model": model,
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
    }


def message():
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": json.dumps(VALID_VERDICT)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


class TestThroughTheEndpoint:
    """The real wiring, the SDKs on in-memory transports (T032, SC-003)."""

    def wired(self, monkeypatch, openai_reply, anthropic_reply):
        """The real factory: only the transports under the SDKs are fake."""
        openai_http = pytest.importorskip("httpx2")
        import httpx

        from app.decision import anthropic_hosted, openai_compatible

        ready(monkeypatch)
        real_openai = openai_compatible.build_client
        real_anthropic = anthropic_hosted.build_client
        monkeypatch.setattr(
            openai_compatible,
            "build_client",
            lambda config: real_openai(
                config,
                http_client=openai_http.AsyncClient(
                    transport=openai_http.MockTransport(openai_reply)
                ),
            ),
        )
        monkeypatch.setattr(
            anthropic_hosted,
            "build_client",
            lambda config: real_anthropic(
                config,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(anthropic_reply)),
            ),
        )
        engine = deps.get_engine()
        assert engine.primary.bounds is PRIMARY_BOUNDS
        assert engine.secondary.bounds is SECONDARY_BOUNDS

    def test_a_run_that_fell_back(self, monkeypatch, tmp_path, caplog):
        import httpx

        httpx2 = pytest.importorskip("httpx2")

        seen = []

        def openai_reply(request):
            return httpx2.Response(503, json={"error": {"message": SENTINEL}})

        def anthropic_reply(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=message())

        self.wired(monkeypatch, openai_reply, anthropic_reply)
        http = TestClient(app, headers=SERVICE)
        with caplog.at_level(logging.DEBUG):
            response = http.post("/api/v1/disputes/investigate", json=PAYLOAD)
        assert response.status_code == 200
        assert SENTINEL not in response.text
        assert SENTINEL not in caplog.text
        assert seen[0]["model"] == "claude-haiku-5-5"
        body = http.get(f"/api/v1/runs/{response.json()['run_id']}").json()
        assert body["run"]["decision_engine"] == deps.FAILOVER
        assert body["run"]["model_id"] == "claude-haiku-5-5"
        assert body["run"]["unknown_cost_attempts"] == 1
        attempts = [(a["number"], a["provider"], a["outcome"]) for a in body["attempts"]]
        assert attempts == [(1, "openai", "unavailable"), (2, "anthropic", "answered")]
        page = http.get(f"/api/v1/runs/{response.json()['run_id']}/view").text
        assert "claude-haiku-5-5" in page
        assert "au moins $" in page
        assert SENTINEL not in json.dumps(body)

    def test_a_wrong_second_model_is_attributed(self, monkeypatch):
        import httpx

        httpx2 = pytest.importorskip("httpx2")

        def openai_reply(request):
            return httpx2.Response(503, json={"error": {"message": SENTINEL}})

        def anthropic_reply(request):
            return httpx.Response(200, json={**message(), "model": "claude-other"})

        self.wired(monkeypatch, openai_reply, anthropic_reply)
        http = TestClient(app, headers=SERVICE)
        run_id = http.post("/api/v1/disputes/investigate", json=PAYLOAD).json()["run_id"]
        body = http.get(f"/api/v1/runs/{run_id}").json()
        assert body["run"]["model_id"] == "claude-other"
        assert body["run"]["unknown_cost_attempts"] == 1
        assert body["run"]["cost_micros"] > 0
        assert "decision_engine_identity_failed" in [e["event"] for e in body["events"]]
        assert [a["outcome"] for a in body["attempts"]] == ["unavailable", "wrong_model"]
        page = http.get(f"/api/v1/runs/{run_id}/view").text
        assert "claude-other" in page
        assert "au moins $" in page

    def test_a_refusal_never_reaches_anthropic(self, monkeypatch, tmp_path):
        import httpx

        httpx2 = pytest.importorskip("httpx2")

        seen = []

        def openai_reply(request):
            return httpx2.Response(403, json={"error": {"message": SENTINEL}})

        def anthropic_reply(request):
            seen.append(request)
            return httpx.Response(200, json=message())

        self.wired(monkeypatch, openai_reply, anthropic_reply)
        http = TestClient(app, headers=SERVICE)
        response = http.post("/api/v1/disputes/investigate", json=PAYLOAD)
        assert response.status_code == 502
        assert seen == []

    def test_luna_answering_leaves_anthropic_alone(self, monkeypatch, tmp_path):
        import httpx

        httpx2 = pytest.importorskip("httpx2")

        seen = []

        def openai_reply(request):
            return httpx2.Response(200, json=completion("gpt-6-luna"))

        def anthropic_reply(request):
            seen.append(request)
            return httpx.Response(200, json=message())

        self.wired(monkeypatch, openai_reply, anthropic_reply)
        http = TestClient(app, headers=SERVICE)
        run_id = http.post("/api/v1/disputes/investigate", json=PAYLOAD).json()["run_id"]
        assert http.get(f"/api/v1/runs/{run_id}").json()["run"]["model_id"] == "gpt-6-luna"
        assert seen == []


class TestTheMeasurement:
    @pytest.fixture(autouse=True)
    def account(self, monkeypatch, tmp_path):
        monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("RESOLVE_ANTHROPIC_ORG_ID", "anth-org")
        monkeypatch.setattr("app.decision.limits.Limiter.acquire", _no_wait)
        monkeypatch.setattr("evals.state.AccountRun.wait", lambda self: 0.0)
        monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
        admitted = []
        monkeypatch.setattr(
            service_run.FirstCalls,
            "admit_full_run",
            lambda self, config: admitted.append(config.name),
        )
        self.admitted = admitted
        self.book = Book()
        monkeypatch.setattr(service_run, "PrepaidLedger", lambda account, currency: self.book)

    def measured(self, monkeypatch, tmp_path, flags):
        vendor = Scripted("claude-haiku-5-5")
        monkeypatch.setitem(engines.HOSTED_ENGINES, "claude-haiku-5-5", lambda: vendor)

        def no_openai():
            raise AssertionError("a client for OpenAI was built")

        monkeypatch.setitem(engines.HOSTED_ENGINES, "gpt-6-luna", no_openai)
        target = tmp_path / "composite.json"
        code = service_run.main(["--data-terms", "contractual", "--json", str(target), *flags])
        files = sorted(tmp_path.glob("composite-*.json"))
        return code, (json.loads(files[-1].read_text(encoding="utf-8")) if files else None), vendor

    def test_the_composite_needs_the_flag(self, monkeypatch, tmp_path):
        with pytest.raises(SystemExit):
            self.measured(monkeypatch, tmp_path, ["--engine", deps.FAILOVER])

    def test_the_flag_needs_the_composite(self, monkeypatch, tmp_path):
        with pytest.raises(SystemExit):
            self.measured(monkeypatch, tmp_path, ["--primary-disabled"])

    def test_every_dispute_reaches_haiku(self, monkeypatch, tmp_path):
        code, result, vendor = self.measured(
            monkeypatch, tmp_path, ["--engine", deps.FAILOVER, "--primary-disabled"]
        )
        assert code in (0, 1)
        assert result["complete"] is True
        assert result["engine"] == deps.FAILOVER
        assert result["primary_disabled"] is True
        assert "failover_v1" in result["engine_version"]
        assert result["failover"]["deadline_s"] == 90.0
        assert [leg["configuration"] for leg in result["failover"]["legs"]] == [
            "gpt-6-luna",
            "claude-haiku-5-5",
        ]
        assert all(leg["fingerprint"] for leg in result["failover"]["legs"])
        assert result["cases_run"] == 41
        assert self.admitted == ["claude-haiku-5-5"]
        assert vendor.calls == 27
        assert {a["provider"] for a in result["attempts"]} == {"anthropic"}
        assert {a["number"] for a in result["attempts"]} == {2}
        assert set(result["answered_by"].values()) == {"claude-haiku-5-5"}
        assert len(result["answered_by"]) == 27

    def test_no_cost_stays_unknown(self, monkeypatch, tmp_path):
        _, result, _ = self.measured(
            monkeypatch, tmp_path, ["--engine", deps.FAILOVER, "--primary-disabled"]
        )
        assert result["cost"]["unknown_cost_attempts"] == 0
        assert result["cost"]["complete"] is True
        assert result["cost"]["settled_amount"] > 0
        assert self.book.spent

    def test_the_prompt_reaches_haiku_and_the_record(self, monkeypatch, tmp_path):
        """specs/012-prompt-v4-usage, FR-005: both legs recorded under the prompt."""
        from dataclasses import replace

        from evals.first_calls import fingerprint

        vendor = Scripted("claude-haiku-5-5")
        asked = []
        monkeypatch.setitem(
            engines.HOSTED_ENGINES,
            "claude-haiku-5-5",
            lambda prompt="decision_v3": asked.append(prompt) or vendor,
        )
        target = tmp_path / "composite.json"
        service_run.main(
            [
                "--data-terms",
                "contractual",
                "--json",
                str(target),
                "--engine",
                deps.FAILOVER,
                "--primary-disabled",
                "--prompt",
                "decision_v4",
            ]
        )
        result = json.loads(sorted(tmp_path.glob("composite-*.json"))[-1].read_text("utf-8"))
        assert asked == ["decision_v4"]
        assert result["prompt_version"] == "decision_v4"
        assert result["engine_version"].endswith("/failover_v1/decision_v4")
        for leg in result["failover"]["legs"]:
            config = replace(service_run.configuration(leg["configuration"]), prompt="decision_v4")
            assert leg["fingerprint"] == fingerprint(config)

    async def test_a_disabled_leg_is_a_known_zero(self):
        from app.decision.base import EngineUnavailable

        leg = service_run.DisabledLeg(LUNA)
        with pytest.raises(EngineUnavailable) as raised:
            await leg.leg(None, None, None, "inv", 0.0)
        assert raised.value.failover_eligible is True
        assert raised.value.telemetry.cost_micros == 0
        assert raised.value.telemetry.unknown_cost_attempts == 0
