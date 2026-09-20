"""Trace of what happened during one investigation.

Events are collected as the run proceeds rather than derived from the result
afterwards. Derived events would be a retelling: they would show what must
have happened given the outcome, not what did happen, and would quietly stop
being able to record a step that produced nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: str
    occurred_at: AwareDatetime
    payload: dict = {}


def event(name: str, **payload) -> AuditEvent:
    return AuditEvent(event=name, occurred_at=datetime.now(UTC), payload=payload)


@runtime_checkable
class AuditSink(Protocol):
    """Where a finished run is written.

    Narrow on purpose: the application layer records, it never reads back.
    """

    def record(self, result, billing_provider: str) -> None: ...
