"""A Stripe test account in memory, for the seed script.

It stores what the seed creates, lists it back filtered by the parameters the
seed uses (as Stripe filters them), updates it, and advances test clocks. It
does not simulate what Stripe does on its own -- invoicing a subscription,
collecting a payment, aggregating usage: the seed's machinery is tested here
with worlds of the tests' own, and the real worlds by the maintainer's run
(T032) and the contract tests.
"""

from __future__ import annotations

import copy
import itertools
from typing import Any

from tests.unit.stripe_stub import StripeObject, missing

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
WRITES = ("create_async", "update_async", "advance_async", "attach_async", "cancel_async")


class Account:
    def __init__(self, page_size: int = 2) -> None:
        self.page_size = page_size
        self.objects: dict[str, list[dict]] = {service: [] for service in SERVICES}
        self.calls: list[tuple[str, tuple, dict]] = []
        #: What a clock's status is once it has been advanced.
        self.clock_status = "ready"
        #: A method that fails once, then answers.
        self.fail_once: dict[str, Exception] = {}
        #: Meter summaries by (meter id, customer id).
        self.summaries: dict[tuple[str, str], list[dict]] = {}
        self._ids = itertools.count(1)

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
        params = copy.deepcopy(params or {})
        self.calls.append((path, args, params))
        if path in self.fail_once:
            raise self.fail_once.pop(path)
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
        return StripeObject(handler(service, *args, params=params))

    # -- the methods -----------------------------------------------------------

    def _create(self, service, params):
        made = {
            "id": params.pop("id", None) or f"{SERVICES[service]}_{next(self._ids):04d}",
            "metadata": {},
            **params,
        }
        if service == "v1.test_helpers.test_clocks":
            made["status"] = "ready"
        self.objects[service].append(made)
        return made

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
        return self._create(service, {"card": token, "customer": params["customer"]})

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
