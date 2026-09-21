"""``POST /api/v1/disputes/investigate``.

The request carries already-structured input: a message, a customer and
possibly an invoice reference. Extracting those from an email is n8n's job and
is deliberately deterministic -- a failed extraction becomes
``insufficient_information``, which is an outcome the system already handles,
rather than a probabilistic call.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.deps import UnknownEngine, UnknownModel, build_investigator
from app.application.investigate_dispute import (
    InvestigationResult,
    ProposedAction,
)
from app.domain.models import Dispute

router = APIRouter(prefix="/api/v1/disputes", tags=["disputes"])


class InvestigateRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10_000)
    customer_email: str | None = None
    customer_id: str | None = None
    invoice_id: str | None = None

    decision_engine: str | None = None
    """Which engine should answer. Validated against an allow-list."""

    model: str | None = None
    """Which model, for engines that take one. Same allow-list treatment.

    Selectable so the demo can replay one dispute across the cost ladder of
    ADR-003 and show two audit rows. Never a free string: on a system that
    proposes credit notes, letting the caller choose the judge is privilege
    escalation dressed as configuration.
    """


class ActionOut(BaseModel):
    type: str
    amount_minor: int | None = None
    currency: str | None = None

    @classmethod
    def of(cls, action: ProposedAction) -> ActionOut:
        return cls(
            type=action.type,
            amount_minor=action.amount.amount_minor if action.amount else None,
            currency=action.amount.currency if action.amount else None,
        )


class InvestigateResponse(BaseModel):
    run_id: str
    classification: str
    confidence: float
    confidence_kind: str
    decision_engine: str
    decision_engine_version: str
    findings: list[str]
    explanation: str
    recommended_action: ActionOut
    requires_human_approval: bool
    authorization_reason: str
    action_id: str | None = None
    short_circuited_by: str | None = None

    @classmethod
    def of(cls, result: InvestigationResult) -> InvestigateResponse:
        return cls(
            run_id=result.run_id,
            classification=result.classification.value,
            confidence=result.confidence,
            confidence_kind=result.confidence_kind,
            decision_engine=result.decision_engine,
            decision_engine_version=result.decision_engine_version,
            findings=[finding.code.value for finding in result.findings],
            explanation=result.explanation,
            recommended_action=ActionOut.of(result.proposed_action),
            requires_human_approval=result.requires_human_approval,
            authorization_reason=result.authorization.reason,
            action_id=result.action_id,
            short_circuited_by=(
                result.short_circuited_by.value if result.short_circuited_by else None
            ),
        )


@router.post("/investigate", response_model=InvestigateResponse)
async def investigate(payload: InvestigateRequest) -> InvestigateResponse:
    if not (payload.customer_email or payload.customer_id):
        raise HTTPException(422, "a dispute must identify a customer by id or by email")

    try:
        investigator = build_investigator(payload.decision_engine, payload.model)
    except (UnknownEngine, UnknownModel) as error:
        # 422 rather than a quiet fallback to the default: a substitution
        # would make the audit row name an engine the caller did not get.
        raise HTTPException(422, str(error)) from error

    dispute = Dispute(
        id=f"dsp_{uuid4().hex[:8]}",
        customer_message=payload.message,
        customer_id=payload.customer_id,
        customer_email=payload.customer_email,
        invoice_id=payload.invoice_id,
    )
    try:
        result = await investigator.investigate(dispute)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    return InvestigateResponse.of(result)
