"""The billed side of the ledger, behind a port.

This repository's whole subject is comparing two records of the same economic
event and explaining the difference. `detect_duplicate_invoice` does it on a
customer's invoices. This module does it on ours: what we *estimated* a run
cost, against what the provider actually *billed*.

The estimate is already in every audit row -- `cost_micros`, computed from a
dated list price. It is an estimate twice over: list prices are not billed
amounts, and the table applies only the rates it knows. Reconciling it is the
only way to find out which of those two is lying, and by how much.

The provider is behind a `UsageReport` port for the same reason billing
platforms are behind `BillingProvider`: Anthropic reports cost one way, Vertex
another, a self-hosted engine not at all. The reconciler must not know which.

**Amounts arrive as decimal strings in cents** and become integer micros here,
at one point, because a per-run cost of $0.0007 summed over a month in a float
drifts -- the same reason `Money` exists and the same reason `cost_micros` is
an integer in the first place. Micros rather than `Money` because USD's minor
unit is the cent, and these figures are routinely smaller than one.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

#: One cent, in millionths of a dollar.
MICROS_PER_CENT = Decimal(10_000)

#: Token categories the provider bills for. Ours is not the complete list --
#: that is the point of reconciling rather than asserting.
CACHE_TOKEN_TYPES = frozenset(
    {"cache_read_input_tokens", "cache_creation_input_tokens"}
)


class BilledLine(BaseModel):
    """One line of what the provider says we owe.

    Deliberately finer-grained than our estimate. The estimate is a single
    number per run; the bill separates uncached input, cached input, cache
    creation and output. A gap that sits entirely in one of those categories
    is a diagnosis, not just a discrepancy.
    """

    model_config = ConfigDict(frozen=True)

    day: date
    amount_micros: int
    cost_type: str
    description: str
    model: str | None = None
    token_type: str | None = None
    workspace_id: str | None = None
    service_tier: str | None = None


@runtime_checkable
class UsageReport(Protocol):
    """Where the billed figures come from. Reads only."""

    name: str

    def billed(self, start: date, end: date) -> list[BilledLine]: ...


def cents_to_micros(amount: str) -> int:
    """`"123.78912"` cents becomes 1_237_891 micros.

    Half-up, matching `Money.from_decimal`, so the two roundings in this
    codebase cannot disagree about a half-cent.
    """
    cents = Decimal(amount)
    return int((cents * MICROS_PER_CENT).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def parse_cost_report(payload: dict) -> list[BilledLine]:
    """Turn a cost-report response into billed lines.

    One parser, two transports: the same function reads a live response and a
    response someone saved to disk. Writing a second parser for the file case
    would create two things that can disagree about what the provider said.

    The response nests results inside time buckets:

        {"data": [{"starting_at": ..., "results": [{...}, ...]}, ...]}
    """
    lines: list[BilledLine] = []
    for bucket in payload.get("data", []):
        day = _day(bucket.get("starting_at"))
        if day is None:
            continue
        for result in bucket.get("results", []):
            amount = result.get("amount")
            if amount is None:
                continue
            lines.append(
                BilledLine(
                    day=day,
                    amount_micros=cents_to_micros(amount),
                    cost_type=result.get("cost_type", "unknown"),
                    description=result.get("description", ""),
                    model=result.get("model"),
                    token_type=result.get("token_type"),
                    workspace_id=result.get("workspace_id"),
                    service_tier=result.get("service_tier"),
                )
            )
    return lines


def _day(timestamp: str | None) -> date | None:
    if not timestamp:
        return None
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
