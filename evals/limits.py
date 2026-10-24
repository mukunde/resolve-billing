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
from collections import deque
from collections.abc import Awaitable, Callable

from app.decision.hosted import CONFIGURATIONS, Configuration

WINDOW_SECONDS = 60.0


class RunStopped(Exception):  # noqa: N818 -- a stop, not a defect
    """The run must stop here: the envelope, the deadline, rate limiting
    that outlasted the attempts, or usage the reservation did not bound.
    Not an outage of one case -- every case after it is left unattempted."""


class Oversized(Exception):  # noqa: N818 -- a refusal, not a defect
    """A request whose reservation exceeds a per-minute quota on its own."""


class Limiter:
    """Tokens per minute over a sliding window, and requests spaced by the
    request rate. `acquire` waits until both allow the request."""

    def __init__(
        self,
        tokens_per_minute: int,
        requests_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.tokens_per_minute = tokens_per_minute
        self.spacing = 1.0 / requests_per_second
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[tuple[float, int]] = deque()
        self._last: float | None = None
        #: Seconds spent waiting on this limiter, across the run.
        self.waited = 0.0

    async def acquire(self, tokens: int, remaining: Callable[[], float] | None = None) -> None:
        """Wait until the request fits; a wait reaching past what remains of
        the run (`remaining`, in seconds) stops it instead."""
        if tokens > self.tokens_per_minute:
            raise Oversized(f"{tokens} tokens reserved, {self.tokens_per_minute} per minute")
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0][0] >= WINDOW_SECONDS:
                self._sent.popleft()
            wait = 0.0
            if self._last is not None:
                wait = max(wait, self._last + self.spacing - now)
            used = sum(count for _, count in self._sent)
            if used + tokens > self.tokens_per_minute:
                # Free the oldest reservations until this one fits.
                freed = used
                for at, count in self._sent:
                    freed -= count
                    if freed + tokens <= self.tokens_per_minute:
                        wait = max(wait, at + WINDOW_SECONDS - now)
                        break
            if wait <= 0:
                break
            if remaining is not None and wait >= remaining():
                raise RunStopped("the run's deadline would pass while waiting on a limiter")
            self.waited += wait
            await self._sleep(wait)
        self._sent.append((now, tokens))
        self._last = now


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
