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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.domain.enums import ActionState
from app.infrastructure.actions import (
    INCIDENTS,
    IllegalTransition,
    NeedsAPerson,
    PendingAction,
    SqliteActionStore,
    WritesNotSupported,
)
from app.providers.base import ALLOCATIONS, BillingProvider, ProviderAction, ProviderRejected

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
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._actions = actions
        self._stale_after = stale_after
        self._now = now or (lambda: datetime.now(UTC))

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

        # An incident stops everything automatic for the action, for good:
        # nothing is reconciled, read or sent on a retry. A person decides.
        if action.attention in INCIDENTS:
            raise NeedsAPerson(action_id, action.state, action.attention)

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
        # The first claim records when the write was first attempted, before
        # anything is sent; a later claim keeps it.
        action = self._actions.transition(
            action_id, ActionState.EXECUTING, first_attempt_at=self._now()
        )
        send_by = action.first_attempt_at + self._provider.idempotency_window

        try:
            # How the write lands on the invoice is planned once, before the
            # first send, and every retry sends exactly that.
            allocation = action.allocation
            if allocation is None:
                allocation = await self._provider.plan_write(action.invoice_id, action.action_type)
                self._actions.annotate(action_id, ActionState.EXECUTING, allocation=allocation)
            result = await write(
                invoice_id=action.invoice_id,
                amount_minor=action.amount.amount_minor,
                currency=action.amount.currency,
                reason=action.reason,
                idempotency_key=action.idempotency_key,
                action_id=action.action_id,
                allocation=allocation,
                send_by=send_by,
            )
        except ProviderRejected as error:
            # The platform answered no: nothing was created.
            self._actions.transition(action_id, ActionState.FAILED, failure_reason=str(error))
            raise
        except Exception as error:  # noqa: BLE001 - recorded, then re-raised
            # No answer, or one we cannot read: the write may have landed.
            # An answer the adapter could not read is an incident besides.
            unreadable = getattr(error, "category", None) == "unreadable_answer"
            self._actions.transition(
                action_id,
                ActionState.UNKNOWN_OUTCOME,
                failure_reason=f"{type(error).__name__}: {error}",
                uncertain_since=self._now(),
                **({"attention": "unreadable_answer"} if unreadable else {}),
            )
            raise

        # The platform's own answer is checked like a write found again:
        # recorded only if it is the write the person approved.
        problem = _unlike(action, allocation, result)
        if problem is not None:
            self._actions.transition(
                action_id,
                ActionState.UNKNOWN_OUTCOME,
                failure_reason=f"the platform's answer is not the write approved ({problem})",
                uncertain_since=self._now(),
                attention=problem,
            )
            raise NeedsAPerson(action_id, ActionState.UNKNOWN_OUTCOME, problem)
        return self._record(action_id, result, how="made")

    def _record(self, action_id: str, made: ProviderAction, how: str) -> PendingAction:
        """The action EXECUTED with the platform's reference, and what the
        platform said of the write, dated. A status but success is put to a
        person; the write is never made again for it."""
        followed = made.status != "succeeded"
        resolution = (
            f"{how} as {made.provider_action_id}; the platform says {made.status} "
            f"({made.observed_at:%Y-%m-%d %H:%M} UTC): to follow up"
            if followed
            else f"{how} as {made.provider_action_id}"
        )
        return self._actions.transition(
            action_id,
            ActionState.EXECUTED,
            provider_action_id=made.provider_action_id,
            resolution=resolution,
            provider_status=made.status,
            provider_status_at=made.observed_at,
            attention="not_successful" if followed else None,
        )

    async def refresh_status(self, action_id: str) -> PendingAction:
        """A person has the platform asked again what became of the write.

        Nothing is ever sent: a read, which needs no writing, so it works
        with writes switched off. For a write made, the status observed is
        recorded with its instant. For an action an incident left to a
        person, the platform's answer settles it only when it holds exactly
        one write alike; anything else leaves it where it is, and never
        permits a new write (specs/002-stripe-writes, FR-003).
        """
        action = self._actions.get(action_id)
        left = action.attention in INCIDENTS and action.state in (
            ActionState.UNKNOWN_OUTCOME,
            ActionState.EXECUTING,
            ActionState.FAILED,
        )
        if action.state is not ActionState.EXECUTED and not left:
            raise IllegalTransition(
                action_id,
                action.state,
                ActionState.EXECUTED,
                why="(only a write made, or an action left to a person, has a status to read)",
            )
        found = await self._provider.find_action(action.idempotency_key, action.invoice_id)
        if action.state is ActionState.EXECUTED:
            return self._observed(action, found)
        return self._settled_by_a_person(action, found)

    def _observed(self, action: PendingAction, found: list[ProviderAction]) -> PendingAction:
        mine = [made for made in found if made.provider_action_id == action.provider_action_id]
        if len(mine) != 1:
            return self._actions.annotate(
                action.action_id,
                ActionState.EXECUTED,
                attention="unlike_the_action",
                resolution=(
                    f"status read: the platform no longer shows {action.provider_action_id} "
                    f"alone under this idempotency key; left to a person"
                ),
            )
        (made,) = mine
        problem = _unlike(action, action.allocation, made)
        if problem is not None:
            return self._actions.annotate(
                action.action_id,
                ActionState.EXECUTED,
                attention=problem,
                resolution=f"status read: {made.provider_action_id} is not the write approved",
            )
        followed = made.status != "succeeded"
        return self._actions.annotate(
            action.action_id,
            ActionState.EXECUTED,
            attention="not_successful" if followed else None,
            provider_status=made.status,
            provider_status_at=made.observed_at,
            resolution=(
                f"status read: the platform says {made.status} "
                f"({made.observed_at:%Y-%m-%d %H:%M} UTC)" + (": to follow up" if followed else "")
            ),
        )

    def _settled_by_a_person(
        self, action: PendingAction, found: list[ProviderAction]
    ) -> PendingAction:
        if len(found) == 1:
            (made,) = found
            problem = _unlike(action, action.allocation, made)
            if problem is None:
                return self._record(action.action_id, made, how="found by a person's status read")
            return self._actions.annotate(
                action.action_id,
                action.state,
                attention=problem,
                resolution=f"status read: {made.provider_action_id} is not the write approved",
            )
        if found:
            return self._actions.annotate(
                action.action_id,
                action.state,
                attention="several_writes",
                resolution=(
                    f"status read: the platform holds {len(found)} writes under this "
                    f"idempotency key; left to a person"
                ),
            )
        return self._actions.annotate(
            action.action_id,
            action.state,
            resolution="status read: nothing under this idempotency key; left to a person",
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

        found = await self._provider.find_action(action.idempotency_key, action.invoice_id)
        if len(found) > 1:
            # Two writes under one key: an incident for a person, never "the
            # first one found". Nothing is written, and nothing settled.
            if action.state is ActionState.EXECUTING:
                self._actions.transition(
                    action_id, ActionState.UNKNOWN_OUTCOME, uncertain_since=self._now()
                )
            return self._actions.annotate(
                action_id,
                ActionState.UNKNOWN_OUTCOME,
                attention="several_writes",
                resolution=(
                    f"reconciled: the platform holds {len(found)} writes under this "
                    f"idempotency key; left to a person"
                ),
            )
        if found:
            (made,) = found
            return self._actions.transition(
                action_id,
                ActionState.EXECUTED,
                provider_action_id=made.provider_action_id,
                resolution=(
                    f"reconciled: the platform already held {made.provider_action_id} "
                    f"under this idempotency key; no second write"
                ),
            )
        return self._actions.transition(
            action_id,
            ActionState.FAILED,
            failure_reason="no write found on the platform after an unknown outcome",
            resolution="reconciled: nothing held under this idempotency key; safe to retry",
        )


def _unlike(action: PendingAction, allocation: str | None, made: ProviderAction) -> str | None:
    """Why a write the platform holds is not the one approved, or None.

    Every field the action fixed is compared; a write that does not say how
    it was allocated, or what became of it, cannot be read. With no
    allocation planned (an action older than the plan, or one whose plan
    never completed), the write's must be one its type allows.
    """
    if made.allocation is None or made.status is None:
        return "unreadable_answer"
    if (
        made.invoice_id != action.invoice_id
        or made.action_type != action.action_type
        or made.amount_minor != action.amount.amount_minor
        or made.currency != action.amount.currency
        or not made.test_mode
        or made.idempotency_key != action.idempotency_key
        or (made.action_id is not None and made.action_id != action.action_id)
    ):
        return "unlike_the_action"
    if allocation is not None and made.allocation != allocation:
        return "unlike_the_action"
    if made.allocation not in ALLOCATIONS.get(action.action_type, frozenset()):
        return "unlike_the_action"
    return None
