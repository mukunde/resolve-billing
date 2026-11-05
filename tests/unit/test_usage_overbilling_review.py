"""An over-billing usage finding sends the case to a person, whatever the engine
answers (.specify/bugs/usage-overbilling-review; taxonomy S1; constitution,
principles II and V)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.investigate_dispute import DisputeInvestigator
from app.decision.base import Classification, Decision, RecommendedAction
from app.domain.enums import RuleCode, Severity
from app.domain.models import Dispute, RuleFinding
from app.policies.action_policy import ActionPolicy, Verdict
from app.providers.fake.adapter import FakeBillingProvider
from evals import runner


def usage_delta(direction="over"):
    return RuleFinding(
        code=RuleCode.SUSPICIOUS_USAGE_DELTA,
        severity=Severity.MEDIUM,
        description="",
        evidence={
            "expected": "20",
            "billed": "47",
            "relative_delta": "1.35",
            "tolerance": "0.10",
            "direction": direction,
        },
    )


def decision(classification, action, confidence=0.85):
    return Decision(
        classification=classification,
        confidence=confidence,
        confidence_kind="self_reported",
        recommended_action=action,
    )


#: Every answer that moves no money and does not escalate: before the fix,
#: each of them reached step 5 and was authorised with no person.
NON_FINANCIAL = [
    (Classification.INSUFFICIENT_INFORMATION, RecommendedAction.REQUEST_INFORMATION),
    (Classification.PRORATION_DISPUTE, RecommendedAction.EXPLAIN_INVOICE),
    (Classification.PRORATION_DISPUTE, RecommendedAction.REQUEST_INFORMATION),
    (Classification.DUPLICATE_BILLING, RecommendedAction.EXPLAIN_INVOICE),
    (Classification.BILLING_AFTER_CANCELLATION, RecommendedAction.EXPLAIN_INVOICE),
    (Classification.PAYMENT_ALREADY_RECEIVED, RecommendedAction.EXPLAIN_INVOICE),
]


class TestThePolicy:
    @pytest.mark.parametrize(("classification", "action"), NON_FINANCIAL)
    def test_it_goes_to_a_person(self, classification, action):
        authorization = ActionPolicy().evaluate(decision(classification, action), [usage_delta()])
        assert authorization.verdict is Verdict.HUMAN_REVIEW
        assert authorization.reason == (
            "L'usage facturé dépasse nettement l'usage mesuré : une personne examine."
        )

    def test_a_confident_engine_changes_nothing(self):
        authorization = ActionPolicy().evaluate(
            decision(
                Classification.INSUFFICIENT_INFORMATION,
                RecommendedAction.REQUEST_INFORMATION,
                confidence=0.99,
            ),
            [usage_delta()],
        )
        assert authorization.verdict is Verdict.HUMAN_REVIEW

    def test_the_earlier_steps_keep_their_reasons(self):
        policy = ActionPolicy()
        valid = policy.evaluate(
            decision(Classification.VALID_INVOICE, RecommendedAction.EXPLAIN_INVOICE),
            [usage_delta()],
        )
        assert valid.verdict is Verdict.HUMAN_REVIEW
        assert "dépasse nettement l'usage mesuré" in valid.reason
        assert valid.reason.startswith("Facture déclarée correcte")
        escalated = policy.evaluate(
            decision(Classification.UNKNOWN, RecommendedAction.ESCALATE), [usage_delta()]
        )
        assert escalated.verdict is Verdict.HUMAN_REVIEW
        assert escalated.reason.startswith("Le moteur demande une revue humaine")
        money = policy.evaluate(
            decision(Classification.PRORATION_DISPUTE, RecommendedAction.PROPOSE_CREDIT_NOTE),
            [usage_delta()],
            amount=None,
            withheld="correction partielle (usage) : montant non calculable à ce jour",
        )
        assert money.verdict is Verdict.HUMAN_REVIEW
        assert money.reason.startswith("Action financière recommandée sans montant calculable")

    def test_under_billing_does_not_trigger_it(self):
        # The investigation removes it before the policy (taxonomy S1); were it
        # ever to arrive, it is the business's loss, not the customer's case.
        authorization = ActionPolicy().evaluate(
            decision(Classification.INSUFFICIENT_INFORMATION, RecommendedAction.REQUEST_INFORMATION),
            [usage_delta(direction="under")],
        )
        assert authorization.verdict is Verdict.AUTO

    @pytest.mark.parametrize(("classification", "action"), NON_FINANCIAL)
    def test_without_the_finding_it_stays_automatic(self, classification, action):
        authorization = ActionPolicy().evaluate(decision(classification, action), [])
        assert authorization.verdict is Verdict.AUTO


class Asking:
    """An engine that asks the customer rather than escalating."""

    name = "asking"
    version = "asking/v1"

    async def decide(self, dispute, context, findings):
        return decision(
            Classification.INSUFFICIENT_INFORMATION, RecommendedAction.REQUEST_INFORMATION
        )


class TestThroughTheInvestigation:
    """The development world of `legitimate_overage`, with its usage record
    lowered so that the invoice bills more than was measured."""

    async def test_no_automatic_reply(self):
        cases = json.loads(Path("evals/cases.json").read_text(encoding="utf-8"))
        case = next(c for c in cases if c["id"] == "legitimate_overage")
        provider = FakeBillingProvider()
        provider._usage = [
            u.model_copy(update={"quantity": Decimal("20")}) if u.subscription_id == "sub_88" else u
            for u in provider._usage
        ]
        dispute = Dispute(
            id="eval_case",
            customer_message=case["message"],
            customer_email=case["customer_email"],
            customer_id=case.get("customer_id"),
            invoice_id=case["invoice_id"],
            headers=runner.header_pairs(case.get("headers")),
        )
        investigator = DisputeInvestigator(provider=provider, engine=Asking())
        result = await investigator.investigate(dispute, now=runner.EVALUATION_DATE)
        codes = [f.code for f in result.findings]
        assert RuleCode.SUSPICIOUS_USAGE_DELTA in codes
        assert result.authorization.verdict is Verdict.HUMAN_REVIEW
        assert result.automatic_reply.allowed is False
        assert result.automatic_reply.reason.value == "needs_a_person"
