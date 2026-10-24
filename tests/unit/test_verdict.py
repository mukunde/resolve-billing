"""What every hosted engine asks a model for, and what it does with an
answer it cannot use (specs/004-second-engines, T020)."""

import pytest
from pydantic import ValidationError

from app.decision.base import Classification, EngineTelemetry, RecommendedAction
from app.decision.verdict import ModelVerdict, escalation, verdict_schema


def test_the_schema_offers_the_closed_sets_and_nothing_else():
    schema = verdict_schema()

    assert schema["additionalProperties"] is False
    assert schema["properties"]["classification"]["enum"] == [c.value for c in Classification]
    assert schema["properties"]["recommended_action"]["enum"] == [
        a.value for a in RecommendedAction
    ]
    assert set(schema["required"]) == set(schema["properties"])


def test_a_verdict_out_of_bounds_does_not_validate():
    with pytest.raises(ValidationError):
        ModelVerdict.model_validate(
            {
                "classification": "duplicate_billing",
                "confidence": 1.4,
                "recommended_action": "propose_credit_note",
                "explanation": "x",
                "evidence_refs": [],
            }
        )


def test_an_unusable_answer_goes_to_a_person():
    telemetry = EngineTelemetry(model_id="m", prompt_version="p")

    decision = escalation(telemetry)

    assert decision.classification is Classification.UNKNOWN
    assert decision.recommended_action is RecommendedAction.ESCALATE
    assert decision.confidence == 0.0
    assert decision.telemetry == telemetry
