"""Build the Pennylane worlds on the declared sandbox (specs/014-pennylane-read,
FR-011, SC-002 as amended).

    python -m dotenv -f .env.seed run -- python -m scripts.pennylane_seed

With its own token, `PENNYLANE_SEED_TOKEN` (read and write on Customers,
Customer invoices and Billing subscriptions), kept in `.env.seed` with
`PENNYLANE_SANDBOX_COMPANY_ID` -- never in `.env`, which the service loads.
Before any write it checks, failing closed, that the token's company is
`PENNYLANE_SANDBOX_COMPANY_ID`. Every write is preceded by a read that decides
whether it is needed; after a write whose answer was lost, the next run reads
to find out, and stops where it cannot tell -- an uncertain write is never sent
again automatically. Intents are noted in a local file before each create, so a
fresh attempt is told from an uncertain one. One line per world, never amounts,
names or payloads.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.providers.base import NotFound, ProviderUnavailable
from app.providers.pennylane.client import Pacer, PennylaneReader
from app.providers.pennylane.keys import ProviderConfigurationError, sandbox_company_id, token
from scripts import pennylane_worlds as worlds

Payload = dict[str, Any]

#: Where intents are noted between runs: outside any checkout, beside the
#: other private state of the maintainer's runs.
INTENTS = Path.home() / ".resolve-billing" / "pennylane-seed-intents.json"


class Uncertain(Exception):  # noqa: N818 -- a state: whether a write landed is unknown
    """A write was sent and its answer lost: nothing is sent again until a read
    settles it."""


class Refused(Exception):  # noqa: N818 -- Pennylane's answer: nothing was made
    def __init__(self, status: int) -> None:
        super().__init__(f"refused ({status})")
        self.status = status


class Stop(Exception):  # noqa: N818 -- the seed cannot tell, and says so
    pass


class PennylaneWriter(PennylaneReader):
    """The reader, plus one-shot writes: never retried after sending."""

    async def write(self, method: str, path: str, body: Payload | None = None) -> Payload | None:
        await self.check_company()
        await self._pacer.wait()
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
        raise Refused(response.status_code)


@dataclass
class Intents:
    """Creates noted before they are sent, cleared once a read shows them."""

    path: Path

    def _load(self) -> dict[str, bool]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def noted(self, key: str) -> bool:
        return self._load().get(key, False)

    def note(self, key: str) -> None:
        state = self._load()
        state[key] = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    def clear(self, key: str) -> None:
        state = self._load()
        if state.pop(key, None) is not None:
            self.path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def _filter(field: str, value: Any) -> str:
    return json.dumps([{"field": field, "operator": "eq", "value": value}])


def _month_before(today: date) -> tuple[date, date]:
    """The calendar month before `today`: its first and last day."""
    last = today.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


class Seeder:
    def __init__(self, writer: PennylaneWriter, intents: Intents, today: date) -> None:
        self.w = writer
        self.intents = intents
        self.today = today
        self.observations: list[str] = []

    # -- one create, safely -----------------------------------------------------

    async def once(
        self,
        key: str,
        find: Callable[[], Awaitable[list[Payload]]],
        create: Callable[[], Awaitable[Payload | None]],
        *,
        duplicate_refusal: bool = False,
    ) -> Payload:
        """Reuse what exists; create only when nothing exists and no earlier
        create is pending; stop when it cannot tell."""
        found = await find()
        if len(found) > 1:
            raise Stop(f"{key}: several found")
        if found:
            self.intents.clear(key)
            return found[0]
        if self.intents.noted(key):
            raise Stop(f"{key}: an earlier create's outcome is unknown")
        self.intents.note(key)
        try:
            made = await create()
        except Refused as refusal:
            if duplicate_refusal and refusal.status == 422:
                # Pennylane refuses a reference already used: it was made.
                again = await find()
                if len(again) == 1:
                    self.intents.clear(key)
                    return again[0]
            self.intents.clear(key)  # refused: nothing was made
            raise
        if not made:
            raise Stop(f"{key}: made, answer unreadable")
        self.intents.clear(key)
        return made

    # -- the objects ---------------------------------------------------------------

    async def customer(self, world: worlds.World, emails: list[str] | None = None) -> Payload:
        async def find():
            return await self.w.every(
                "/customers", {"filter": _filter("external_reference", world.reference)})

        async def create():
            return await self.w.write("POST", "/company_customers", {
                "name": f"RB {world.name}",
                "emails": emails or [world.email],
                "external_reference": world.reference,
                "billing_address": {"address": "1 rue des Essais", "postal_code": "75001",
                                    "city": "Paris", "country_alpha2": "FR"},
            })

        return await self.once(f"{world.name}/customer", find, create)

    async def invoice(self, world: worlds.World, customer: Payload, suffix: str = "invoice",
                      **line_changes: Any) -> Payload:
        reference = f"{world.reference}:{suffix}"
        start, end = _month_before(self.today)
        discount = line_changes.pop("invoice_discount", None)

        async def find():
            items = await self.w.every(
                "/customer_invoices", {"filter": _filter("customer_id", customer["id"])})
            return [i for i in items if i.get("external_reference") == reference]

        async def create():
            line = {"label": "Abonnement Pro", "quantity": 1, "unit": "piece",
                    "raw_currency_unit_price": "100.00", "vat_rate": "FR_200",
                    "imputation_dates": {"start_date": start.isoformat(),
                                         "end_date": end.isoformat()}}
            line.update(line_changes)
            body = {"customer_id": customer["id"], "date": start.isoformat(),
                    "deadline": (start + timedelta(days=30)).isoformat(),
                    "currency": "EUR", "external_reference": reference,
                    "invoice_lines": [line]}
            if discount:
                body["discount"] = discount
            return await self.w.write("POST", "/customer_invoices", body)

        return await self.once(f"{world.name}/{suffix}", find, create, duplicate_refusal=True)

    # -- the worlds -------------------------------------------------------------------

    async def build(self, world: worlds.World) -> str:
        builder = getattr(self, f"_{world.name}")
        return await builder(world)

    async def _taxed_invoice(self, world):
        customer = await self.customer(world)
        made = await self.invoice(world, customer)
        return f"built, invoice {made.get('id')}"

    async def _paid_invoice(self, world):
        customer = await self.customer(world)
        made = await self.invoice(world, customer)
        detail = await self.w.get(f"/customer_invoices/{made['id']}")
        if not detail.get("paid"):
            await self.w.write("PUT", f"/customer_invoices/{made['id']}/mark_as_paid")
        payments = await self.w.every(f"/customer_invoices/{made['id']}/payments")
        statuses = sorted(str(p.get("status")) for p in payments)
        self.observations.append(f"paid_invoice: payments after mark_as_paid: {statuses}")
        return f"built, invoice {made['id']}"

    async def _subscription(self, world):
        customer = await self.customer(world)

        async def find():
            return await self.w.every(
                "/billing_subscriptions", {"filter": _filter("customer_id", customer["id"])})

        async def create():
            return await self.w.write("POST", "/billing_subscriptions", {
                "customer_id": customer["id"], "start": self.today.isoformat(),
                "label": "RB subscription", "mode": {"type": "awaiting_validation"},
                "payment_conditions": "upon_receipt", "payment_method": "offline",
                "recurring_rule": {"type": "monthly", "interval": 1,
                                   "day_of_month": min(self.today.day, 28)},
                "customer_invoice_data": {"invoice_lines": [
                    {"label": "Abonnement Pro", "quantity": 1, "unit": "piece",
                     "raw_currency_unit_price": "49.00", "vat_rate": "FR_200"}]},
            })

        made = await self.once(f"{world.name}/subscription", find, create)
        settings = (made.get("mode") or {}).get("type"), made.get("payment_method")
        if settings != ("awaiting_validation", "offline"):
            raise Stop("subscription: its settings are not those sent")
        generated = await self.w.every(
            "/customer_invoices", {"filter": _filter("billing_subscription_id", made["id"])})
        if made.get("status") != "stopped":
            await self.w.write("PUT", f"/billing_subscriptions/{made['id']}", {"stop": True})
        after = await self.w.get(f"/billing_subscriptions/{made['id']}")
        self.observations.append(
            f"subscription: generated {len(generated)} invoice(s); after stop: "
            f"status={after.get('status')}, stopped_at set={after.get('stopped_at') is not None}, "
            f"next_occurrence set={after.get('next_occurrence') is not None}, "
            f"finish set={after.get('finish') is not None}")
        if after.get("status") != "stopped":
            raise Stop("subscription: not stopped")
        return f"built, subscription {made['id']}"

    async def _credited_invoice(self, world):
        customer = await self.customer(world)
        original = await self.invoice(world, customer)
        note = await self.invoice(world, customer, suffix="credit_note", quantity=-1)
        detail = await self.w.get(f"/customer_invoices/{note['id']}")
        if detail.get("credited_invoice") is None:
            await self.w.write("POST", f"/customer_invoices/{original['id']}/link_credit_note",
                               {"credit_note_id": note["id"]})
        return f"built, invoice {original['id']}"

    async def _two_addresses(self, world):
        customer = await self.customer(world, emails=[world.email, f"second-{world.email}"])
        await self.invoice(world, customer)
        return f"built, customer {customer['id']}"

    async def _mixed_vat(self, world):
        customer = await self.customer(world)
        made = await self.invoice(world, customer, vat_rate="mixed")
        return f"built, invoice {made['id']}"

    async def _invoice_discount(self, world):
        customer = await self.customer(world)
        made = await self.invoice(world, customer,
                                  invoice_discount={"type": "absolute", "value": "10.00"})
        return f"built, invoice {made['id']}"

    async def _fractional_unit_price(self, world):
        customer = await self.customer(world)
        made = await self.invoice(world, customer, raw_currency_unit_price="100.005")
        return f"built, invoice {made['id']}"

    async def _taxed_duplicate(self, world):
        raise Stop("needs two invoices of one subscription: no documented way to attach a "
                   "hand-made invoice to a subscription")

    async def _taxed_billed_after_cancellation(self, world):
        raise Stop("needs a subscription's invoice issued after its stop: not controllable "
                   "through the documented API")


async def run(writer: PennylaneWriter, intents: Intents, today: date) -> list[str]:
    seeder = Seeder(writer, intents, today)
    try:
        await writer.check_company()
    except ProviderConfigurationError as refusal:
        return [f"stopped before any write: {refusal.category}"]
    lines = []
    for world in worlds.WORLDS:
        if world.name in worlds.NOT_SEEDED:
            lines.append(f"{world.name}: excluded ({worlds.NOT_SEEDED[world.name]})")
            continue
        try:
            lines.append(f"{world.name}: {await seeder.build(world)}")
        except Refused as refusal:
            lines.append(f"{world.name}: REPORTED -- Pennylane refused ({refusal.status})")
        except Uncertain:
            lines.append(f"{world.name}: REPORTED -- a write's outcome is unknown; run again")
        except Stop as stop:
            lines.append(f"{world.name}: REPORTED -- {stop}")
        except (ProviderUnavailable, NotFound) as error:
            lines.append(f"{world.name}: REPORTED -- {type(error).__name__}")
    return lines + seeder.observations


def main() -> int:
    try:
        writer = PennylaneWriter(token("PENNYLANE_SEED_TOKEN"), sandbox_company_id(),
                                 pacer=Pacer())
    except ProviderConfigurationError as error:
        print(f"pennylane_seed: {error.category}")
        return 2
    for line in asyncio.run(run(writer, Intents(INTENTS), date.today())):
        print(line)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
