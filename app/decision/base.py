"""The decision-engine boundary. See ADR-002.

A decision engine classifies a dispute and says how confident it is. It does
not write prose (that is :mod:`app.drafting`), it does not fetch data, and it
does not execute anything.

That narrowness is deliberate. A port that required an ``explanation`` string
could only ever be implemented by a generative model; with the explanation
optional, the same port fits a System One model returning a typed choice and a
probability distribution. See the V5 note in PLAN.md.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import BillingContext, Dispute, RuleFinding


class Classification(StrEnum):
    """What kind of dispute this is. Closed set.

    ``UNKNOWN`` is a first-class member, not an oversight. A typed engine
    cannot return an out-of-schema value, so the escape hatch has to be a
    value of the enum rather than a validation failure -- otherwise a novel
    dispute gets forced into whichever box fits worst.
    """

    DUPLICATE_BILLING = "duplicate_billing"
    VALID_INVOICE = "valid_invoice"
    BILLING_AFTER_CANCELLATION = "billing_after_cancellation"
    PRORATION_DISPUTE = "proration_dispute"
    PAYMENT_ALREADY_RECEIVED = "payment_already_received"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    UNKNOWN = "unknown"


class RecommendedAction(StrEnum):
    """What the engine suggests doing. Still only a suggestion.

    The engine names the *kind* of action. It never names an amount: what a
    credit note is worth is computed from the invoice, in code. The model says
    "propose a credit note", the code says "99,00 EUR".
    """

    PROPOSE_CREDIT_NOTE = "propose_credit_note"
    PROPOSE_REFUND = "propose_refund"
    EXPLAIN_INVOICE = "explain_invoice"
    REQUEST_INFORMATION = "request_information"
    ESCALATE = "escalate"


#: Actions that move money. None of them runs without a human. See ADR-004.
FINANCIAL_ACTIONS = frozenset(
    {RecommendedAction.PROPOSE_CREDIT_NOTE, RecommendedAction.PROPOSE_REFUND}
)


class EngineTelemetry(BaseModel):
    """What one engine call cost and how long it took.

    Lives on the decision rather than being returned alongside it, so the port
    keeps a single return type. That is defensible beyond convenience: in an
    auditable system, how a decision was produced is part of the decision.

    ``cost_micros`` is an integer count of millionths of a dollar, for the
    same reason money is an integer count of minor units. It is ``None`` when
    no rate is known for the model -- distinct from ``0``, which asserts the
    call genuinely had no per-call price.

    ``pricing_as_of`` travels with it for the same reason ``confidence_kind``
    travels with ``confidence``: a number whose provenance is lost cannot be
    interpreted later. It is set only when a cost was actually computed.
    """

    model_config = ConfigDict(frozen=True)

    model_id: str
    prompt_version: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_micros: int | None = None
    pricing_as_of: str | None = None
    latency_ms: int = 0


class Decision(BaseModel):
    """A classification, a confidence, and what the engine suggests."""

    model_config = ConfigDict(frozen=True)

    classification: Classification
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_kind: Literal["self_reported", "distribution_derived", "calibrated"]
    recommended_action: RecommendedAction
    evidence_refs: list[str] = []
    explanation: str | None = None
    """Optional on purpose.

    A non-generative engine returns none, and the application layer assembles
    a deterministic explanation from the rule findings instead -- which is
    traceable line by line, and therefore better for an audit log than model
    prose would have been.
    """

    telemetry: EngineTelemetry | None = None
    """Absent for engines that cost nothing, such as the mock."""


@runtime_checkable
class DecisionEngine(Protocol):
    """Classify a dispute against its billing context and the rule findings.

    ``name`` and ``version`` are written to every audit row. Without them a
    decision is neither replayable nor comparable against another engine,
    which would empty the whole sovereignty argument of ADR-002.
    """

    name: str
    version: str

    async def decide(
        self,
        dispute: Dispute,
        context: BillingContext,
        findings: list[RuleFinding],
    ) -> Decision: ...
