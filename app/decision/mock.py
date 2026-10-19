"""A deterministic decision engine, for tests and evaluations.

Not a stand-in for the real thing in the demo: it exists so that integration
tests and the evaluation suite are free, offline and identical on every run.
The scoring harness needs a fixed point to compare a real engine against, and
a model that costs money and varies between calls cannot be that fixed point.

It returns no explanation, which is deliberate: it exercises the deterministic
fallback path that a System One engine will also take.
"""

from __future__ import annotations

from app.decision.base import Classification, Decision, RecommendedAction
from app.domain.enums import RuleCode
from app.domain.models import BillingContext, Dispute, RuleFinding

#: First match wins. Ordered by how much the finding constrains the outcome.
_BY_FINDING: list[tuple[RuleCode, Classification, RecommendedAction, float]] = [
    (
        RuleCode.INVOICE_AFTER_CANCELLATION,
        Classification.BILLING_AFTER_CANCELLATION,
        RecommendedAction.PROPOSE_CREDIT_NOTE,
        0.95,
    ),
    (
        RuleCode.DUPLICATE_INVOICE,
        Classification.DUPLICATE_BILLING,
        RecommendedAction.PROPOSE_CREDIT_NOTE,
        0.95,
    ),
    (
        RuleCode.DUPLICATE_PAYMENT,
        Classification.DUPLICATE_BILLING,
        RecommendedAction.PROPOSE_REFUND,
        0.95,
    ),
    (
        RuleCode.PRORATION_MISMATCH,
        Classification.PRORATION_DISPUTE,
        RecommendedAction.PROPOSE_CREDIT_NOTE,
        0.80,
    ),
    (
        RuleCode.UNKNOWN_INVOICE,
        Classification.INSUFFICIENT_INFORMATION,
        RecommendedAction.REQUEST_INFORMATION,
        0.90,
    ),
    (
        RuleCode.SUSPICIOUS_USAGE_DELTA,
        Classification.UNKNOWN,
        RecommendedAction.ESCALATE,
        0.55,
    ),
]


class MockDecisionEngine:
    """Maps rule findings onto a decision, with no model involved."""

    name = "mock"
    version = "1.0.0"

    async def decide(
        self,
        dispute: Dispute,
        context: BillingContext,
        findings: list[RuleFinding],
    ) -> Decision:
        codes = {finding.code for finding in findings}

        for code, classification, action, confidence in _BY_FINDING:
            if code in codes:
                return self._decision(classification, action, confidence, findings, context)

        # No finding fired. Either the invoice is fine, or we were never told
        # which invoice the customer means.
        if context.target_invoice is not None:
            return self._decision(
                Classification.VALID_INVOICE,
                RecommendedAction.EXPLAIN_INVOICE,
                0.85,
                findings,
                context,
            )
        return self._decision(
            Classification.INSUFFICIENT_INFORMATION,
            RecommendedAction.REQUEST_INFORMATION,
            0.90,
            findings,
            context,
        )

    def _decision(
        self,
        classification: Classification,
        action: RecommendedAction,
        confidence: float,
        findings: list[RuleFinding],
        context: BillingContext,
    ) -> Decision:
        return Decision(
            classification=classification,
            confidence=confidence,
            # A constant, not a measurement. The mock has no opinion about how
            # often it is right; only the evaluation suite can establish that.
            confidence_kind="self_reported",
            recommended_action=action,
            evidence_refs=_evidence_refs(findings, context),
            explanation=None,
        )


def _evidence_refs(findings: list[RuleFinding], context: BillingContext) -> list[str]:
    """Invoice and subscription identifiers the findings point at."""
    refs: list[str] = []
    if context.target_invoice_id:
        refs.append(context.target_invoice_id)
    for finding in findings:
        for key, value in finding.evidence.items():
            if key.startswith(("invoice", "subscription")) and isinstance(value, str):
                refs.append(value)
    return sorted(set(refs))
