"""Pennylane's Company API v2 over HTTP: reads (specs/014-pennylane-read, FR-007,
FR-008) and the one-shot writes of credit notes (specs/015-pennylane-credit-notes).

What the adapter needs from the wire, and nothing of Pennylane's vocabulary:
requests paced under the limit per token, process-wide; every list read to its
end or not at all; retries within the port call's time budget; errors by
category, never with a URL, a header, a body or an exception's text. The
company check lives here too: every read starts with it. A write is sent
once and never retried; an answer lost is `Uncertain`, a refusal `Refused`.
Every request may be preceded by a hook (`before`) that raises when the
attempt may no longer send (spec 015, FR-011): it runs after pacing, before
each send, retries included.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.providers.base import BeforeRequest, NotFound, ProviderUnavailable
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

    @property
    def refused_company(self) -> bool:
        """Whether the company check was made and refused."""
        return isinstance(self._verdict, ProviderConfigurationError)

    async def check_company(self, before: BeforeRequest | None = None) -> None:
        """Raise unless the token's company is the declared sandbox; the
        verdict, either way, is kept for every later call."""
        if self._verdict is True:
            return
        if isinstance(self._verdict, ProviderConfigurationError):
            raise self._verdict
        try:
            me = await self._get("/me", before=before)
        except (NotFound, _Unreadable):
            me = None  # no identity to compare: not the declared sandbox
        company = (me or {}).get("company") if isinstance(me, dict) else None
        company_id = company.get("id") if isinstance(company, dict) else None
        if company_id != self._company_id:
            self._verdict = ProviderConfigurationError("sandbox")
            raise self._verdict
        self._verdict = True

    # -- reads ------------------------------------------------------------------

    async def get(self, path: str, *, before: BeforeRequest | None = None) -> Payload:
        """One object; `NotFound` on a 404."""
        await self.check_company(before)
        body = await self._get(path, before=before)
        if not isinstance(body, dict):
            raise ProviderUnavailable("pennylane: an answer that is not an object")
        return body

    async def every(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        before: BeforeRequest | None = None,
    ) -> list[Payload]:
        """Every item of a list, to its end; a list that cannot be read to its
        end is `ProviderUnavailable`, never a shorter list."""
        await self.check_company(before)
        items: list[Payload] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page_params = dict(params or {}, limit=PAGE_LIMIT)
            if cursor is not None:
                page_params["cursor"] = cursor
            try:
                page = await self._get(path, page_params, before=before)
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

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        before: BeforeRequest | None = None,
    ) -> Any:
        delay = 0.5
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._pacer.wait()
            if before is not None:
                await before()
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


class Uncertain(Exception):  # noqa: N818 -- a state: whether a write landed is unknown
    """A write was sent and its answer lost or unreadable: nothing is sent
    again until a read settles it."""


class Refused(Exception):  # noqa: N818 -- Pennylane's answer: nothing was made
    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"refused ({status})")
        self.status = status
        #: Pennylane's own words on why, so a report can say which field.
        self.detail = detail


#: Paths a writer never requests: sending a document by email, or anything of
#: electronic invoicing (spec 015, FR-012). Checked on every write.
FORBIDDEN_PATH_PARTS = ("send_by_email", "email", "e_invoic", "einvoic", "e-invoic")


class PennylaneWriter(PennylaneReader):
    """The reader, plus one-shot writes: never retried after sending."""

    async def write(
        self,
        method: str,
        path: str,
        body: Payload | None = None,
        *,
        before: BeforeRequest | None = None,
    ) -> Payload | None:
        lowered = path.lower()
        if any(part in lowered for part in FORBIDDEN_PATH_PARTS):
            raise ValueError("pennylane: an email or e-invoicing path is never requested")
        await self.check_company(before)
        await self._pacer.wait()
        if before is not None:
            await before()
        try:
            response = await self._client.request(method, path, json=body)
        except httpx.HTTPError:
            raise Uncertain(path) from None
        if response.status_code in (200, 201):
            try:
                return response.json()
            except ValueError:
                raise Uncertain(path) from None
        if response.status_code == 204:
            return None
        if response.status_code >= 500 or response.status_code == 429:
            raise Uncertain(path)
        raise Refused(response.status_code, response.text[:500])
