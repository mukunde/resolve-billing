"""Pending financial actions, and the state machine that guards them.

This table is the state of truth for "an action is waiting for a human". It
lives here rather than in the orchestrator on purpose: n8n's Wait node keeps
its resume state inside n8n, so an orchestrator restart would drop every
pending approval silently. With the state in the database, n8n becomes what
it should be -- an approval interface and a trigger -- and a run can be
resumed days later through ``GET /runs/{run_id}``.

The idempotency key is derived, not generated. Two requests describing the
same credit note on the same invoice produce the same key, so a retry after a
timeout finds the original action instead of issuing a second one.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.domain.enums import ActionState
from app.domain.exceptions import BillingDomainError
from app.domain.money import Money

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_actions (
    action_id           TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,

    state               TEXT NOT NULL,
    action_type         TEXT NOT NULL,
    invoice_id          TEXT NOT NULL,
    amount_minor        INTEGER NOT NULL,
    currency            TEXT NOT NULL,
    reason              TEXT NOT NULL,

    idempotency_key     TEXT NOT NULL UNIQUE,
    approved_by         TEXT,
    provider_action_id  TEXT,
    failure_reason      TEXT
);

CREATE INDEX IF NOT EXISTS idx_actions_run ON pending_actions(run_id);
CREATE INDEX IF NOT EXISTS idx_actions_state ON pending_actions(state);
"""

#: Which transitions are legal. Anything absent is a 409, not a silent no-op:
#: approving an already-executed action is a symptom, not a nuisance.
ALLOWED: dict[ActionState, set[ActionState]] = {
    ActionState.PENDING_APPROVAL: {
        ActionState.APPROVED,
        ActionState.REJECTED,
        ActionState.EXPIRED,
    },
    ActionState.APPROVED: {ActionState.EXECUTED, ActionState.FAILED},
    ActionState.REJECTED: set(),
    ActionState.EXECUTED: set(),
    ActionState.FAILED: {ActionState.EXECUTED},
    ActionState.EXPIRED: set(),
}


class IllegalTransition(BillingDomainError):
    def __init__(self, action_id: str, current: ActionState, wanted: ActionState) -> None:
        super().__init__(
            f"action {action_id} is {current.value}; it cannot become {wanted.value}"
        )
        self.current = current
        self.wanted = wanted


class UnknownAction(BillingDomainError):
    pass


class PendingAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    action_id: str
    run_id: str
    state: ActionState
    action_type: str
    invoice_id: str
    amount: Money
    reason: str
    idempotency_key: str
    approved_by: str | None = None
    provider_action_id: str | None = None
    failure_reason: str | None = None


def idempotency_key(
    provider: str, invoice_id: str, action_type: str, amount: Money
) -> str:
    """Derived from what the action *is*, never from when it was asked for.

    Includes the amount and currency: a 99,00 EUR credit note and a 49,00 EUR
    one on the same invoice are different actions and must not collide.
    """
    material = f"{provider}|{invoice_id}|{action_type}|{amount.amount_minor}|{amount.currency}"
    return hashlib.sha256(material.encode()).hexdigest()


class SqliteActionStore:
    def __init__(self, database: str | Path = "audit.db") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def create(
        self,
        run_id: str,
        action_type: str,
        invoice_id: str,
        amount: Money,
        reason: str,
        provider: str,
    ) -> PendingAction:
        """Register an action awaiting a human.

        Replaying an identical proposal returns the existing one rather than
        queueing a second approval for the same credit note.
        """
        key = idempotency_key(provider, invoice_id, action_type, amount)
        existing = self.by_idempotency_key(key)
        if existing is not None:
            return existing

        now = datetime.now(UTC).isoformat()
        action_id = f"act_{key[:12]}"
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO pending_actions
                (action_id, run_id, created_at, updated_at, state, action_type,
                 invoice_id, amount_minor, currency, reason, idempotency_key,
                 approved_by, provider_action_id, failure_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    action_id,
                    run_id,
                    now,
                    now,
                    ActionState.PENDING_APPROVAL.value,
                    action_type,
                    invoice_id,
                    amount.amount_minor,
                    amount.currency,
                    reason,
                    key,
                ),
            )
        return self.get(action_id)

    def get(self, action_id: str) -> PendingAction:
        row = self._connection.execute(
            "SELECT * FROM pending_actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise UnknownAction(f"unknown action {action_id!r}")
        return _to_action(row)

    def by_idempotency_key(self, key: str) -> PendingAction | None:
        row = self._connection.execute(
            "SELECT * FROM pending_actions WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return _to_action(row) if row else None

    def for_run(self, run_id: str) -> list[PendingAction]:
        rows = self._connection.execute(
            "SELECT * FROM pending_actions WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
        return [_to_action(row) for row in rows]

    def transition(
        self,
        action_id: str,
        to: ActionState,
        approved_by: str | None = None,
        provider_action_id: str | None = None,
        failure_reason: str | None = None,
    ) -> PendingAction:
        current = self.get(action_id)
        if to not in ALLOWED[current.state]:
            raise IllegalTransition(action_id, current.state, to)

        with self._connection:
            self._connection.execute(
                """
                UPDATE pending_actions
                SET state = ?, updated_at = ?,
                    approved_by = COALESCE(?, approved_by),
                    provider_action_id = COALESCE(?, provider_action_id),
                    failure_reason = ?
                WHERE action_id = ?
                """,
                (
                    to.value,
                    datetime.now(UTC).isoformat(),
                    approved_by,
                    provider_action_id,
                    failure_reason,
                    action_id,
                ),
            )
        return self.get(action_id)

    def close(self) -> None:
        self._connection.close()


def _to_action(row: sqlite3.Row) -> PendingAction:
    return PendingAction(
        action_id=row["action_id"],
        run_id=row["run_id"],
        state=ActionState(row["state"]),
        action_type=row["action_type"],
        invoice_id=row["invoice_id"],
        amount=Money(amount_minor=row["amount_minor"], currency=row["currency"]),
        reason=row["reason"],
        idempotency_key=row["idempotency_key"],
        approved_by=row["approved_by"],
        provider_action_id=row["provider_action_id"],
        failure_reason=row["failure_reason"],
    )
