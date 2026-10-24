"""What a hosted model is asked for, and what an unusable answer becomes.

Shared by every engine that calls a model behind an API: the fields the model
may fill in, the JSON schema that constrains them, and the escalation built
when an answer cannot be used. Moved out of the Claude engine so the engines
added for evaluation (specs/004-second-engines) hold the model to exactly the
same contract; the Claude engine's behaviour did not change.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.decision.base import (
    Classification,
    Decision,
    EngineTelemetry,
    RecommendedAction,
)


class ModelVerdict(BaseModel):
    """Exactly what the model is allowed to fill in.

    Note what is absent: ``confidence_kind``. Whether a number is calibrated
    is not the model's to declare -- the engine sets it, and only an
    evaluation run can promote it.
    """

    classification: Classification
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_action: RecommendedAction
    explanation: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)


def verdict_schema() -> dict:
    """The JSON schema of a verdict, generated from the domain enums.

    Hand-built rather than derived from the Pydantic model so the enum values
    are flat and ``additionalProperties`` is explicit -- and so that adding a
    classification to the enum updates the schema automatically.
    """
    return {
        "type": "object",
        "properties": {
            "classification": {
                "type": "string",
                "enum": [c.value for c in Classification],
            },
            # No minimum/maximum: Anthropic's API rejects those keywords on a
            # number ("properties maximum, minimum are not supported"). The
            # bound is enforced on arrival instead, by ModelVerdict, and a
            # value outside it fails validation and escalates to a human --
            # the same safety, checked one step later.
            "confidence": {"type": "number"},
            "recommended_action": {
                "type": "string",
                "enum": [a.value for a in RecommendedAction],
            },
            "explanation": {"type": "string"},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "classification",
            "confidence",
            "recommended_action",
            "explanation",
            "evidence_refs",
        ],
        "additionalProperties": False,
    }


def escalation(telemetry: EngineTelemetry) -> Decision:
    """The engine did not answer usably. That is a decision to escalate."""
    return Decision(
        classification=Classification.UNKNOWN,
        confidence=0.0,
        confidence_kind="self_reported",
        recommended_action=RecommendedAction.ESCALATE,
        explanation=(
            "Le moteur de décision n'a pas produit de sortie conforme au schéma. "
            "Le litige part en revue humaine."
        ),
        evidence_refs=[],
        telemetry=telemetry,
    )
