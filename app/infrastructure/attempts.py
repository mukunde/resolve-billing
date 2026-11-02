"""Every send to a vendor, kept whether or not the investigation completes
(specs/009-luna-service, FR-007a).

Apart from `agent_runs`, whose row is written once the investigation ends: a
refusal or a stopped process never reaches that point, and those are the
attempts that matter most. Each write commits on its own. A row with no
`outcome` reads as unfinished, and nothing fills one in after the fact.

No vendor text is kept: a closed outcome, a model id, counts and a cost.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.decision.base import AttemptEnd, AttemptStart

SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_attempts (
    attempt_id       TEXT PRIMARY KEY,
    invocation_id    TEXT    NOT NULL,
    dispute_id       TEXT    NOT NULL,
    number           INTEGER NOT NULL,
    provider         TEXT    NOT NULL,
    requested_model  TEXT    NOT NULL,
    reserved_tokens  INTEGER NOT NULL,
    started_at       TEXT    NOT NULL,
    returned_model   TEXT,
    ended_at         TEXT,
    duration_ms      INTEGER,
    -- NULL means the attempt never ended on record: unfinished, not failed.
    outcome          TEXT,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    details_json     TEXT,
    -- NULL means unknown, not free.
    cost_micros      INTEGER,
    pricing_as_of    TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_invocation ON engine_attempts(invocation_id, number);
CREATE INDEX IF NOT EXISTS idx_attempts_dispute ON engine_attempts(dispute_id);
"""


class SqliteAttemptStore:
    """An `AttemptRecorder` on the audit file."""

    def __init__(self, database: str | Path = "audit.db") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def started(self, attempt: AttemptStart) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO engine_attempts (
                    attempt_id, invocation_id, dispute_id, number, provider,
                    requested_model, reserved_tokens, started_at
                ) VALUES (
                    :attempt_id, :invocation_id, :dispute_id, :number, :provider,
                    :requested_model, :reserved_tokens, :started_at
                )
                """,
                vars(attempt),
            )

    def finished(self, attempt: AttemptEnd) -> None:
        details = {
            "cached_input_tokens": attempt.cached_input_tokens,
            "cache_write_tokens": attempt.cache_write_tokens,
            "reasoning_tokens": attempt.reasoning_tokens,
        }
        with self._connection:
            self._connection.execute(
                """
                UPDATE engine_attempts SET
                    outcome = :outcome, ended_at = :ended_at, duration_ms = :duration_ms,
                    returned_model = :returned_model, input_tokens = :input_tokens,
                    output_tokens = :output_tokens, details_json = :details_json,
                    cost_micros = :cost_micros, pricing_as_of = :pricing_as_of
                WHERE attempt_id = :attempt_id AND outcome IS NULL
                """,
                {
                    "attempt_id": attempt.attempt_id,
                    "outcome": attempt.outcome.value,
                    "ended_at": attempt.ended_at,
                    "duration_ms": attempt.duration_ms,
                    "returned_model": attempt.returned_model,
                    "input_tokens": attempt.input_tokens,
                    "output_tokens": attempt.output_tokens,
                    "details_json": json.dumps(
                        {key: value for key, value in details.items() if value is not None}
                    ),
                    "cost_micros": attempt.cost_micros,
                    "pricing_as_of": attempt.pricing_as_of,
                },
            )

    def for_invocation(self, invocation_id: str | None) -> list[sqlite3.Row]:
        """One investigation's attempts, in order; none for a run without any."""
        if invocation_id is None:
            return []
        return self._connection.execute(
            "SELECT * FROM engine_attempts WHERE invocation_id = ? ORDER BY number",
            (invocation_id,),
        ).fetchall()

    def for_dispute(self, dispute_id: str) -> list[sqlite3.Row]:
        """All the attempts for a dispute: a refusal leaves no run to link to."""
        return self._connection.execute(
            "SELECT * FROM engine_attempts WHERE dispute_id = ? ORDER BY started_at",
            (dispute_id,),
        ).fetchall()

    def close(self) -> None:
        self._connection.close()
