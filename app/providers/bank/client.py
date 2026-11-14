"""Revolut Business over HTTP, read-only (specs/016-bank-evidence, FR-002).

What the adapter needs from the wire, and nothing of Revolut's vocabulary:

- **GET only, one host**: every request goes to the pinned Sandbox origin, by HTTPS, under
  `/api/1.0/`; anything else is refused before it is sent. Redirects are never followed:
  an answer that redirects is a failure.
- **Signed**: every request carries `Revolut-Api-Timestamp` (Unix milliseconds) and
  `Revolut-Api-Signature`, the Base64 Ed25519 signature of timestamp + method + path +
  query (as sent, without its `?`) + body -- the construction the Sandbox accepted on
  2026-11-14. Signed from the request as it leaves, so what is signed is what is sent.
- **Paced**: at most 60 requests in any 60 seconds per key, process-wide (Revolut's limit
  is per business); a read that would wait past its time budget stops (`BudgetSpent`),
  and the adapter reports it incomplete.
- **Failures by category**: `BankUnavailable` carries a fixed word, never a URL, a header,
  a body or an exception's text. Amounts are parsed as decimals, never floats (IV).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from app.providers.bank_base import BankUnavailable

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

#: The Sandbox, pinned: Production is a later decision (spec 016, no Production claim).
HOST = "sandbox-b2b.revolut.com"
API_PREFIX = "/api/1.0"
BASE_URL = f"https://{HOST}{API_PREFIX}"

#: Revolut's limit, per business: 60 requests in any 60 seconds.
WINDOW_SECONDS = 60.0
WINDOW_REQUESTS = 60
#: The time one read may take, waits and retries included.
DEFAULT_BUDGET = 20.0
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = 10.0

# Nothing of a request -- its URL, its headers -- reaches a log.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.CRITICAL + 1)
    logging.getLogger(_name).propagate = False


class RevolutConfigurationError(Exception):
    """Revolut cannot be used as configured: a missing variable or an unreadable signing
    key. Fails visibly, by category, never with a value."""

    def __init__(self, category: str) -> None:
        super().__init__(f"revolut configuration error: {category}")
        self.category = category


class BudgetSpent(Exception):  # noqa: N818 -- a state: the read ran out of time
    """The read's time budget would be exceeded by the next wait."""


def load_signing_key(path: str | Path) -> Ed25519PrivateKey:
    """The Ed25519 private key at `path` (PEM, unencrypted); nothing of it, or of why it
    could not be read, is ever put in a message."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key = load_pem_private_key(Path(path).read_bytes(), password=None)
    except Exception:  # noqa: BLE001 -- no text of the failure is kept
        raise RevolutConfigurationError("signing_key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise RevolutConfigurationError("signing_key")
    return key


def signature(
    key: Ed25519PrivateKey, timestamp: str, method: str, path: str, query: str, body: bytes
) -> str:
    """Base64 of the Ed25519 signature of timestamp + method + path + query + body."""
    message = (timestamp + method + path + query).encode("utf-8") + body
    return base64.b64encode(key.sign(message)).decode("ascii")


def _no_constant(_: str) -> Any:
    raise ValueError("a non-finite number")


def parse(content: bytes) -> Any:
    """JSON with every fractional number a `Decimal`; NaN and infinities refused."""
    return json.loads(content, parse_float=Decimal, parse_constant=_no_constant)


class Pacer:
    """At most WINDOW_REQUESTS sends in any WINDOW_SECONDS, for one key, shared by every
    reader of that key in the process."""

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
    def for_key(cls, key: str) -> Pacer:
        # Keyed by the key itself, held in memory only, never printed.
        return cls._shared.setdefault(key, cls())

    async def wait(self, deadline: float) -> None:
        """Take a slot, waiting for one if need be; `BudgetSpent` when the wait would end
        after `deadline`."""
        async with self._lock:
            while True:
                now = self._clock()
                if now >= deadline:
                    raise BudgetSpent
                while self._sent and now - self._sent[0] >= WINDOW_SECONDS:
                    self._sent.popleft()
                if len(self._sent) < WINDOW_REQUESTS:
                    self._sent.append(now)
                    return
                pause = WINDOW_SECONDS - (now - self._sent[0])
                if now + pause > deadline:
                    raise BudgetSpent
                await self._sleep(pause)


class RevolutReader:
    """Signed GETs on the Business API's Sandbox, and nothing else."""

    def __init__(
        self,
        api_key: str,
        signing_key: Ed25519PrivateKey,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        pacer: Pacer | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wall_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._signing_key = signing_key
        self._pacer = pacer or Pacer.for_key(api_key)
        self._clock = clock
        self._sleep = sleep
        self._wall_ms = wall_ms
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            transport=transport,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=False,
            event_hooks={"request": [self._guard_and_sign]},
        )

    def clock(self) -> float:
        return self._clock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _guard_and_sign(self, request: httpx.Request) -> None:
        """The last gate before a request leaves: GET, HTTPS, the pinned host, the API's
        path, no body -- else nothing is sent. Then the signature, over the request as
        it is."""
        url = request.url
        path, _, query = url.raw_path.decode("ascii").partition("?")
        if (
            request.method != "GET"
            or url.scheme != "https"
            or url.host != HOST
            or url.port not in (None, 443)
            or not path.startswith(API_PREFIX + "/")
            or request.content
        ):
            raise BankUnavailable("request_refused")
        timestamp = str(self._wall_ms())
        request.headers["Revolut-Api-Timestamp"] = timestamp
        request.headers["Revolut-Api-Signature"] = signature(
            self._signing_key, timestamp, "GET", path, query, b""
        )

    async def get(self, path: str, params: dict[str, str] | None, deadline: float) -> Any:
        """One GET under the API prefix, its answer parsed. `BudgetSpent` when the budget
        runs out first -- waiting for a slot, sending, receiving, or by the time the answer
        is in: the whole call is cancelled at the deadline. `BankUnavailable(category)` on
        any failure."""
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise BudgetSpent
        try:
            async with asyncio.timeout(remaining):
                return await self._get(path, params, deadline)
        except TimeoutError:
            raise BudgetSpent from None

    async def _get(self, path: str, params: dict[str, str] | None, deadline: float) -> Any:
        delay = 0.5
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._pacer.wait(deadline)
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise BudgetSpent
            try:
                response = await self._client.get(
                    BASE_URL + path, params=params, timeout=min(REQUEST_TIMEOUT, remaining)
                )
            except httpx.HTTPError:
                if self._clock() >= deadline:
                    raise BudgetSpent from None
                if attempt == MAX_ATTEMPTS:
                    raise BankUnavailable("unreachable") from None
                await self._pause(delay, deadline)
                delay *= 2
                continue
            if self._clock() > deadline:
                raise BudgetSpent
            status = response.status_code
            if 300 <= status < 400 or response.url.host != HOST:
                raise BankUnavailable("redirect")
            if status == 200:
                try:
                    return parse(response.content)
                except (ValueError, RecursionError):
                    raise BankUnavailable("malformed_answer") from None
            if status == 401:
                raise BankUnavailable("authentication")
            if status == 403:
                raise BankUnavailable("permission")
            if status == 429 or status >= 500:
                if attempt == MAX_ATTEMPTS:
                    raise BankUnavailable("rate_limited" if status == 429 else "unreachable")
                header = response.headers.get("retry-after", "")
                await self._pause(float(header) if header.isdigit() else delay, deadline)
                delay *= 2
                continue
            raise BankUnavailable("rejected")
        raise BankUnavailable("unreachable")  # pragma: no cover -- the loop always returns

    async def _pause(self, seconds: float, deadline: float) -> None:
        if self._clock() + seconds > deadline:
            raise BudgetSpent
        await self._sleep(seconds)
