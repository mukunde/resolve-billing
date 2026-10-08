"""Approve, reject, execute. The only path by which money moves.

Nothing else in the codebase calls ``create_credit_note``. That is the point
of ADR-004: the decision engine recommends, the action policy authorises, and
this single module executes -- after a human has said yes.

Retry policy: reads retry freely, the one write never retries blindly. The
platform either answers -- the write succeeded, or it was refused -- or it
does not, and then the outcome is unknown: the credit note may exist with its
response lost. An unknown outcome is settled by asking the platform what it
holds under the idempotency key, never by writing again on a guess. See
ADR-008.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.enums import ActionState
from app.infrastructure.actions import IllegalTransition, PendingAction, SqliteActionStore
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

    def approve(self, action_id: str, approved_by: str) -> PendingAction:
        """Record who said yes. Approving does not execute.

        Two steps rather than one so that the audit answers "who authorised
        this" separately from "did it go through", and so a provider outage
        does not force a second human decision.
        """
        return self._actions.transition(
            action_id, ActionState.APPROVED, approved_by=approved_by
        )

    def reject(self, action_id: str, rejected_by: str) -> PendingAction:
        return self._actions.transition(
            action_id, ActionState.REJECTED, approved_by=rejected_by
        )

    async def execute(self, action_id: str) -> PendingAction:
        """Carry out an approved action against the billing platform."""
        action = self._actions.get(action_id)

        # Replaying an execute on something already done returns it unchanged
        # rather than transitioning again -- the caller may simply be a retry
        # that lost the first response.
        if action.state is ActionState.EXECUTED:
            return action

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

        # Claim the write before making it. Of two concurrent executions, one
        # moves the action to EXECUTING and rings the provider; the other
        # finds it claimed and is refused. The claim is made before the first
        # await, so nothing can interleave between the read and the claim in
        # this process, and the store's conditional UPDATE covers the others.
        self._actions.transition(action_id, ActionState.EXECUTING)

        try:
            result = await self._provider.create_credit_note(
                invoice_id=action.invoice_id,
                amount_minor=action.amount.amount_minor,
                currency=action.amount.currency,
                reason=action.reason,
                idempotency_key=action.idempotency_key,
            )
        except ProviderRejected as error:
            # The platform answered no: nothing was created.
            self._actions.transition(
                action_id, ActionState.FAILED, failure_reason=str(error)
            )
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
