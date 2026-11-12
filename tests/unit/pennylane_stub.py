"""A Pennylane Company API v2 in memory, served through httpx's MockTransport,
for the adapter's tests without network (specs/014-pennylane-read). Shapes are
written from the reference; no real data."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from app.providers.pennylane.adapter import PennylaneBillingProvider
from app.providers.pennylane.client import PennylaneReader

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
