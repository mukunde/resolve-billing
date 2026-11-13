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
        self.eligible: list[str] = []  # e-invoicing addresses Pennylane would list

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
            if path.endswith("/electronic_billing_addresses"):
                return httpx.Response(200, json={"items": [
                    {"electronic_billing_address": a} for a in self.eligible]})
            if path.startswith("/customers/"):
                found = next(c for c in self.customers if c["id"] == int(path.split("/")[2]))
                return httpx.Response(200, json=found)
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
                       "credited_invoice": None, "draft": bool(body.get("draft")),
                       "date": body.get("date"), "lines": body.get("invoice_lines")}
            self.invoices.append(invoice)
            return httpx.Response(201, json=invoice)
        if path.endswith("/mark_as_paid"):
            invoice_id = int(path.split("/")[2])
            next(i for i in self.invoices if i["id"] == invoice_id)["paid"] = True
            self.payments[invoice_id] = [{"id": self._id(), "status": "confirmed"}]
            return httpx.Response(204)
        if path.endswith("/finalize"):
            doc = next(i for i in self.invoices if i["id"] == int(path.split("/")[2]))
            doc["draft"] = False
            return httpx.Response(200, json=doc)
        if path.endswith("/link_credit_note"):
            note = next(i for i in self.invoices if i["id"] == body["credit_note_id"])
            note["credited_invoice"] = {"id": int(path.split("/")[2])}
            return httpx.Response(200, json=note)
        if path == "/billing_subscriptions":
            sub = {"id": self._id(), "customer": {"id": body["customer_id"]},
                   "status": "in_progress", "mode": body["mode"]["type"],
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
    outcomes = [line for line in lines
                if line.split(": ", 1)[1].startswith(("built", "REPORTED", "excluded"))]
    names = [line.split(":")[0] for line in outcomes]
    assert names == [w.name for w in worlds.WORLDS]
    assert any(line.startswith("taxed_duplicate: excluded (") for line in lines)


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
    assert any(line.startswith("subscription: generated invoices") for line in lines)
    assert any(line.startswith("subscription: after stop {'status': 'stopped'") for line in lines)


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


async def test_a_subscription_with_other_settings_is_still_stopped(tmp_path):
    """r369, finding 2: a failure after the subscription exists never leaves it running."""
    api = Api()
    original = api._write

    def other_settings(method, path, body):
        response = original(method, path, body)
        if path == "/billing_subscriptions":
            api.subscriptions[-1]["payment_method"] = "gocardless_direct_debit"
            return httpx.Response(201, json=api.subscriptions[-1])
        return response

    api._write = other_settings
    lines = await run(api, tmp_path)
    assert any(line.startswith("subscription: REPORTED -- subscription: its settings")
               for line in lines)
    assert api.subscriptions[-1]["status"] == "stopped"


async def test_a_lost_stop_answer_is_never_resent_blind(tmp_path):
    """r369, finding 3: the stop's intent kept; a later run reads before acting."""
    api = Api()
    original = api._write

    def stop_lost(method, path, body):
        response = original(method, path, body)
        if path.startswith("/billing_subscriptions/") and body.get("stop"):
            api.subscriptions[-1]["status"] = "in_progress"  # it did not land
            return httpx.Response(504)
        return response

    api._write = stop_lost
    first = await run(api, tmp_path)
    assert any("subscription: REPORTED" in line for line in first)
    api._write = original
    second = await run(api, tmp_path)
    assert any("an earlier change's outcome is unknown" in line for line in second)


async def test_a_lost_mark_as_paid_that_landed_is_settled_by_a_read(tmp_path):
    api = Api()
    api.lose_next.add("/customer_invoices/{id}/mark_as_paid")
    original = api.handle

    def lose_mark(request):
        if request.url.path.endswith("/mark_as_paid") and api.lose_next:
            api.lose_next.clear()
            path = request.url.path.removeprefix("/api/external/v2")
            api.writes.append(f"{request.method} {path}")
            api._write(request.method, path, {})
            return httpx.Response(504)
        return original(request)

    api.handle = lose_mark
    first = await run(api, tmp_path)
    assert any(line.startswith("paid_invoice: REPORTED") for line in first)
    second = await run(api, tmp_path)
    assert any(line.startswith("paid_invoice: built") for line in second)
    assert sum(1 for w in api.writes if w.endswith("/mark_as_paid")) == 1


async def test_the_unbuildable_worlds_are_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(worlds, "NOT_SEEDED", {})
    lines = await run(Api(), tmp_path)
    for name in ("partially_paid", "charged_back", "taxed_duplicate",
                 "taxed_billed_after_cancellation"):
        assert any(line.startswith(f"{name}: REPORTED") for line in lines), name


async def test_an_approved_exclusion_is_not_attempted(tmp_path):
    api = Api()
    lines = await run(api, tmp_path)
    for name in worlds.NOT_SEEDED:
        assert any(line.startswith(f"{name}: excluded (") for line in lines), name
    assert "rb:mixed_vat" not in {c["external_reference"] for c in api.customers}


async def test_a_lost_subscription_create_that_landed_is_stopped_at_once(tmp_path):
    """r370, finding 1: a create whose answer is lost is looked for and stopped
    in the same run, not left invoicing until a later one."""
    api = Api()
    api.lose_next.add("/billing_subscriptions")
    lines = await run(api, tmp_path)
    assert any(line.startswith("subscription: REPORTED") for line in lines)
    (sub,) = api.subscriptions
    assert sub["status"] == "stopped"


async def test_a_failing_subscription_read_still_stops_it(tmp_path):
    """r370, finding 1: the first read of the made subscription fails; it is
    stopped all the same."""
    api = Api()
    original = api.handle
    failed = []

    def first_read_fails(request):
        path = request.url.path.removeprefix("/api/external/v2")
        if request.method == "GET" and path.startswith("/billing_subscriptions/") and not failed:
            failed.append(path)
            return httpx.Response(404)
        return original(request)

    api.handle = first_read_fails
    lines = await run(api, tmp_path)
    assert failed
    assert any(line.startswith("subscription: REPORTED") for line in lines)
    (sub,) = api.subscriptions
    assert sub["status"] == "stopped"


async def test_every_found_subscription_is_tried_when_one_will_not_stop(tmp_path):
    """r371: two subscriptions found (the seed cannot tell which is its own);
    the first refuses to stop, the second is stopped all the same, and the
    failure is reported."""
    api = Api()
    await run(api, tmp_path)  # builds the customer and one stopped subscription
    customer = api.subscriptions[0]["customer"]
    for _ in range(2):
        api.subscriptions.append({"id": api._id(), "customer": customer,
                                  "status": "in_progress"})
    api.subscriptions.pop(0)
    first, second = api.subscriptions
    original = api._write

    def first_refuses(method, path, body):
        if path == f"/billing_subscriptions/{first['id']}":
            return httpx.Response(400)
        return original(method, path, body)

    api._write = first_refuses
    lines = await run(api, tmp_path)
    assert any("subscription: REPORTED" in line and "several found" in line for line in lines)
    assert first["status"] == "in_progress"
    assert second["status"] == "stopped"
    assert any(line.startswith("subscription: NOT STOPPED") and str(first["id"]) in line
               for line in lines)


async def test_a_refusal_is_reported_with_pennylane_s_own_words(tmp_path):
    """Seen on the sandbox: without the invoice numbering set, every invoice is
    refused; the report says why, not only the status."""
    api = Api()
    original = api._write

    def numbering_unset(method, path, body):
        if path == "/customer_invoices":
            return httpx.Response(422, json={"error": "configure the invoice numbering"})
        return original(method, path, body)

    api._write = numbering_unset
    lines = await run(api, tmp_path)
    assert any(line.startswith("taxed_invoice: REPORTED -- Pennylane refused (422): ")
               and "configure the invoice numbering" in line for line in lines)


# -- the write contract's worlds (specs/015-pennylane-credit-notes, SC-002) -------------

async def write_run(api, tmp_path):
    return await seed.run_writes(writer(api), seed.Intents(tmp_path / "intents.json"), TODAY)


async def test_a_write_run_builds_one_generation_finalised_and_dated_the_day(tmp_path):
    api = Api()
    lines = await write_run(api, tmp_path)
    assert lines[0] == "write generation 1"
    built = {line.split(":")[0] for line in lines[1:] if ": built" in line}
    assert built == {worlds.write_world(1, n).name for n in worlds.WRITE_WORLDS}
    made = [i for i in api.invoices if i["external_reference"].startswith("rb:w1_")]
    assert len(made) == len(worlds.WRITE_WORLDS)
    assert all(i["draft"] is False and i["date"] == TODAY.isoformat() for i in made)
    paid = {i["external_reference"]: i["paid"] for i in made}
    assert paid == {f"rb:w1_{n}:invoice": p for n, (_, p) in worlds.WRITE_WORLDS.items()}
    mixed = next(i for i in made if i["external_reference"] == "rb:w1_mixed:invoice")
    assert {line["vat_rate"] for line in mixed["lines"]} == {"FR_55", "FR_200", "exempt"}


async def test_the_next_write_run_is_a_new_generation(tmp_path):
    api = Api()
    await write_run(api, tmp_path)
    lines = await write_run(api, tmp_path)
    assert lines[0] == "write generation 2"
    assert len([c for c in api.customers if c["external_reference"].startswith("rb:w2_")]) == 5


async def test_an_unfinished_generation_is_resumed_not_skipped(tmp_path):
    """Codex r391: the last world's invoice failing leaves the generation
    unfinished; the next run finishes it, and only then is it the latest."""
    api = Api()
    original = api._write

    def cap_refused(method, path, body):
        if path == "/customer_invoices" and body.get("external_reference") == "rb:w1_cap:invoice":
            return httpx.Response(400)
        return original(method, path, body)

    api._write = cap_refused
    await write_run(api, tmp_path)
    assert await seed.latest_write_generation(writer(api)) == 0
    api._write = original
    lines = await write_run(api, tmp_path)
    assert lines[0] == "write generation 1 (resumed)"
    assert await seed.latest_write_generation(writer(api)) == 1


async def test_generations_have_no_upper_bound(tmp_path):
    api = Api()
    for name in worlds.WRITE_WORLDS:
        api.customers.append({"id": api._id(), "emails": [],
                              "external_reference": f"rb:w1000_{name}"})
        api.invoices.append({"id": api._id(), "external_reference": f"rb:w1000_{name}:invoice",
                             "draft": False, "paid": True, "customer": {"id": 0}})
    lines = await write_run(api, tmp_path)
    assert lines[0] == "write generation 1001"


@pytest.mark.parametrize("malformed", [{}, "", False, None])
async def test_an_unreadable_eligibility_list_never_finalises(tmp_path, malformed):
    api = Api()
    original = api.handle

    def unreadable(request):
        if request.url.path.endswith("/electronic_billing_addresses"):
            return httpx.Response(200, json={"items": malformed})
        return original(request)

    api.handle = unreadable
    lines = await write_run(api, tmp_path)
    assert all("REPORTED -- not finalised" in line for line in lines[1:])
    assert not any(w.endswith("/finalize") for w in api.writes)


async def test_a_write_world_that_could_be_routed_out_is_never_finalised(tmp_path):
    api = Api()
    api.eligible = ["123456789"]
    lines = await write_run(api, tmp_path)
    assert all("REPORTED -- not finalised" in line for line in lines[1:])
    assert not any(w.endswith("/finalize") for w in api.writes)
