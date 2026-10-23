"""A Stripe account in memory, behind the SDK's own read methods.

The adapter's client is handed this in place of `stripe.StripeClient`. It
answers only the reads the adapter may make, with the SDK's parameter names
(pinned against the real SDK in test_stripe_adapter when it is installed);
any other method -- a create, an update, anything -- fails the test, except
the one write a writable account takes: a credit note
(specs/002-stripe-writes, FR-016). Lists
are served a few objects per page, and nested lists cut short, so a client
that does not read to the end is caught. Responses are objects with
`to_dict()`, like the SDK's.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from typing import Any

#: The reads the adapter may make, and their parameters as the SDK names them.
READS: dict[str, tuple[str, ...]] = {
    "v1.test_helpers.test_clocks.list_async": ("params", "options"),
    "v1.customers.list_async": ("params", "options"),
    "v1.customers.retrieve_async": ("customer", "params", "options"),
    "v1.subscriptions.list_async": ("params", "options"),
    "v1.subscriptions.retrieve_async": ("subscription_exposed_id", "params", "options"),
    "v1.subscription_items.list_async": ("params", "options"),
    "v1.invoices.list_async": ("params", "options"),
    "v1.invoices.retrieve_async": ("invoice", "params", "options"),
    "v1.invoices.line_items.list_async": ("invoice", "params", "options"),
    "v1.invoice_payments.list_async": ("params", "options"),
    "v1.prices.retrieve_async": ("price", "params", "options"),
    "v1.billing.meters.retrieve_async": ("id", "params", "options"),
    "v1.billing.meters.event_summaries.list_async": ("id", "params", "options"),
    "v1.credit_notes.list_async": ("params", "options"),
}

#: The one write, answered only by an account built writable.
WRITES: dict[str, tuple[str, ...]] = {
    "v1.credit_notes.create_async": ("params", "options"),
}

REFUND_EXPANSION = "refunds.refund"

PAYMENT_EXPANSION = "data.payment.payment_intent.latest_charge"


# Errors named as the SDK names them: the client recognises Stripe's errors
# by their class names, so the free CI job needs no SDK to test the mapping.
class StripeError(Exception):
    def __init__(
        self, message: str = "", code: str | None = None, http_status: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class APIConnectionError(StripeError): ...


class RateLimitError(StripeError): ...


class APIError(StripeError): ...


class AuthenticationError(StripeError): ...


class PermissionError(StripeError): ...  # noqa: A001 -- the SDK's own name


class InvalidRequestError(StripeError): ...


class IdempotencyError(StripeError): ...


def missing(what: str) -> InvalidRequestError:
    return InvalidRequestError(f"No such {what}: SENTINEL-platform-text", code="resource_missing")


class StripeObject:
    def __init__(self, data: dict) -> None:
        self._data = data

    def to_dict(self) -> dict:
        return copy.deepcopy(self._data)


class Account:
    """What the account holds, how it serves it, and every call made to it."""

    def __init__(self, page_size: int = 2, embedded: int = 1, writable: bool = False) -> None:
        self.page_size = page_size
        self.embedded = embedded
        self.writable = writable
        self.credit_notes: list[dict] = []
        #: (method, options) for every call: what was sent beside the params.
        self.options: list[tuple[str, dict]] = []
        self.clocks: list[dict] = []
        self.customers: list[dict] = []
        self.subscriptions: list[dict] = []
        self.invoices: list[dict] = []
        self.invoice_payments: list[dict] = []
        self.prices: dict[str, dict] = {}
        self.meters: dict[str, dict] = {}
        #: (customer id, summary): summaries are read per customer.
        self.summaries: list[tuple[str, dict]] = []
        self.calls: list[tuple[str, tuple, dict]] = []
        #: A method that raises instead of answering.
        self.failures: dict[str, Exception] = {}
        #: A method that answers this, whatever it is asked.
        self.answers: dict[str, dict] = {}
        #: Seconds every call takes.
        self.delay = 0.0

    def calls_to(self, method: str) -> list[tuple[tuple, dict]]:
        return [(args, params) for name, args, params in self.calls if name == method]

    # -- serving -------------------------------------------------------------

    async def serve(self, method: str, *args: Any, **kwargs: Any) -> StripeObject:
        names = READS.get(method) or WRITES[method]
        bound = inspect.Signature(
            [
                inspect.Parameter(
                    name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=None if name in ("params", "options") else inspect.Parameter.empty,
                )
                for name in names
            ]
        ).bind(*args, **kwargs)
        positional = tuple(bound.arguments.get(n) for n in names if n not in ("params", "options"))
        params = dict(bound.arguments.get("params") or {})
        self.calls.append((method, positional, params))
        self.options.append((method, dict(bound.arguments.get("options") or {})))
        if self.delay:
            await asyncio.sleep(self.delay)
        if method in self.failures:
            raise self.failures[method]
        if method in self.answers:
            return StripeObject(self.answers[method])
        handler = getattr(
            self, "_" + method.removeprefix("v1.").removesuffix("_async").replace(".", "_")
        )
        return StripeObject(handler(*positional, params))

    def _page(self, objects: list[dict], params: dict) -> dict:
        start = 0
        if params.get("starting_after"):
            start = (
                next(i for i, o in enumerate(objects) if o["id"] == params["starting_after"]) + 1
            )
        size = min(params.get("limit", 10), self.page_size)
        data = objects[start : start + size]
        return {
            "object": "list",
            "data": data,
            "has_more": start + size < len(objects),
            "url": "/v1",
        }

    def _cut(self, nested: dict) -> dict:
        data = nested["data"]
        return {**nested, "data": data[: self.embedded], "has_more": len(data) > self.embedded}

    def _subscription(self, payload: dict) -> dict:
        return {**payload, "items": self._cut(payload["items"])}

    def _invoice(self, payload: dict) -> dict:
        return {**payload, "lines": self._cut(payload["lines"])}

    def _test_helpers_test_clocks_list(self, params):
        return self._page(self.clocks, params)

    def _customers_list(self, params):
        clock = params.get("test_clock")
        return self._page([c for c in self.customers if c.get("test_clock") == clock], params)

    def _customers_retrieve(self, customer_id, params):
        found = next((c for c in self.customers if c["id"] == customer_id), None)
        if found is None:
            raise missing("customer")
        return found

    def _subscriptions_list(self, params):
        found = [s for s in self.subscriptions if s["customer"] == params.get("customer")]
        if params.get("status") != "all":
            # Stripe leaves canceled subscriptions out unless asked.
            found = [s for s in found if s["status"] != "canceled"]
        return self._page([self._subscription(s) for s in found], params)

    def _subscriptions_retrieve(self, subscription_id, params):
        found = next((s for s in self.subscriptions if s["id"] == subscription_id), None)
        if found is None:
            raise missing("subscription")
        return self._subscription(found)

    def _subscription_items_list(self, params):
        found = next(s for s in self.subscriptions if s["id"] == params["subscription"])
        return self._page(found["items"]["data"], params)

    def _invoices_list(self, params):
        found = [i for i in self.invoices if i["customer"] == params.get("customer")]
        return self._page([self._invoice(i) for i in found], params)

    def _invoices_retrieve(self, invoice_id, params):
        found = next((i for i in self.invoices if i["id"] == invoice_id), None)
        if found is None:
            raise missing("invoice")
        return self._invoice(found)

    def _invoices_line_items_list(self, invoice_id, params):
        found = next(i for i in self.invoices if i["id"] == invoice_id)
        return self._page(found["lines"]["data"], params)

    def _invoice_payments_list(self, params):
        found = [p for p in self.invoice_payments if p["invoice"] == params.get("invoice")]
        if PAYMENT_EXPANSION not in params.get("expand", ()):
            found = [_unexpanded(p) for p in found]
        return self._page(found, params)

    def _prices_retrieve(self, price_id, params):
        if price_id not in self.prices:
            raise missing("price")
        return self.prices[price_id]

    def _billing_meters_retrieve(self, meter_id, params):
        if meter_id not in self.meters:
            raise missing("meter")
        return self.meters[meter_id]

    def _billing_meters_event_summaries_list(self, meter_id, params):
        found = [
            summary
            for customer_id, summary in self.summaries
            if customer_id == params["customer"]
            and summary["meter"] == meter_id
            and summary["start_time"] >= params["start_time"]
            and summary["end_time"] <= params["end_time"]
        ]
        return self._page(found, params)

    def _credit_notes_list(self, params):
        found = [cn for cn in self.credit_notes if cn["invoice"] == params.get("invoice")]
        if f"data.{REFUND_EXPANSION}" not in params.get("expand", ()):
            found = [_bare_refunds(cn) for cn in found]
        return self._page(found, params)

    def _credit_notes_create(self, params):
        """One credit note per idempotency key, as Stripe replays a key."""
        _, options = self.options[-1]
        key = options.get("idempotency_key")
        replayed = next(
            (cn for cn in self.credit_notes if cn["metadata"].get("rb_idempotency_key") == key),
            None,
        )
        made = replayed or self._credit_note_from(params)
        if replayed is None:
            self.credit_notes.append(made)
        return made if REFUND_EXPANSION in params.get("expand", ()) else _bare_refunds(made)

    def _credit_note_from(self, params: dict) -> dict:
        from tests.unit import stripe_payloads as stripe

        invoice = next(i for i in self.invoices if i["id"] == params["invoice"])
        amount = params["amount"]
        refunded = params.get("refund_amount", 0)
        after_payment = invoice["status"] == "paid"
        return stripe.credit_note(
            id=f"cn_RbMade{len(self.credit_notes) + 1:010d}",
            amount=amount,
            kind="post_payment" if after_payment else "pre_payment",
            refunds=[stripe.credit_note_refund(amount=refunded)] if refunded else [],
            metadata=dict(params.get("metadata") or {}),
            invoice=invoice["id"],
            customer=invoice["customer"],
        )


def _bare_refunds(credit_note: dict) -> dict:
    return {
        **credit_note,
        "refunds": [
            {**entry, "refund": entry["refund"]["id"]}
            if isinstance(entry["refund"], dict)
            else entry
            for entry in credit_note["refunds"]
        ],
    }


def _unexpanded(payload: dict) -> dict:
    intent = payload["payment"]["payment_intent"]
    return {**payload, "payment": {**payload["payment"], "payment_intent": intent["id"]}}


class _Node:
    def __init__(self, account: Account, path: str) -> None:
        self._account = account
        self._path = path

    def __getattr__(self, name: str):
        path = f"{self._path}.{name}"
        if name.endswith("_async") or name in ("list", "retrieve", "create", "update", "delete"):
            allowed = path in READS or (path in WRITES and self._account.writable)
            if not allowed:
                raise AssertionError(f"the adapter called {path}, which it must never call")

            async def call(*args, **kwargs):
                return await self._account.serve(path, *args, **kwargs)

            return call
        return _Node(self._account, path)


class StubSdk:
    """Stands for `stripe.StripeClient`: only `v1` and its read methods."""

    def __init__(self, account: Account) -> None:
        self.v1 = _Node(account, "v1")
