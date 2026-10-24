"""An engine evaluated on a free tier, held to its limits
(specs/004-second-engines).

Wraps a `HostedEngine` for the runner. Per call: the run's deadline; the
reservation estimated and refused if it can never fit a quota; the limiters
waited on; for Mistral, the attempt's maximum cost reserved in the ledger
before it is sent; the answer's reported usage checked against the
reservation; attempts bounded, waiting what `Retry-After` asks. It records,
per case, a status, the attempts, and the time spent waiting and on the
network.

Whatever cannot be bounded stops the run (`RunStopped`) rather than costing
more than was reserved: the envelope, the deadline, rate limiting outlasting
the attempts, usage above the reservation or not reported.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from app.decision.base import (
    Decision,
    EngineRejected,
    EngineTelemetry,
    EngineUnavailable,
    RecommendedAction,
)
from app.decision.hosted import Answer, HostedEngine, usable
from app.decision.verdict import escalation
from evals.ledger import Ledger
from evals.limits import Limiter, RunStopped

ATTEMPTS = 3
#: The whole run, in seconds: 34 cases at Gemini's placeholder pace fit.
DEADLINE = 3600.0
#: The wait between attempts when the vendor names none.
BACKOFF = 2.0


class Governed:
    def __init__(
        self,
        engine: HostedEngine,
        limiters: list[Limiter],
        ledger: Ledger | None,
        attempts: int = ATTEMPTS,
        deadline: float = DEADLINE,
        run_id: str = "",
        on_send: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if engine.config.vendor == "mistral" and ledger is None:
            raise ValueError("a Mistral configuration runs against the envelope's ledger")
        self.engine = engine
        self.config = engine.config
        self.name = engine.name
        self.version = engine.version
        self._limiters = limiters
        self._ledger = ledger
        self._attempts = attempts
        self._deadline = deadline
        self._run_id = run_id
        #: Told of each request to the model before it is sent.
        self._on_send = on_send
        self._clock = clock
        self._sleep = sleep
        self._started = clock()
        self._case = ""
        #: Per case: the status, the attempts sent, the seconds spent
        #: waiting on the limiters and on the network.
        self.statuses: dict[str, str] = {}
        self.attempts: dict[str, int] = {}
        #: Per case, the requests made to count tokens, apart from attempts.
        self.counts: dict[str, int] = {}
        self.waits: dict[str, float] = {}
        self.network: dict[str, float] = {}
        #: The reported cost of the run's attempts, in dollars (priced
        #: configurations only).
        self.cost_usd = 0.0

    # The provider hands the engine how invoice references are written (D10).
    @property
    def invoice_references(self):
        return self.engine.invoice_references

    @invoice_references.setter
    def invoice_references(self, finder) -> None:
        self.engine.invoice_references = finder

    def begin(self, case_id: str) -> None:
        self._case = case_id

    async def aclose(self) -> None:
        await self.engine.aclose()

    def _remaining(self) -> float:
        return self._deadline - (self._clock() - self._started)

    def _check_deadline(self) -> None:
        if self._remaining() <= 0:
            raise RunStopped(f"the run's deadline of {self._deadline:.0f} s is reached")

    async def decide(self, dispute, context, findings) -> Decision:
        # Until an answer says otherwise: a case that entered here and left
        # without one is never scored as a decision of the model.
        self.statuses[self._case] = "unavailable"
        self._check_deadline()
        if self.config.reservation == "uncapped":
            raise RunStopped(f"{self.name}: its reasoning has no billable cap; never sent")
        system, user, names = self.engine.request_texts(dispute, context, findings)
        # Counting is a call too: held to the same attempts and deadline.
        reserved, _, _ = await self._attempted(
            lambda: self.engine.reservation(system, user),
            counting=self.engine.counts_remotely,
        )
        if any(reserved > limiter.tokens_per_minute for limiter in self._limiters):
            self.statuses[self._case] = "not_sent_oversized"
            self.attempts[self._case] = 0
            return escalation(_no_call(self.config.model, self.version))
        answer, ticket, latency = await self._attempted(
            lambda: self.engine._complete(system, user), reserved
        )
        self._check_usage(answer, reserved, ticket)
        decision = self.engine.decision_from(answer, names, int(latency * 1000))
        self.statuses[self._case] = _status(answer, decision)
        return decision

    async def _attempted(self, call, reserved: int | None = None, counting: bool = False):
        """`call` within the attempts and the deadline: its result, the
        ledger's ticket, and the seconds it took. With `reserved`, each
        attempt is a request to the model: paced by the limiters, reserved in
        the ledger before it is sent, counted. With `counting`, a request to
        count tokens: paced by the request rate, noted, counted apart --
        whether the vendor counts it against the same quota is not
        documented, so it is assumed to."""
        for attempt in range(1, self._attempts + 1):
            self._check_deadline()
            ticket = None
            if counting:
                await self._acquire(0)
                self.counts[self._case] = attempt
                if self._on_send is not None:
                    self._on_send()
            if reserved is not None:
                await self._acquire(reserved)
                ticket = self._reserve(attempt, reserved)
                self.attempts[self._case] = attempt
                if self._on_send is not None:
                    self._on_send()
            started = self._clock()
            outage = None
            try:
                result = await asyncio.wait_for(call(), timeout=max(self._remaining(), 0.001))
            except TimeoutError:
                # Sent, perhaps processed: the reservation stays spent.
                raise RunStopped(
                    f"the run's deadline of {self._deadline:.0f} s is reached"
                ) from None
            except EngineRejected:
                self._settle(ticket, 0.0)
                raise
            except EngineUnavailable as error:
                outage = error
            finally:
                elapsed = self._clock() - started
                self.network[self._case] = self.network.get(self._case, 0.0) + elapsed
            if outage is None:
                return result, ticket, elapsed
            if str(outage) == "rate_limited":
                # Refused before it was processed: nothing was spent.
                self._settle(ticket, 0.0)
            if attempt == self._attempts:
                if str(outage) == "rate_limited":
                    raise RunStopped(f"rate limiting persisted past {self._attempts} attempts")
                raise outage
            wait = getattr(outage, "retry_after", None) or BACKOFF * attempt
            if wait >= self._remaining():
                raise RunStopped(f"the run's deadline of {self._deadline:.0f} s is reached")
            self.waits[self._case] = self.waits.get(self._case, 0.0) + wait
            await self._sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _acquire(self, reserved: int) -> None:
        for limiter in self._limiters:
            before = limiter.waited
            await limiter.acquire(reserved, self._remaining)
            self.waits[self._case] = self.waits.get(self._case, 0.0) + limiter.waited - before
        self._check_deadline()

    def _reserve(self, attempt: int, reserved: int) -> str | None:
        if self._ledger is None:
            return None
        ticket = f"{self._run_id}/{self.name}/{self._case}/{attempt}"
        # The reservation covers input and output: priced at the dearer rate.
        self._ledger.reserve(ticket, reserved * max(self.config.price_per_mtok) / 1e6)
        return ticket

    def _settle(self, ticket: str | None, usd: float) -> None:
        if ticket is not None:
            self._ledger.settle(ticket, usd)
            self.cost_usd += usd

    def _check_usage(self, answer: Answer, reserved: int, ticket: str | None) -> None:
        if answer.input_tokens is None or answer.output_tokens is None:
            # The cost cannot be known: the reservation stays spent.
            raise RunStopped(f"{self.name}: an answer came without its usage; the run stops")
        if self.config.price_per_mtok is not None:
            price_in, price_out = self.config.price_per_mtok
            self._settle(
                ticket, (answer.input_tokens * price_in + answer.output_tokens * price_out) / 1e6
            )
        used = answer.input_tokens + answer.output_tokens
        if used > reserved:
            raise RunStopped(
                f"{self.name}: usage above the reservation ({used} tokens, {reserved} reserved)"
            )


def _status(answer: Answer, decision: Decision) -> str:
    if not usable(answer):
        return "invalid"
    if decision.recommended_action is RecommendedAction.ESCALATE:
        return "abstained"
    return "decided"


def _no_call(model: str, version: str) -> EngineTelemetry:
    return EngineTelemetry(
        model_id=model,
        prompt_version=version,
        input_tokens=0,
        output_tokens=0,
        cost_micros=None,
        latency_ms=0,
    )
