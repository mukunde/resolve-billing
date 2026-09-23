"""Compare what we estimated against what we were billed.

The comparison is per **(day, model)**, and not finer, because it cannot be:
our audit rows are per run, the provider's cost report is per day. That is the
ordinary shape of a reconciliation -- two ledgers agree on the economic event
and disagree on the grain -- and pretending otherwise would produce a
per-run "billed" figure that nobody issued.

What the reconciler adds beyond a subtraction is the **diagnosis**. A gap is
only useful if you can say where it sits. The provider bills by token type;
our price table knows only uncached input and output. So a gap concentrated in
cache lines is not a mystery, it is a missing rate -- and the report says so
rather than leaving a number to be argued about.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from app.reconciliation.base import CACHE_TOKEN_TYPES, BilledLine

#: Below this, a gap is rounding and reporting lag rather than a defect.
#: Deliberately generous: the point of the first run is to find the big,
#: structural differences, not to litigate a tenth of a cent.
TOLERANCE_PCT = 2.0


@dataclass(frozen=True)
class Estimated:
    """What our own audit says a day of one model cost us."""

    day: date
    model: str
    cost_micros: int
    runs: int
    priced_runs: int
    cache_read_tokens: int


@dataclass
class Row:
    day: date
    model: str
    estimated_micros: int
    billed_micros: int
    runs: int

    @property
    def gap_micros(self) -> int:
        return self.billed_micros - self.estimated_micros

    @property
    def gap_pct(self) -> float | None:
        """None when there is nothing to divide by, rather than zero.

        A billed amount against no estimate is not a 0 % gap; it is a run we
        never priced, and the two must not print the same.
        """
        if self.estimated_micros == 0:
            return None
        return self.gap_micros / self.estimated_micros * 100


@dataclass
class Reconciliation:
    rows: list[Row] = field(default_factory=list)
    unpriced: dict[str, int] = field(default_factory=dict)
    unknown_estimate_runs: int = 0

    @property
    def estimated_micros(self) -> int:
        return sum(r.estimated_micros for r in self.rows)

    @property
    def billed_micros(self) -> int:
        return sum(r.billed_micros for r in self.rows)

    @property
    def gap_micros(self) -> int:
        return self.billed_micros - self.estimated_micros

    @property
    def gap_pct(self) -> float | None:
        if self.estimated_micros == 0:
            return None
        return self.gap_micros / self.estimated_micros * 100

    @property
    def within_tolerance(self) -> bool:
        pct = self.gap_pct
        return pct is not None and abs(pct) <= TOLERANCE_PCT

    def diagnosis(self) -> list[str]:
        """Say where the gap sits, in the terms that would fix it."""
        findings: list[str] = []

        cache = sum(
            micros
            for token_type, micros in self.unpriced.items()
            if token_type in CACHE_TOKEN_TYPES
        )
        if cache:
            findings.append(
                f"{_usd(cache)} of the bill is cache tokens, which the price "
                "table does not model: estimate_cost_micros applies input and "
                "output rates only. Expect a systematic under-estimate "
                "wherever prompt caching is used."
            )

        other = {
            token_type: micros
            for token_type, micros in self.unpriced.items()
            if token_type not in CACHE_TOKEN_TYPES
        }
        if other:
            listed = ", ".join(f"{k} {_usd(v)}" for k, v in sorted(other.items()))
            findings.append(f"Billed under categories we do not price: {listed}.")

        if self.unknown_estimate_runs:
            findings.append(
                f"{self.unknown_estimate_runs} run(s) carry no estimate at all "
                "-- the model was absent from the price table, so the audit "
                "recorded an unknown cost rather than a wrong one. They are "
                "billed but not estimated, and inflate the gap."
            )

        if not findings:
            findings.append("No structural cause found; the gap is arithmetic.")
        return findings


def estimated_from_audit(
    database: str, start: date, end: date, engine: str | None = None
) -> list[Estimated]:
    """Read our own side of the ledger out of the audit log.

    `cost_micros IS NULL` means the model was unknown to the price table, and
    is counted separately rather than summed as zero -- the distinction the
    engine goes out of its way to preserve must survive the reporting too.
    """
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    query = """
        SELECT created_at, model_id, cost_micros, cache_read_input_tokens
        FROM agent_runs
        WHERE model_id IS NOT NULL AND date(created_at) >= ? AND date(created_at) < ?
    """
    params: list = [start.isoformat(), end.isoformat()]
    if engine is not None:
        query += " AND decision_engine = ?"
        params.append(engine)

    grouped: dict[tuple[date, str], list[sqlite3.Row]] = defaultdict(list)
    try:
        for row in connection.execute(query, params):
            day = datetime.fromisoformat(row["created_at"]).date()
            grouped[(day, row["model_id"])].append(row)
    finally:
        connection.close()

    return [
        Estimated(
            day=day,
            model=model,
            cost_micros=sum(r["cost_micros"] or 0 for r in rows),
            runs=len(rows),
            priced_runs=sum(1 for r in rows if r["cost_micros"] is not None),
            cache_read_tokens=sum(r["cache_read_input_tokens"] or 0 for r in rows),
        )
        for (day, model), rows in sorted(grouped.items())
    ]


def reconcile(
    estimated: list[Estimated],
    billed: list[BilledLine],
    workspace_id: str | None = None,
) -> Reconciliation:
    """Match the two ledgers on (day, model) and explain what is left over.

    `workspace_id` matters more than it looks: the bill covers everything on
    the account, including runs this audit database never saw. Reconciling a
    project's estimate against an organisation's bill compares two different
    populations and calls the difference an error.
    """
    if workspace_id is not None:
        billed = [line for line in billed if line.workspace_id == workspace_id]

    billed_by_key: dict[tuple[date, str], int] = defaultdict(int)
    unpriced: dict[str, int] = defaultdict(int)

    for line in billed:
        if line.model is None:
            continue
        billed_by_key[(line.day, line.model)] += line.amount_micros
        if line.token_type and line.token_type not in _PRICED_TOKEN_TYPES:
            unpriced[line.token_type] += line.amount_micros

    result = Reconciliation(unpriced=dict(unpriced))
    seen: set[tuple[date, str]] = set()

    for entry in estimated:
        key = (entry.day, entry.model)
        seen.add(key)
        result.unknown_estimate_runs += entry.runs - entry.priced_runs
        result.rows.append(
            Row(
                day=entry.day,
                model=entry.model,
                estimated_micros=entry.cost_micros,
                billed_micros=billed_by_key.get(key, 0),
                runs=entry.runs,
            )
        )

    # Billed on a day and model we have no runs for. Not an error: it is the
    # other half of the same question, and hiding it would make the totals a
    # comparison of whatever happened to match.
    for key, micros in sorted(billed_by_key.items()):
        if key not in seen:
            result.rows.append(
                Row(day=key[0], model=key[1], estimated_micros=0, billed_micros=micros, runs=0)
            )

    result.rows.sort(key=lambda r: (r.day, r.model))
    return result


#: The token categories `estimate_cost_micros` actually applies a rate to.
#: Everything else the provider bills is, by definition, unmodelled.
_PRICED_TOKEN_TYPES = frozenset({"uncached_input_tokens", "output_tokens"})


def _usd(micros: int) -> str:
    return f"${micros / 1e6:.5f}"
