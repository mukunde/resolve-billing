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
import os
import sqlite3
from datetime import UTC, datetime, timedelta
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
    -- Callback the orchestrator asked us to ring once a human has decided.
    -- It lives here rather than in a URL the reviewer carries around: the
    -- state of truth holds it, so the link can be reopened and the page
    -- reloaded without losing the way back.
    resume_url          TEXT,
    approved_by         TEXT,
    provider_action_id  TEXT,
    failure_reason      TEXT,
    -- How an unknown outcome was settled, in words: what the platform was
    -- asked and what it answered. The audit of a recovery, not of a write.
    resolution          TEXT,
    -- When the request stops being decidable. A decision is taken on the
    -- facts of the day it is shown; a week later they may not hold. D13.
    expires_at          TEXT,
    -- What the approver approved: the fingerprint of the proposal and of
    -- the invoice as they were. Execution refuses if it no longer holds.
    approved_fingerprint TEXT
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
    # Executing starts with a claim. Only the caller that moves the action
    # into EXECUTING rings the provider; a concurrent one finds it taken.
    # An approval also lapses if the invoice changed before execution. D13.
    ActionState.APPROVED: {ActionState.EXECUTING, ActionState.EXPIRED},
    ActionState.EXECUTING: {
        ActionState.EXECUTED,
        ActionState.FAILED,
        ActionState.UNKNOWN_OUTCOME,
    },
    # No answer from the platform: resolved by asking it what it holds under
    # the idempotency key -- the write landed, or it can be made again.
    ActionState.UNKNOWN_OUTCOME: {ActionState.EXECUTED, ActionState.FAILED},
    ActionState.REJECTED: set(),
    ActionState.EXECUTED: set(),
    # A failed write may be retried, through a fresh claim: it then succeeds
    # with the provider's reference or fails again. Reaching EXECUTED takes
    # that reference -- see transition() -- so a retry cannot be skipped by
    # asking for the end state directly.
    ActionState.FAILED: {ActionState.EXECUTING, ActionState.EXPIRED},
    ActionState.EXPIRED: set(),
}


class IllegalTransition(BillingDomainError):
    def __init__(
        self,
        action_id: str,
        current: ActionState,
        wanted: ActionState,
        why: str | None = None,
    ) -> None:
        message = f"action {action_id} is {current.value}; it cannot become {wanted.value}"
        super().__init__(f"{message} {why}" if why else message)
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
    resume_url: str | None = None
    approved_by: str | None = None
    provider_action_id: str | None = None
    failure_reason: str | None = None
    resolution: str | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    approved_fingerprint: str | None = None

    def expired(self, now: datetime | None = None) -> bool:
        """Past the deadline for a decision."""
        return self.expires_at is not None and (now or datetime.now(UTC)) >= self.expires_at


def approval_ttl() -> timedelta:
    """How long a request for approval stays decidable. 72 hours unless
    ``RESOLVE_APPROVAL_TTL_HOURS`` says otherwise."""
    return timedelta(hours=float(os.getenv("RESOLVE_APPROVAL_TTL_HOURS", "72")))


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
        self._migrate()
        self._connection.commit()

    def _migrate(self) -> None:
        """Add columns that postdate an existing database file.

        Same reasoning as the audit repository: CREATE TABLE IF NOT EXISTS
        does nothing to a table that already exists.
        """
        held = {r["name"] for r in self._connection.execute("PRAGMA table_info(pending_actions)")}
        for column, ddl in (
            ("resume_url", "TEXT"),
            ("resolution", "TEXT"),
            ("expires_at", "TEXT"),
            ("approved_fingerprint", "TEXT"),
        ):
            if column not in held:
                self._connection.execute(
                    f"ALTER TABLE pending_actions ADD COLUMN {column} {ddl}"
                )

    def create(
        self,
        run_id: str,
        action_type: str,
        invoice_id: str,
        amount: Money,
        reason: str,
        provider: str,
        ttl: timedelta | None = None,
    ) -> PendingAction:
        """Register an action awaiting a human.

        Replaying an identical proposal returns the existing one rather than
        queueing a second approval for the same credit note.
        """
        key = idempotency_key(provider, invoice_id, action_type, amount)
        existing = self.by_idempotency_key(key)
        if existing is not None:
            return existing

        created = datetime.now(UTC)
        now = created.isoformat()
        expires_at = (created + (ttl if ttl is not None else approval_ttl())).isoformat()
        action_id = f"act_{key[:12]}"
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO pending_actions
                (action_id, run_id, created_at, updated_at, state, action_type,
                 invoice_id, amount_minor, currency, reason, idempotency_key,
                 resume_url, approved_by, provider_action_id, failure_reason,
                 expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)
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
                    expires_at,
                ),
            )
        return self.get(action_id)

    def set_resume_url(self, action_id: str, resume_url: str) -> PendingAction:
        """Remember where to call back once a human has decided.

        Overwrites on purpose: a re-run of the same dispute produces the same
        action by idempotency but a new orchestrator execution, and the stale
        callback would resume nothing.
        """
        self.get(action_id)
        with self._connection:
            self._connection.execute(
                "UPDATE pending_actions SET resume_url = ?, updated_at = ? WHERE action_id = ?",
                (resume_url, datetime.now(UTC).isoformat(), action_id),
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
        resolution: str | None = None,
        approved_fingerprint: str | None = None,
    ) -> PendingAction:
        current = self.get(action_id)
        if to not in ALLOWED[current.state]:
            raise IllegalTransition(action_id, current.state, to)
        # EXECUTED is a claim that money moved, so it takes the provider's
        # reference as proof. Without one, the state would say a credit note
        # exists that nobody issued.
        if to is ActionState.EXECUTED and not provider_action_id:
            raise IllegalTransition(
                action_id, current.state, to, why="without the provider's reference"
            )

        # The check above was made on a read. The UPDATE names the state that
        # read found, so if anything moved the action in between -- another
        # request, another process -- it changes nothing and says so, instead
        # of overwriting a transition it never saw.
        with self._connection:
            updated = self._connection.execute(
                """
                UPDATE pending_actions
                SET state = ?, updated_at = ?,
                    approved_by = COALESCE(?, approved_by),
                    provider_action_id = COALESCE(?, provider_action_id),
                    failure_reason = ?,
                    resolution = COALESCE(?, resolution),
                    approved_fingerprint = COALESCE(?, approved_fingerprint)
                WHERE action_id = ? AND state = ?
                """,
                (
                    to.value,
                    datetime.now(UTC).isoformat(),
                    approved_by,
                    provider_action_id,
                    failure_reason,
                    resolution,
                    approved_fingerprint,
                    action_id,
                    current.state.value,
                ),
            ).rowcount
        if updated != 1:
            raise IllegalTransition(
                action_id, current.state, to, why="(its state changed concurrently)"
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
        resume_url=row["resume_url"],
        approved_by=row["approved_by"],
        provider_action_id=row["provider_action_id"],
        failure_reason=row["failure_reason"],
        resolution=row["resolution"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
        expires_at=(
            datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
        ),
        approved_fingerprint=row["approved_fingerprint"],
    )
