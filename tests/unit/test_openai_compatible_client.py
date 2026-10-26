"""The OpenAI-compatible client: OpenAI and Scaleway
(specs/007-openai-qwen-engines, T030).

No network: the client is the pinned SDK itself, handed an HTTP transport
that answers in memory, so what is checked is what would go on the wire --
the URL, the headers, the body -- and how the SDK's own responses and
errors are read. Skipped where the `engines` extra is not installed; CI
installs it.
"""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

import json
import logging

import pytest

from app.decision.base import Classification, EngineRejected, EngineUnavailable
from app.decision.hosted import configuration
from app.decision.verdict import verdict_schema
from tests.unit.test_claude_engine import (  # noqa: F401 -- the fixtures are used by name
    VALID_VERDICT,
    context,
    dispute,
    findings,
)

openai = pytest.importorskip("openai", reason="the `engines` extra is not installed")
httpx2 = pytest.importorskip("httpx2")

from app.decision.openai_compatible import (  # noqa: E402
    AmbientSettingRefused,
    OpenAICompatibleEngine,
    ambient_refusal,
    build_client,
)

KEY = "sk-SENTINEL-KEY-0123456789"
SENTINEL = "SENTINEL-vendor-text"
REASONING = "SENTINEL-reasoning-content"
ORG, PROJECT = "org-test", "proj_test"
SCW_ORG, SCW_PROJECT = "11111111-scw-org", "22222222-scw-project"


@pytest.fixture(autouse=True)
def accounts(monkeypatch):
    for variable in ("OPENAI_CUSTOM_HEADERS", "OPENAI_LOG", "OPENAI_BASE_URL"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_ORG_ID", ORG)
    monkeypatch.setenv("OPENAI_PROJECT_ID", PROJECT)
    monkeypatch.setenv("SCALEWAY_API_KEY", KEY)
    monkeypatch.setenv("SCALEWAY_ORGANIZATION_ID", SCW_ORG)
    monkeypatch.setenv("SCALEWAY_PROJECT_ID", SCW_PROJECT)


def completion(
    content=None,
    finish="stop",
    model="gpt-6-luna",
    usage="full",
    refusal=None,
    reasoning_content=None,
):
    if content is None and refusal is None:
        content = json.dumps(VALID_VERDICT)
    message = {"role": "assistant", "content": content, "refusal": refusal}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage == "full":
        body["usage"] = {
            "prompt_tokens": 900,
            "completion_tokens": 140,
            "total_tokens": 1040,
            "prompt_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 50},
            "completion_tokens_details": {"reasoning_tokens": 40},
        }
    elif usage == "totals":
        body["usage"] = {"prompt_tokens": 900, "completion_tokens": 140, "total_tokens": 1040}
    return body


class Wire:
    """An in-memory transport: records each request, answers in turn."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list = []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, httpx2.Response):
            return answer
        return httpx2.Response(200, json=answer)

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


def engine(name, *answers):
    wire = Wire(*answers)
    config = configuration(name)
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(wire))
    return OpenAICompatibleEngine(config, client=build_client(config, http_client=http)), wire


class TestTheRequest:
    async def test_openai_s_as_pinned(self, context, findings, dispute):
        luna, wire = engine("gpt-6-luna", completion())

        await luna.decide(dispute, context, findings)

        request = wire.requests[0]
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        body = wire.body
        assert body["model"] == "gpt-6-luna"
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert body["max_completion_tokens"] == 4096
        assert "max_tokens" not in body
        assert body["reasoning_effort"] == "low"
        assert body["store"] is False
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "verdict", "schema": verdict_schema(), "strict": True},
        }
        assert "temperature" not in body
        assert request.headers["openai-organization"] == ORG
        assert request.headers["openai-project"] == PROJECT

    async def test_scaleway_s_as_pinned(self, context, findings, dispute):
        qwen, wire = engine("qwen3.5-397b-a17b", completion(model="qwen3.5-397b-a17b"))

        await qwen.decide(dispute, context, findings)

        request = wire.requests[0]
        assert str(request.url) == f"https://api.scaleway.ai/{SCW_PROJECT}/v1/chat/completions"
        body = wire.body
        assert body["model"] == "qwen3.5-397b-a17b"
        assert body["max_completion_tokens"] == 4096
        assert body["reasoning_effort"] == "low"
        # Not supported by Scaleway: never sent.
        assert "store" not in body
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "verdict", "schema": verdict_schema()},
        }

    async def test_the_payload_is_the_shared_core_s(self, context, findings, dispute):
        luna, wire = engine("gpt-6-luna", completion())

        await luna.decide(dispute, context, findings)

        system, user, _ = luna.request_texts(dispute, context, findings)
        assert wire.body["messages"] == [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    async def test_the_two_openai_requests_differ_only_by_model(self, context, findings, dispute):
        luna, luna_wire = engine("gpt-6-luna", completion())
        sol, sol_wire = engine("gpt-6.1-sol", completion(model="gpt-6.1-sol"))

        await luna.decide(dispute, context, findings)
        await sol.decide(dispute, context, findings)

        a, b = luna_wire.body, sol_wire.body
        assert a.pop("model") == "gpt-6-luna" and b.pop("model") == "gpt-6.1-sol"
        assert a == b


class TestTheHeadersSent:
    @pytest.mark.parametrize("name", ["gpt-6-luna", "qwen3.5-397b-a17b"])
    async def test_the_configured_key_alone_authorises(self, name, context, findings, dispute):
        client, wire = engine(name, completion(model=name))

        await client.decide(dispute, context, findings)

        assert wire.requests[0].headers["authorization"] == f"Bearer {KEY}"

    async def test_no_openai_account_header_reaches_scaleway(self, context, findings, dispute):
        qwen, wire = engine("qwen3.5-397b-a17b", completion(model="qwen3.5-397b-a17b"))

        await qwen.decide(dispute, context, findings)

        sent = {name.lower() for name in wire.requests[0].headers}
        assert "openai-organization" not in sent
        assert "openai-project" not in sent
        assert ORG not in str(wire.requests[0].headers)

    @pytest.mark.parametrize("variable", ["OPENAI_CUSTOM_HEADERS", "OPENAI_LOG"])
    def test_ambient_sdk_settings_are_refused_before_any_client(self, variable, monkeypatch):
        monkeypatch.setenv(variable, "Authorization: Bearer other\nX-Extra: 1")

        assert variable in ambient_refusal()
        with pytest.raises(AmbientSettingRefused):
            build_client(configuration("gpt-6-luna"))

    def test_the_base_url_ignores_the_environment(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://elsewhere.example")

        client = build_client(configuration("gpt-6-luna"))

        assert str(client.base_url).startswith("https://api.openai.com/v1")

    @pytest.mark.parametrize("missing", ["OPENAI_ORG_ID", "OPENAI_PROJECT_ID"])
    def test_a_missing_openai_account_variable_builds_no_client(self, missing, monkeypatch):
        monkeypatch.delenv(missing)

        with pytest.raises(AmbientSettingRefused, match=missing):
            build_client(configuration("gpt-6-luna"))

    def test_a_missing_scaleway_project_builds_no_client(self, monkeypatch):
        monkeypatch.delenv("SCALEWAY_PROJECT_ID")

        with pytest.raises(AmbientSettingRefused, match="SCALEWAY_PROJECT_ID"):
            build_client(configuration("qwen3.5-397b-a17b"))

    def test_retries_are_off_and_responses_are_validated_strictly(self):
        client = build_client(configuration("gpt-6-luna"))

        assert client.max_retries == 0
        assert client._strict_response_validation is True


class TestTheAnswer:
    async def test_a_valid_answer_with_its_usage_details_and_model(
        self, context, findings, dispute
    ):
        luna, _ = engine("gpt-6-luna", completion())

        answer = await luna._complete("s", "u")
        decision = luna.decision_from(answer, _names(luna, dispute, context, findings), 10)

        assert answer.finish == "stop"
        assert (answer.input_tokens, answer.output_tokens) == (900, 140)
        assert (answer.cached_input_tokens, answer.cache_write_tokens) == (100, 50)
        assert answer.reasoning_tokens == 40
        assert answer.model_version == "gpt-6-luna"
        assert decision.classification is Classification.DUPLICATE_BILLING

    async def test_details_not_reported_are_told_from_zero(self):
        luna, _ = engine("gpt-6-luna", completion(usage="totals"))

        answer = await luna._complete("s", "u")

        assert (answer.input_tokens, answer.output_tokens) == (900, 140)
        assert answer.cached_input_tokens is None
        assert answer.cache_write_tokens is None
        assert answer.reasoning_tokens is None

    async def test_no_usage_is_told_from_zero(self):
        luna, _ = engine("gpt-6-luna", completion(usage=None))

        answer = await luna._complete("s", "u")

        assert answer.input_tokens is None and answer.output_tokens is None

    async def test_reasoning_content_is_dropped_and_never_kept(self):
        qwen, _ = engine(
            "qwen3.5-397b-a17b",
            completion(model="qwen3.5-397b-a17b", reasoning_content=REASONING),
        )

        answer = await qwen._complete("s", "u")

        assert REASONING not in repr(answer)
        assert REASONING not in repr(qwen.notes)

    @pytest.mark.parametrize(
        ("finish", "expected"),
        [("length", "length"), ("content_filter", "filtered"), ("tool_calls", "other")],
    )
    async def test_any_finish_but_stop_is_unusable(self, finish, expected):
        luna, _ = engine("gpt-6-luna", completion(finish=finish))

        answer = await luna._complete("s", "u")

        assert answer.finish == expected

    async def test_a_refusal_is_unusable_and_its_text_is_not_kept(self):
        luna, _ = engine("gpt-6-luna", completion(content=None, refusal=SENTINEL))

        answer = await luna._complete("s", "u")

        assert answer.finish == "filtered"
        assert answer.text is None
        assert SENTINEL not in repr(answer)

    async def test_an_empty_answer_is_unusable(self, context, findings, dispute):
        luna, _ = engine("gpt-6-luna", completion(content=""))

        decision = await luna.decide(dispute, context, findings)

        assert decision.recommended_action.value == "escalate"

    async def test_a_malformed_200_is_unreadable_its_text_not_kept(self):
        luna, _ = engine("gpt-6-luna", httpx2.Response(200, json={"choices": SENTINEL, "model": 3}))

        answer = await luna._complete("s", "u")

        assert answer.finish == "other"
        assert "response_unreadable" in answer.notes
        assert answer.input_tokens is None
        assert SENTINEL not in repr(answer)


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "kind", "category"),
        [
            (429, EngineUnavailable, "rate_limited"),
            (500, EngineUnavailable, "server_error"),
            (503, EngineUnavailable, "server_error"),
            (400, EngineRejected, "400"),
            (401, EngineRejected, "401"),
            (404, EngineRejected, "404"),
        ],
    )
    async def test_by_kind_without_vendor_text(self, status, kind, category):
        error = httpx2.Response(
            status, json={"error": {"message": SENTINEL}}, headers={"retry-after": "7"}
        )
        luna, _ = engine("gpt-6-luna", error)

        with pytest.raises(kind) as raised:
            await luna._complete("s", "u")

        assert str(raised.value) == category
        assert SENTINEL not in str(raised.value)
        assert SENTINEL not in repr(raised.value.args)
        if status == 429:
            assert raised.value.retry_after == 7.0

    async def test_transport_failures_are_outages(self):
        luna, _ = engine("gpt-6-luna", httpx2.ConnectError(SENTINEL))

        with pytest.raises(EngineUnavailable) as raised:
            await luna._complete("s", "u")

        assert str(raised.value) == "transport"

    async def test_a_defect_is_not_an_outage(self, monkeypatch):
        luna, _ = engine("gpt-6-luna", completion())

        async def broken(**_):
            raise ZeroDivisionError

        monkeypatch.setattr(luna._client.chat.completions, "create", broken)
        with pytest.raises(ZeroDivisionError):
            await luna._complete("s", "u")


class TestNothingLeaks:
    async def test_no_key_vendor_text_or_reasoning_in_the_logs(self, caplog):
        caplog.set_level(logging.DEBUG)
        qwen, _ = engine(
            "qwen3.5-397b-a17b",
            completion(model="qwen3.5-397b-a17b", reasoning_content=REASONING),
        )
        await qwen._complete("s", "u")
        rejected, _ = engine("gpt-6-luna", httpx2.Response(400, json={"error": SENTINEL}))
        with pytest.raises(EngineRejected):
            await rejected._complete("s", "u")

        for secret in (KEY, SENTINEL, REASONING):
            assert secret not in caplog.text


class TestThePinnedSdk:
    def test_the_pinned_version(self):
        from importlib.metadata import version

        assert version("openai") == "3.29.0"

    async def test_the_reservation_bounds_the_bytes_plus_its_margins(self):
        luna, _ = engine("gpt-6-luna", completion())

        reserved = await luna.reservation("SYSTÈME", "UTILISATEUR")

        text = "SYSTÈME" + "UTILISATEUR" + json.dumps(verdict_schema())
        assert reserved == len(text.encode("utf-8")) + 512 + 4096


def _names(client, dispute, context, findings):
    return client.request_texts(dispute, context, findings)[2]
