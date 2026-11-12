"""Human-in-the-loop endpoints, and reading a run back.

These are what n8n's Wait node resumes into. The orchestrator holds no state:
it holds two URLs.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_serializer

from app.api.auth import Anyone, Approver, Service
from app.api.deps import get_actions, get_approvals, get_attempts, get_audit
from app.application.execute_action import IN_FLIGHT, RESOLUTION_NOTE_LIMIT, ApprovalService
from app.domain.enums import ActionState
from app.infrastructure.actions import (
    RESOLVABLE,
    IllegalTransition,
    PendingAction,
    SqliteActionStore,
    UnknownAction,
)
from app.infrastructure.attempts import SqliteAttemptStore
from app.infrastructure.audit import SqliteAuditRepository
from app.providers.base import ProviderRejected, ProviderUnavailable, ProviderUnsupported

router = APIRouter(prefix="/api/v1", tags=["approvals"])

#: What an action of a type the platform does not write says (spec 015, FR-001).
NOT_WRITTEN_HERE = (
    "cannot be executed on this platform; a person carries it out outside ResolveBilling"
)

#: Fields absent from the answer unless they hold something: older clients
#: read the same shape as before spec 015.
_OPTIONAL = (
    "notice",
    "warning",
    "partial_document",
    "last_request_at",
    "status_read_at",
    "status_read_found",
    "resolved_by",
    "resolved_at",
    "resolution_note",
)


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
    resolution: str | None = None
    expires_at: datetime | None = None
    #: When the write was first claimed, before it was first sent.
    first_attempt_at: datetime | None = None
    #: What the platform said of the write, and when: an observation, not
    #: the current state (specs/002-stripe-writes, FR-003).
    provider_status: str | None = None
    provider_status_at: datetime | None = None
    #: Why the action needs a person, if it does.
    attention: str | None = None
    #: While the action awaits a decision: what an approval must name,
    #: the fingerprint of the proposal and of its invoice as they stand.
    proposal: str | None = None
    #: What the amount was made of when proposed, on a taxed invoice; absent
    #: otherwise (specs/013-invoice-tax, FR-007).
    tax_breakdown: dict | None = None
    #: The invoice's tax is no longer the one proposed: nothing to approve.
    #: Absent unless true.
    stale: bool = False
    #: Set when the platform does not write this type of action: a person
    #: carries it out outside ResolveBilling (spec 015, FR-001).
    notice: str | None = None
    #: Set on a partial write, or a write without trace, not yet resolved: a
    #: request may still have been in flight (spec 015, FR-009a).
    warning: str | None = None
    #: The document left half-way, its id and stage (FR-006).
    partial_document: dict | None = None
    #: When the attempt last sent a request, and a person's latest status
    #: read: when and what it found (FR-009a).
    last_request_at: datetime | None = None
    status_read_at: datetime | None = None
    status_read_found: str | None = None
    #: Who recorded the resolution of a partial write, when, and what they did.
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None

    @model_serializer(mode="wrap")
    def _without_absent_breakdown(self, handler):
        data = handler(self)
        if self.tax_breakdown is None:
            data.pop("tax_breakdown", None)
        if not self.stale:
            data.pop("stale", None)
        for name in _OPTIONAL:
            if getattr(self, name) is None:
                data.pop(name, None)
        return data

    @classmethod
    def of(
        cls, action: PendingAction, proposal: str | None = None, *, written: bool = True
    ) -> ActionOut:
        """`written` is whether the platform writes the action's type."""
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
            resolution=action.resolution,
            expires_at=action.expires_at,
            first_attempt_at=action.first_attempt_at,
            provider_status=action.provider_status,
            provider_status_at=action.provider_status_at,
            attention=action.attention,
            proposal=proposal,
            tax_breakdown=(
                action.tax_breakdown.to_record() if action.tax_breakdown is not None else None
            ),
            notice=None if written else NOT_WRITTEN_HERE,
            warning=(
                IN_FLIGHT
                if action.attention in RESOLVABLE and action.resolved_at is None
                else None
            ),
            partial_document=action.partial_document,
            last_request_at=action.last_request_at,
            status_read_at=action.status_read_at,
            status_read_found=action.status_read_found,
            resolved_by=action.resolved_by,
            resolved_at=action.resolved_at,
            resolution_note=action.resolution_note,
        )


class ResolutionIn(BaseModel):
    #: What the person did on the platform, in their words.
    note: str = Field(min_length=1, max_length=RESOLUTION_NOTE_LIMIT)


class ApprovalIn(BaseModel):
    #: The proposal fingerprint the approver was shown, as read from
    #: ``GET /actions/{id}`` or the review page. D13.
    proposal: str


async def _guard(call):
    try:
        result = call()
        return await result if inspect.isawaitable(result) else result
    except UnknownAction as error:
        raise HTTPException(404, str(error)) from error
    except IllegalTransition as error:
        # 409, not 400: the request is well formed, the action is simply not
        # in a state where it means anything.
        raise HTTPException(409, str(error)) from error


@router.post("/actions/{action_id}/approve", response_model=ActionOut)
async def approve(
    action_id: str,
    approver: Approver,
    approvals: Annotated[ApprovalService, Depends(get_approvals)],
    body: ApprovalIn | None = None,
) -> ActionOut:
    """Approve as the authenticated approver, the proposal that was shown.

    A name in the body is not read: identity comes from authentication
    (ADR-009). The body names the proposal approved; if it changed since,
    the action expires (D13). Optional in the signature so that an
    unauthenticated caller is turned away before the body is judged.
    """
    if body is None:
        raise HTTPException(422, "an approval names the proposal it approves")
    return ActionOut.of(
        await _guard(lambda: approvals.approve(action_id, approver.identity, body.proposal))
    )


@router.post("/actions/{action_id}/reject", response_model=ActionOut)
async def reject(
    action_id: str,
    approver: Approver,
    approvals: Annotated[ApprovalService, Depends(get_approvals)],
) -> ActionOut:
    return ActionOut.of(await _guard(lambda: approvals.reject(action_id, approver.identity)))


@router.post("/actions/{action_id}/execute", response_model=ActionOut)
async def execute(
    action_id: str,
    _orchestrator: Service,
    approvals: Annotated[ApprovalService, Depends(get_approvals)],
) -> ActionOut:
    """Only the orchestrator executes, and only what a person approved."""
    try:
        return ActionOut.of(await approvals.execute(action_id))
    except UnknownAction as error:
        raise HTTPException(404, str(error)) from error
    except IllegalTransition as error:
        raise HTTPException(409, str(error)) from error
    except ProviderUnsupported:
        # A read before the claim found data the domain cannot represent:
        # nothing was claimed, the state is unchanged, and the handler answers
        # 422 -- this is not an unknown outcome.
        raise
    except ProviderRejected as error:
        # The platform answered no: the action is FAILED, nothing was written.
        raise HTTPException(502, f"the billing platform refused the write: {error}") from error
    except Exception as error:
        # No answer: the action is UNKNOWN_OUTCOME. Calling execute again
        # asks the platform what it holds before writing anything, or finds
        # the action left to a person. ADR-008.
        raise HTTPException(
            504,
            "no answer from the billing platform; the outcome is unknown and will be "
            "reconciled on the next execute, or put to a person",
        ) from error


@router.post("/actions/{action_id}/status", response_model=ActionOut)
async def read_status(
    action_id: str,
    _approver: Approver,
    approvals: Annotated[ApprovalService, Depends(get_approvals)],
) -> ActionOut:
    """A person has the platform asked again what became of the write.

    Nothing is sent to the platform; the answer is recorded as a new, dated
    observation (specs/002-stripe-writes, FR-003).
    """
    try:
        return ActionOut.of(await _guard(lambda: approvals.refresh_status(action_id)))
    except ProviderUnavailable as error:
        raise HTTPException(503, "billing platform unavailable: nothing was read") from error


@router.post("/actions/{action_id}/resolution", response_model=ActionOut)
async def record_resolution(
    action_id: str,
    approver: Approver,
    approvals: Annotated[ApprovalService, Depends(get_approvals)],
    body: ResolutionIn | None = None,
) -> ActionOut:
    """A person records how they resolved a partial write, or a write that
    left no trace (spec 015, FR-009a).

    By POST only, as the authenticated approver; a name in the body is not
    read. Refused (409) unless the action is one of those two, unresolved,
    and a status read was made at least ten minutes after the attempt's last
    request. It releases the customer's blocker and never executes anything.
    """
    if body is None:
        raise HTTPException(422, "a resolution says what was done")
    try:
        action = await _guard(lambda: approvals.resolve(action_id, approver.identity, body.note))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return ActionOut.of(action, written=approvals.writes(action.action_type))


@router.get("/actions/{action_id}", response_model=ActionOut)
async def read_action(
    action_id: str,
    _caller: Anyone,
    approvals: Annotated[ApprovalService, Depends(get_approvals)],
) -> ActionOut:
    action = await _guard(lambda: approvals.expire_if_overdue(action_id))
    written = approvals.writes(action.action_type)
    if action.state is not ActionState.PENDING_APPROVAL:
        return ActionOut.of(action, written=written)
    # No fingerprint for a proposal whose invoice no longer carries the tax
    # recorded with it: it is shown as stale, not offered (specs/013, FR-007).
    proposal = await approvals.presented(action)
    if proposal is None:
        return ActionOut.of(action, written=written).model_copy(update={"stale": True})
    return ActionOut.of(action, proposal, written=written)


@router.get("/runs/{run_id}")
async def read_run(
    run_id: str,
    _caller: Anyone,
    audit: Annotated[SqliteAuditRepository, Depends(get_audit)],
    actions: Annotated[SqliteActionStore, Depends(get_actions)],
    attempts: Annotated[SqliteAttemptStore, Depends(get_attempts)],
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
        "attempts": [dict(a) for a in attempts.for_invocation(row["engine_invocation_id"])],
        "actions": [ActionOut.of(a).model_dump() for a in actions.for_run(run_id)],
    }
