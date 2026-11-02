"""An evaluated engine, held to what the service needs (specs/009-luna-service).

The evaluation runner governs a hosted engine from outside: its attempts, its
deadline, the model's identity. The service has no runner, and `app/` does not
import `evals/`, so this wrapper does the same work for one dispute. It sends
what the wrapped engine sends: the request is built once, and each attempt
carries the same bytes.

Its bounds are not the runner's. The runner's deadline covers a whole run; here
the API answers only once the investigation ends, so a person waits on them.

It never switches to another engine (FR-005a): when it fails, the investigation
escalates or the API answers with an error.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from app.decision.base import (
    AttemptEnd,
    AttemptOutcome,
    AttemptRecorder,
    AttemptStart,
    Decision,
    EngineMisidentified,
    EngineRefused,
    EngineRejected,
    EngineUnavailable,
)
from app.decision.hosted import (
    MODEL_ID,
    Answer,
    HostedEngine,
    InconsistentUsage,
    attempt_cost,
    usable,
)
from app.decision.limits import Limiter, Oversized, WaitTooLong


@dataclass(frozen=True)
class Bounds:
    """What one dispute may take from the vendor (FR-005, FR-006)."""

    attempts: int = 3
    per_attempt: float = 60.0
    #: The whole engine stage: waits and the limiter included.
    deadline: float = 90.0
    in_flight: int = 4
    #: The waits between attempts when the vendor names none.
    backoff: tuple[float, ...] = (2.0, 4.0)


class _Invocation:
    """One `decide`, its bookkeeping local to it: the wrapper is shared by
    concurrent requests."""

    def __init__(self, dispute_id: str, deadline: float, clock: Callable[[], float]) -> None:
        self.id = uuid4().hex
        self.dispute_id = dispute_id
        self._clock = clock
        self._started = clock()
        self._deadline = deadline
        self.known_cost: float | None = None
        self.unknown_cost_attempts = 0
        self.pricing_as_of: str | None = None

    def remaining(self) -> float:
        return self._deadline - self.elapsed()

    def elapsed(self) -> float:
        return self._clock() - self._started


class BoundedEngine:
    """A `DecisionEngine` around a `HostedEngine`."""

    def __init__(
        self,
        engine: HostedEngine,
        recorder: AttemptRecorder,
        bounds: Bounds = Bounds(),  # noqa: B008 -- frozen
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if (engine.config.currency or "USD") != "USD":
            raise ValueError("the audit records dollars: this engine's prices are not")
        self.engine = engine
        self.name = engine.name
        self.version = engine.version
        self.bounds = bounds
        self._recorder = recorder
        self._clock = clock
        self._sleep = sleep
        self._slots = asyncio.Semaphore(bounds.in_flight)
        config = engine.config
        self._limiter = Limiter(
            config.tokens_per_minute, config.requests_per_second, clock=clock, sleep=sleep
        )

    # The provider hands the engine how invoice references are written (D10).
    @property
    def invoice_references(self):
        return self.engine.invoice_references

    @invoice_references.setter
    def invoice_references(self, finder) -> None:
        self.engine.invoice_references = finder

    async def aclose(self) -> None:
        await self.engine.aclose()

    async def decide(self, dispute, context, findings) -> Decision:
        run = _Invocation(dispute.id, self.bounds.deadline, self._clock)
        system, user, names = self.engine.request_texts(dispute, context, findings)
        reserved = await self.engine.reservation(system, user)
        for number in range(1, self.bounds.attempts + 1):
            try:
                answer = await self._attempt(run, number, system, user, reserved)
            except EngineUnavailable as outage:
                outage.invocation_id = run.id
                await self._wait_for_next(run, number, outage)
                continue
            return self._decision(run, answer, names)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _wait_for_next(self, run: _Invocation, number: int, outage) -> None:
        """Return when another attempt may start; raise the outage otherwise."""
        if number == self.bounds.attempts:
            raise outage
        asked = getattr(outage, "retry_after", None)
        wait = asked if asked is not None else self.bounds.backoff[number - 1]
        if wait >= run.remaining():
            raise outage
        await self._sleep(wait)

    async def _attempt(
        self, run: _Invocation, number: int, system: str, user: str, reserved: int
    ) -> Answer:
        """One send, recorded from before it leaves to how it ended."""
        try:
            async with asyncio.timeout(max(run.remaining(), 0.001)):
                await self._slots.acquire()
        except TimeoutError:
            raise EngineUnavailable("deadline") from None
        try:
            try:
                await self._limiter.acquire(reserved, run.remaining)
            except (WaitTooLong, Oversized):
                # Nothing was sent, so no attempt is on record.
                raise EngineUnavailable("deadline") from None
            return await self._sent(run, number, system, user, reserved)
        finally:
            self._slots.release()

    async def _sent(
        self, run: _Invocation, number: int, system: str, user: str, reserved: int
    ) -> Answer:
        config = self.engine.config
        attempt_id = uuid4().hex
        self._recorder.started(
            AttemptStart(
                attempt_id=attempt_id,
                invocation_id=run.id,
                dispute_id=run.dispute_id,
                number=number,
                provider=config.vendor,
                requested_model=config.model,
                started_at=_now(),
                reserved_tokens=reserved,
            )
        )
        began = self._clock()

        def end(outcome: AttemptOutcome, answer: Answer | None = None) -> None:
            cost = self._account(run, answer)
            self._recorder.finished(
                AttemptEnd(
                    attempt_id=attempt_id,
                    outcome=outcome,
                    ended_at=_now(),
                    duration_ms=int((self._clock() - began) * 1000),
                    returned_model=_named(answer),
                    input_tokens=answer.input_tokens if answer else None,
                    output_tokens=answer.output_tokens if answer else None,
                    cached_input_tokens=answer.cached_input_tokens if answer else None,
                    cache_write_tokens=answer.cache_write_tokens if answer else None,
                    reasoning_tokens=answer.reasoning_tokens if answer else None,
                    cost_micros=cost,
                    pricing_as_of=run.pricing_as_of if cost is not None else None,
                )
            )

        try:
            async with asyncio.timeout(min(self.bounds.per_attempt, max(run.remaining(), 0.001))):
                answer = await self.engine.complete(system, user)
        except TimeoutError:
            end(AttemptOutcome.TIMEOUT)
            raise EngineUnavailable("timeout") from None
        except EngineRejected as refusal:
            end(AttemptOutcome.REJECTED)
            error = EngineRefused(str(refusal))
            error.invocation_id = run.id
            raise error from None
        except EngineUnavailable as outage:
            limited = str(outage) == "rate_limited"
            end(AttemptOutcome.RATE_LIMITED if limited else AttemptOutcome.UNAVAILABLE)
            raise
        except asyncio.CancelledError:
            end(AttemptOutcome.CANCELLED)
            raise
        except Exception:
            end(AttemptOutcome.FAILED)
            raise

        expected = config.returned_model or config.model
        if config.check_identity and answer.model_version != expected:
            end(AttemptOutcome.WRONG_MODEL, answer)
            error = EngineMisidentified("wrong_model")
            error.invocation_id = run.id
            raise error
        end(AttemptOutcome.ANSWERED if usable(answer) else AttemptOutcome.UNUSABLE, answer)
        return answer

    def _account(self, run: _Invocation, answer: Answer | None) -> int | None:
        """The attempt's cost in millionths of a dollar, added to the
        invocation's; None, and counted as unknown, when it cannot be known."""
        config = self.engine.config
        if answer is None or answer.input_tokens is None or answer.output_tokens is None:
            run.unknown_cost_attempts += 1
            return None
        try:
            cost = attempt_cost(answer, config)
        except InconsistentUsage:
            run.unknown_cost_attempts += 1
            return None
        run.known_cost = (run.known_cost or 0.0) + cost
        run.pricing_as_of = config.price_source
        return round(cost * 1_000_000)

    def _decision(self, run: _Invocation, answer: Answer, names) -> Decision:
        decision = self.engine.decision_from(answer, names, int(run.elapsed() * 1000))
        # Those lists suit a run. A service would keep them for ever.
        self.engine.notes.clear()
        self.engine.model_versions.clear()
        usage_known = answer.input_tokens is not None and answer.output_tokens is not None
        telemetry = decision.telemetry.model_copy(
            update={
                "model_id": _named(answer) or self.engine.config.model,
                "usage_known": usage_known,
                "unknown_cost_attempts": run.unknown_cost_attempts,
                "invocation_id": run.id,
                "cost_micros": (
                    round(run.known_cost * 1_000_000) if run.known_cost is not None else None
                ),
                "pricing_as_of": run.pricing_as_of,
            }
        )
        return decision.model_copy(update={"telemetry": telemetry})


def _named(answer: Answer | None) -> str | None:
    """The model an answer names, kept only if it reads as a model id, never
    as free text."""
    if answer is None or not isinstance(answer.model_version, str):
        return None
    return answer.model_version if MODEL_ID.fullmatch(answer.model_version) else None


def _now() -> str:
    return datetime.now(UTC).isoformat()
