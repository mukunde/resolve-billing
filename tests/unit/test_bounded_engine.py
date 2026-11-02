"""`BoundedEngine`: an evaluated engine under the service's bounds
(specs/009-luna-service, FR-002 to FR-008).

No network: a fake inner engine, or the pinned SDK on an in-memory transport.
"""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

import asyncio
import json

import pytest

from app.decision.base import (
    AttemptOutcome,
    Classification,
    EngineMisidentified,
    EngineRefused,
    EngineRejected,
    EngineUnavailable,
)
from app.decision.bounded import BoundedEngine, Bounds
from app.decision.hosted import Answer, configuration
from app.decision.limits import Limiter
from app.decision.verdict import verdict_schema
from tests.unit.test_claude_engine import (  # noqa: F401 -- the fixtures are used by name
    VALID_VERDICT,
    context,
    dispute,
    findings,
)

LUNA = configuration("gpt-6-luna")


def answer(model="gpt-6-luna", text=None, finish="stop", tokens=(100, 50)):
    return Answer(
        json.dumps(VALID_VERDICT) if text is None else text,
        finish,
        tokens[0] if tokens else None,
        tokens[1] if tokens else None,
        model,
    )


def outage(category, retry_after=None):
    error = EngineUnavailable(category)
    error.retry_after = retry_after
    return error


class Book:
    def __init__(self):
        self.starts, self.ends = [], []

    def started(self, attempt):
        self.starts.append(attempt)

    def finished(self, attempt):
        self.ends.append(attempt)

    @property
    def outcomes(self):
        return [end.outcome for end in self.ends]


class FakeEngine:
    """The surface of a `HostedEngine` that `BoundedEngine` reads."""

    def __init__(self, *script):
        from app.decision.hosted import HostedEngine

        self._real = HostedEngine(LUNA)
        self.config = LUNA
        self.name = LUNA.name
        self.version = LUNA.prompt_version
        self.invoice_references = None
        self.notes, self.model_versions = [], []
        self.script = list(script)
        self.sent = []
        self.closed = False

    def request_texts(self, dispute, context, findings):
        return self._real.request_texts(dispute, context, findings)

    def decision_from(self, answer, names, latency_ms):
        return self._real.decision_from(answer, names, latency_ms)

    async def reservation(self, system, user):
        return 1000

    async def complete(self, system, user):
        self.sent.append((system, user))
        step = self.script.pop(0) if self.script else answer()
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return await step()
        return step

    async def aclose(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def bounded(engine, book=None, bounds=Bounds(), clock=None):  # noqa: B008
    book = book if book is not None else Book()
    extra = {"clock": clock, "sleep": clock.sleep} if clock else {}
    return BoundedEngine(engine, book, bounds=bounds, **extra), book


class TestTheBounds:
    def test_they_are_the_specifications(self):
        bounds = Bounds()
        assert (bounds.attempts, bounds.per_attempt, bounds.deadline, bounds.in_flight) == (
            3,
            60.0,
            90.0,
            4,
        )
        assert bounds.backoff == (2.0, 4.0)

    def test_the_outcomes_are_closed(self):
        assert {o.value for o in AttemptOutcome} == {
            "answered",
            "not_sent",
            "unusable",
            "wrong_model",
            "timeout",
            "unavailable",
            "rate_limited",
            "rejected",
            "cancelled",
            "failed",
        }

    def test_a_vendor_priced_in_euros_is_refused(self):
        engine = FakeEngine()
        engine.config = configuration("qwen3.5-397b-a17b")
        with pytest.raises(ValueError, match="dollars"):
            BoundedEngine(engine, Book())


class TestRetries:
    @pytest.mark.parametrize(
        ("error", "outcome"),
        [
            (outage("transport"), AttemptOutcome.UNAVAILABLE),
            (outage("server_error"), AttemptOutcome.UNAVAILABLE),
            (outage("rate_limited"), AttemptOutcome.RATE_LIMITED),
        ],
    )
    async def test_an_outage_is_retried(self, error, outcome, dispute, context, findings):
        clock = Clock()
        engine = FakeEngine(error, answer())
        wrapper, book = bounded(engine, clock=clock)
        decision = await wrapper.decide(dispute, context, findings)
        assert decision.classification == Classification(VALID_VERDICT["classification"])
        assert book.outcomes == [outcome, AttemptOutcome.ANSWERED]
        assert clock.slept == [2.0]

    async def test_exhausted_it_is_the_outage_path(self, dispute, context, findings):
        clock = Clock()
        engine = FakeEngine(outage("transport"), outage("transport"), outage("transport"))
        wrapper, book = bounded(engine, clock=clock)
        with pytest.raises(EngineUnavailable) as raised:
            await wrapper.decide(dispute, context, findings)
        assert len(engine.sent) == 3
        assert clock.slept == [2.0, 4.0]
        assert raised.value.invocation_id == book.starts[0].invocation_id

    async def test_a_refusal_is_not_retried(self, dispute, context, findings):
        engine = FakeEngine(EngineRejected("401"))
        wrapper, book = bounded(engine)
        with pytest.raises(EngineRefused) as raised:
            await wrapper.decide(dispute, context, findings)
        assert str(raised.value) == "401"
        assert len(engine.sent) == 1
        assert book.outcomes == [AttemptOutcome.REJECTED]

    async def test_a_wrong_model_is_not_retried_and_its_answer_is_dropped(
        self, dispute, context, findings
    ):
        engine = FakeEngine(answer(model="gpt-6-other"))
        wrapper, book = bounded(engine)
        with pytest.raises(EngineMisidentified) as raised:
            await wrapper.decide(dispute, context, findings)
        assert len(engine.sent) == 1
        assert book.outcomes == [AttemptOutcome.WRONG_MODEL]
        assert book.ends[0].returned_model == "gpt-6-other"
        assert book.ends[0].cost_micros is not None
        assert raised.value.invocation_id == book.starts[0].invocation_id

    async def test_a_wrong_model_as_free_text_is_not_recorded(self, dispute, context, findings):
        engine = FakeEngine(answer(model="not a model, a sentence"))
        wrapper, book = bounded(engine)
        with pytest.raises(EngineMisidentified):
            await wrapper.decide(dispute, context, findings)
        assert book.ends[0].returned_model is None

    @pytest.mark.parametrize(
        "unusable",
        [answer(text="not json"), answer(finish="length"), answer(finish="filtered")],
    )
    async def test_an_unusable_answer_escalates_once(self, unusable, dispute, context, findings):
        engine = FakeEngine(unusable)
        wrapper, book = bounded(engine)
        decision = await wrapper.decide(dispute, context, findings)
        assert decision.recommended_action.value == "escalate"
        assert len(engine.sent) == 1
        assert book.outcomes == [AttemptOutcome.UNUSABLE]

    async def test_a_defect_stays_a_defect_on_record(self, dispute, context, findings):
        engine = FakeEngine(RuntimeError("a bug, with SENTINEL text"))
        wrapper, book = bounded(engine)
        with pytest.raises(RuntimeError):
            await wrapper.decide(dispute, context, findings)
        assert book.outcomes == [AttemptOutcome.FAILED]

    async def test_every_attempt_sends_the_same_bytes(self, dispute, context, findings):
        clock = Clock()
        engine = FakeEngine(outage("transport"), outage("transport"), answer())
        wrapper, _ = bounded(engine, clock=clock)
        await wrapper.decide(dispute, context, findings)
        assert len(set(engine.sent)) == 1

    async def test_nothing_goes_to_another_engine(self, dispute, context, findings):
        clock = Clock()
        engine = FakeEngine(outage("transport"), outage("transport"), outage("transport"))
        wrapper, book = bounded(engine, clock=clock)
        with pytest.raises(EngineUnavailable):
            await wrapper.decide(dispute, context, findings)
        assert {start.provider for start in book.starts} == {"openai"}
        assert {start.requested_model for start in book.starts} == {"gpt-6-luna"}


class TestTime:
    async def test_retry_after_is_honoured(self, dispute, context, findings):
        clock = Clock()
        engine = FakeEngine(outage("rate_limited", retry_after=7.0), answer())
        wrapper, _ = bounded(engine, clock=clock)
        await wrapper.decide(dispute, context, findings)
        assert clock.slept == [7.0]

    async def test_a_zero_retry_after_is_honoured_too(self, dispute, context, findings):
        clock = Clock()
        engine = FakeEngine(outage("rate_limited", retry_after=0.0), answer())
        wrapper, _ = bounded(engine, clock=clock)
        await wrapper.decide(dispute, context, findings)
        assert clock.slept[0] == 0.0

    async def test_retry_after_past_the_time_left_stops_at_once(self, dispute, context, findings):
        clock = Clock()
        engine = FakeEngine(outage("rate_limited", retry_after=120.0), answer())
        wrapper, _ = bounded(engine, clock=clock)
        with pytest.raises(EngineUnavailable):
            await wrapper.decide(dispute, context, findings)
        assert clock.slept == []
        assert len(engine.sent) == 1

    async def test_a_stalling_vendor_stops_at_the_attempts_time(self, dispute, context, findings):
        async def stall():
            await asyncio.sleep(30)

        engine = FakeEngine(stall, stall, stall)
        bounds = Bounds(per_attempt=0.01, deadline=5.0, backoff=(0.0, 0.0))
        wrapper, book = bounded(engine, bounds=bounds)
        with pytest.raises(EngineUnavailable) as raised:
            await wrapper.decide(dispute, context, findings)
        assert str(raised.value) == "timeout"
        assert book.outcomes == [AttemptOutcome.TIMEOUT] * 3

    async def test_a_stall_stops_at_the_deadline_whatever_the_attempt_allows(
        self, dispute, context, findings
    ):
        async def stall():
            await asyncio.sleep(30)

        engine = FakeEngine(stall)
        bounds = Bounds(per_attempt=30.0, deadline=0.02)
        wrapper, _ = bounded(engine, bounds=bounds)
        started = asyncio.get_running_loop().time()
        with pytest.raises(EngineUnavailable):
            await wrapper.decide(dispute, context, findings)
        assert asyncio.get_running_loop().time() - started < 1.0

    async def test_the_limiter_counts_against_the_deadline(self, dispute, context, findings):
        clock = Clock()
        engine = FakeEngine()
        wrapper, book = bounded(engine, bounds=Bounds(deadline=30.0), clock=clock)
        wrapper._limiter = Limiter(1500, 100.0, clock=clock, sleep=clock.sleep)
        await wrapper.decide(dispute, context, findings)
        with pytest.raises(EngineUnavailable):
            await wrapper.decide(dispute, context, findings)
        # The second never left: nothing is on record for it.
        assert len(book.starts) == 1

    async def test_cancellation_propagates_on_record(self, dispute, context, findings):
        async def stall():
            await asyncio.sleep(30)

        engine = FakeEngine(stall)
        wrapper, book = bounded(engine)
        task = asyncio.create_task(wrapper.decide(dispute, context, findings))
        while not engine.sent:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert book.outcomes == [AttemptOutcome.CANCELLED]
        assert wrapper._slots.locked() is False


class TestTheDeadlineBoundsTheStage:
    async def test_a_stalled_reservation_stops_at_the_deadline(self, dispute, context, findings):
        engine = FakeEngine()

        async def stall(system, user):
            await asyncio.sleep(30)

        engine.reservation = stall
        wrapper, book = bounded(engine, bounds=Bounds(deadline=0.02))
        with pytest.raises(EngineUnavailable) as raised:
            await wrapper.decide(dispute, context, findings)
        assert str(raised.value) == "deadline"
        assert engine.sent == []
        assert raised.value.invocation_id
        assert book.starts == []

    async def test_a_sleep_that_overshoots_sends_nothing_more(self, dispute, context, findings):
        clock = Clock()

        async def overshoot(seconds):
            clock.now += 500.0

        engine = FakeEngine(outage("transport"), answer())
        wrapper = BoundedEngine(engine, Book(), clock=clock, sleep=overshoot)
        with pytest.raises(EngineUnavailable):
            await wrapper.decide(dispute, context, findings)
        assert len(engine.sent) == 1

    async def test_a_stalled_sleep_stops_at_the_deadline(self, dispute, context, findings):
        async def stall(seconds):
            await asyncio.sleep(30)

        engine = FakeEngine(outage("transport"), answer())
        wrapper = BoundedEngine(engine, Book(), bounds=Bounds(deadline=0.05), sleep=stall)
        with pytest.raises(EngineUnavailable):
            await wrapper.decide(dispute, context, findings)
        assert len(engine.sent) == 1

    async def test_a_stalled_limiter_stops_at_the_deadline(self, dispute, context, findings):
        engine = FakeEngine()
        wrapper, _ = bounded(engine, bounds=Bounds(deadline=0.02))

        async def stall(tokens, remaining=None):
            await asyncio.sleep(30)

        wrapper._limiter.acquire = stall
        with pytest.raises(EngineUnavailable):
            await wrapper.decide(dispute, context, findings)
        assert engine.sent == []
        assert wrapper._slots.locked() is False

    async def test_a_full_queue_stops_at_the_deadline(self, dispute, context, findings):
        engine = FakeEngine()
        wrapper, _ = bounded(engine, bounds=Bounds(deadline=0.02, in_flight=1))
        await wrapper._slots.acquire()
        with pytest.raises(EngineUnavailable):
            await wrapper.decide(dispute, context, findings)
        assert engine.sent == []

    async def test_a_slow_record_sends_nothing_past_the_deadline(self, dispute, context, findings):
        clock = Clock()

        class Slow(Book):
            def started(self, attempt):
                super().started(attempt)
                clock.now += 500.0

        engine = FakeEngine()
        book = Slow()
        wrapper = BoundedEngine(engine, book, clock=clock, sleep=clock.sleep)
        with pytest.raises(EngineUnavailable) as raised:
            await wrapper.decide(dispute, context, findings)
        assert str(raised.value) == "deadline"
        assert engine.sent == []
        assert book.outcomes == [AttemptOutcome.NOT_SENT]
        assert book.ends[0].cost_micros == 0


class TestAnUnreadableResponse:
    async def test_it_is_unusable_not_misidentified(self, dispute, context, findings):
        unreadable = Answer(None, "other", None, None, None, ("response_unreadable",))
        wrapper, book = bounded(FakeEngine(unreadable))
        decision = await wrapper.decide(dispute, context, findings)
        assert decision.recommended_action.value == "escalate"
        assert book.outcomes == [AttemptOutcome.UNUSABLE]
        assert book.ends[0].cost_micros is None

    async def test_a_usable_answer_naming_no_model_is_refused(self, dispute, context, findings):
        wrapper, book = bounded(FakeEngine(answer(model=None)))
        with pytest.raises(EngineMisidentified):
            await wrapper.decide(dispute, context, findings)
        assert book.outcomes == [AttemptOutcome.WRONG_MODEL]

    async def test_an_unusable_answer_from_another_model_is_misidentified(
        self, dispute, context, findings
    ):
        wrapper, book = bounded(FakeEngine(answer(model="gpt-6-other", finish="length")))
        with pytest.raises(EngineMisidentified):
            await wrapper.decide(dispute, context, findings)
        assert book.outcomes == [AttemptOutcome.WRONG_MODEL]


class TestConcurrency:
    async def test_in_flight_stays_bounded(self, dispute, context, findings):
        flying = peak = 0

        async def slow():
            nonlocal flying, peak
            flying += 1
            peak = max(peak, flying)
            await asyncio.sleep(0.01)
            flying -= 1
            return answer()

        engine = FakeEngine(*[slow] * 9)
        config = LUNA
        wrapper, _ = bounded(engine)
        wrapper._limiter = Limiter(config.tokens_per_minute, 100_000.0)
        await asyncio.gather(*[wrapper.decide(dispute, context, findings) for _ in range(9)])
        assert peak == 4

    async def test_both_limits_hold_on_concurrent_calls(self, dispute, context, findings):
        clock = Clock()
        engine = FakeEngine()
        wrapper, _ = bounded(engine, clock=clock)
        wrapper._limiter = Limiter(2000, 0.5, clock=clock, sleep=clock.sleep)
        spaced = []
        original = engine.complete

        async def complete(system, user):
            spaced.append(clock.now)
            return await original(system, user)

        engine.complete = complete
        await asyncio.gather(*[wrapper.decide(dispute, context, findings) for _ in range(2)])
        assert spaced[1] - spaced[0] >= 2.0

    async def test_attempts_of_two_calls_stay_apart(self, dispute, context, findings):
        clock = Clock()
        engine = FakeEngine(outage("transport"), answer(), answer())
        wrapper, book = bounded(engine, clock=clock)
        wrapper._limiter = Limiter(10_000_000, 100_000.0, clock=clock, sleep=clock.sleep)
        first, second = await asyncio.gather(
            wrapper.decide(dispute, context, findings),
            wrapper.decide(dispute, context, findings),
        )
        assert first.telemetry.invocation_id != second.telemetry.invocation_id
        groups = {start.invocation_id for start in book.starts}
        assert groups == {first.telemetry.invocation_id, second.telemetry.invocation_id}
        assert {start.dispute_id for start in book.starts} == {dispute.id}


class TestTelemetry:
    async def test_it_names_the_model_that_answered(self, dispute, context, findings):
        wrapper, _ = bounded(FakeEngine(answer()))
        decision = await wrapper.decide(dispute, context, findings)
        telemetry = decision.telemetry
        assert telemetry.model_id == "gpt-6-luna"
        assert telemetry.usage_known is True
        assert telemetry.unknown_cost_attempts == 0
        assert telemetry.cost_micros > 0
        assert decision.confidence_kind == "self_reported"

    async def test_a_success_after_an_unknown_cost_stays_a_lower_bound(
        self, dispute, context, findings
    ):
        clock = Clock()
        wrapper, _ = bounded(FakeEngine(outage("transport"), answer()), clock=clock)
        telemetry = (await wrapper.decide(dispute, context, findings)).telemetry
        assert telemetry.unknown_cost_attempts == 1
        assert telemetry.cost_micros > 0

    async def test_an_answer_without_usage_is_unknown_not_zero(self, dispute, context, findings):
        wrapper, book = bounded(FakeEngine(answer(tokens=None)))
        telemetry = (await wrapper.decide(dispute, context, findings)).telemetry
        assert telemetry.usage_known is False
        assert telemetry.cost_micros is None
        assert telemetry.unknown_cost_attempts == 1
        assert book.ends[0].input_tokens is None
        assert book.ends[0].cost_micros is None

    async def test_a_service_keeps_no_growing_lists(self, dispute, context, findings):
        engine = FakeEngine(answer())
        engine._real.notes = engine.notes
        engine._real.model_versions = engine.model_versions
        wrapper, _ = bounded(engine)
        await wrapper.decide(dispute, context, findings)
        assert engine.notes == []
        assert engine.model_versions == []


class TestRecording:
    async def test_a_recorder_that_fails_sends_nothing(self, dispute, context, findings):
        class Broken(Book):
            def started(self, attempt):
                raise OSError("the disk")

        engine = FakeEngine()
        wrapper = BoundedEngine(engine, Broken())
        with pytest.raises(OSError):
            await wrapper.decide(dispute, context, findings)
        assert engine.sent == []

    async def test_attempts_are_numbered(self, dispute, context, findings):
        clock = Clock()
        wrapper, book = bounded(FakeEngine(outage("transport"), answer()), clock=clock)
        await wrapper.decide(dispute, context, findings)
        assert [start.number for start in book.starts] == [1, 2]


class TestRecorderFailures:
    async def test_it_does_not_replace_a_cancellation(self, dispute, context, findings):
        class Broken(Book):
            def finished(self, attempt):
                raise OSError("the disk")

        async def stall():
            await asyncio.sleep(30)

        engine = FakeEngine(stall)
        wrapper = BoundedEngine(engine, Broken())
        task = asyncio.create_task(wrapper.decide(dispute, context, findings))
        while not engine.sent:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestPrices:
    async def test_as_of_is_a_date_not_a_citation(self, dispute, context, findings):
        wrapper, book = bounded(FakeEngine(answer()))
        telemetry = (await wrapper.decide(dispute, context, findings)).telemetry
        assert telemetry.pricing_as_of == "2026-10-26"
        assert book.ends[0].pricing_as_of == "2026-10-26"

    async def test_reported_cache_reads_reach_the_telemetry(self, dispute, context, findings):
        reply = Answer(
            json.dumps(VALID_VERDICT),
            "stop",
            100,
            50,
            "gpt-6-luna",
            cached_input_tokens=40,
        )
        telemetry = (
            await bounded(FakeEngine(reply))[0].decide(dispute, context, findings)
        ).telemetry
        assert telemetry.cache_read_input_tokens == 40

    async def test_a_cache_count_not_reported_stays_unknown(self, dispute, context, findings):
        telemetry = await bounded(FakeEngine(answer()))[0].decide(dispute, context, findings)
        assert telemetry.telemetry.cache_read_input_tokens is None

    async def test_an_unpriced_configuration_is_unknown_not_a_crash(
        self, dispute, context, findings
    ):
        from dataclasses import replace

        engine = FakeEngine(answer())
        engine.config = replace(LUNA, price_per_mtok=None)
        wrapper, book = bounded(engine)
        telemetry = (await wrapper.decide(dispute, context, findings)).telemetry
        assert telemetry.cost_micros is None
        assert telemetry.unknown_cost_attempts == 1
        assert book.outcomes == [AttemptOutcome.ANSWERED]


class TestTheWrapper:
    async def test_it_keeps_the_engines_identity(self):
        wrapper, _ = bounded(FakeEngine())
        assert (wrapper.name, wrapper.version) == (LUNA.name, LUNA.prompt_version)

    async def test_the_masking_hook_reaches_the_engine(self):
        engine = FakeEngine()
        wrapper, _ = bounded(engine)
        finder = object()
        wrapper.invoice_references = finder
        assert engine.invoice_references is finder

    async def test_it_closes_the_engine(self):
        engine = FakeEngine()
        wrapper, _ = bounded(engine)
        await wrapper.aclose()
        assert engine.closed


class TestParity:
    """The service's request is the evaluated one (FR-002, SC-003)."""

    def test_the_fingerprint_is_the_manifests(self):
        from pathlib import Path

        from evals.first_calls import fingerprint

        manifest = json.loads(
            Path("evals/comparisons/008/manifest.json").read_text(encoding="utf-8")
        )
        assert fingerprint(LUNA) == manifest["candidates"]["gpt-6-luna"]["fingerprint"]

    async def test_the_wire_body_equals_the_evaluated_one(
        self, monkeypatch, dispute, context, findings
    ):
        pytest.importorskip("openai")
        httpx2 = pytest.importorskip("httpx2")
        from app.decision.openai_compatible import OpenAICompatibleEngine, build_client

        for variable, value in (
            ("OPENAI_API_KEY", "sk-test"),
            ("OPENAI_ORG_ID", "org-test"),
            ("OPENAI_PROJECT_ID", "proj_test"),
        ):
            monkeypatch.setenv(variable, value)
        bodies = []

        def respond(request):
            bodies.append(json.loads(request.content))
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

        def engine():
            client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
            return OpenAICompatibleEngine(LUNA, build_client(LUNA, http_client=client))

        evaluated = engine()
        await evaluated.decide(dispute, context, findings)
        await BoundedEngine(engine(), Book()).decide(dispute, context, findings)
        assert bodies[0] == bodies[1]
        assert bodies[0]["store"] is False
        assert bodies[0]["response_format"]["json_schema"]["schema"] == verdict_schema()
