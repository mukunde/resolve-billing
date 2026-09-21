"""The audit log: what was decided, by what, on what evidence, at what cost.

SQLite, on purpose. The demo has to run with one command and no container,
and everything that matters here is the *shape* of the record rather than the
engine holding it -- the table below ports to PostgreSQL unchanged apart from
the column types. The point to defend is that a run is reconstructable months
later, not which database is underneath.

Two rules the schema follows:

**Only columns that have a writer.** A column nobody fills reads, to whoever
inherits this, like a feature that exists.

**A run row is immutable.** Approval and execution are not amendments to the
investigation; they are a separate lifecycle with its own table
(:mod:`app.infrastructure.actions`), joined on ``run_id``. Updating a run row
in place would lose the distinction between what was decided at the time and
what happened to it afterwards -- which is the one thing an audit log exists
to preserve.

**The engine and its version are not optional.** Without them a decision is
neither replayable nor comparable against another engine, which would empty
the sovereignty argument of ADR-002 of its content.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.application.investigate_dispute import InvestigationResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id                   TEXT PRIMARY KEY,
    created_at               TEXT    NOT NULL,

    billing_provider         TEXT    NOT NULL,
    decision_engine          TEXT    NOT NULL,
    decision_engine_version  TEXT    NOT NULL,

    customer_id              TEXT,
    invoice_id               TEXT,
    input_message            TEXT    NOT NULL,

    classification           TEXT    NOT NULL,
    confidence               REAL    NOT NULL,
    confidence_kind          TEXT    NOT NULL,
    explanation              TEXT    NOT NULL,
    findings_json            TEXT    NOT NULL,
    short_circuited_by       TEXT,

    recommended_action       TEXT    NOT NULL,
    amount_minor             INTEGER,
    currency                 TEXT,

    verdict                  TEXT    NOT NULL,
    authorization_reason     TEXT    NOT NULL,
    autonomy_available       INTEGER NOT NULL,

    model_id                 TEXT,
    prompt_version           TEXT,
    input_tokens             INTEGER,
    output_tokens            INTEGER,
    -- NULL means "no rate known", not "it was free". Zero is a claim.
    cost_micros              INTEGER,
    pricing_as_of            TEXT,
    latency_ms               INTEGER
);

CREATE TABLE IF NOT EXISTS agent_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL REFERENCES agent_runs(run_id),
    occurred_at   TEXT NOT NULL,
    event         TEXT NOT NULL,
    payload_json  TEXT,
    sequence      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run ON agent_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_runs_customer ON agent_runs(customer_id, created_at);
"""


class SqliteAuditRepository:
    """Append-only record of every investigation."""

    def __init__(self, database: str | Path = "audit.db") -> None:
        self._database = str(database)
        self._connection = sqlite3.connect(self._database, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._migrate()
        self._connection.commit()

    def _migrate(self) -> None:
        """Add columns that postdate an existing database file.

        ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already
        exists, so a month-old ``audit.db`` would fail with ``no such column``
        on the next write. No migration framework for a POC, but six lines
        beats that surprise -- and an audit log that refuses to append is
        exactly the failure you notice last.
        """
        held = {row["name"] for row in self._connection.execute("PRAGMA table_info(agent_runs)")}
        for column, ddl in (("pricing_as_of", "TEXT"),):
            if column not in held:
                self._connection.execute(
                    f"ALTER TABLE agent_runs ADD COLUMN {column} {ddl}"  # noqa: S608
                )

    def record(
        self,
        result: InvestigationResult,
        billing_provider: str,
        now: datetime | None = None,
    ) -> None:
        """Write the run and its events in one transaction.

        One transaction because a run without its events, or events without
        their run, is worse than no record: it looks complete.
        """
        created_at = (now or datetime.now(UTC)).isoformat()
        telemetry = result.telemetry
        action = result.proposed_action
        authorization = result.authorization

        with self._connection:
            # Columns named explicitly rather than positionally: a positional
            # INSERT silently shifts every value when a column is added.
            self._connection.execute(
                """
                INSERT INTO agent_runs (
                    run_id, created_at,
                    billing_provider, decision_engine, decision_engine_version,
                    customer_id, invoice_id, input_message,
                    classification, confidence, confidence_kind, explanation,
                    findings_json, short_circuited_by,
                    recommended_action, amount_minor, currency,
                    verdict, authorization_reason, autonomy_available,
                    model_id, prompt_version,
                    input_tokens, output_tokens, cost_micros, pricing_as_of, latency_ms
                ) VALUES (
                    :run_id, :created_at,
                    :billing_provider, :decision_engine, :decision_engine_version,
                    :customer_id, :invoice_id, :input_message,
                    :classification, :confidence, :confidence_kind, :explanation,
                    :findings_json, :short_circuited_by,
                    :recommended_action, :amount_minor, :currency,
                    :verdict, :authorization_reason, :autonomy_available,
                    :model_id, :prompt_version,
                    :input_tokens, :output_tokens, :cost_micros, :pricing_as_of, :latency_ms
                )
                """,
                {
                    "run_id": result.run_id,
                    "created_at": created_at,
                    "billing_provider": billing_provider,
                    "decision_engine": result.decision_engine,
                    "decision_engine_version": result.decision_engine_version,
                    "customer_id": result.dispute.customer_id,
                    "invoice_id": result.dispute.invoice_id,
                    # Stored verbatim, injection attempts included: the point
                    # of an audit log is what actually arrived.
                    "input_message": result.dispute.customer_message,
                    "classification": result.classification.value,
                    "confidence": result.confidence,
                    "confidence_kind": result.confidence_kind,
                    "explanation": result.explanation,
                    "findings_json": json.dumps(
                        [f.model_dump(mode="json") for f in result.findings],
                        ensure_ascii=False,
                    ),
                    "short_circuited_by": (
                        result.short_circuited_by.value if result.short_circuited_by else None
                    ),
                    "recommended_action": action.type,
                    "amount_minor": action.amount.amount_minor if action.amount else None,
                    "currency": action.amount.currency if action.amount else None,
                    "verdict": authorization.verdict.value,
                    "authorization_reason": authorization.reason,
                    "autonomy_available": int(authorization.autonomy_available),
                    "model_id": telemetry.model_id if telemetry else None,
                    "prompt_version": telemetry.prompt_version if telemetry else None,
                    "input_tokens": telemetry.input_tokens if telemetry else None,
                    "output_tokens": telemetry.output_tokens if telemetry else None,
                    "cost_micros": telemetry.cost_micros if telemetry else None,
                    "pricing_as_of": telemetry.pricing_as_of if telemetry else None,
                    "latency_ms": telemetry.latency_ms if telemetry else None,
                },
            )
            self._connection.executemany(
                """
                INSERT INTO agent_events (run_id, occurred_at, event, payload_json, sequence)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        result.run_id,
                        event.occurred_at.isoformat(),
                        event.event,
                        json.dumps(event.payload, ensure_ascii=False) if event.payload else None,
                        index,
                    )
                    for index, event in enumerate(result.events)
                ],
            )

    # -- reads, for the demo and the tests ---------------------------------

    def run(self, run_id: str) -> sqlite3.Row | None:
        cursor = self._connection.execute(
            "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
        )
        return cursor.fetchone()

    def events(self, run_id: str) -> list[sqlite3.Row]:
        cursor = self._connection.execute(
            "SELECT * FROM agent_events WHERE run_id = ? ORDER BY sequence", (run_id,)
        )
        return cursor.fetchall()

    def count(self) -> int:
        return self._connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]

    def close(self) -> None:
        self._connection.close()
