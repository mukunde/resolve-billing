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
import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.domain.enums import ActionState
from app.domain.exceptions import BillingDomainError
from app.domain.models import TaxBreakdown
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
    approved_fingerprint TEXT,
    -- When the write was first claimed, before it was first sent; never
    -- reset. Every send falls within the platform's window from it.
    first_attempt_at    TEXT,
    -- How the write lands on the invoice, planned once before the first
    -- send and reused by every retry.
    allocation          TEXT,
    -- When an attempt's outcome first became unknown; never cleared. A
    -- refusal after it settles nothing on its own.
    uncertain_since     TEXT,
    -- What the platform said of the write once made, and when it said it:
    -- an observation, not the current state.
    provider_status     TEXT,
    provider_status_at  TEXT,
    -- Why the action needs a person, if it does (ATTENTION).
    attention           TEXT,
    -- What the amount was made of when proposed, on a taxed invoice: JSON
    -- (specs/013-invoice-tax, FR-007). NULL on an untaxed one.
    tax_breakdown       TEXT,
    -- The verified customer the action was proposed for; NULL on actions
    -- older than the column, which never block another (spec 015, FR-009).
    customer_id         TEXT,
    -- Who holds the current claim, set at each claim; a worker whose token
    -- is no longer the row's sends nothing more (spec 015, FR-011).
    claim_token         TEXT,
    -- When the current attempt last sent a request, recorded before each
    -- request: durable, so a crashed worker's last request is known.
    last_request_at     TEXT,
    -- A document made and not completed: JSON {document_id, stage}.
    partial_document    TEXT,
    -- A person's latest status read: when, and what it found.
    status_read_at      TEXT,
    status_read_found   TEXT,
    -- A person's account of how they resolved a partial write or a write
    -- that left no trace; it releases the customer's blocker (FR-009a).
    resolved_by         TEXT,
    resolved_at         TEXT,
    resolution_note     TEXT
);

CREATE INDEX IF NOT EXISTS idx_actions_run ON pending_actions(run_id);
CREATE INDEX IF NOT EXISTS idx_actions_state ON pending_actions(state);
"""

#: Created after the migration, which may be what adds the column.
CUSTOMER_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_actions_customer ON pending_actions(customer_id)"
)

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
    # asking for the end state directly. FAILED -> EXECUTED is there for one
    # caller only: a person's status read that finds the write on the
    # platform for an action needing a person (specs/002-stripe-writes).
    ActionState.FAILED: {ActionState.EXECUTING, ActionState.EXPIRED, ActionState.EXECUTED},
    ActionState.EXPIRED: set(),
}


#: Why an action needs a person. Closed: a new reason is a decision.
ATTENTION = frozenset(
    {
        # The platform may no longer answer the key: nothing is sent, and an
        # absence proves nothing.
        "window_passed",
        # When the first attempt was sent cannot be shown (an action older
        # than the column): treated as past the window.
        "window_unknown",
        "several_writes",
        "unlike_the_action",
        "unreadable_answer",
        # A write was made and its status is not success: to follow up.
        "not_successful",
        # A document was made and not completed: a person's (spec 015, FR-006).
        "partial_write",
        # No window and nothing found after an unknown outcome: the creation may
        # still land; a person's status read can settle it (spec 015, FR-007).
        "no_trace",
    }
)

#: The attentions that stop everything automatic for the action, for good.
INCIDENTS = ATTENTION - {"not_successful"}

#: The incidents a person may resolve, which releases the customer's blocker:
#: a document left half-way, and a creation that left no trace (FR-009a).
RESOLVABLE = frozenset({"partial_write", "no_trace"})

#: How long after an attempt's last request a person's status read must be
#: made for its resolution to be recorded: a request may still be in flight.
SETTLING_DELAY = timedelta(minutes=10)

#: The one type whose actions block each other per customer (FR-009).
BLOCKING_TYPE = "credit_note"


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


class WritesNotSupported(IllegalTransition):
    """The billing platform cannot write: an approved action stays as it is.

    Raised before anything is read, reconciled or claimed, so nothing has
    happened and nothing is unknown. An `IllegalTransition`, so the API
    answers 409 like any other action that cannot become executed now.
    """

    def __init__(
        self, action_id: str, current: ActionState, action_type: str | None = None
    ) -> None:
        why = (
            "(this billing platform cannot write)"
            if action_type is None
            else (
                f"(this billing platform does not write a {action_type}: a person carries "
                "it out outside ResolveBilling)"
            )
        )
        super().__init__(action_id, current, ActionState.EXECUTED, why=why)
        self.action_type = action_type


class NeedsAPerson(IllegalTransition):
    """The action is left to a person: nothing automatic happens to it.

    An `IllegalTransition`, so the API answers 409 with the reason; never an
    unknown outcome to retry (specs/002-stripe-writes).
    """

    def __init__(self, action_id: str, current: ActionState, attention: str) -> None:
        super().__init__(
            action_id, current, ActionState.EXECUTED, why=f"(it needs a person: {attention})"
        )
        self.attention = attention


class BlockedByAnother(IllegalTransition):
    """Another credit note of the same customer is in flight, of unknown
    outcome or left half-way, and nobody resolved it: nothing is claimed
    (spec 015, FR-009). An `IllegalTransition`, so the API answers 409."""

    def __init__(self, action_id: str, current: ActionState, blocker: str | None) -> None:
        super().__init__(
            action_id,
            current,
            ActionState.EXECUTING,
            why=(
                f"(the customer's credit note {blocker or 'another action'} is executing, "
                "of unknown outcome or left half-way; a person resolves it first)"
            ),
        )
        self.blocker = blocker


class ResolutionRefused(IllegalTransition):
    """A resolution that may not be recorded: the action is not a partial
    write or a write without trace, it was resolved already, or no status
    read was made long enough after its last request (FR-009a)."""

    def __init__(self, action_id: str, current: ActionState, why: str) -> None:
        BillingDomainError.__init__(
            self, f"action {action_id} is {current.value}; no resolution recorded ({why})"
        )
        self.current = current
        self.wanted = current
        self.reason = why


class UnknownAction(BillingDomainError):
    pass


#: `annotate` leaves the attention as it is unless told otherwise.
_KEEP = object()


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
    first_attempt_at: datetime | None = None
    allocation: str | None = None
    uncertain_since: datetime | None = None
    provider_status: str | None = None
    provider_status_at: datetime | None = None
    attention: str | None = None
    tax_breakdown: TaxBreakdown | None = None
    customer_id: str | None = None
    claim_token: str | None = None
    last_request_at: datetime | None = None
    #: The document left half-way: {"document_id": ..., "stage": ...}.
    partial_document: dict | None = None
    status_read_at: datetime | None = None
    status_read_found: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None

    def expired(self, now: datetime | None = None) -> bool:
        """Past the deadline for a decision."""
        return self.expires_at is not None and (now or datetime.now(UTC)) >= self.expires_at


def approval_ttl() -> timedelta:
    """How long a request for approval stays decidable. 72 hours unless
    ``RESOLVE_APPROVAL_TTL_HOURS`` says otherwise."""
    return timedelta(hours=float(os.getenv("RESOLVE_APPROVAL_TTL_HOURS", "72")))


def idempotency_key(provider: str, invoice_id: str, action_type: str, amount: Money) -> str:
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
        self._connection.execute(CUSTOMER_INDEX)
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
            ("first_attempt_at", "TEXT"),
            ("allocation", "TEXT"),
            ("uncertain_since", "TEXT"),
            ("provider_status", "TEXT"),
            ("provider_status_at", "TEXT"),
            ("attention", "TEXT"),
            ("tax_breakdown", "TEXT"),
            ("customer_id", "TEXT"),
            ("claim_token", "TEXT"),
            ("last_request_at", "TEXT"),
            ("partial_document", "TEXT"),
            ("status_read_at", "TEXT"),
            ("status_read_found", "TEXT"),
            ("resolved_by", "TEXT"),
            ("resolved_at", "TEXT"),
            ("resolution_note", "TEXT"),
        ):
            if column not in held:
                self._connection.execute(f"ALTER TABLE pending_actions ADD COLUMN {column} {ddl}")
        if "first_attempt_at" not in held:
            # An action already attempted when the column did not exist: when
            # its first attempt was sent cannot be shown, so it is never sent
            # again automatically. Its status can still be read.
            self._connection.execute(
                "UPDATE pending_actions SET attention = 'window_unknown' WHERE state IN (?, ?, ?)",
                (
                    ActionState.EXECUTING.value,
                    ActionState.UNKNOWN_OUTCOME.value,
                    ActionState.FAILED.value,
                ),
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
        tax_breakdown: TaxBreakdown | None = None,
        customer_id: str | None = None,
    ) -> PendingAction:
        """Register an action awaiting a human.

        Replaying an identical proposal returns the existing one rather than
        queueing a second approval for the same credit note. `customer_id` is
        the verified customer's, which the per-customer blocker reads.
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
                 expires_at, tax_breakdown, customer_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?)
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
                    (
                        json.dumps(tax_breakdown.to_record(), sort_keys=True)
                        if tax_breakdown is not None
                        else None
                    ),
                    customer_id,
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
        *,
        first_attempt_at: datetime | None = None,
        uncertain_since: datetime | None = None,
        provider_status: str | None = None,
        provider_status_at: datetime | None = None,
        attention: str | None | object = _KEEP,
        unless_incident: bool = False,
    ) -> PendingAction:
        """Move the action to `to`, from the state it is read in.

        `first_attempt_at` and `uncertain_since` are kept once set: the
        first value wins. The others replace what was there when given; the
        attention is kept as the row holds it -- not as it was read -- unless
        given, and None clears it. A claim (`EXECUTING`), and any transition
        made `unless_incident` -- what the system settles on its own -- is
        refused, in the `UPDATE` itself, while the action has an incident:
        one another process recorded after this one read the action stops it
        too. Only a person's status read settles an incident.

        A claim takes a fresh `claim_token`, and, for a credit note of a known
        customer, is refused in the same `UPDATE` while another credit note
        of that customer is executing, of unknown outcome, or marked
        `partial_write` or `no_trace`, and not resolved by a person
        (spec 015, FR-009): one statement, so two actions of one customer
        cannot both claim, in this process or another.
        """
        current = self.get(action_id)
        keep = attention is _KEEP
        _check_attention(None if keep else attention)
        guarded = to is ActionState.EXECUTING or unless_incident
        if guarded and current.attention in INCIDENTS:
            raise NeedsAPerson(action_id, current.state, current.attention)
        if to not in ALLOWED[current.state]:
            raise IllegalTransition(action_id, current.state, to)
        # EXECUTED is a claim that money moved, so it takes the provider's
        # reference as proof. Without one, the state would say a credit note
        # exists that nobody issued.
        if to is ActionState.EXECUTED and not provider_action_id:
            raise IllegalTransition(
                action_id, current.state, to, why="without the provider's reference"
            )
        claiming = to is ActionState.EXECUTING
        blockable = (
            claiming and current.customer_id is not None and current.action_type == BLOCKING_TYPE
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
                    approved_fingerprint = COALESCE(?, approved_fingerprint),
                    first_attempt_at = COALESCE(first_attempt_at, ?),
                    uncertain_since = COALESCE(uncertain_since, ?),
                    provider_status = COALESCE(?, provider_status),
                    provider_status_at = COALESCE(?, provider_status_at),
                    attention = CASE WHEN ? THEN attention ELSE ? END,
                    claim_token = COALESCE(?, claim_token)
                WHERE action_id = ? AND state = ?
                  AND NOT (? AND COALESCE(attention, '') IN ({incidents}))
                  AND NOT (? AND {blocker})
                """.format(incidents=", ".join("?" * len(INCIDENTS)), blocker=_BLOCKER),
                (
                    to.value,
                    datetime.now(UTC).isoformat(),
                    approved_by,
                    provider_action_id,
                    failure_reason,
                    resolution,
                    approved_fingerprint,
                    _iso(first_attempt_at),
                    _iso(uncertain_since),
                    provider_status,
                    _iso(provider_status_at),
                    keep,
                    None if keep else attention,
                    uuid.uuid4().hex if claiming else None,
                    action_id,
                    current.state.value,
                    guarded,
                    *sorted(INCIDENTS),
                    blockable,
                    *_blocker_parameters(current),
                ),
            ).rowcount
        if updated != 1:
            held = self._attention_now(action_id)
            if guarded and held in INCIDENTS:
                raise NeedsAPerson(action_id, current.state, held)
            if blockable:
                blocker = self.blocking(current)
                if blocker is not None:
                    raise BlockedByAnother(action_id, current.state, blocker)
            raise IllegalTransition(
                action_id, current.state, to, why="(its state changed concurrently)"
            )
        return self.get(action_id)

    def blocking(self, action: PendingAction) -> str | None:
        """The action that blocks `action`'s claim now, or None: another
        credit note of its customer, unresolved, executing, of unknown
        outcome or left half-way (FR-009). Older rows, with no customer,
        never block."""
        if action.customer_id is None or action.action_type != BLOCKING_TYPE:
            return None
        row = self._connection.execute(
            f"SELECT other.action_id FROM pending_actions AS other WHERE {_BLOCKER_WHERE} "
            "ORDER BY other.created_at LIMIT 1",
            _blocker_parameters(action),
        ).fetchone()
        return row[0] if row else None

    def record_request(self, action_id: str, claim_token: str, at: datetime) -> bool:
        """Before a request of an attempt: whether the row is still
        `EXECUTING` under this claim, recording the request's instant if it
        is -- one conditional `UPDATE`, durable before the request leaves
        (spec 015, FR-011). False means the claim is gone: send nothing."""
        with self._connection:
            updated = self._connection.execute(
                """
                UPDATE pending_actions
                SET last_request_at = ?
                WHERE action_id = ? AND state = ? AND claim_token = ?
                """,
                (at.isoformat(), action_id, ActionState.EXECUTING.value, claim_token),
            ).rowcount
        return updated == 1

    def mark_partial(
        self,
        action_id: str,
        *,
        attention: str,
        resolution: str,
        at: datetime,
        failure_reason: str | None = None,
        partial_document: dict | None = None,
    ) -> PendingAction:
        """A document left half-way, or a write that may still land with no
        trace (`partial_write`, `no_trace`): recorded whatever state the row
        is in now -- an `EXECUTING` one becomes `UNKNOWN_OUTCOME`, any other
        keeps its state -- so a worker whose claim was taken over still
        leaves what it made on the record. Final for everything automatic.
        `partial_write` replaces `no_trace`, never the reverse: a document
        found is more than none."""
        if attention not in RESOLVABLE:
            raise ValueError(f"not a partial outcome: {attention!r}")
        self.get(action_id)
        with self._connection:
            self._connection.execute(
                """
                UPDATE pending_actions
                SET state = CASE WHEN state = ? THEN ? ELSE state END,
                    updated_at = ?,
                    attention = CASE WHEN attention = ? THEN attention ELSE ? END,
                    resolution = ?,
                    failure_reason = COALESCE(?, failure_reason),
                    uncertain_since = COALESCE(uncertain_since, ?),
                    partial_document = COALESCE(?, partial_document)
                WHERE action_id = ?
                """,
                (
                    ActionState.EXECUTING.value,
                    ActionState.UNKNOWN_OUTCOME.value,
                    datetime.now(UTC).isoformat(),
                    "partial_write",
                    attention,
                    resolution,
                    failure_reason,
                    at.isoformat(),
                    _document(partial_document),
                    action_id,
                ),
            )
        return self.get(action_id)

    def record_status_read(self, action_id: str, at: datetime, found: str) -> PendingAction:
        """A person's status read: when it was made and what it found,
        empty results included (FR-009a). Kept whatever the action's state."""
        self.get(action_id)
        with self._connection:
            self._connection.execute(
                "UPDATE pending_actions SET status_read_at = ?, status_read_found = ? "
                "WHERE action_id = ?",
                (at.isoformat(), found, action_id),
            )
        return self.get(action_id)

    def record_resolution(self, action_id: str, by: str, note: str, at: datetime) -> PendingAction:
        """A person's account of how they resolved a partial write or a write
        that left no trace (spec 015, FR-009a).

        One conditional `UPDATE`: only on `partial_write` or `no_trace`, only
        once, and only when the latest status read was made at least
        `SETTLING_DELAY` after the attempt's last request -- read from the
        durable times, never from the caller. It releases the customer's
        blocker; the incident stays, so the action is never executed again.
        """
        current = self.get(action_id)
        with self._connection:
            updated = self._connection.execute(
                f"""
                UPDATE pending_actions
                SET resolved_by = ?, resolved_at = ?, resolution_note = ?, updated_at = ?
                WHERE action_id = ?
                  AND attention IN (?, ?)
                  AND resolved_at IS NULL
                  AND {_SETTLED}
                """,
                (
                    by,
                    at.isoformat(),
                    note,
                    datetime.now(UTC).isoformat(),
                    action_id,
                    *sorted(RESOLVABLE),
                    SETTLING_DELAY.total_seconds(),
                ),
            ).rowcount
        if updated != 1:
            raise ResolutionRefused(action_id, current.state, self._why_not_resolvable(action_id))
        return self.get(action_id)

    def _why_not_resolvable(self, action_id: str) -> str:
        action = self.get(action_id)
        if action.attention not in RESOLVABLE:
            return "only a partial write, or a write that left no trace, is resolved by a person"
        if action.resolved_at is not None:
            return f"resolved already, by {action.resolved_by}"
        return (
            "a status read made at least ten minutes after the attempt's last request "
            "comes first: a request may still have been in flight"
        )

    def _attention_now(self, action_id: str) -> str | None:
        """The attention as the row holds it now, read past any cached view."""
        row = self._connection.execute(
            "SELECT attention FROM pending_actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        return row["attention"] if row else None

    def annotate(
        self,
        action_id: str,
        expected: ActionState,
        *,
        attention: str | None | object = _KEEP,
        resolution: str | None = None,
        allocation: str | None = None,
        provider_status: str | None = None,
        provider_status_at: datetime | None = None,
    ) -> PendingAction:
        """Record something about an action that stays in its state.

        Conditional like a transition: if the action is no longer in
        `expected`, nothing changes and it raises. The attention is replaced
        when given -- None clears it -- and kept otherwise; the allocation is
        kept once set.
        """
        current = self.get(action_id)
        if current.state is not expected:
            raise IllegalTransition(action_id, current.state, expected)
        keep = attention is _KEEP
        _check_attention(None if keep else attention)
        with self._connection:
            updated = self._connection.execute(
                """
                UPDATE pending_actions
                SET updated_at = ?,
                    attention = CASE WHEN ? THEN attention ELSE ? END,
                    resolution = COALESCE(?, resolution),
                    allocation = COALESCE(allocation, ?),
                    provider_status = COALESCE(?, provider_status),
                    provider_status_at = COALESCE(?, provider_status_at)
                WHERE action_id = ? AND state = ?
                """,
                (
                    datetime.now(UTC).isoformat(),
                    keep,
                    None if keep else attention,
                    resolution,
                    allocation,
                    provider_status,
                    _iso(provider_status_at),
                    action_id,
                    expected.value,
                ),
            ).rowcount
        if updated != 1:
            raise IllegalTransition(
                action_id, current.state, expected, why="(its state changed concurrently)"
            )
        return self.get(action_id)

    def close(self) -> None:
        self._connection.close()


#: Another credit note of the same customer that blocks a claim: unresolved,
#: and executing, of unknown outcome, or left half-way (FR-009). Parameters:
#: customer, own action id, type, two states, two attentions.
_BLOCKER_WHERE = """other.customer_id = ? AND other.action_id != ? AND other.action_type = ?
      AND other.resolved_at IS NULL
      AND (other.state IN (?, ?) OR COALESCE(other.attention, '') IN (?, ?))"""
_BLOCKER = f"EXISTS (SELECT 1 FROM pending_actions AS other WHERE {_BLOCKER_WHERE})"

#: When a status read may settle a resolution: at least `SETTLING_DELAY`
#: (seconds, the one parameter) after the attempt's last request -- or, when
#: no request was recorded, after its outcome became unknown; the later of the
#: two when both are known.
_SETTLED = """status_read_at IS NOT NULL
      AND COALESCE(last_request_at, uncertain_since) IS NOT NULL
      AND (julianday(status_read_at) - julianday(MAX(
              COALESCE(last_request_at, uncertain_since),
              COALESCE(uncertain_since, last_request_at)))) * 86400.0 >= ?"""


def _blocker_parameters(action: PendingAction) -> tuple:
    return (
        action.customer_id,
        action.action_id,
        BLOCKING_TYPE,
        ActionState.EXECUTING.value,
        ActionState.UNKNOWN_OUTCOME.value,
        *sorted(RESOLVABLE),
    )


def _document(document: dict | None) -> str | None:
    return json.dumps(document, sort_keys=True) if document is not None else None


def _check_attention(attention: object) -> None:
    if attention is not None and attention not in ATTENTION:
        raise ValueError(f"unknown attention {attention!r}")


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _moment(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


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
        expires_at=(datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None),
        approved_fingerprint=row["approved_fingerprint"],
        first_attempt_at=_moment(row["first_attempt_at"]),
        allocation=row["allocation"],
        uncertain_since=_moment(row["uncertain_since"]),
        provider_status=row["provider_status"],
        provider_status_at=_moment(row["provider_status_at"]),
        attention=row["attention"],
        tax_breakdown=(
            TaxBreakdown.from_record(json.loads(row["tax_breakdown"]))
            if row["tax_breakdown"]
            else None
        ),
        customer_id=row["customer_id"],
        claim_token=row["claim_token"],
        last_request_at=_moment(row["last_request_at"]),
        partial_document=(
            json.loads(row["partial_document"]) if row["partial_document"] else None
        ),
        status_read_at=_moment(row["status_read_at"]),
        status_read_found=row["status_read_found"],
        resolved_by=row["resolved_by"],
        resolved_at=_moment(row["resolved_at"]),
        resolution_note=row["resolution_note"],
    )
