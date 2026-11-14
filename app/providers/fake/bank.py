"""A bank on fixtures, for tests and evaluation (specs/016-bank-evidence).

`fixtures/bank.json`, beside the fake's other fixtures, holds **named worlds**; a fake
is built for one world, so each evaluation case selects its own (`bank_world`), and a
case with none has no bank at all.
A world:

    {"accounts": [{"id": "acc_eur", "currency": "EUR", "visible": true}],
     "credits": [{"account_id": "acc_eur", "transaction_id": "tx_1", "type": "transfer",
                  "state": "completed", "amount": 4900, "currency": "EUR",
                  "created_at": "2026-10-03T09:00:00Z",
                  "completed_at": "2026-10-03T09:00:01Z",
                  "reference": "Facture inv_060", "origin": "external",
                  "fx": false, "fee": false}],
     "returns": [{"id": "tx_9", "type": "topup_return", "amount": -4900, "currency": "EUR",
                  "created_at": "...", "completed_at": "...", "related_credit_id": "tx_1"}],
     "incomplete": [],          # reasons from the closed set: the read is then incomplete
     "controlled": true}        # its author says no item completes long after creation

Coverage is established only for a controlled world **and** when no credit or return the
read leaves out (created before the lower bound) completed at or after that bound or has
no completion instant: a world that breaks its own guarantee is not covered, whatever it
declares.

Amounts in minor units. The fake filters by `created_at` as the real read does.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.domain.bank import BankCredit, BankRead, BankReturn, DeclaredAccount
from app.domain.money import Money
from app.providers.fake.fixtures import FIXTURES_DIR

BANK_FIXTURES = FIXTURES_DIR / "bank.json"


def _at(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def worlds(path: Path = BANK_FIXTURES) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")).get("worlds", {})


class FakeBank:
    name = "fake"

    def __init__(self, world: str, path: Path = BANK_FIXTURES) -> None:
        self._world = worlds(path)[world]

    async def read(
        self, window_start: datetime, window_end: datetime, margin_days: int, now: datetime
    ) -> BankRead:
        lower = window_start - timedelta(days=margin_days)
        w = self._world

        def inside(raw: dict) -> bool:
            created = _at(raw["created_at"])
            return lower <= created <= window_end

        credits = tuple(
            BankCredit(
                account_id=c["account_id"], transaction_id=c["transaction_id"],
                type=c["type"], state=c["state"], observed_at=now,
                amount=Money(amount_minor=c["amount"], currency=c["currency"]),
                created_at=_at(c["created_at"]), completed_at=_at(c.get("completed_at")),
                reference=c.get("reference"), origin=c.get("origin", "unknown"),
                fx=c.get("fx", False), fee=c.get("fee", False),
            )
            for c in w.get("credits", []) if inside(c)
        )
        returns = tuple(
            BankReturn(
                id=r["id"], type=r["type"],
                amount=Money(amount_minor=r["amount"], currency=r["currency"]),
                created_at=_at(r["created_at"]), completed_at=_at(r.get("completed_at")),
                related_credit_id=r.get("related_credit_id"),
            )
            for r in w.get("returns", []) if inside(r)
        )
        def missed(raw: dict) -> bool:
            # Left out by creation, yet possibly relevant: completed inside the read, or
            # with no completion instant at all (a reverted credit may have none).
            completed = _at(raw.get("completed_at"))
            return _at(raw["created_at"]) < lower and (completed is None or completed >= lower)

        every = [*w.get("credits", []), *w.get("returns", [])]
        covered = bool(w.get("controlled", False)) and not any(missed(r) for r in every)
        accounts = tuple(DeclaredAccount(**a) for a in w.get("accounts", []))
        incomplete = tuple(w.get("incomplete", []))
        if any(not a.visible for a in accounts) and "account_not_visible" not in incomplete:
            incomplete += ("account_not_visible",)
        return BankRead(
            accounts=accounts, credits=credits, returns=returns,
            window_start=window_start, window_end=window_end, margin_days=margin_days,
            complete=not incomplete, incompleteness=incomplete,
            coverage_established=covered,
        )
