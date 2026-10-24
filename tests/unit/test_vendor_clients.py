"""The two vendors' clients (specs/004-second-engines, T040, T042).

No network: each client is handed a stub with the SDK's call shape, and the
responses and errors are the pinned SDKs' own types, built from the shapes
their sources define. Where the SDK is not installed these are skipped; CI
installs it (the `engines` extra).
"""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

import json
from types import SimpleNamespace

import pytest

from app.decision.base import (
    Classification,
    EngineRejected,
    EngineUnavailable,
    RecommendedAction,
)
from app.decision.hosted import configuration
from app.decision.verdict import verdict_schema
from tests.unit.test_claude_engine import (  # noqa: F401 -- the fixtures are used by name
    VALID_VERDICT,
    context,
    dispute,
    findings,
)

genai = pytest.importorskip("google.genai", reason="the `engines` extra is not installed")
mistralai = pytest.importorskip("mistralai.client", reason="the `engines` extra is not installed")

from google.genai import errors as genai_errors  # noqa: E402
from google.genai import types as genai_types  # noqa: E402
from mistralai.client import errors as mistral_errors  # noqa: E402
from mistralai.client import models as mistral_models  # noqa: E402

from app.decision.gemini import GeminiEngine  # noqa: E402
from app.decision.mistral import MistralEngine  # noqa: E402

SENTINEL = "SENTINEL-vendor-text"


# -- Gemini ------------------------------------------------------------------


def gemini_response(text=None, finish="STOP", usage=True, thought=None, candidates=True):
    parts = []
    if thought is not None:
        parts.append({"text": thought, "thought": True})
    if text is not None:
        parts.append({"text": text})
    body = {"model_version": "gemini-3.8-flash-001"}
    if candidates:
        body["candidates"] = [
            {"content": {"role": "model", "parts": parts}, "finish_reason": finish}
        ]
    if usage:
        body["usage_metadata"] = {
            "prompt_token_count": 900,
            "candidates_token_count": 100,
            "thoughts_token_count": 40,
            "total_token_count": 1040,
        }
    return genai_types.GenerateContentResponse.model_validate(body)


class GeminiModels:
    def __init__(self, outcome, counted=850):
        self.outcome = outcome
        self.counted = counted
        self.generated: list[dict] = []
        self.counts: list[dict] = []

    async def generate_content(self, *, model, contents, config=None):
        self.generated.append({"model": model, "contents": contents, "config": config})
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def count_tokens(self, *, model, contents, config=None):
        self.counts.append({"model": model, "contents": contents, "config": config})
        return genai_types.CountTokensResponse(total_tokens=self.counted)


def gemini(outcome, counted=850):
    models = GeminiModels(outcome, counted)
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    return GeminiEngine(configuration("gemini-3.8-flash"), client=client), models


class TestGemini:
    async def test_the_request_as_pinned(self, context, findings, dispute):
        engine, models = gemini(gemini_response(json.dumps(VALID_VERDICT)))

        await engine.decide(dispute, context, findings)

        (sent,) = models.generated
        config = sent["config"]
        assert sent["model"] == "gemini-3.8-flash"
        assert isinstance(config, genai_types.GenerateContentConfig)
        assert config.response_mime_type == "application/json"
        assert config.response_json_schema == verdict_schema()
        assert config.max_output_tokens == 4096
        assert config.thinking_config.thinking_level == genai_types.ThinkingLevel.LOW
        assert config.temperature is None
        assert config.http_options.retry_options.attempts == 1
        assert config.system_instruction.startswith("You classify billing disputes")
        assert "<customer_message>" in sent["contents"]

    async def test_a_valid_answer_with_its_usage_and_version(self, context, findings, dispute):
        engine, _ = gemini(gemini_response(json.dumps(VALID_VERDICT), thought="reasoning"))

        decision = await engine.decide(dispute, context, findings)

        assert decision.classification is Classification.DUPLICATE_BILLING
        assert decision.telemetry.input_tokens == 900
        assert decision.telemetry.output_tokens == 140  # answer and thoughts
        assert engine.model_versions == ["gemini-3.8-flash-001"]

    @pytest.mark.parametrize(
        "response",
        [
            gemini_response(json.dumps(VALID_VERDICT), finish="MAX_TOKENS"),
            gemini_response(json.dumps(VALID_VERDICT), finish="SAFETY"),
            gemini_response(json.dumps(VALID_VERDICT), finish="PROHIBITED_CONTENT"),
            gemini_response(None),
            gemini_response(None, thought=json.dumps(VALID_VERDICT)),
            gemini_response(candidates=False),
        ],
        ids=["truncated", "safety", "prohibited", "empty", "only-a-thought", "no-candidate"],
    )
    async def test_an_unusable_answer_goes_to_a_person(self, context, findings, dispute, response):
        engine, _ = gemini(response)

        decision = await engine.decide(dispute, context, findings)

        assert decision.recommended_action is RecommendedAction.ESCALATE

    async def test_no_usage_reported_is_said_so(self, context, findings, dispute):
        engine, _ = gemini(gemini_response(json.dumps(VALID_VERDICT), usage=False))

        await engine.decide(dispute, context, findings)

        assert engine.usage_missing == 1

    @pytest.mark.parametrize(
        ("error", "kind", "category"),
        [
            (
                genai_errors.ClientError(429, {"error": {"message": SENTINEL}}),
                EngineUnavailable,
                "rate_limited",
            ),
            (
                genai_errors.ClientError(401, {"error": {"message": SENTINEL}}),
                EngineRejected,
                "401",
            ),
            (
                genai_errors.ClientError(403, {"error": {"message": SENTINEL}}),
                EngineRejected,
                "403",
            ),
            (
                genai_errors.ClientError(400, {"error": {"message": SENTINEL}}),
                EngineRejected,
                "400",
            ),
            (
                genai_errors.ServerError(503, {"error": {"message": SENTINEL}}),
                EngineUnavailable,
                "server_error",
            ),
            (TimeoutError(SENTINEL), EngineUnavailable, "transport"),
        ],
        ids=["429", "401", "403", "400", "503", "timeout"],
    )
    async def test_errors_by_kind_without_vendor_text(
        self, context, findings, dispute, error, kind, category
    ):
        engine, _ = gemini(error)

        with pytest.raises(kind) as raised:
            await engine.decide(dispute, context, findings)

        assert str(raised.value) == category
        assert SENTINEL not in str(raised.value)
        assert raised.value.__suppress_context__

    async def test_the_reservation_is_estimated_locally_never_counted_by_google(self):
        engine, models = gemini(gemini_response(json.dumps(VALID_VERDICT)), counted=850)

        reserved = await engine.reservation("SYSTEM", "USÉR")

        assert models.counts == []
        material = "SYSTEM" + "USÉR" + json.dumps(verdict_schema())
        assert reserved == len(material.encode("utf-8")) + 512 + 4096
        assert engine.counts_remotely is False


# -- Mistral -----------------------------------------------------------------


def mistral_response(content, finish="stop", usage=True, model="mistral-large-4"):
    body = {
        "id": "cmpl-1",
        "object": "chat.completion",
        "model": model,
        "created": 1,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish,
            }
        ],
    }
    # The SDK requires a `usage` object; one that carries no counts is how an
    # absent usage arrives, its fields then defaulted to 0 by the SDK.
    body["usage"] = (
        {"prompt_tokens": 900, "completion_tokens": 140, "total_tokens": 1040} if usage else {}
    )
    return mistral_models.ChatCompletionResponse.model_validate(body)


def thinking(text):
    return {"type": "thinking", "thinking": [{"type": "text", "text": text}]}


def answer_chunk(text):
    return {"type": "text", "text": text}


class MistralChat:
    def __init__(self, outcome):
        self.outcome = outcome
        self.sent: list[dict] = []

    async def complete_async(self, **kwargs):
        self.sent.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def mistral(name, outcome):
    chat = MistralChat(outcome)
    client = SimpleNamespace(chat=chat)
    return MistralEngine(configuration(name), client=client), chat


def http_error(status):
    import httpx2

    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.mistral.ai"))
    return mistral_errors.SDKError(SENTINEL, response, SENTINEL)


class TestMistral:
    async def test_the_large_4_request_as_pinned(self, context, findings, dispute):
        engine, chat = mistral("mistral-large-4", mistral_response(json.dumps(VALID_VERDICT)))

        await engine.decide(dispute, context, findings)

        (sent,) = chat.sent
        assert sent["model"] == "mistral-large-4"
        assert sent["max_tokens"] == 4096
        assert sent["reasoning_effort"] == "none"
        assert sent["retries"] is None
        assert "temperature" not in sent
        assert sent["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "verdict", "schema": verdict_schema(), "strict": True},
        }
        assert [m["role"] for m in sent["messages"]] == ["system", "user"]

    async def test_the_glm_request_reasons_at_the_lowest_level(self, context, findings, dispute):
        response = mistral_response(
            [thinking("hmm"), answer_chunk(json.dumps(VALID_VERDICT))], model="zai-glm-5-3"
        )
        engine, chat = mistral("zai-glm-5-3", response)

        await engine.decide(dispute, context, findings)

        assert chat.sent[0]["reasoning_effort"] == "low"

    async def test_glm_blocks_only_the_final_text_is_read(self, context, findings, dispute):
        response = mistral_response(
            [
                thinking(json.dumps({**VALID_VERDICT, "classification": "unknown"})),
                answer_chunk(json.dumps(VALID_VERDICT)),
            ],
            model="zai-glm-5-3",
        )
        engine, _ = mistral("zai-glm-5-3", response)

        decision = await engine.decide(dispute, context, findings)

        assert decision.classification is Classification.DUPLICATE_BILLING
        assert "hmm" not in repr(engine.__dict__)

    async def test_glm_without_a_final_text_is_invalid(self, context, findings, dispute):
        response = mistral_response([thinking(json.dumps(VALID_VERDICT))], model="zai-glm-5-3")
        engine, _ = mistral("zai-glm-5-3", response)

        decision = await engine.decide(dispute, context, findings)

        assert decision.recommended_action is RecommendedAction.ESCALATE

    @pytest.mark.parametrize("finish", ["length", "model_length", "error", "tool_calls"])
    async def test_any_finish_but_stop_is_unusable(self, context, findings, dispute, finish):
        engine, _ = mistral(
            "mistral-large-4", mistral_response(json.dumps(VALID_VERDICT), finish=finish)
        )

        decision = await engine.decide(dispute, context, findings)

        assert decision.recommended_action is RecommendedAction.ESCALATE

    async def test_usage_absent_is_told_from_zero(self, context, findings, dispute):
        engine, _ = mistral(
            "mistral-large-4", mistral_response(json.dumps(VALID_VERDICT), usage=False)
        )

        decision = await engine.decide(dispute, context, findings)

        assert engine.usage_missing == 1
        assert decision.telemetry.input_tokens == 0

    @pytest.mark.parametrize(
        ("error", "kind", "category"),
        [
            (http_error(429), EngineUnavailable, "rate_limited"),
            (http_error(401), EngineRejected, "401"),
            (http_error(403), EngineRejected, "403"),
            (http_error(422), EngineRejected, "422"),
            (http_error(503), EngineUnavailable, "server_error"),
            (mistral_errors.NoResponseError(SENTINEL), EngineUnavailable, "transport"),
            (TimeoutError(SENTINEL), EngineUnavailable, "transport"),
        ],
        ids=["429", "401", "403", "422", "503", "no-response", "timeout"],
    )
    async def test_errors_by_kind_without_vendor_text(
        self, context, findings, dispute, error, kind, category
    ):
        engine, _ = mistral("mistral-large-4", error)

        with pytest.raises(kind) as raised:
            await engine.decide(dispute, context, findings)

        assert str(raised.value) == category
        assert SENTINEL not in str(raised.value)

    async def test_the_reservation_bounds_the_bytes_plus_its_margins(self):
        engine, _ = mistral("mistral-large-4", mistral_response(json.dumps(VALID_VERDICT)))

        reserved = await engine.reservation("SYSTÈME", "UTILISATEUR")

        text = "SYSTÈME" + "UTILISATEUR" + json.dumps(verdict_schema())
        assert reserved == len(text.encode("utf-8")) + 512 + 4096


class TestAgainstThePinnedSdks:
    """The stubs above stand for the SDKs; these hold them to the real
    signatures, so a stub cannot drift from what the client really calls."""

    def test_gemini_methods_take_what_the_client_passes(self):
        import inspect

        from google.genai.models import AsyncModels

        for method in (AsyncModels.generate_content,):
            parameters = inspect.signature(method).parameters
            assert {"model", "contents", "config"} <= set(parameters), method.__name__

    async def test_mistral_completion_takes_what_the_client_passes(
        self, context, findings, dispute
    ):
        import inspect

        from mistralai.client.chat import Chat

        engine, chat = mistral("zai-glm-5-3", mistral_response([answer_chunk("{}")]))
        await engine.decide(dispute, context, findings)

        parameters = set(inspect.signature(Chat.complete_async).parameters)
        assert set(chat.sent[0]) <= parameters

    def test_the_pinned_versions(self):
        from importlib.metadata import version

        assert version("google-genai") == "2.29.0"
        assert version("mistralai") == "3.2.0"


# -- After review r160 -----------------------------------------------------------


class TestPinnedAndHonest:
    def test_gemini_s_address_and_backend_ignore_the_environment(self, monkeypatch):
        from app.decision.gemini import _default_client

        monkeypatch.setenv("GEMINI_API_KEY", "set-by-the-test")
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://elsewhere.example")
        monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "a-project")
        monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

        api = _default_client(configuration("gemini-3.8-flash"))._api_client

        assert api.vertexai is False
        assert api._http_options.base_url == "https://generativelanguage.googleapis.com"
        assert api._http_options.api_version == "v1beta"

    def test_mistral_s_address_is_pinned(self, monkeypatch):
        from app.decision.mistral import _default_client

        monkeypatch.setenv("MISTRAL_API_KEY", "set-by-the-test")

        client = _default_client(configuration("mistral-large-4"))

        assert client.sdk_configuration.get_server_details()[0] == "https://api.mistral.ai"

    async def test_a_gemini_decision_sends_no_counting_request(self, context, findings, dispute):
        engine, models = gemini(gemini_response(json.dumps(VALID_VERDICT)))

        await engine.reservation(*engine.request_texts(dispute, context, findings)[:2])
        await engine.decide(dispute, context, findings)

        assert models.counts == []
        assert len(models.generated) == 1

    @pytest.mark.parametrize("defect", [TypeError("a bug"), AttributeError("a bug")])
    async def test_a_defect_in_the_gemini_call_is_not_an_outage(
        self, context, findings, dispute, defect
    ):
        engine, _ = gemini(defect)

        with pytest.raises(type(defect)):
            await engine.decide(dispute, context, findings)

    @pytest.mark.parametrize("defect", [TypeError("a bug"), AttributeError("a bug")])
    async def test_a_defect_in_the_mistral_call_is_not_an_outage(
        self, context, findings, dispute, defect
    ):
        engine, _ = mistral("mistral-large-4", defect)

        with pytest.raises(type(defect)):
            await engine.decide(dispute, context, findings)

    async def test_mistral_s_own_transport_errors_are_outages(self, context, findings, dispute):
        import httpx2

        engine, _ = mistral("mistral-large-4", httpx2.ConnectError(SENTINEL))

        with pytest.raises(EngineUnavailable, match="^transport$"):
            await engine.decide(dispute, context, findings)

    async def test_gemini_s_transport_errors_are_outages(self, context, findings, dispute):
        import httpx

        engine, _ = gemini(httpx.ConnectError(SENTINEL))

        with pytest.raises(EngineUnavailable, match="^transport$"):
            await engine.decide(dispute, context, findings)

    @pytest.mark.parametrize(
        ("usage", "expected"),
        [
            ({"prompt_token_count": 900, "total_token_count": 1040}, (900, 140)),
            ({"prompt_token_count": 900, "candidates_token_count": 100}, (900, 100)),
            ({"prompt_token_count": 900}, (900, None)),
            ({"prompt_token_count": 900, "thoughts_token_count": 40}, (900, None)),
        ],
        ids=["total", "candidates-only", "prompt-only", "thoughts-only"],
    )
    def test_gemini_never_takes_an_unreported_count_as_zero(self, usage, expected):
        from app.decision.gemini import _answer

        response = gemini_response(json.dumps(VALID_VERDICT), usage=False)
        response.usage_metadata = genai_types.GenerateContentResponseUsageMetadata(**usage)

        answer = _answer(response)

        assert (answer.input_tokens, answer.output_tokens) == expected
