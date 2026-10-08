"""Approve, reject, execute. The only path by which money moves.

Nothing else in the codebase calls ``create_credit_note``. That is the point
of ADR-004: the decision engine recommends, the action policy authorises, and
this single module executes -- after a human has said yes.

Retry policy, from the plan: reads retry freely, the one write never retries
blindly. On a timeout the caller re-issues the same request, the idempotency
key resolves to the same action, and the provider returns the credit note it
already created instead of issuing a second one.
"""

from __future__ import annotations

from app.domain.enums import ActionState
from app.infrastructure.actions import IllegalTransition, PendingAction, SqliteActionStore
from app.providers.base import BillingProvider


class ApprovalService:
    def __init__(self, provider: BillingProvider, actions: SqliteActionStore) -> None:
        self._provider = provider
        self._actions = actions

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

        # An approved action is executed; a failed one is retried, through the
        # provider and with the same idempotency key, never by marking it
        # done. Anything else -- awaiting approval, rejected, expired -- is
        # refused with its current state, which is what a caller needs in
        # order to know whether to retry.
        if action.state not in (ActionState.APPROVED, ActionState.FAILED):
            raise IllegalTransition(action_id, action.state, ActionState.EXECUTED)

        try:
            result = await self._provider.create_credit_note(
                invoice_id=action.invoice_id,
                amount_minor=action.amount.amount_minor,
                currency=action.amount.currency,
                reason=action.reason,
                idempotency_key=action.idempotency_key,
            )
        except Exception as error:  # noqa: BLE001 - recorded, then re-raised
            self._actions.transition(
                action_id, ActionState.FAILED, failure_reason=str(error)
            )
            raise

        return self._actions.transition(
            action_id,
            ActionState.EXECUTED,
            provider_action_id=result.provider_action_id,
        )
