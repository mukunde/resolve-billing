"""One engine, then another, when the first cannot be reached
(specs/010-haiku-fallback).

Two bounded legs under one deadline. The second is asked only after a request
actually sent to the first ended in a timeout, a broken connection, a 429 or a
5xx: anything else says something is wrong, and a second vendor would bury it. A
refusal, a wrong model, an invalid answer, a failure before the send, a
cancellation and a defect all pass through as they are.

The choice is made per dispute and keeps no memory of earlier failures. Each
leg's limits stay shared across disputes, since each leg is built once.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import uuid4

from app.decision.base import (
    Decision,
    EngineMisidentified,
    EngineTelemetry,
    EngineUnavailable,
)
from app.decision.bounded import Bounds
from app.observability import tracing

#: The whole engine stage, both legs, waits and recording included.
DEADLINE = 90.0
#: One attempt each. A passing 429 moves on rather than waits, on purpose.
PRIMARY_BOUNDS = Bounds(attempts=1, per_attempt=30.0)
SECONDARY_BOUNDS = Bounds(attempts=1, per_attempt=60.0)
#: Changes with the order or the bounds.
POLICY = "failover_v1"


class FailoverEngine:
    """A `DecisionEngine` around two legs, each with a `leg` method."""

    def __init__(
        self,
        primary,
        secondary,
        name: str,
        version: str,
        deadline: float = DEADLINE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.name = name
        self.version = version
        self._deadline = deadline
        self._clock = clock

    # The provider hands the engines how invoice references are written (D10).
    @property
    def invoice_references(self):
        return self.primary.invoice_references

    @invoice_references.setter
    def invoice_references(self, finder) -> None:
        self.primary.invoice_references = finder
        self.secondary.invoice_references = finder

    async def aclose(self) -> None:
        try:
            await self.primary.aclose()
        finally:
            await self.secondary.aclose()

    async def decide(self, dispute, context, findings) -> Decision:
        invocation_id = uuid4().hex
        ends_at = self._clock() + self._deadline
        try:
            return await self.primary.leg(dispute, context, findings, invocation_id, ends_at)
        except EngineUnavailable as outage:
            if not getattr(outage, "failover_eligible", False) or self._clock() >= ends_at:
                raise
            carried = outage.telemetry
        try:
            decision = await self.secondary.leg(
                dispute, context, findings, invocation_id, ends_at, first_number=2
            )
        except (EngineUnavailable, EngineMisidentified) as failure:
            failure.telemetry = _merged(carried, failure.telemetry)
            tracing.set_on_current(**tracing.usage_attributes(failure.telemetry))
            raise
        telemetry = _merged(carried, decision.telemetry)
        tracing.set_on_current(**tracing.usage_attributes(telemetry))
        return decision.model_copy(update={"telemetry": telemetry})


def _merged(first: EngineTelemetry, second: EngineTelemetry) -> EngineTelemetry:
    """The second leg's telemetry, with what the first spent added: the model
    is the one that answered, and an unknown cost stays counted."""
    costs = [cost for cost in (first.cost_micros, second.cost_micros) if cost is not None]
    return second.model_copy(
        update={
            "cost_micros": sum(costs) if costs else None,
            "unknown_cost_attempts": first.unknown_cost_attempts + second.unknown_cost_attempts,
            "latency_ms": first.latency_ms + second.latency_ms,
            "pricing_as_of": second.pricing_as_of or first.pricing_as_of,
        }
    )
