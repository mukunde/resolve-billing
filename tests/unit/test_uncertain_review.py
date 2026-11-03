"""A check that cannot conclude sends the case to a person, whatever the engine
answers (.specify/bugs/uncertain-human-review; constitution, principle II)."""

import json
from pathlib import Path

import pytest

from app.application.investigate_dispute import DisputeInvestigator
from app.decision.base import Classification, Decision, RecommendedAction
from app.domain.enums import RuleCode, Severity
from app.domain.models import Dispute, RuleFinding
from app.policies.action_policy import ActionPolicy, Verdict
from app.providers.fake.adapter import FakeBillingProvider
from evals import runner


def finding(code):
    return RuleFinding(code=code, severity=Severity.LOW, description="", evidence={})


def decision(classification, action, confidence=0.75):
    return Decision(
        classification=classification,
        confidence=confidence,
        confidence_kind="self_reported",
        recommended_action=action,
    )


class TestThePolicy:
    @pytest.mark.parametrize("code", [RuleCode.NOT_EVALUABLE, RuleCode.USAGE_UNAVAILABLE])
    @pytest.mark.parametrize(
        ("classification", "action"),
        [
            (Classification.INSUFFICIENT_INFORMATION, RecommendedAction.REQUEST_INFORMATION),
            (Classification.DUPLICATE_BILLING, RecommendedAction.REQUEST_INFORMATION),
            (Classification.DUPLICATE_BILLING, RecommendedAction.EXPLAIN_INVOICE),
            (Classification.PAYMENT_ALREADY_RECEIVED, RecommendedAction.EXPLAIN_INVOICE),
            (Classification.PRORATION_DISPUTE, RecommendedAction.REQUEST_INFORMATION),
        ],
    )
    def test_it_goes_to_a_person(self, code, classification, action):
        authorization = ActionPolicy().evaluate(decision(classification, action), [finding(code)])
        assert authorization.verdict is Verdict.HUMAN_REVIEW

    def test_a_confident_engine_changes_nothing(self):
        authorization = ActionPolicy().evaluate(
            decision(
                Classification.INSUFFICIENT_INFORMATION,
                RecommendedAction.REQUEST_INFORMATION,
                confidence=0.99,
            ),
            [finding(RuleCode.NOT_EVALUABLE)],
        )
        assert authorization.verdict is Verdict.HUMAN_REVIEW

    def test_an_ordinary_question_stays_automatic(self):
        authorization = ActionPolicy().evaluate(
            decision(
                Classification.INSUFFICIENT_INFORMATION, RecommendedAction.REQUEST_INFORMATION
            ),
            [],
        )
        assert authorization.verdict is Verdict.AUTO


class Answering:
    """An engine that answers as the recorded run did."""

    name = "recorded"
    version = "recorded/v1"

    async def decide(self, dispute, context, findings):
        return decision(
            Classification.INSUFFICIENT_INFORMATION, RecommendedAction.REQUEST_INFORMATION
        )


class TestTheRecordedCase:
    """A counterfactual: what the measurement's answer gives under the fixed
    policy. The stored result stays what it was."""

    async def test_no_automatic_reply_and_no_unsafe_action(self):
        cases = json.loads(Path("evals/cases.json").read_text(encoding="utf-8"))
        case = next(c for c in cases if c["id"] == "duplicate_check_not_evaluable")
        dispute = Dispute(
            id="eval_case",
            customer_message=case["message"],
            customer_email=case["customer_email"],
            customer_id=case.get("customer_id"),
            invoice_id=case["invoice_id"],
            headers=runner.header_pairs(case.get("headers")),
        )
        investigator = DisputeInvestigator(provider=FakeBillingProvider(), engine=Answering())
        result = await investigator.investigate(dispute, now=runner.EVALUATION_DATE)
        assert result.authorization.verdict is Verdict.HUMAN_REVIEW
        assert result.automatic_reply.allowed is False
        assert result.automatic_reply.reason.value == "needs_a_person"
        assert runner._evaluate(case, result).unsafe is None
