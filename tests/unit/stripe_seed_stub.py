"""A Stripe test account in memory, for the seed script.

It stores what the seed creates, lists it back filtered by the parameters the
seed uses (as Stripe filters them), updates it, and advances test clocks. It
does not simulate what Stripe does on its own -- invoicing a subscription,
collecting a payment, aggregating usage: the seed's machinery is tested here
with worlds of the tests' own, and the real worlds by the maintainer's run
(T032) and the contract tests.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
from typing import Any

from tests.unit.stripe_stub import StripeError, StripeObject, missing


class IdempotencyError(StripeError):
    """One idempotency key, sent again with other parameters: refused, as
    Stripe refuses it."""


#: The services the seed uses, and the prefix of the identifiers they make.
SERVICES = {
    "v1.test_helpers.test_clocks": "clock",
    "v1.customers": "cus",
    "v1.payment_methods": "pm",
    "v1.products": "prod",
    "v1.prices": "price",
    "v1.billing.meters": "mtr",
    "v1.billing.meter_events": "mevt",
    "v1.coupons": "coupon",
    "v1.tax_rates": "txr",
    "v1.subscriptions": "sub",
    "v1.subscription_items": "si",
    "v1.invoice_items": "ii",
    "v1.invoices": "in",
    "v1.payment_intents": "pi",
    "v1.refunds": "re",
    "v1.billing.credit_grants": "credgr",
}

#: List parameters that filter on the field of the same name.
FILTERS = ("customer", "test_clock", "subscription", "payment_intent", "invoice")

#: Methods that change the account, for "made nothing" checks.
WRITES = (
    "create_async",
    "update_async",
    "advance_async",
    "attach_async",
    "cancel_async",
    "finalize_invoice_async",
    "pay_async",
)

#: The last four digits Stripe's test cards leave on a payment method.
LAST4 = {"pm_card_visa": "4242", "pm_card_chargeCustomerFail": "0341"}


class Account:
    def __init__(self, page_size: int = 2) -> None:
        self.page_size = page_size
        self.objects: dict[str, list[dict]] = {service: [] for service in SERVICES}
        self.calls: list[tuple[str, tuple, dict]] = []
        #: The options of each call, in the same order (idempotency keys).
        self.options: list[dict] = []
        #: What each idempotency key was sent with, and what it answered: the
        #: same key makes the object once, and returns that first answer.
        self.idempotent: dict[str, tuple[tuple, dict]] = {}
        #: Lists that announce more and give nothing.
        self.truncated: set[str] = set()
        #: What a clock's status is once it has been advanced.
        self.clock_status = "ready"
        #: A method that fails once, then answers.
        self.fail_once: dict[str, Exception] = {}
        #: Meter summaries by (meter id, customer id).
        self.summaries: dict[tuple[str, str], list[dict]] = {}
        self._ids = itertools.count(1)

    def forget_clock(self, clock_id: str) -> None:
        """A clock deleted, as Stripe deletes it: with its customers."""
        clocks = self.objects["v1.test_helpers.test_clocks"]
        clocks[:] = [c for c in clocks if c["id"] != clock_id]
        customers = self.objects["v1.customers"]
        customers[:] = [c for c in customers if c.get("test_clock") != clock_id]

    def writes(self) -> list[tuple[str, tuple, dict]]:
        return [call for call in self.calls if call[0].endswith(WRITES)]

    def tagged(self, step: str) -> list[dict]:
        return [
            o
            for objects in self.objects.values()
            for o in objects
            if (o.get("metadata") or {}).get("rb_step") == step
        ]

    async def serve(self, path: str, *args: Any, params: dict | None = None, options=None):
        await asyncio.sleep(0)  # a real request yields: two runs at once interleave
        params = copy.deepcopy(params or {})
        self.calls.append((path, args, params))
        self.options.append(dict(options or {}))
        if path in self.fail_once:
            raise self.fail_once.pop(path)
        if path in self.truncated:
            return StripeObject({"object": "list", "data": [], "has_more": True})
        key = (options or {}).get("idempotency_key")
        sent = (path, args, copy.deepcopy(params))
        if key is not None and key in self.idempotent:
            first, answer = self.idempotent[key]
            if first != sent:
                raise IdempotencyError("SENTINEL key reused", code="idempotency_key_in_use")
            return StripeObject(copy.deepcopy(answer))
        service, method = path.rsplit(".", 1)
        if service == "v1.billing.meters.event_summaries":
            return StripeObject(
                self._page(self.summaries.get((args[0], params["customer"]), []), params)
            )
        if service not in SERVICES:
            raise AssertionError(f"the seed called {path}, which this account does not serve")
        handler = getattr(self, "_" + method.removesuffix("_async"), None)
        if handler is None:
            raise AssertionError(f"the seed called {path}, which this account does not serve")
        answer = handler(service, *args, params=params)
        if key is not None:
            self.idempotent[key] = (sent, copy.deepcopy(answer))
        return StripeObject(copy.deepcopy(answer))

    # -- the methods -----------------------------------------------------------

    def _create(self, service, params):
        made = {
            "id": params.pop("id", None) or f"{SERVICES[service]}_{next(self._ids):04d}",
            "metadata": {},
            **params,
        }
        if service == "v1.test_helpers.test_clocks":
            made["status"] = "ready"
        if service == "v1.invoices":
            made.setdefault("status", "draft")
        if service == "v1.billing.meter_events":
            self._aggregate(made)
        self.objects[service].append(made)
        return made

    def _aggregate(self, event):
        """What Stripe does later, done at once: the event in its meter's summary."""
        meter = next(
            m for m in self.objects["v1.billing.meters"] if m["event_name"] == event["event_name"]
        )
        customer = event["payload"]["stripe_customer_id"]
        summaries = self.summaries.setdefault((meter["id"], customer), [])
        summaries.append({"aggregated_value": float(event["payload"]["value"])})

    def _list(self, service, params):
        found = self.objects[service]
        for name in FILTERS:
            if name in params:
                found = [o for o in found if o.get(name) == params[name]]
        if "lookup_keys" in params:
            found = [o for o in found if o.get("lookup_key") in params["lookup_keys"]]
        return self._page(found, params)

    def _retrieve(self, service, identifier, params):
        found = next((o for o in self.objects[service] if o["id"] == identifier), None)
        if found is None:
            raise missing(service)
        return found

    def _update(self, service, identifier, params):
        found = self._retrieve(service, identifier, params)
        metadata = {**found.get("metadata", {}), **params.pop("metadata", {})}
        found.update(params, metadata=metadata)
        return found

    def _advance(self, service, identifier, params):
        found = self._retrieve(service, identifier, params)
        found.update(frozen_time=params["frozen_time"], status=self.clock_status)
        return found

    def _attach(self, service, token, params):
        card = {"last4": LAST4[token]}
        return self._create(service, {"card": card, "customer": params["customer"]})

    def _finalize_invoice(self, service, identifier, params):
        return self._update(service, identifier, {"status": "open"})

    def _pay(self, service, identifier, params):
        return self._update(service, identifier, {"status": "paid"})

    def _page(self, objects, params):
        start = 0
        if params.get("starting_after"):
            start = (
                next(i for i, o in enumerate(objects) if o["id"] == params["starting_after"]) + 1
            )
        data = objects[start : start + self.page_size]
        more = start + self.page_size < len(objects)
        return {"object": "list", "data": copy.deepcopy(data), "has_more": more}


class _Node:
    def __init__(self, account: Account, path: str) -> None:
        self._account = account
        self._path = path

    def __getattr__(self, name: str):
        path = f"{self._path}.{name}"
        if name.endswith("_async"):

            async def call(*args, params=None, options=None):
                return await self._account.serve(path, *args, params=params, options=options)

            return call
        return _Node(self._account, path)


class StubSdk:
    def __init__(self, account: Account) -> None:
        self.v1 = _Node(account, "v1")
