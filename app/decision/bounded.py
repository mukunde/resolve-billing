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
import contextlib
import re
import time
from collections.abc import Awaitable, Callable, Coroutine
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
from app.observability import tracing


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
        try:
            reserved = await self._within(run, self.engine.reservation(system, user))
        except EngineUnavailable as outage:
            outage.invocation_id = run.id
            raise
        for number in range(1, self.bounds.attempts + 1):
            try:
                answer = await self._attempt(run, number, system, user, reserved)
            except EngineUnavailable as outage:
                outage.invocation_id = run.id
                await self._wait_for_next(run, number, outage)
                continue
            return self._decision(run, answer, names)
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    async def _within(run: _Invocation, step: Coroutine):
        """A step before the send, held to the time left: every wait counts."""
        remaining = run.remaining()
        if remaining <= 0:
            step.close()
            raise EngineUnavailable("deadline")
        try:
            async with asyncio.timeout(remaining):
                return await step
        except TimeoutError:
            raise EngineUnavailable("deadline") from None

    async def _wait_for_next(self, run: _Invocation, number: int, outage) -> None:
        """Return when another attempt may start; raise the outage otherwise."""
        if number == self.bounds.attempts:
            raise outage
        asked = getattr(outage, "retry_after", None)
        wait = asked if asked is not None else self.bounds.backoff[number - 1]
        if wait >= run.remaining():
            raise outage
        try:
            await self._within(run, self._sleep(wait))
        except EngineUnavailable:
            raise outage from None

    async def _attempt(
        self, run: _Invocation, number: int, system: str, user: str, reserved: int
    ) -> Answer:
        """One send, recorded from before it leaves to how it ended."""
        await self._within(run, self._slots.acquire())
        try:
            try:
                await self._within(run, self._limiter.acquire(reserved, run.remaining))
            except (WaitTooLong, Oversized):
                # Nothing was sent, so no attempt is on record.
                raise EngineUnavailable("deadline") from None
            if run.remaining() <= 0:
                raise EngineUnavailable("deadline")
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
        attributes = tracing.chat_attributes(
            model=config.model,
            prompt_version=self.version,
            max_tokens=config.max_output_tokens,
            provider=config.vendor,
        )
        with tracing.span(f"chat {config.model}", **attributes) as current:
            return await self._outcome(run, number, attempt_id, began, system, user, current)

    async def _outcome(
        self,
        run: _Invocation,
        number: int,
        attempt_id: str,
        began: float,
        system: str,
        user: str,
        current,
    ) -> Answer:
        config = self.engine.config

        def end(outcome: AttemptOutcome, answer: Answer | None = None) -> None:
            cost = None if outcome is AttemptOutcome.NOT_SENT else self._account(run, answer)
            ended = AttemptEnd(
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
            tracing.set_attributes(current, **tracing.attempt_attributes(ended, number, run.id))
            self._recorder.finished(ended)

        # Recording takes time too: the last check before anything leaves.
        if run.remaining() <= 0:
            end(AttemptOutcome.NOT_SENT)
            raise EngineUnavailable("deadline")
        try:
            async with asyncio.timeout(min(self.bounds.per_attempt, run.remaining())):
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
            # A record that cannot be written leaves the row unfinished, which
            # is true; it must not turn a cancellation into something else.
            with contextlib.suppress(Exception):
                end(AttemptOutcome.CANCELLED)
            raise
        except Exception:
            end(AttemptOutcome.FAILED)
            raise

        if _wrong_model(answer, config):
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
        if (
            answer is None
            or answer.input_tokens is None
            or answer.output_tokens is None
            or config.price_per_mtok is None
        ):
            run.unknown_cost_attempts += 1
            return None
        try:
            cost = attempt_cost(answer, config)
        except InconsistentUsage:
            run.unknown_cost_attempts += 1
            return None
        run.known_cost = (run.known_cost or 0.0) + cost
        run.pricing_as_of = _priced_on(config.price_source)
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
                "cache_read_input_tokens": answer.cached_input_tokens,
                "usage_known": usage_known,
                "unknown_cost_attempts": run.unknown_cost_attempts,
                "invocation_id": run.id,
                "cost_micros": (
                    round(run.known_cost * 1_000_000) if run.known_cost is not None else None
                ),
                "pricing_as_of": run.pricing_as_of,
            }
        )
        # The attempts' spans hold each send; the investigation's, the total.
        tracing.set_on_current(**tracing.usage_attributes(telemetry))
        return decision.model_copy(update={"telemetry": telemetry})


def _wrong_model(answer: Answer, config) -> bool:
    """Whether an answer names, or fails to name, the configured model. A
    response nobody could read names none, and is unusable, not misidentified."""
    if not config.check_identity:
        return False
    if answer.model_version is None and not usable(answer):
        return False
    return answer.model_version != (config.returned_model or config.model)


def _named(answer: Answer | None) -> str | None:
    """The model an answer names, kept only if it reads as a model id, never
    as free text."""
    if answer is None or not isinstance(answer.model_version, str):
        return None
    return answer.model_version if MODEL_ID.fullmatch(answer.model_version) else None


def _priced_on(source: str) -> str | None:
    """The date a price source was read: what `pricing_as_of` holds."""
    found = re.findall(r"\d{4}-\d{2}-\d{2}", source)
    return found[-1] if found else None


def _now() -> str:
    return datetime.now(UTC).isoformat()
