"""Claude Haiku 5.5 on the governed evaluation path
(specs/008-deployment-model-choice, T020).

The service's `ClaudeDecisionEngine` is what would be deployed; the
evaluation adapter must send what it sends and decide what it decides. Both
are driven here by the same stub client. The adapter is then checked on the
pinned SDK itself, with an in-memory HTTP transport: no key, no network, no
spend.
"""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

import json
import logging

import pytest

from app.api import deps
from app.decision.base import EngineRejected, EngineUnavailable
from app.decision.claude import ClaudeDecisionEngine
from app.decision.hosted import CONFIGURATIONS, configuration
from evals import accounts
from tests.unit.test_claude_engine import (  # noqa: F401 -- the fixtures are used by name
    VALID_VERDICT,
    StubClient,
    context,
    dispute,
    findings,
    reply,
)

anthropic = pytest.importorskip("anthropic", reason="the `claude` extra is not installed")
httpx = pytest.importorskip("httpx")

from app.decision.anthropic_eval import (  # noqa: E402
    AmbientSettingRefused,
    AnthropicEvaluationEngine,
    ambient_refusal,
    build_client,
)

KEY = "sk-ant-SENTINEL-KEY-0123456789"
SENTINEL = "SENTINEL-vendor-text"
HAIKU = "claude-haiku-5-5"


@pytest.fixture(autouse=True)
def account(monkeypatch):
    for variable in ("ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_LOG", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("RESOLVE_ANTHROPIC_ORG_ID", "anthropic-org-test")


def adapter(outcome) -> tuple[AnthropicEvaluationEngine, StubClient]:
    client = StubClient(outcome)
    return AnthropicEvaluationEngine(configuration(HAIKU), client=client), client


def service(outcome) -> tuple[ClaudeDecisionEngine, StubClient]:
    client = StubClient(outcome)
    return ClaudeDecisionEngine(client=client, model=HAIKU), client


def answered(body=None, stop_reason="end_turn", model=HAIKU, **usage):
    response = reply(body if body is not None else VALID_VERDICT, stop_reason=stop_reason)
    response.model = model
    for name, value in usage.items():
        setattr(response.usage, name, value)
    return response


class TestParityWithTheServiceEngine:
    async def test_the_request_is_the_service_engine_s(self, context, findings, dispute):
        evaluated, evaluated_client = adapter(answered())
        deployed, deployed_client = service(answered())

        await evaluated.decide(dispute, context, findings)
        await deployed.decide(dispute, context, findings)

        assert evaluated_client.messages.last_request == deployed_client.messages.last_request

    @pytest.mark.parametrize(
        ("body", "stop_reason"),
        [
            (None, "end_turn"),
            (None, "stop_sequence"),
            (None, "pause_turn"),
            ("not json", "end_turn"),
            ({"classification": "not_a_class"}, "end_turn"),
            (None, "refusal"),
            (None, "max_tokens"),
            ("", "end_turn"),
        ],
    )
    async def test_the_decision_is_the_service_engine_s(
        self, body, stop_reason, context, findings, dispute
    ):
        evaluated, _ = adapter(answered(body, stop_reason))
        deployed, _ = service(answered(body, stop_reason))

        a = await evaluated.decide(dispute, context, findings)
        b = await deployed.decide(dispute, context, findings)

        for field in (
            "classification",
            "confidence",
            "confidence_kind",
            "recommended_action",
            "explanation",
            "evidence_refs",
        ):
            assert getattr(a, field) == getattr(b, field), field


class TestTheAnswer:
    async def test_stop_reasons_as_the_service_parser_decides(self):
        for stop_reason, finish in [
            ("end_turn", "stop"),
            ("stop_sequence", "stop"),
            ("tool_use", "stop"),
            ("pause_turn", "stop"),
            ("max_tokens", "length"),
            ("refusal", "filtered"),
        ]:
            evaluated, _ = adapter(answered(stop_reason=stop_reason))
            assert (await evaluated._complete("s", "u")).finish == finish, stop_reason

    async def test_cache_tokens_are_counted_in_the_input_never_twice(self):
        evaluated, _ = adapter(
            answered(
                input_tokens=1000,
                output_tokens=200,
                cache_read_input_tokens=300,
                cache_creation_input_tokens=50,
            )
        )

        answer = await evaluated._complete("s", "u")

        assert answer.input_tokens == 1350
        assert (answer.cached_input_tokens, answer.cache_write_tokens) == (300, 50)
        assert answer.output_tokens == 200

    async def test_thinking_tokens_are_the_reasoning_detail(self):
        from types import SimpleNamespace

        evaluated, _ = adapter(
            answered(output_tokens=200, output_tokens_details=SimpleNamespace(thinking_tokens=80))
        )

        answer = await evaluated._complete("s", "u")

        assert (answer.output_tokens, answer.reasoning_tokens) == (200, 80)

    async def test_the_returned_model_is_recorded(self):
        evaluated, _ = adapter(answered(model="claude-haiku-5-5"))

        assert (await evaluated._complete("s", "u")).model_version == "claude-haiku-5-5"

    async def test_missing_usage_is_told_from_zero(self):
        response = answered()
        response.usage = None
        evaluated, _ = adapter(response)

        answer = await evaluated._complete("s", "u")

        assert answer.input_tokens is None and answer.output_tokens is None


class TestTheConfiguration:
    def test_it_is_closed(self):
        config = CONFIGURATIONS[HAIKU]

        assert config.vendor == "anthropic"
        assert config.developer == "Anthropic"
        assert config.model == HAIKU
        assert config.endpoint == "https://api.anthropic.com"
        assert config.key_variable == "ANTHROPIC_API_KEY"
        assert config.account_variables == ("RESOLVE_ANTHROPIC_ORG_ID",)
        assert config.reasoning == "low"
        assert config.max_output_tokens == 8192
        assert config.currency == "USD"
        assert config.price_per_mtok == (0.10, 0.50)
        assert config.cached_input_price_per_mtok == 0.01
        assert config.cache_write_price_per_mtok == 0.125
        assert config.check_identity is True
        assert config.store is None

    def test_the_service_refuses_it(self, monkeypatch):
        monkeypatch.setenv("DECISION_ENGINE", HAIKU)
        deps.get_engine.cache_clear()

        with pytest.raises(RuntimeError, match="unknown decision engine"):
            deps.get_engine()

        deps.get_engine.cache_clear()

    def test_an_organisation_only_account(self):
        config = configuration(HAIKU)

        assert accounts.identity(config) == {"organisation": "anthropic-org-test"}
        assert accounts.billing_account(config) == "anthropic-anthropic-org-test"

    def test_its_account_variable_is_not_one_the_sdk_reads(self):
        assert "ANTHROPIC_ORGANIZATION_ID" not in configuration(HAIKU).account_variables


# -- on the pinned SDK -------------------------------------------------------


def message_body(model=HAIKU, stop_reason="end_turn", text=None):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text or json.dumps(VALID_VERDICT)}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 900, "output_tokens": 120},
    }


class Wire:
    def __init__(self, answer):
        self.answer = answer
        self.requests: list = []

    def __call__(self, request):
        self.requests.append(request)
        if isinstance(self.answer, BaseException):
            raise self.answer
        if isinstance(self.answer, httpx.Response):
            return self.answer
        return httpx.Response(200, json=self.answer)


def on_the_wire(answer):
    wire = Wire(answer)
    config = configuration(HAIKU)
    http = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    return AnthropicEvaluationEngine(config, client=build_client(config, http_client=http)), wire


class TestOnThePinnedSdk:
    async def test_the_request_on_the_wire(self):
        evaluated, wire = on_the_wire(message_body())

        answer = await evaluated._complete("SYSTEM", "USER")

        request = wire.requests[0]
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == KEY
        body = json.loads(request.content)
        assert body["model"] == HAIKU
        assert body["max_tokens"] == 8192
        assert body["system"] == "SYSTEM"
        assert body["output_config"]["effort"] == "low"
        assert body["messages"] == [{"role": "user", "content": "USER"}]
        assert answer.finish == "stop" and answer.input_tokens == 900

    def test_retries_off_strict_validation_and_the_address_pinned(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.example")

        client = build_client(configuration(HAIKU))

        assert client.max_retries == 0
        assert client._strict_response_validation is True
        assert str(client.base_url).startswith("https://api.anthropic.com")

    @pytest.mark.parametrize("variable", ["ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_LOG"])
    def test_ambient_sdk_settings_are_refused(self, variable, monkeypatch):
        monkeypatch.setenv(variable, "debug")

        assert variable in ambient_refusal()
        with pytest.raises(AmbientSettingRefused):
            build_client(configuration(HAIKU))

    def test_a_missing_account_builds_no_client(self, monkeypatch):
        monkeypatch.delenv("RESOLVE_ANTHROPIC_ORG_ID")

        with pytest.raises(AmbientSettingRefused, match="RESOLVE_ANTHROPIC_ORG_ID"):
            build_client(configuration(HAIKU))

    @pytest.mark.parametrize(
        ("status", "kind", "category"),
        [
            (429, EngineUnavailable, "rate_limited"),
            (529, EngineUnavailable, "server_error"),
            (500, EngineUnavailable, "server_error"),
            (408, EngineUnavailable, "server_error"),
            (409, EngineUnavailable, "server_error"),
            (400, EngineRejected, "400"),
            (401, EngineRejected, "401"),
            (404, EngineRejected, "404"),
        ],
    )
    async def test_errors_by_kind_without_vendor_text(self, status, kind, category):
        evaluated, _ = on_the_wire(
            httpx.Response(
                status,
                json={"type": "error", "error": {"type": "x", "message": SENTINEL}},
                headers={"retry-after": "3"},
            )
        )

        with pytest.raises(kind) as raised:
            await evaluated._complete("s", "u")

        assert str(raised.value) == category
        assert SENTINEL not in repr(raised.value.args)

    async def test_an_unexpected_status_keeps_its_outcome_unknown(self):
        from app.decision.anthropic_eval import _translated

        error = anthropic.APIStatusError(
            SENTINEL,
            response=httpx.Response(302, request=httpx.Request("POST", "https://x")),
            body=None,
        )

        translated = _translated(error)

        assert isinstance(translated, EngineUnavailable)
        assert str(translated) == "server_error"

    async def test_a_transport_failure_is_an_outage(self):
        evaluated, _ = on_the_wire(httpx.ConnectError(SENTINEL))

        with pytest.raises(EngineUnavailable) as raised:
            await evaluated._complete("s", "u")

        assert str(raised.value) == "transport"

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, json={"content": SENTINEL, "model": 3}),
            httpx.Response(
                200,
                content=f"{{not json {SENTINEL}".encode(),
                headers={"content-type": "application/json"},
            ),
        ],
    )
    async def test_an_unreadable_answer_keeps_its_usage_unknown(self, response):
        evaluated, _ = on_the_wire(response)

        answer = await evaluated._complete("s", "u")

        assert answer.finish == "other"
        assert "response_unreadable" in answer.notes
        assert answer.input_tokens is None
        assert SENTINEL not in repr(answer)

    async def test_a_defect_is_not_an_outage(self, monkeypatch):
        evaluated, _ = on_the_wire(message_body())

        async def broken(**_):
            raise ZeroDivisionError

        monkeypatch.setattr(evaluated._client.messages, "create", broken)
        with pytest.raises(ZeroDivisionError):
            await evaluated._complete("s", "u")

    async def test_nothing_leaks_into_logs_or_output(self, caplog, capsys):
        caplog.set_level(logging.DEBUG, logger="anthropic")
        evaluated, _ = on_the_wire(message_body())
        await evaluated._complete("s", "u")
        refused, _ = on_the_wire(httpx.Response(400, json={"error": {"message": SENTINEL}}))
        with pytest.raises(EngineRejected):
            await refused._complete("s", "u")

        out = capsys.readouterr()
        for secret in (KEY, SENTINEL):
            assert secret not in caplog.text
            assert secret not in out.out + out.err

    async def test_the_payload_never_reaches_the_logs(self, caplog):
        """With DEBUG on everywhere, as an application might configure it."""
        import app.decision.hosted as hosted

        caplog.set_level(logging.DEBUG)
        evaluated, _ = on_the_wire(message_body())
        await evaluated._complete(f"SYSTEM {SENTINEL}", f"USER {SENTINEL}")

        assert SENTINEL not in caplog.text

        # A positive control: without the filter, the SDK logs the payload.
        caplog.clear()
        logger = logging.getLogger("anthropic._base_client")
        dropped = [f for f in logger.filters if isinstance(f, hosted._BelowInfoDropped)]
        for f in dropped:
            logger.removeFilter(f)
        try:
            await evaluated._complete(f"SYSTEM {SENTINEL}", f"USER {SENTINEL}")
            assert SENTINEL in caplog.text
        finally:
            for f in dropped:
                logger.addFilter(f)

    async def test_the_api_version_header_is_the_recorded_one(self):
        evaluated, wire = on_the_wire(message_body())

        await evaluated._complete("s", "u")

        assert wire.requests[0].headers["anthropic-version"] == "2023-06-01"
        assert configuration(HAIKU).api_version == "2023-06-01"

    def test_the_pinned_version(self):
        from importlib.metadata import version

        assert version("anthropic") == "0.105.2"
