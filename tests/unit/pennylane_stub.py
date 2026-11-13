"""A Pennylane Company API v2 in memory, served through httpx's MockTransport,
for the adapter's tests without network (specs/014-pennylane-read), and the
writes of a credit note (specs/015-pennylane-credit-notes). Shapes are written
from the reference and the sandbox's observations; no real data."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from app.providers.pennylane.adapter import PennylaneBillingProvider
from app.providers.pennylane.client import PennylaneReader, PennylaneWriter

SANDBOX = 282871


def line(id_, excl="100.00", tax="20.00", code="FR_200", start="2026-09-01", end="2026-09-30"):
    total = f"{float(excl) + float(tax):.2f}"
    return {
        "id": id_, "label": "Abonnement Pro", "quantity": "1",
        "raw_currency_unit_price": excl, "currency_amount_before_tax": excl,
        "currency_tax": tax, "currency_amount": total, "vat_rate": code,
        "discount": {"type": "relative", "value": "0"},
        "imputation_dates": (
            {"start_date": start, "end_date": end} if start and end else None
        ),
    }


def invoice(id_, customer, number, day="2026-09-01", status="paid", subscription=None,
            excl="100.00", tax="20.00", credited=None):
    total = f"{float(excl) + float(tax):.2f}"
    return {
        "id": id_, "invoice_number": number, "customer": {"id": customer, "url": "u"},
        "date": day, "status": status, "currency": "EUR", "currency_amount": total,
        "currency_amount_before_tax": excl, "currency_tax": tax,
        "discount": {"type": "absolute", "value": "0"},
        "credited_invoice": {"id": credited, "url": "u"} if credited else None,
        "billing_subscription": {"id": subscription} if subscription else None,
    }


def subscription(id_, customer, status="in_progress", prev="2026-09-01", nxt="2026-10-01"):
    return {
        "id": id_, "status": status, "label": "Pro", "customer": {"id": customer},
        "start": "2026-01-01", "finish": None, "stopped_at": None,
        "prev_occurrence": prev, "next_occurrence": nxt,
        "recurring_rule": {"rule_type": "monthly", "interval": 1},
    }


@dataclass
class Account:
    company_id: int = SANDBOX
    customers: list[dict] = field(default_factory=list)
    invoices: list[dict] = field(default_factory=list)
    lines: dict[int, list[dict]] = field(default_factory=dict)
    payments: dict[int, list[dict]] = field(default_factory=dict)
    subscriptions: list[dict] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/external/v2")
        self.requests.append(path)
        params = request.url.params
        filters = json.loads(params["filter"]) if "filter" in params else []

        def matching(items):
            for rule in filters:
                field_, value = rule["field"], rule["value"]
                items = [i for i in items if (i.get("customer") or {}).get("id") == value] \
                    if field_ == "customer_id" else items
            return items

        def page(items):
            return httpx.Response(200, json={"items": items, "has_more": False,
                                             "next_cursor": None})

        parts = path.strip("/").split("/")
        if parts == ["me"]:
            return httpx.Response(200, json={"company": {"id": self.company_id}})
        if parts == ["customers"]:
            return page(self.customers)
        if parts[0] == "customers" and len(parts) == 2:
            return self._one(self.customers, int(parts[1]))
        if parts == ["customer_invoices"]:
            return page(matching(self.invoices))
        if parts[0] == "customer_invoices" and len(parts) == 2:
            return self._one(self.invoices, int(parts[1]))
        if parts[0] == "customer_invoices" and parts[2] == "invoice_lines":
            return page(self.lines.get(int(parts[1]), []))
        if parts[0] == "customer_invoices" and parts[2] == "payments":
            return page(self.payments.get(int(parts[1]), []))
        if parts == ["billing_subscriptions"]:
            return page(matching(self.subscriptions))
        if parts[0] == "billing_subscriptions" and len(parts) == 2:
            return self._one(self.subscriptions, int(parts[1]))
        return httpx.Response(404)

    @staticmethod
    def _one(items, id_):
        found = next((i for i in items if i["id"] == id_), None)
        return httpx.Response(200, json=found) if found else httpx.Response(404)


class NoPacer:
    """No pacing in tests: the pacer has its own tests."""

    async def wait(self):
        return None


async def _no_sleep(_seconds):
    return None


def provider(account: Account, budget: float = 5.0) -> PennylaneBillingProvider:
    reader = PennylaneReader(
        "test-token", SANDBOX, transport=httpx.MockTransport(account.handle),
        pacer=NoPacer(), sleep=_no_sleep,
    )
    return PennylaneBillingProvider(reader, budget=budget)


def duplicate_world() -> Account:
    """Two invoices of one subscription, same period, 100.00 + 20 % each."""
    return Account(
        customers=[{"id": 7, "name": "Vega Conseil", "emails": ["lea@vega-conseil.fr"]}],
        invoices=[invoice(600, 7, "F20260600", subscription=9),
                  invoice(601, 7, "F20260601", day="2026-09-02", subscription=9)],
        lines={600: [line(1)], 601: [line(2)]},
        payments={600: [{"id": 1, "status": "confirmed", "currency": "EUR",
                         "currency_amount": "120.00"}],
                  601: [{"id": 2, "status": "confirmed", "currency": "EUR",
                         "currency_amount": "120.00"}]},
        subscriptions=[subscription(9, 7)],
    )


# -- writes (specs/015-pennylane-credit-notes) -------------------------------------------

#: Pennylane's words, as seen on the sandbox on 2026-11-12.
CAP_REFUSAL = "The credit notes amount is bigger than the invoice amount."
TAKEN_REFUSAL = "External reference has already been taken"
RATES = {"FR_200": Decimal(20), "FR_100": Decimal(10), "FR_55": Decimal("5.5"),
         "FR_21": Decimal("2.1"), "exempt": Decimal(0)}


def _cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Lost(Exception):
    """The answer never reaches the client."""


@dataclass
class WritingAccount(Account):
    """The account, plus the writes a credit note takes, as the sandbox showed
    them: a draft created with negative lines, linked under the cumulative cap,
    finalised to `cancelled`, deleted while a draft. `faults` makes a step
    fail: {"create" | "link" | "finalize" | "delete": "lost" (applied, answer
    lost), "lost_before" (not applied), "ack_only" (answered, not applied),
    (status, text) (refused)}; `read_faults` makes a GET of a path fail."""

    tokens: dict[str, int] = field(default_factory=dict)
    faults: dict[str, Any] = field(default_factory=dict)
    read_faults: dict[str, int] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    sent: list[tuple[str, str, Any]] = field(default_factory=list)
    next_id: int = 9000
    #: Called on each request, before it is served (a late answer, a change).
    on_request: Callable[[str, str], None] | None = None
    #: How the draft's tax is computed: "line" (per line, rounded) by default.
    draft_change: Callable[[dict, list[dict]], None] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/external/v2")
        self.calls.append((request.method, path))
        if self.on_request is not None:
            self.on_request(request.method, path)
        if path == "/me":
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            self.requests.append(path)
            return httpx.Response(200, json={"company": {"id": self.tokens.get(
                token, self.company_id)}})
        if request.method == "GET":
            if path in self.read_faults:
                self.requests.append(path)
                return httpx.Response(self.read_faults[path])
            if path.endswith("/electronic_billing_addresses"):
                self.requests.append(path)
                items = [{"electronic_billing_address": a} for a in getattr(self, "eligible", [])]
                return httpx.Response(200, json={"items": items, "has_more": False,
                                                 "next_cursor": None})
            return self._read(request)
        self.requests.append(path)
        parts = path.strip("/").split("/")
        body = json.loads(request.content) if request.content else None
        self.sent.append((request.method, path, body))
        if request.method == "POST" and parts == ["customer_invoices"]:
            return self._fault("create", lambda: self._create(body))
        if request.method == "POST" and parts[-1] == "link_credit_note":
            return self._fault("link", lambda: self._link(int(parts[1]), body))
        if request.method == "PUT" and parts[-1] == "finalize":
            return self._fault("finalize", lambda: self._finalize(int(parts[1])))
        if request.method == "DELETE" and len(parts) == 2:
            return self._fault("delete", lambda: self._delete(int(parts[1])))
        return httpx.Response(404)

    def _read(self, request: httpx.Request) -> httpx.Response:
        return Account.handle(self, request)

    def _fault(self, step: str, apply: Callable[[], httpx.Response]) -> httpx.Response:
        fault = self.faults.get(step)
        if fault == "lost_before":
            raise httpx.ConnectError("lost")
        if fault == "ack_only":
            return httpx.Response(204)
        if isinstance(fault, tuple):
            return httpx.Response(fault[0], text=fault[1])
        answer = apply()
        if fault == "lost":
            raise httpx.ReadTimeout("lost")
        if fault == "unreadable":
            return httpx.Response(200, text="not json")
        if isinstance(fault, list) and fault and fault[0] == "answer":
            return httpx.Response(201, json=fault[1])
        return answer

    def document(self, id_: int) -> dict | None:
        return next((d for d in self.invoices if d["id"] == id_), None)

    def _create(self, body: dict) -> httpx.Response:
        reference = body.get("external_reference")
        if reference and any(d.get("external_reference") == reference for d in self.invoices):
            return httpx.Response(422, text=TAKEN_REFUSAL)
        self.next_id += 1
        id_ = self.next_id
        lines, excl_total, tax_total = [], Decimal(0), Decimal(0)
        for index, sent in enumerate(body["invoice_lines"]):
            excl = _cents(Decimal(sent["quantity"]) * Decimal(sent["raw_currency_unit_price"]))
            tax = _cents(excl * RATES[sent["vat_rate"]] / 100)
            excl_total += excl
            tax_total += tax
            lines.append({
                "id": id_ * 100 + index, "label": sent["label"], "quantity": sent["quantity"],
                "unit": sent.get("unit"), "imputation_dates": None,
                "raw_currency_unit_price": sent["raw_currency_unit_price"],
                "currency_amount_before_tax": f"{excl:.2f}", "currency_tax": f"{tax:.2f}",
                "currency_amount": f"{excl + tax:.2f}", "vat_rate": sent["vat_rate"],
                "discount": None,
            })
        document = {
            # As Pennylane answered on the sandbox: a draft credit note reads
            # status `credit_note` and an empty number (spec 015's run, 2026-11-13).
            "id": id_, "invoice_number": "", "customer": {"id": body["customer_id"]},
            "date": body["date"], "status": "credit_note", "draft": True,
            "currency": body["currency"], "currency_amount": f"{excl_total + tax_total:.2f}",
            "currency_amount_before_tax": f"{excl_total:.2f}",
            "currency_tax": f"{tax_total:.2f}", "discount": None, "credited_invoice": None,
            "billing_subscription": None, "external_reference": reference,
            "electronic_billing_address": None, "e_invoicing": None,
        }
        if self.draft_change is not None:
            self.draft_change(document, lines)
        self.invoices.append(document)
        self.lines[id_] = lines
        return httpx.Response(201, json={"id": id_, "draft": True})

    def _link(self, invoice_id: int, body: dict) -> httpx.Response:
        invoice_ = self.document(invoice_id)
        note = self.document(body["credit_note_id"])
        if invoice_ is None or note is None:
            return httpx.Response(404)
        credited = sum(-Decimal(d["currency_amount"]) for d in self.invoices
                       if (d.get("credited_invoice") or {}).get("id") == invoice_id)
        if credited - Decimal(note["currency_amount"]) > Decimal(invoice_["currency_amount"]):
            return httpx.Response(400, text=CAP_REFUSAL)
        note["credited_invoice"] = {"id": invoice_id, "url": "u"}
        return httpx.Response(200, json={})

    def _finalize(self, id_: int) -> httpx.Response:
        note = self.document(id_)
        if note is None or not note.get("draft"):
            return httpx.Response(422, text="not a draft")
        note["draft"] = False
        note["status"] = "cancelled"
        note["invoice_number"] = f"F{id_}"
        linked = (note.get("credited_invoice") or {}).get("id")
        invoice_ = self.document(linked) if linked else None
        if invoice_ is not None and invoice_["status"] in ("upcoming", "late"):
            credited = sum(-Decimal(d["currency_amount"]) for d in self.invoices
                           if (d.get("credited_invoice") or {}).get("id") == linked
                           and not d.get("draft"))
            whole = credited >= Decimal(invoice_["currency_amount"])
            invoice_["status"] = "cancelled" if whole else "partially_cancelled"
        return httpx.Response(200, json={"id": id_})

    def _delete(self, id_: int) -> httpx.Response:
        note = self.document(id_)
        if note is None:
            return httpx.Response(404)
        if not note.get("draft"):
            return httpx.Response(422, text="not a draft")
        self.invoices.remove(note)
        self.lines.pop(id_, None)
        return httpx.Response(204)

    def written(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]


def writing_provider(account: WritingAccount, *, writes: bool = True,
                     write_token: str = "write-token") -> PennylaneBillingProvider:
    transport = httpx.MockTransport(account.handle)
    reader = PennylaneReader("test-token", SANDBOX, transport=transport, pacer=NoPacer(),
                             sleep=_no_sleep)
    writer = PennylaneWriter(write_token, SANDBOX, transport=transport, pacer=NoPacer(),
                             sleep=_no_sleep) if writes else None
    return PennylaneBillingProvider(
        reader, budget=5.0, writer=writer, write_budget=5.0,
        now=lambda: datetime(2026, 11, 12, 10, tzinfo=UTC),
    )


TEST_EMAIL = "taxed-invoice@worlds.resolve-billing.test"


def credit_world(status: str = "upcoming", lines: list[dict] | None = None,
                 customer: dict | None = None) -> WritingAccount:
    """One customer at a test address, one invoice of 100.00 + 20 %."""
    lines = lines or [line(1)]
    excl = sum(Decimal(item["currency_amount_before_tax"]) for item in lines)
    tax = sum(Decimal(item["currency_tax"]) for item in lines)
    return WritingAccount(
        customers=[customer or {"id": 7, "name": "RB taxed", "emails": [TEST_EMAIL],
                                "reg_no": None, "vat_number": None}],
        invoices=[invoice(600, 7, "F20260600", status=status, excl=f"{excl:.2f}",
                          tax=f"{tax:.2f}")],
        lines={600: lines},
    )
