"""Reads from Stripe, as plain dicts. The only module that imports `stripe`.

`connect` checks the key, then builds the SDK's client with the API version
pinned and bounded network retries. `StripeReader` makes reads only -- it has
no method that could write -- and reads every list to its end, nested lists
included: a list cut short is `ProviderUnavailable`, never a partial answer
(FR-018). Stripe's errors become the port's, by category, never with the
platform's text (FR-020). The reader takes anything shaped like the SDK's
client, so the tests hand it an account in memory instead.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.providers.base import NotFound, ProviderUnavailable
from app.providers.stripe.keys import ProviderConfigurationError, check_read_key

#: The API version this SDK release targets (stripe 15.4.0), pinned (FR-022):
#: the mapping and its payload tests are written to it.
API_VERSION = "2026-07-29.dahlia"

#: The SDK's own retries on connection errors, rate limits and 5xx, with
#: backoff. Reads only, so retrying is safe.
NETWORK_RETRIES = 2

#: Stripe's largest page.
PAGE = 100

#: Invoice payments come with their payment intent and its latest charge:
#: what the payment mapping reads, and no deeper (plan section 4).
PAYMENT_EXPANSION = ["data.payment.payment_intent.latest_charge"]


#: The SDK's console logging levels: at these it prints to stderr.
_CONSOLE_LEVELS = {"debug", "info"}


def connect(
    api_key: str | None,
    *,
    api_base: str | None = None,
    retries: int = NETWORK_RETRIES,
    http_client: Any = None,
) -> StripeReader:
    """A reader on the account `api_key` opens, once the key is checked.

    `http_client` replaces the SDK's own, for tests that must not reach the
    network.
    """
    check_read_key(api_key)
    import stripe

    _silence(stripe)
    sdk = stripe.StripeClient(
        api_key,
        stripe_version=API_VERSION,
        max_network_retries=retries,
        base_addresses={"api": api_base} if api_base else None,
        http_client=http_client,
    )
    return StripeReader(sdk)


def _silence(stripe: Any) -> None:
    """Keep the SDK from saying what Stripe answered (FR-020, FR-024).

    Before raising, the SDK logs Stripe's error message, and in debug the
    body of every answer, to its `stripe` logger -- and prints them to stderr
    when console logging is on (`STRIPE_LOG` or `stripe.log`). The logger is
    switched off; console logging, which would bypass it, is refused.
    """
    console = {
        stripe.log,
        os.environ.get("STRIPE_LOG"),
        getattr(getattr(stripe, "_util", None), "STRIPE_LOG", None),
    }
    if console & _CONSOLE_LEVELS:
        raise ProviderConfigurationError("sdk_logging")
    logging.getLogger("stripe").disabled = True


class StripeReader:
    """The reads the adapter needs, each complete, as plain dicts."""

    def __init__(self, sdk: Any) -> None:
        self._v1 = sdk.v1

    # -- customers and test clocks ---------------------------------------

    async def test_clocks(self) -> list[dict]:
        return await self._every(self._v1.test_helpers.test_clocks.list_async)

    async def customers(self, test_clock: str | None = None) -> list[dict]:
        """Customers without a test clock, or those on `test_clock`: Stripe's
        list leaves clock customers out unless asked (FR-011)."""
        params = {"test_clock": test_clock} if test_clock else {}
        return await self._every(self._v1.customers.list_async, params=params)

    async def customer(self, customer_id: str) -> dict:
        payload = await self._call(self._v1.customers.retrieve_async, customer_id)
        if payload.get("deleted"):
            raise NotFound("no such customer on the platform")
        return payload

    # -- subscriptions -----------------------------------------------------

    async def subscriptions(self, customer_id: str) -> list[dict]:
        """Every subscription of the customer, canceled ones included."""
        found = await self._every(
            self._v1.subscriptions.list_async, params={"customer": customer_id, "status": "all"}
        )
        return [await self._with_every_item(payload) for payload in found]

    async def subscription(self, subscription_id: str) -> dict:
        payload = await self._call(self._v1.subscriptions.retrieve_async, subscription_id)
        return await self._with_every_item(payload)

    async def _with_every_item(self, payload: dict) -> dict:
        if payload["items"].get("has_more"):
            items = await self._every(
                self._v1.subscription_items.list_async, params={"subscription": payload["id"]}
            )
            payload["items"] = {**payload["items"], "data": items, "has_more": False}
        return payload

    # -- invoices and payments -------------------------------------------------

    async def invoices(self, customer_id: str) -> list[dict]:
        found = await self._every(self._v1.invoices.list_async, params={"customer": customer_id})
        return [await self._with_every_line(payload) for payload in found]

    async def invoice(self, invoice_id: str, *, every_line: bool = True) -> dict:
        """The invoice; its lines complete unless only the invoice itself is
        wanted (`every_line=False`, for its owner)."""
        payload = await self._call(self._v1.invoices.retrieve_async, invoice_id)
        return await self._with_every_line(payload) if every_line else payload

    async def _with_every_line(self, payload: dict) -> dict:
        if payload["lines"].get("has_more"):
            lines = await self._every(self._v1.invoices.line_items.list_async, payload["id"])
            payload["lines"] = {**payload["lines"], "data": lines, "has_more": False}
        return payload

    async def invoice_payments(self, invoice_id: str) -> list[dict]:
        return await self._every(
            self._v1.invoice_payments.list_async,
            params={"invoice": invoice_id, "expand": PAYMENT_EXPANSION},
        )

    # -- prices and meters -------------------------------------------------

    async def price(self, price_id: str) -> dict:
        return await self._call(self._v1.prices.retrieve_async, price_id)

    async def meter(self, meter_id: str) -> dict:
        return await self._call(self._v1.billing.meters.retrieve_async, meter_id)

    async def meter_summaries(
        self, meter_id: str, customer_id: str, start: int, end: int
    ) -> list[dict]:
        """The meter's event summaries for the customer, `start` inclusive,
        `end` exclusive, both on whole minutes."""
        return await self._every(
            self._v1.billing.meters.event_summaries.list_async,
            meter_id,
            params={"customer": customer_id, "start_time": start, "end_time": end},
        )

    # -- the two primitives ------------------------------------------------

    async def _every(self, method, *args: Any, params: dict | None = None) -> list[dict]:
        """Every object of a list, page after page, to the end."""
        params = {**(params or {}), "limit": PAGE}
        found: list[dict] = []
        while True:
            page = await self._call(method, *args, params=params)
            found.extend(page["data"])
            if not page.get("has_more"):
                return found
            if not page["data"]:
                # More announced and nothing given: the list cannot be finished.
                raise ProviderUnavailable("stripe: a list could not be read to its end")
            params = {**params, "starting_after": page["data"][-1]["id"]}

    async def _call(self, method, *args: Any, **kwargs: Any) -> dict:
        try:
            response = await method(*args, **kwargs)
        except Exception as error:
            translated = _translated(error)
            if translated is None:
                raise
            # `from None`: the platform's message stays out of every traceback.
            raise translated from None
        return response.to_dict() if hasattr(response, "to_dict") else dict(response)


def _translated(error: Exception) -> Exception | None:
    """The port's error for a Stripe one, by category; None for anything else.

    Recognised by the SDK's class names, so the error mapping is tested in CI
    without the SDK (and pinned to the real classes where it is installed).
    """
    names = {cls.__name__ for cls in type(error).__mro__}
    if "StripeError" not in names:
        return None
    if "AuthenticationError" in names:
        return ProviderConfigurationError("authentication")
    if "PermissionError" in names:
        return ProviderConfigurationError("permission")
    if "InvalidRequestError" in names:
        if getattr(error, "code", None) == "resource_missing":
            return NotFound("no such object on the platform")
        # A request this adapter should not have built: our defect, not an outage.
        return ProviderConfigurationError("invalid_request")
    if "RateLimitError" in names:
        return ProviderUnavailable("stripe: rate limited")
    if "APIConnectionError" in names:
        return ProviderUnavailable("stripe: connection failed")
    return ProviderUnavailable("stripe: platform error")
