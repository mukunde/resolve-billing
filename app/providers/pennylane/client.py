"""Pennylane's Company API v2, read over HTTP (specs/014-pennylane-read, FR-007,
FR-008).

What the adapter needs from the wire, and nothing of Pennylane's vocabulary:
requests paced under the limit per token, process-wide; every list read to its
end or not at all; retries within the port call's time budget; errors by
category, never with a URL, a header, a body or an exception's text. The
company check lives here too: every read starts with it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.providers.base import NotFound, ProviderUnavailable
from app.providers.pennylane.keys import ProviderConfigurationError

BASE_URL = "https://app.pennylane.com/api/external/v2"
#: Pennylane's limit, per token: 25 requests in any 5 seconds.
WINDOW_SECONDS = 5.0
WINDOW_REQUESTS = 25
#: The time a port call may take, waits and retries included (as Stripe's).
DEFAULT_BUDGET = 20.0
MAX_ATTEMPTS = 4
PAGE_LIMIT = 100

Payload = dict[str, Any]

# Nothing of a request -- its URL, its headers -- reaches a log.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.CRITICAL + 1)
    logging.getLogger(_name).propagate = False


class _Unreadable(ProviderUnavailable):
    """An answer received and not readable as JSON."""


class Pacer:
    """At most WINDOW_REQUESTS sends in any WINDOW_SECONDS, for one token,
    shared by every client of that token in the process."""

    _shared: dict[str, Pacer] = {}

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._clock = clock
        self._sleep = sleep

    @classmethod
    def for_token(cls, token: str) -> Pacer:
        # Keyed by the token itself, held in memory only, never printed.
        return cls._shared.setdefault(token, cls())

    async def wait(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                while self._sent and now - self._sent[0] >= WINDOW_SECONDS:
                    self._sent.popleft()
                if len(self._sent) < WINDOW_REQUESTS:
                    self._sent.append(now)
                    return
                await self._sleep(WINDOW_SECONDS - (now - self._sent[0]))


class PennylaneReader:
    """GETs on the Company API v2, for the declared sandbox only."""

    def __init__(
        self,
        token: str,
        company_id: int,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        pacer: Pacer | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            transport=transport,
            timeout=10.0,
        )
        self._company_id = company_id
        self._pacer = pacer or Pacer.for_token(token)
        self._sleep = sleep
        #: The company check's verdict, once made: None until then.
        self._verdict: ProviderConfigurationError | bool | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- the company check (Q2) --------------------------------------------

    async def check_company(self) -> None:
        """Raise unless the token's company is the declared sandbox; the
        verdict, either way, is kept for every later call."""
        if self._verdict is True:
            return
        if isinstance(self._verdict, ProviderConfigurationError):
            raise self._verdict
        try:
            me = await self._get("/me")
        except (NotFound, _Unreadable):
            me = None  # no identity to compare: not the declared sandbox
        company = (me or {}).get("company") if isinstance(me, dict) else None
        company_id = company.get("id") if isinstance(company, dict) else None
        if company_id != self._company_id:
            self._verdict = ProviderConfigurationError("sandbox")
            raise self._verdict
        self._verdict = True

    # -- reads ------------------------------------------------------------------

    async def get(self, path: str) -> Payload:
        """One object; `NotFound` on a 404."""
        await self.check_company()
        body = await self._get(path)
        if not isinstance(body, dict):
            raise ProviderUnavailable("pennylane: an answer that is not an object")
        return body

    async def every(self, path: str, params: dict[str, Any] | None = None) -> list[Payload]:
        """Every item of a list, to its end; a list that cannot be read to its
        end is `ProviderUnavailable`, never a shorter list."""
        await self.check_company()
        items: list[Payload] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page_params = dict(params or {}, limit=PAGE_LIMIT)
            if cursor is not None:
                page_params["cursor"] = cursor
            try:
                page = await self._get(path, page_params)
            except NotFound:
                raise ProviderUnavailable("pennylane: a list answered not found") from None
            batch = page.get("items") if isinstance(page, dict) else None
            if not isinstance(batch, list) or not all(isinstance(i, dict) for i in batch):
                raise ProviderUnavailable("pennylane: a malformed page")
            items.extend(batch)
            more = page.get("has_more")
            if more is False:
                return items
            cursor = page.get("next_cursor")
            if more is not True or not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ProviderUnavailable("pennylane: unusable pagination")
            seen.add(cursor)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        delay = 0.5
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._pacer.wait()
            try:
                response = await self._client.get(path, params=params)
            except httpx.HTTPError:
                if attempt == MAX_ATTEMPTS:
                    raise ProviderUnavailable("pennylane: no answer") from None
                await self._sleep(delay)
                delay *= 2
                continue
            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError:
                    raise _Unreadable("pennylane: an unreadable answer") from None
            if status == 404:
                raise NotFound("pennylane: not found")
            if status == 401:
                raise ProviderConfigurationError("authentication")
            if status == 403:
                raise ProviderConfigurationError("permission")
            if status == 429 or status >= 500:
                if attempt == MAX_ATTEMPTS:
                    raise ProviderUnavailable(f"pennylane: still failing ({status})")
                header = response.headers.get("retry-after", "")
                await self._sleep(float(header) if header.isdigit() else delay)
                delay *= 2
                continue
            raise ProviderConfigurationError("invalid_request")
        raise ProviderUnavailable("pennylane: no answer")
