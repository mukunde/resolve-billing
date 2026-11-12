"""The Pennylane seed against a stub API: the guard, recovery after a lost
answer, the subscription's settings and stop (specs/014-pennylane-read, FR-011)."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from scripts import pennylane_seed as seed
from scripts import pennylane_worlds as worlds

SANDBOX = 282871
TODAY = date(2026, 11, 12)


class Api:
    """Enough of Pennylane to seed on: objects kept in memory, writes counted."""

    def __init__(self, company_id=SANDBOX):
        self.company_id = company_id
        self.customers: list[dict] = []
        self.invoices: list[dict] = []
        self.subscriptions: list[dict] = []
        self.payments: dict[int, list[dict]] = {}
        self.writes: list[str] = []
        self.lose_next: set[str] = set()  # write paths whose answer is lost once
        self.next_id = 100

    def _id(self):
        self.next_id += 1
        return self.next_id

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/external/v2")
        params = request.url.params
        rules = json.loads(params["filter"]) if "filter" in params else []

        def page(items):
            for rule in rules:
                f, v = rule["field"], rule["value"]
                if f == "customer_id":
                    items = [i for i in items if (i.get("customer") or {}).get("id") == v]
                elif f == "external_reference":
                    items = [i for i in items if i.get("external_reference") == v]
                elif f == "billing_subscription_id":
                    items = [i for i in items
                             if (i.get("billing_subscription") or {}).get("id") == v]
            return httpx.Response(200, json={"items": items, "has_more": False,
                                             "next_cursor": None})

        if request.method == "GET":
            if path == "/me":
                return httpx.Response(200, json={"company": {"id": self.company_id}})
            if path == "/customers":
                return page(self.customers)
            if path == "/customer_invoices":
                return page(self.invoices)
            if path == "/billing_subscriptions":
                return page(self.subscriptions)
            if path.endswith("/payments"):
                return page(self.payments.get(int(path.split("/")[2]), []))
            if path.startswith("/customer_invoices/"):
                found = next(i for i in self.invoices if i["id"] == int(path.split("/")[2]))
                return httpx.Response(200, json=found)
            if path.startswith("/billing_subscriptions/"):
                found = next(s for s in self.subscriptions if s["id"] == int(path.split("/")[2]))
                return httpx.Response(200, json=found)
            return httpx.Response(404)

        self.writes.append(f"{request.method} {path}")
        body = json.loads(request.content) if request.content else {}
        made = self._write(request.method, path, body)
        if path in self.lose_next:
            self.lose_next.discard(path)
            return httpx.Response(504)
        return made

    def _write(self, method, path, body):
        if path == "/company_customers":
            customer = {"id": self._id(), "emails": body["emails"],
                        "external_reference": body["external_reference"]}
            self.customers.append(customer)
            return httpx.Response(201, json=customer)
        if path == "/customer_invoices":
            if any(i["external_reference"] == body["external_reference"] for i in self.invoices):
                return httpx.Response(422)
            invoice = {"id": self._id(), "external_reference": body["external_reference"],
                       "customer": {"id": body["customer_id"]}, "paid": False,
                       "credited_invoice": None}
            self.invoices.append(invoice)
            return httpx.Response(201, json=invoice)
        if path.endswith("/mark_as_paid"):
            invoice_id = int(path.split("/")[2])
            next(i for i in self.invoices if i["id"] == invoice_id)["paid"] = True
            self.payments[invoice_id] = [{"id": self._id(), "status": "confirmed"}]
            return httpx.Response(204)
        if path.endswith("/link_credit_note"):
            note = next(i for i in self.invoices if i["id"] == body["credit_note_id"])
            note["credited_invoice"] = {"id": int(path.split("/")[2])}
            return httpx.Response(200, json=note)
        if path == "/billing_subscriptions":
            sub = {"id": self._id(), "customer": {"id": body["customer_id"]},
                   "status": "in_progress", "mode": body["mode"],
                   "payment_method": body["payment_method"], "stopped_at": None,
                   "next_occurrence": "2026-12-12", "finish": None, "sent": body}
            self.subscriptions.append(sub)
            return httpx.Response(201, json=sub)
        if path.startswith("/billing_subscriptions/") and body.get("stop"):
            sub = next(s for s in self.subscriptions if s["id"] == int(path.split("/")[2]))
            sub.update(status="stopped", stopped_at="2026-11-12T10:00:00Z")
            return httpx.Response(200, json=sub)
        return httpx.Response(400)


class NoPacer:
    """No pacing in tests: the pacer has its own tests."""

    async def wait(self):
        return None


async def _no_sleep(_):
    return None


def writer(api):
    return seed.PennylaneWriter("seed-token", SANDBOX, transport=httpx.MockTransport(api.handle),
                                pacer=NoPacer(), sleep=_no_sleep)


async def run(api, tmp_path):
    return await seed.run(writer(api), seed.Intents(tmp_path / "intents.json"), TODAY)


async def test_another_company_writes_nothing(tmp_path):
    api = Api(company_id=1)
    lines = await run(api, tmp_path)
    assert lines == ["stopped before any write: sandbox"]
    assert api.writes == []


async def test_every_world_is_built_or_reported(tmp_path):
    api = Api()
    lines = await run(api, tmp_path)
    names = [line.split(":")[0] for line in lines if not line.startswith(("paid_invoice: pay",
                                                                          "subscription: gen"))]
    assert names == [w.name for w in worlds.WORLDS]
    assert any(line.startswith("taxed_duplicate: REPORTED") for line in lines)


async def test_a_second_run_writes_nothing_more(tmp_path):
    api = Api()
    await run(api, tmp_path)
    before = list(api.writes)
    await run(api, tmp_path)
    assert api.writes == before


async def test_a_lost_create_answer_stops_without_writing_twice(tmp_path):
    api = Api()
    api.lose_next.add("/company_customers")
    first = await run(api, tmp_path)
    assert first[0].startswith("taxed_invoice: REPORTED -- a write's outcome is unknown")
    customers = len(api.customers)
    second = await run(api, tmp_path)
    # The customer did land: found by its reference, reused, never made again.
    assert second[0].startswith("taxed_invoice: built")
    assert len([c for c in api.customers
                if c["external_reference"] == "rb:taxed_invoice"]) == 1
    assert len(api.customers) >= customers


async def test_a_lost_create_that_did_not_land_stops(tmp_path):
    api = Api()
    intents = seed.Intents(tmp_path / "intents.json")
    intents.note("taxed_invoice/customer")  # an earlier attempt, outcome unknown
    lines = await seed.run(writer(api), intents, TODAY)
    assert "an earlier create's outcome is unknown" in lines[0]
    assert not any(c["external_reference"] == "rb:taxed_invoice" for c in api.customers)


async def test_a_reference_already_used_is_read_as_made(tmp_path):
    api = Api()
    api.lose_next.add("/customer_invoices")
    await run(api, tmp_path)
    await run(api, tmp_path)
    refs = [i["external_reference"] for i in api.invoices]
    assert len(refs) == len(set(refs))


async def test_the_subscription_sends_no_mail_pays_offline_and_is_stopped(tmp_path):
    api = Api()
    lines = await run(api, tmp_path)
    (sub,) = api.subscriptions
    assert sub["sent"]["mode"] == {"type": "awaiting_validation"}
    assert sub["sent"]["payment_method"] == "offline"
    assert sub["status"] == "stopped"
    assert any(line.startswith("subscription: generated") for line in lines)


async def test_the_payment_observed_after_mark_as_paid_is_reported(tmp_path):
    lines = await run(Api(), tmp_path)
    assert "paid_invoice: payments after mark_as_paid: ['confirmed']" in lines


def test_every_refused_world_names_a_reason_of_the_closed_set():
    from app.providers.base import UNSUPPORTED_REASONS

    for world in worlds.WORLDS:
        if world.kind == "refused":
            assert world.reason in UNSUPPORTED_REASONS


@pytest.mark.parametrize("world", [w for w in worlds.WORLDS if w.kind == "compared"])
def test_every_compared_world_names_a_development_case(world):
    from evals.engines import DEVELOPMENT_SET

    cases = {c["id"] for c in json.loads(DEVELOPMENT_SET.read_bytes())}
    assert world.counterpart in cases
