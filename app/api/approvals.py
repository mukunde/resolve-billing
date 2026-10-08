"""Human-in-the-loop endpoints, and reading a run back.

These are what n8n's Wait node resumes into. The orchestrator holds no state:
it holds two URLs.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import get_actions, get_approvals, get_audit
from app.application.execute_action import ApprovalService
from app.infrastructure.actions import (
    IllegalTransition,
    PendingAction,
    SqliteActionStore,
    UnknownAction,
)
from app.infrastructure.audit import SqliteAuditRepository
from app.providers.base import ProviderRejected

router = APIRouter(prefix="/api/v1", tags=["approvals"])


class Decider(BaseModel):
    by: str = Field(min_length=1, max_length=120)
    """Who is approving. Recorded; an approval with no name is not one."""


class ActionOut(BaseModel):
    action_id: str
    run_id: str
    state: str
    action_type: str
    invoice_id: str
    amount_minor: int
    currency: str
    idempotency_key: str
    approved_by: str | None = None
    provider_action_id: str | None = None
    failure_reason: str | None = None

    @classmethod
    def of(cls, action: PendingAction) -> ActionOut:
        return cls(
            action_id=action.action_id,
            run_id=action.run_id,
            state=action.state.value,
            action_type=action.action_type,
            invoice_id=action.invoice_id,
            amount_minor=action.amount.amount_minor,
            currency=action.amount.currency,
            idempotency_key=action.idempotency_key,
            approved_by=action.approved_by,
            provider_action_id=action.provider_action_id,
            failure_reason=action.failure_reason,
        )


def _guard(call):
    try:
        return call()
    except UnknownAction as error:
        raise HTTPException(404, str(error)) from error
    except IllegalTransition as error:
        # 409, not 400: the request is well formed, the action is simply not
        # in a state where it means anything.
        raise HTTPException(409, str(error)) from error


@router.post("/actions/{action_id}/approve", response_model=ActionOut)
async def approve(
    action_id: str,
    decider: Decider,
    approvals: Annotated[ApprovalService, Depends(get_approvals)],
) -> ActionOut:
    return ActionOut.of(_guard(lambda: approvals.approve(action_id, decider.by)))


@router.post("/actions/{action_id}/reject", response_model=ActionOut)
async def reject(
    action_id: str,
    decider: Decider,
    approvals: Annotated[ApprovalService, Depends(get_approvals)],
) -> ActionOut:
    return ActionOut.of(_guard(lambda: approvals.reject(action_id, decider.by)))


@router.post("/actions/{action_id}/execute", response_model=ActionOut)
async def execute(
    action_id: str,
    approvals: Annotated[ApprovalService, Depends(get_approvals)],
) -> ActionOut:
    try:
        return ActionOut.of(await approvals.execute(action_id))
    except UnknownAction as error:
        raise HTTPException(404, str(error)) from error
    except IllegalTransition as error:
        raise HTTPException(409, str(error)) from error
    except ProviderRejected as error:
        # The platform answered no: the action is FAILED, nothing was written.
        raise HTTPException(502, f"the billing platform refused the write: {error}") from error
    except Exception as error:
        # No answer: the action is UNKNOWN_OUTCOME. Calling execute again
        # asks the platform what it holds before writing anything. ADR-008.
        raise HTTPException(
            504,
            "no answer from the billing platform; the outcome is unknown and will be "
            "reconciled on the next execute",
        ) from error


@router.get("/actions/{action_id}", response_model=ActionOut)
async def read_action(
    action_id: str,
    actions: Annotated[SqliteActionStore, Depends(get_actions)],
) -> ActionOut:
    return ActionOut.of(_guard(lambda: actions.get(action_id)))


@router.get("/runs/{run_id}")
async def read_run(
    run_id: str,
    audit: Annotated[SqliteAuditRepository, Depends(get_audit)],
    actions: Annotated[SqliteActionStore, Depends(get_actions)],
) -> dict:
    """Everything known about a run, days later if need be.

    This is what makes the flow resumable without an orchestrator holding
    state: the run, its trace, and whatever action is still waiting.
    """
    row = audit.run(run_id)
    if row is None:
        raise HTTPException(404, f"unknown run {run_id!r}")
    return {
        "run": dict(row),
        "events": [dict(event) for event in audit.events(run_id)],
        "actions": [ActionOut.of(a).model_dump() for a in actions.for_run(run_id)],
    }
