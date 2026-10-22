"""Approve, reject, execute. The only path by which money moves.

Nothing else in the codebase calls ``create_credit_note`` or
``create_refund``. That is the point of ADR-004: the decision engine
recommends, the action policy authorises, and this single module executes --
after a human has said yes, and exactly the instrument the human approved.

Retry policy: reads retry freely, a write never retries blindly. The
platform either answers -- the write succeeded, or it was refused -- or it
does not, and then the outcome is unknown: the credit note may exist with its
response lost. An unknown outcome is settled by asking the platform what it
holds under the idempotency key, never by writing again on a guess. See
ADR-008.

An approval holds for what was shown, and not forever. A request carries a
deadline; past it, it can no longer be decided. The approver decides on a
fingerprint of the proposal and of the invoice as the platform presented
it; if either changed in between, or changes between approval and
execution, the action expires instead of going through on facts nobody
looked at.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from app.domain.enums import ActionState
from app.infrastructure.actions import (
    IllegalTransition,
    PendingAction,
    SqliteActionStore,
    WritesNotSupported,
)
from app.providers.base import BillingProvider, ProviderRejected

#: How long a claim on the write may go without a recorded result before it
#: is treated as abandoned -- a process that died between ringing the
#: provider and writing down the answer. Generous on purpose: reconciling a
#: claim that is still in flight is safe, since the idempotency key makes the
#: two writes one, but it is noise.
STALE_CLAIM = timedelta(minutes=5)


class ApprovalService:
    def __init__(
        self,
        provider: BillingProvider,
        actions: SqliteActionStore,
        stale_after: timedelta = STALE_CLAIM,
    ) -> None:
        self._provider = provider
        self._actions = actions
        self._stale_after = stale_after

    async def proposal_fingerprint(self, action: PendingAction) -> str:
        """What an approver is asked to approve, reduced to a comparable value.

        The proposal itself -- instrument, invoice, amount -- and the invoice
        as the platform holds it now: status, total, lines. Read from the
        platform, not from a copy of ours, so a change made there shows.
        """
        invoice = await self._provider.find_invoice(action.invoice_id)
        material = {
            "action_type": action.action_type,
            "invoice_id": action.invoice_id,
            "amount_minor": action.amount.amount_minor,
            "currency": action.amount.currency,
            "invoice": None if invoice is None else invoice.model_dump(mode="json"),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()[:32]

    async def approve(self, action_id: str, approved_by: str, presented: str) -> PendingAction:
        """Record who said yes, to what. Approving does not execute.

        Two steps rather than one so that the audit answers "who authorised
        this" separately from "did it go through", and so a provider outage
        does not force a second human decision.

        ``presented`` is the fingerprint of the proposal as it was shown to
        the approver. If the proposal no longer matches it, the yes was given
        to something else: the action expires, and the dispute goes back to
        be investigated on today's facts.
        """
        # Past its deadline, the request is expired and the transition
        # below refuses it.
        action = self.expire_if_overdue(action_id)
        current = await self.proposal_fingerprint(action)
        if action.state is ActionState.PENDING_APPROVAL and presented != current:
            self._actions.transition(
                action_id,
                ActionState.EXPIRED,
                resolution=(
                    "the proposal or its invoice changed after it was presented; "
                    "approval refused, to be investigated again"
                ),
            )
            raise IllegalTransition(
                action_id,
                ActionState.EXPIRED,
                ActionState.APPROVED,
                why="(the proposal changed after it was presented)",
            )
        return self._actions.transition(
            action_id,
            ActionState.APPROVED,
            approved_by=approved_by,
            approved_fingerprint=current,
        )

    def reject(self, action_id: str, rejected_by: str) -> PendingAction:
        self.expire_if_overdue(action_id)
        return self._actions.transition(action_id, ActionState.REJECTED, approved_by=rejected_by)

    def expire_if_overdue(self, action_id: str) -> PendingAction:
        """Expire a request nobody decided in time; return the action.

        Checked when someone tries to decide, not by a sweeper: a request
        that is never opened again does no harm by staying pending, and one
        that is opened finds out then.
        """
        action = self._actions.get(action_id)
        if action.state is ActionState.PENDING_APPROVAL and action.expired():
            return self._actions.transition(
                action_id,
                ActionState.EXPIRED,
                resolution=(
                    f"no decision before the deadline, {action.expires_at:%Y-%m-%d %H:%M} UTC"
                ),
            )
        return action

    async def execute(self, action_id: str) -> PendingAction:
        """Carry out an approved action against the billing platform."""
        action = self._actions.get(action_id)

        # Replaying an execute on something already done returns it unchanged
        # rather than transitioning again -- the caller may simply be a retry
        # that lost the first response.
        if action.state is ActionState.EXECUTED:
            return action

        # A platform that cannot write is never asked: refused here, before
        # any read, reconciliation or claim, so nothing has happened.
        if not self._provider.can_write:
            raise WritesNotSupported(action_id, action.state)

        # The last attempt got no answer, or a claim was abandoned by a
        # process that died mid-write. Find out what happened before anything
        # else: if the write landed, it is recorded and returned; if not, the
        # action is FAILED and the write is made again below. A claim still
        # in flight is left alone, and refused below.
        if action.state in (ActionState.UNKNOWN_OUTCOME, ActionState.EXECUTING):
            action = await self.reconcile(action_id)
            if action.state is ActionState.EXECUTED:
                return action

        # An approved action is executed; a failed one is retried, through the
        # provider and with the same idempotency key, never by marking it
        # done. Anything else -- awaiting approval, rejected, expired, being
        # executed -- is refused with its current state, which is what a
        # caller needs in order to know whether to retry.
        if action.state not in (ActionState.APPROVED, ActionState.FAILED):
            raise IllegalTransition(action_id, action.state, ActionState.EXECUTED)

        # The approval was given to a proposal on a given invoice. If the
        # invoice changed since -- paid, voided, re-issued -- it no longer
        # covers this write: the action expires and nothing is claimed. An
        # approval recorded before fingerprints existed carries none, and is
        # held to the guards below only.
        if (
            action.approved_fingerprint is not None
            and await self.proposal_fingerprint(action) != action.approved_fingerprint
        ):
            self._actions.transition(
                action_id,
                ActionState.EXPIRED,
                resolution=(
                    "the invoice changed after approval; nothing written, to be investigated again"
                ),
            )
            raise IllegalTransition(
                action_id,
                ActionState.EXPIRED,
                ActionState.EXECUTED,
                why="(the invoice changed after approval)",
            )

        # The write is the one the human approved, by type. A type with no
        # write on the platform is refused before anything is claimed: never
        # replaced by the nearest instrument that does exist.
        writes = {
            "credit_note": self._provider.create_credit_note,
            "refund": self._provider.create_refund,
        }
        write = writes.get(action.action_type)
        if write is None:
            raise IllegalTransition(
                action_id,
                action.state,
                ActionState.EXECUTED,
                why=f"(no write on the platform for action type {action.action_type!r})",
            )

        # One guard of our own, before anything is claimed: never more than
        # the invoice itself. The platform holds the ledger and caps what an
        # invoice can still give back (ADR-008); this only stops a proposal
        # bug from relying on the platform alone. Read from the platform, so
        # the figure is its own, not one we keep.
        invoice = await self._provider.find_invoice(action.invoice_id)
        if invoice is not None and (
            action.amount.currency != invoice.total.currency
            or action.amount.amount_minor > invoice.total.amount_minor
        ):
            raise IllegalTransition(
                action_id,
                action.state,
                ActionState.EXECUTED,
                why=(
                    f"({action.amount} exceeds invoice {invoice.id}, which totals {invoice.total})"
                ),
            )

        # Claim the write before making it. Of two concurrent executions, one
        # moves the action to EXECUTING and rings the provider; the other
        # finds it claimed and is refused. The guarantee is the store's
        # conditional UPDATE: transition() re-reads the state and only moves
        # it from the one it found, so whichever caller claims second fails,
        # in this process or another.
        self._actions.transition(action_id, ActionState.EXECUTING)

        try:
            result = await write(
                invoice_id=action.invoice_id,
                amount_minor=action.amount.amount_minor,
                currency=action.amount.currency,
                reason=action.reason,
                idempotency_key=action.idempotency_key,
            )
        except ProviderRejected as error:
            # The platform answered no: nothing was created.
            self._actions.transition(action_id, ActionState.FAILED, failure_reason=str(error))
            raise
        except Exception as error:  # noqa: BLE001 - recorded, then re-raised
            # No answer, or one we cannot read: the write may have landed.
            self._actions.transition(
                action_id,
                ActionState.UNKNOWN_OUTCOME,
                failure_reason=f"{type(error).__name__}: {error}",
            )
            raise

        return self._actions.transition(
            action_id,
            ActionState.EXECUTED,
            provider_action_id=result.provider_action_id,
        )

    async def reconcile(
        self, action_id: str, stale_after: timedelta | None = None
    ) -> PendingAction:
        """Settle an action whose outcome nobody recorded.

        Two situations reach here: the platform did not answer
        (UNKNOWN_OUTCOME), or a claim was left in EXECUTING for longer than
        ``stale_after`` by a process that died before writing the answer
        down. Either way the platform is asked what it holds under the
        idempotency key. If the write landed, the action is EXECUTED with the
        platform's reference; if not, it is FAILED, which ``execute`` retries.
        Anything else is returned unchanged.
        """
        if not self._provider.can_write:
            raise WritesNotSupported(action_id, self._actions.get(action_id).state)
        action = self._actions.get(action_id)
        stale_after = self._stale_after if stale_after is None else stale_after
        abandoned = (
            action.state is ActionState.EXECUTING
            and action.updated_at is not None
            and datetime.now(UTC) - action.updated_at >= stale_after
        )
        if action.state is not ActionState.UNKNOWN_OUTCOME and not abandoned:
            return action

        found = await self._provider.find_action(action.idempotency_key)
        if found is not None:
            return self._actions.transition(
                action_id,
                ActionState.EXECUTED,
                provider_action_id=found.provider_action_id,
                resolution=(
                    f"reconciled: the platform already held {found.provider_action_id} "
                    f"under this idempotency key; no second write"
                ),
            )
        return self._actions.transition(
            action_id,
            ActionState.FAILED,
            failure_reason="no write found on the platform after an unknown outcome",
            resolution="reconciled: nothing held under this idempotency key; safe to retry",
        )
