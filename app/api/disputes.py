"""``POST /api/v1/disputes/investigate``.

The request carries already-structured input: a message, a customer and
possibly an invoice reference. Extracting those from an email is n8n's job and
is deliberately deterministic -- a failed extraction becomes
``insufficient_information``, which is an outcome the system already handles,
rather than a probabilistic call.
"""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import Service
from app.api.deps import get_investigator
from app.application.investigate_dispute import (
    DisputeInvestigator,
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
    proposed_action: ActionOut
    """What the system proposes, amount computed from the invoice. Not the
    engine's recommendation: an escalation reads `none` here."""
    requires_human_approval: bool
    authorization_reason: str
    action_id: str | None = None
    short_circuited_by: str | None = None
    sender: str
    """`known`, `unknown` or `suspected_impersonation`. See ADR-007."""

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
            proposed_action=ActionOut.of(result.proposed_action),
            requires_human_approval=result.requires_human_approval,
            authorization_reason=result.authorization.reason,
            action_id=result.action_id,
            short_circuited_by=(
                result.short_circuited_by.value if result.short_circuited_by else None
            ),
            sender=result.sender.value,
        )


@router.post("/investigate", response_model=InvestigateResponse)
async def investigate(
    payload: InvestigateRequest,
    _orchestrator: Service,
    investigator: Annotated[DisputeInvestigator, Depends(get_investigator)],
) -> InvestigateResponse:
    if not (payload.customer_email or payload.customer_id):
        raise HTTPException(422, "a dispute must identify a customer by id or by email")

    dispute = Dispute(
        id=f"dsp_{uuid4().hex[:8]}",
        customer_message=payload.message,
        customer_id=payload.customer_id,
        customer_email=payload.customer_email,
        invoice_id=payload.invoice_id,
    )
    result = await investigator.investigate(dispute)
    return InvestigateResponse.of(result)
