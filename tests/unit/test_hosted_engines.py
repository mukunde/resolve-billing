"""The shared core of the engines evaluated on free tiers
(specs/004-second-engines, T022): what a vendor receives, what an answer
becomes, and what the configurations allow."""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

import json

import pytest

from app.decision.base import (
    Classification,
    EngineRejected,
    EngineUnavailable,
    RecommendedAction,
)
from app.decision.hosted import (
    CONFIGURATIONS,
    Answer,
    HostedEngine,
    configuration,
)
from tests.unit.test_claude_engine import (  # noqa: F401 -- the fixtures are used by name
    VALID_VERDICT,
    context,
    dispute,
    engine_with,
    findings,
    reply,
)


class Scripted(HostedEngine):
    """A hosted engine whose vendor answers what the test says."""

    def __init__(self, answer: Answer | BaseException, name: str = "gemini-3.8-flash"):
        super().__init__(configuration(name))
        self.answer = answer
        self.sent: list[tuple[str, str]] = []

    async def _complete(self, system: str, user: str) -> Answer:
        self.sent.append((system, user))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def answered(body, finish="stop", **usage) -> Answer:
    text = body if body is None or isinstance(body, str) else json.dumps(body)
    return Answer(
        text=text,
        finish=finish,
        input_tokens=usage.get("input_tokens", 900),
        output_tokens=usage.get("output_tokens", 120),
        model_version=usage.get("model_version", "gemini-3.8-flash-001"),
    )


class TestWhatTheVendorReceives:
    async def test_the_same_payload_as_the_claude_engine(self, context, findings, dispute):
        claude, client = engine_with(reply(VALID_VERDICT))
        await claude.decide(dispute, context, findings)
        engine = Scripted(answered(VALID_VERDICT))

        await engine.decide(dispute, context, findings)

        (system, user) = engine.sent[0]
        request = client.messages.last_request
        assert system == request["system"]
        assert user == request["messages"][0]["content"]

    async def test_no_real_identifier_leaves(self, context, findings, dispute):
        engine = Scripted(answered(VALID_VERDICT))

        await engine.decide(dispute, context, findings)

        (_, user) = engine.sent[0]
        for real in ("inv_001", "inv_002", "cus_acme", "sub_77", "pay_001", "alice@acme.fr"):
            assert real not in user


class TestWhatAnAnswerBecomes:
    async def test_a_valid_verdict_becomes_a_decision_with_real_references(
        self, context, findings, dispute
    ):
        engine = Scripted(answered(VALID_VERDICT))

        decision = await engine.decide(dispute, context, findings)

        assert decision.classification is Classification.DUPLICATE_BILLING
        assert decision.recommended_action is RecommendedAction.PROPOSE_CREDIT_NOTE
        assert decision.confidence_kind == "self_reported"
        assert decision.evidence_refs == ["inv_001", "inv_002"]
        assert decision.telemetry.model_id == "gemini-3.8-flash"
        assert decision.telemetry.prompt_version == "gemini-3.8-flash/decision_v3"
        assert decision.telemetry.cost_micros is None

    async def test_a_reference_never_issued_is_dropped(self, context, findings, dispute):
        engine = Scripted(answered({**VALID_VERDICT, "evidence_refs": ["INVOICE_1", "INVOICE_9"]}))

        decision = await engine.decide(dispute, context, findings)

        assert decision.evidence_refs == ["inv_001"]

    @pytest.mark.parametrize(
        "answer",
        [
            answered("{not json"),
            answered(None),
            answered(""),
            answered(VALID_VERDICT, finish="length"),
            answered(VALID_VERDICT, finish="filtered"),
            answered(VALID_VERDICT, finish="other"),
            answered({**VALID_VERDICT, "classification": "fraud"}),
            answered({**VALID_VERDICT, "confidence": 3}),
        ],
        ids=[
            "not-json",
            "no-text",
            "empty",
            "truncated",
            "filtered",
            "other-finish",
            "out-of-set",
            "out-of-bounds",
        ],
    )
    async def test_an_unusable_answer_goes_to_a_person(self, context, findings, dispute, answer):
        decision = await Scripted(answer).decide(dispute, context, findings)

        assert decision.classification is Classification.UNKNOWN
        assert decision.recommended_action is RecommendedAction.ESCALATE

    @pytest.mark.parametrize("raised", [EngineUnavailable("rate_limited"), EngineRejected("401")])
    async def test_vendor_failures_keep_their_kind(self, context, findings, dispute, raised):
        with pytest.raises(type(raised)):
            await Scripted(raised).decide(dispute, context, findings)

    async def test_what_the_vendor_reported_is_noted(self, context, findings, dispute):
        engine = Scripted(answered(VALID_VERDICT))
        engine.answer = Answer(
            text=json.dumps(VALID_VERDICT),
            finish="stop",
            input_tokens=900,
            output_tokens=120,
            model_version="gemini-3.8-flash-001",
            notes=("temperature ignored",),
        )

        await engine.decide(dispute, context, findings)

        assert engine.notes == ["temperature ignored"]
        assert engine.model_versions == ["gemini-3.8-flash-001"]


class TestTheLimitsAsRead:
    """The limits the maintainer read on 2026-10-24 (spec 004 amendment)."""

    def test_gemini_s_real_limits_and_daily_quota(self):
        config = configuration("gemini-3.8-flash")

        assert config.requests_per_second == pytest.approx(5 / 60)
        assert config.tokens_per_minute == 250_000
        assert config.requests_per_day == 20
        assert config.quota_timezone == "America/Los_Angeles"
        assert config.attempts == 1
        assert config.stop_on_outage is True
        assert "resolve-billing-eval" in config.limits_source
        assert "2026-10-24" in config.limits_source

    @pytest.mark.parametrize("name", ["mistral-large-4", "zai-glm-5-3"])
    def test_mistral_keeps_its_attempts_and_has_no_daily_quota(self, name):
        config = configuration(name)

        assert config.requests_per_day is None
        assert config.quota_timezone is None
        assert config.attempts == 3
        assert config.stop_on_outage is False
        assert "2026-10-24" in config.limits_source

    @pytest.mark.parametrize("name", ["gemini-3.8-flash", "mistral-large-4", "zai-glm-5-3"])
    def test_one_named_estimator_for_all_three(self, name):
        assert configuration(name).reservation_estimator == "utf8-bytes-v1"

    def test_the_estimator_counts_bytes_not_characters(self):
        from app.decision.hosted import estimated_tokens
        from app.decision.verdict import verdict_schema

        text = "é" * 1000 + "日本" * 500  # 2000 characters, 5000 bytes

        estimate = estimated_tokens("", text, margin=512, max_output=4096)

        schema = len(json.dumps(verdict_schema()).encode("utf-8"))
        assert estimate == 5000 + schema + 512 + 4096


class TestTheConfigurations:
    def test_exactly_three_closed_configurations(self):
        assert set(CONFIGURATIONS) == {"gemini-3.8-flash", "mistral-large-4", "zai-glm-5-3"}

    @pytest.mark.parametrize("name", sorted(CONFIGURATIONS))
    def test_each_is_pinned_to_its_vendor_endpoint_and_key(self, name):
        config = configuration(name)

        assert config.model == name
        assert "latest" not in config.model
        assert config.endpoint.startswith("https://")
        assert config.key_variable in {"GEMINI_API_KEY", "MISTRAL_API_KEY"}
        assert config.reservation == "estimated"

    def test_the_vendors_and_their_keys(self):
        assert configuration("gemini-3.8-flash").vendor == "google"
        assert configuration("gemini-3.8-flash").key_variable == "GEMINI_API_KEY"
        for name in ("mistral-large-4", "zai-glm-5-3"):
            assert configuration(name).vendor == "mistral"
            assert configuration(name).key_variable == "MISTRAL_API_KEY"
        assert configuration("zai-glm-5-3").developer == "Z.ai"

    def test_an_unknown_name_is_refused(self):
        with pytest.raises(KeyError):
            configuration("mistral-large-latest")
