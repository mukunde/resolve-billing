"""`FailoverEngine`: Luna, then Haiku (specs/010-haiku-fallback). No network."""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

import asyncio
import json

import pytest

from app.decision.base import (
    AttemptOutcome,
    EngineMisidentified,
    EngineRefused,
    EngineRejected,
    EngineUnavailable,
)
from app.decision.bounded import BoundedEngine, Bounds
from app.decision.failover import (
    DEADLINE,
    PRIMARY_BOUNDS,
    SECONDARY_BOUNDS,
    FailoverEngine,
)
from app.decision.hosted import configuration, unavailable
from app.decision.limits import Limiter
from tests.unit.test_bounded_engine import Book, Clock, FakeEngine, answer, outage
from tests.unit.test_claude_engine import (  # noqa: F401 -- the fixtures are used by name
    VALID_VERDICT,
    context,
    dispute,
    findings,
)

HAIKU = configuration("claude-haiku-5-5")


def haiku_answer(model="claude-haiku-5-5", tokens=(100, 50)):
    return answer(model=model, tokens=tokens)


class Status:
    """An HTTP response, as far as `unavailable` reads one."""

    def __init__(self, status_code, retry_after=None):
        self.status_code = status_code
        self.headers = {} if retry_after is None else {"retry-after": str(retry_after)}


def rig(primary_script, secondary_script=(), clock=None, bounds=None):
    clock = clock or Clock()
    book = Book()
    luna = FakeEngine(*primary_script)
    haiku = FakeEngine(*secondary_script)
    haiku.config = HAIKU
    haiku.name = HAIKU.name
    haiku.version = HAIKU.prompt_version
    first, second = bounds or (PRIMARY_BOUNDS, SECONDARY_BOUNDS)
    extra = {"clock": clock, "sleep": clock.sleep}
    engine = FailoverEngine(
        BoundedEngine(luna, book, bounds=first, **extra),
        BoundedEngine(haiku, book, bounds=second, **extra),
        name="both",
        version="both/failover_v1/decision_v3",
        clock=clock,
    )
    return engine, luna, haiku, book


class TestTheBounds:
    def test_they_are_the_specifications(self):
        assert DEADLINE == 90.0
        assert (PRIMARY_BOUNDS.attempts, PRIMARY_BOUNDS.per_attempt) == (1, 30.0)
        assert (SECONDARY_BOUNDS.attempts, SECONDARY_BOUNDS.per_attempt) == (1, 60.0)


class TestTriggers:
    @pytest.mark.parametrize(
        "failure",
        [
            outage("transport"),
            unavailable("server_error", Status(500)),
            unavailable("server_error", Status(503)),
            unavailable("server_error", Status(599)),
            unavailable("rate_limited", Status(429)),
        ],
    )
    async def test_each_falls_back(self, failure, dispute, context, findings):
        engine, luna, haiku, book = rig([failure], [haiku_answer()])
        decision = await engine.decide(dispute, context, findings)
        assert decision.telemetry.model_id == "claude-haiku-5-5"
        assert len(luna.sent) == len(haiku.sent) == 1
        assert book.outcomes[-1] is AttemptOutcome.ANSWERED

    async def test_a_timeout_falls_back(self, dispute, context, findings):
        async def stall():
            await asyncio.sleep(30)

        bounds = (Bounds(attempts=1, per_attempt=0.01), SECONDARY_BOUNDS)
        engine, _, haiku, book = rig([stall], [haiku_answer()], bounds=bounds)
        await engine.decide(dispute, context, findings)
        assert book.outcomes[0] is AttemptOutcome.TIMEOUT
        assert len(haiku.sent) == 1

    async def test_retry_after_is_not_waited_for(self, dispute, context, findings):
        clock = Clock()
        failure = unavailable("rate_limited", Status(429, retry_after=60))
        engine, _, haiku, _ = rig([failure], [haiku_answer()], clock=clock)
        await engine.decide(dispute, context, findings)
        assert 60 not in clock.slept
        assert len(haiku.sent) == 1


class TestExclusions:
    async def _not_called(self, engine, haiku, expected, dispute, context, findings):
        with pytest.raises(expected):
            await engine.decide(dispute, context, findings)
        assert haiku.sent == []

    async def test_a_refusal(self, dispute, context, findings):
        engine, _, haiku, _ = rig([EngineRejected("401")])
        await self._not_called(engine, haiku, EngineRefused, dispute, context, findings)

    async def test_a_wrong_model(self, dispute, context, findings):
        engine, _, haiku, _ = rig([answer(model="gpt-6-other")])
        await self._not_called(engine, haiku, EngineMisidentified, dispute, context, findings)

    async def test_an_unusable_answer(self, dispute, context, findings):
        engine, _, haiku, _ = rig([answer(text="not json")])
        decision = await engine.decide(dispute, context, findings)
        assert decision.recommended_action.value == "escalate"
        assert haiku.sent == []

    async def test_a_defect(self, dispute, context, findings):
        engine, _, haiku, _ = rig([RuntimeError("a bug")])
        await self._not_called(engine, haiku, RuntimeError, dispute, context, findings)

    @pytest.mark.parametrize("status", [200, 204, 301, 304])
    async def test_an_unexpected_status(self, status, dispute, context, findings):
        failure = unavailable("server_error", Status(status))
        engine, _, haiku, _ = rig([failure])
        await self._not_called(engine, haiku, EngineUnavailable, dispute, context, findings)

    async def test_a_server_error_with_no_status(self, dispute, context, findings):
        engine, _, haiku, _ = rig([outage("server_error")])
        await self._not_called(engine, haiku, EngineUnavailable, dispute, context, findings)

    async def test_the_limiter(self, dispute, context, findings):
        clock = Clock()
        engine, luna, haiku, _ = rig([], clock=clock)
        engine.primary._limiter = Limiter(10, 100.0, clock=clock, sleep=clock.sleep)
        await self._not_called(engine, haiku, EngineUnavailable, dispute, context, findings)
        assert luna.sent == []

    async def test_the_queue(self, dispute, context, findings):
        engine, luna, haiku, _ = rig([])
        engine._deadline = 0.02
        await engine.primary._slots.acquire()
        for _ in range(engine.primary.bounds.in_flight - 1):
            await engine.primary._slots.acquire()
        await self._not_called(engine, haiku, EngineUnavailable, dispute, context, findings)

    async def test_a_recorder_that_refuses(self, dispute, context, findings):
        engine, luna, haiku, book = rig([])

        def refuse(attempt):
            raise OSError("the disk")

        book.started = refuse
        await self._not_called(engine, haiku, OSError, dispute, context, findings)
        assert luna.sent == []

    async def test_not_sent_past_the_deadline(self, dispute, context, findings):
        clock = Clock()
        engine, luna, haiku, book = rig([], clock=clock)
        original = book.started

        def slow(attempt):
            original(attempt)
            clock.now += 500.0

        book.started = slow
        await self._not_called(engine, haiku, EngineUnavailable, dispute, context, findings)
        assert book.outcomes == [AttemptOutcome.NOT_SENT]

    async def test_a_cancellation(self, dispute, context, findings):
        async def stall():
            await asyncio.sleep(30)

        engine, luna, haiku, book = rig([stall])
        task = asyncio.create_task(engine.decide(dispute, context, findings))
        while not luna.sent:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert haiku.sent == []


class TestTime:
    async def test_with_a_clock_far_from_zero(self, dispute, context, findings):
        clock = Clock()
        clock.now = 1_000_000.0
        engine, _, haiku, _ = rig([outage("transport")], [haiku_answer()], clock=clock)
        await engine.decide(dispute, context, findings)
        assert len(haiku.sent) == 1

    async def test_a_slow_luna_leaves_haiku_the_rest(self, dispute, context, findings):
        clock = Clock()

        async def slow():
            clock.now += 70.0
            raise outage("transport")

        engine, _, haiku, _ = rig([slow], [haiku_answer()], clock=clock)
        decision = await engine.decide(dispute, context, findings)
        assert decision.telemetry.model_id == "claude-haiku-5-5"
        assert len(haiku.sent) == 1

    async def test_haiku_is_cut_at_the_shared_deadline(self, dispute, context, findings):
        clock = Clock()

        async def slow():
            clock.now += 89.99
            raise outage("transport")

        async def stall():
            await asyncio.sleep(30)

        engine, _, _, book = rig([slow], [stall], clock=clock)
        loop = asyncio.get_running_loop()
        began = loop.time()
        with pytest.raises(EngineUnavailable):
            await engine.decide(dispute, context, findings)
        assert loop.time() - began < 1.0
        assert book.outcomes[-1] is AttemptOutcome.TIMEOUT

    async def test_no_time_left_the_outage_stands(self, dispute, context, findings):
        clock = Clock()

        async def spent():
            clock.now += 500.0
            raise outage("transport")

        engine, _, haiku, _ = rig([spent], [haiku_answer()], clock=clock)
        with pytest.raises(EngineUnavailable) as raised:
            await engine.decide(dispute, context, findings)
        assert str(raised.value) == "transport"
        assert haiku.sent == []


class TestTheSecondLeg:
    async def test_an_outage_is_the_end(self, dispute, context, findings):
        engine, luna, haiku, book = rig([outage("transport")], [outage("transport")])
        with pytest.raises(EngineUnavailable):
            await engine.decide(dispute, context, findings)
        assert len(luna.sent) + len(haiku.sent) == 2
        assert [start.number for start in book.starts] == [1, 2]

    async def test_a_refusal_is_a_refusal(self, dispute, context, findings):
        engine, _, _, _ = rig([outage("transport")], [EngineRejected("401")])
        with pytest.raises(EngineRefused):
            await engine.decide(dispute, context, findings)

    async def test_an_unusable_answer_escalates(self, dispute, context, findings):
        engine, _, _, _ = rig(
            [outage("transport")], [answer(model="claude-haiku-5-5", text="not json")]
        )
        decision = await engine.decide(dispute, context, findings)
        assert decision.recommended_action.value == "escalate"

    async def test_a_wrong_model_keeps_both_legs_accounting(self, dispute, context, findings):
        engine, _, _, _ = rig([outage("transport")], [answer(model="claude-other")])
        with pytest.raises(EngineMisidentified) as raised:
            await engine.decide(dispute, context, findings)
        telemetry = raised.value.telemetry
        assert telemetry.model_id == "claude-other"
        assert telemetry.unknown_cost_attempts == 1
        assert telemetry.cost_micros > 0
        assert telemetry.usage_known is False


class TestAttribution:
    async def test_one_correlation_id_and_two_numbers(self, dispute, context, findings):
        engine, _, _, book = rig([outage("transport")], [haiku_answer()])
        decision = await engine.decide(dispute, context, findings)
        ids = {start.invocation_id for start in book.starts}
        assert ids == {decision.telemetry.invocation_id}
        assert [(s.number, s.provider) for s in book.starts] == [(1, "openai"), (2, "anthropic")]

    async def test_the_floor_is_kept(self, dispute, context, findings):
        engine, _, _, book = rig([outage("transport")], [haiku_answer()])
        telemetry = (await engine.decide(dispute, context, findings)).telemetry
        assert telemetry.unknown_cost_attempts == 1
        assert telemetry.cost_micros == book.ends[1].cost_micros

    async def test_a_known_cost_on_the_first_leg_is_summed(self, dispute, context, findings):
        engine, _, _, book = rig([outage("transport")], [haiku_answer()])
        engine.primary._account = lambda run, answer: _noted(run, 0.0005)
        telemetry = (await engine.decide(dispute, context, findings)).telemetry
        assert telemetry.cost_micros == 500 + book.ends[1].cost_micros

    async def test_luna_answers_when_it_can(self, dispute, context, findings):
        engine, _, haiku, _ = rig([answer()])
        decision = await engine.decide(dispute, context, findings)
        assert decision.telemetry.model_id == "gpt-6-luna"
        assert haiku.sent == []

    async def test_no_memory_between_disputes(self, dispute, context, findings):
        engine, luna, haiku, _ = rig([outage("transport"), answer()], [haiku_answer()])
        await engine.decide(dispute, context, findings)
        decision = await engine.decide(dispute, context, findings)
        assert decision.telemetry.model_id == "gpt-6-luna"
        assert len(luna.sent) == 2
        assert len(haiku.sent) == 1

    async def test_limits_stay_shared_across_disputes(self, dispute, context, findings):
        engine, _, _, _ = rig([answer(), answer()])
        limiter = engine.primary._limiter
        await engine.decide(dispute, context, findings)
        await engine.decide(dispute, context, findings)
        assert engine.primary._limiter is limiter
        assert len(limiter._sent) == 2


class TestTheWrapper:
    async def test_the_masking_hook_reaches_both(self):
        engine, luna, haiku, _ = rig([])
        finder = object()
        engine.invoice_references = finder
        assert luna.invoice_references is finder
        assert haiku.invoice_references is finder

    async def test_both_close(self):
        engine, luna, haiku, _ = rig([])
        await engine.aclose()
        assert luna.closed and haiku.closed

    async def test_the_second_closes_even_if_the_first_fails(self):
        engine, luna, haiku, _ = rig([])

        async def broken():
            raise RuntimeError("no close")

        luna.aclose = broken
        with pytest.raises(RuntimeError):
            await engine.aclose()
        assert haiku.closed


def _noted(run, cost):
    run.known_cost = (run.known_cost or 0.0) + cost
    return round(cost * 1_000_000)
