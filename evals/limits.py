"""The pace an engine evaluated on a free tier is held to
(specs/004-second-engines).

A limiter per configuration -- tokens per minute and requests per second --
and, for configurations served from one vendor account, a second one they
share: Mistral's two models count against one key. Until the maintainer
confirms the shared ceiling (T071), the shared limiter takes the smaller of
the per-model limits, applied to both models together.

Every request reserves its estimated tokens before it is sent. A reservation
larger than a per-minute quota on its own can never fit: that request is not
sent (`Oversized`), and its case is never counted as a decision of the model.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from app.decision.hosted import CONFIGURATIONS, Configuration
from app.decision.limits import Limiter as _Limiter
from app.decision.limits import Oversized

__all__ = ["Limiter", "Oversized", "RunStopped", "limiters_for"]


class RunStopped(Exception):  # noqa: N818 -- a stop, not a defect
    """The run must stop here: the envelope, the deadline, rate limiting
    that outlasted the attempts, or usage the reservation did not bound.
    Not an outage of one case -- every case after it is left unattempted."""


class Limiter(_Limiter):
    """The shared limiter; here, a wait past the deadline stops the run."""

    past_deadline = RunStopped


def limiters_for(
    config: Configuration,
    shared: dict[str, Limiter],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[Limiter]:
    """The configuration's own limiter, and the one it shares with the other
    configurations of its account, taken from `shared` (built once)."""
    limiters = [
        Limiter(config.tokens_per_minute, config.requests_per_second, clock=clock, sleep=sleep)
    ]
    if config.shared_limits is not None:
        if config.shared_limits not in shared:
            siblings = [
                c for c in CONFIGURATIONS.values() if c.shared_limits == config.shared_limits
            ]
            shared[config.shared_limits] = Limiter(
                min(c.tokens_per_minute for c in siblings),
                min(c.requests_per_second for c in siblings),
                clock=clock,
                sleep=sleep,
            )
        limiters.append(shared[config.shared_limits])
    return limiters
