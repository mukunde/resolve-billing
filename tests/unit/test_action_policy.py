"""ADR-004: no financial mutation without a human, and why the switch is off."""

import pytest

from app.decision.base import Classification, Decision, RecommendedAction
from app.domain.enums import RuleCode, Severity
from app.domain.models import RuleFinding
from app.domain.money import Money
from app.policies.action_policy import (
    ActionPolicy,
    AutonomyPolicy,
    Verdict,
)

NINETY_NINE = Money(amount_minor=9900, currency="EUR")
SMALL = Money(amount_minor=500, currency="EUR")


def decision(
    classification=Classification.DUPLICATE_BILLING,
    action=RecommendedAction.PROPOSE_CREDIT_NOTE,
    confidence=0.97,
    confidence_kind="self_reported",
) -> Decision:
    return Decision(
        classification=classification,
        confidence=confidence,
        confidence_kind=confidence_kind,
        recommended_action=action,
    )


def injection_finding() -> RuleFinding:
    return RuleFinding(
        code=RuleCode.SUSPECTED_PROMPT_INJECTION,
        severity=Severity.HIGH,
        description="Motif d'instruction adressée au système.",
        evidence={},
    )


@pytest.fixture
def policy() -> ActionPolicy:
    return ActionPolicy()


class TestFinancialActions:
    """Case A: a credit note always needs a human."""

    @pytest.mark.parametrize(
        "action",
        [RecommendedAction.PROPOSE_CREDIT_NOTE, RecommendedAction.PROPOSE_REFUND],
    )
    def test_money_always_requires_approval(self, policy, action):
        auth = policy.evaluate(decision(action=action), [], NINETY_NINE)

        assert auth.verdict is Verdict.REQUIRES_APPROVAL
        assert auth.requires_human_approval is True

    def test_the_reason_names_the_calibration_gap(self, policy):
        """The audit row should say why, not just that."""
        auth = policy.evaluate(decision(), [], NINETY_NINE)
        assert "calibr" in auth.reason.lower()

    def test_a_financial_action_without_an_amount_goes_to_review(self, policy):
        auth = policy.evaluate(decision(), [], amount=None)
        assert auth.verdict is Verdict.HUMAN_REVIEW


class TestNonFinancialActions:
    """Case B: nothing moves, nobody is woken up."""

    @pytest.mark.parametrize(
        "action",
        [RecommendedAction.EXPLAIN_INVOICE, RecommendedAction.REQUEST_INFORMATION],
    )
    def test_replies_run_without_approval(self, policy, action):
        auth = policy.evaluate(
            decision(classification=Classification.VALID_INVOICE, action=action), []
        )

        assert auth.verdict is Verdict.AUTO
        assert auth.requires_human_approval is False


class TestBlockingFindings:
    """Case F: the finding outranks the engine."""

    def test_injection_forces_review(self, policy):
        auth = policy.evaluate(decision(), [injection_finding()], NINETY_NINE)

        assert auth.verdict is Verdict.HUMAN_REVIEW
        assert "SUSPECTED_PROMPT_INJECTION" in auth.reason

    def test_it_outranks_a_confident_engine_and_a_real_duplicate(self, policy):
        """The most important test in the file.

        Everything else says "issue the credit note": the classification is
        confident, the amount is known, a genuine duplicate was detected. The
        blocking finding still wins.
        """
        real_duplicate = RuleFinding(
            code=RuleCode.DUPLICATE_INVOICE,
            severity=Severity.HIGH,
            description="Deux factures couvrent la même période.",
            evidence={"invoice_1": "inv_001", "invoice_2": "inv_002"},
        )

        auth = policy.evaluate(
            decision(confidence=1.0, confidence_kind="calibrated"),
            [real_duplicate, injection_finding()],
            NINETY_NINE,
        )

        assert auth.verdict is Verdict.HUMAN_REVIEW

    def test_suspected_impersonation_forces_review(self, policy):
        impersonation = RuleFinding(
            code=RuleCode.SUSPECTED_IMPERSONATION,
            severity=Severity.HIGH,
            description="Un expéditeur inconnu vise le compte d'un client connu.",
            evidence={},
        )

        auth = policy.evaluate(decision(), [impersonation], NINETY_NINE)

        assert auth.verdict is Verdict.HUMAN_REVIEW
        assert "SUSPECTED_IMPERSONATION" in auth.reason


class TestNonActionableClassifications:
    def test_unknown_goes_to_a_human(self, policy):
        auth = policy.evaluate(
            decision(classification=Classification.UNKNOWN, action=RecommendedAction.ESCALATE),
            [],
        )
        assert auth.verdict is Verdict.HUMAN_REVIEW

    @pytest.mark.parametrize("classification", list(Classification))
    def test_an_escalation_reaches_a_human_whatever_the_classification(
        self, policy, classification
    ):
        """Prompt v2 paired `escalate` with `duplicate_billing` on a real run,
        and the policy, reading only the classification, authorised it as an
        automatic reply. The engine asking for a human is enough."""
        auth = policy.evaluate(
            decision(classification=classification, action=RecommendedAction.ESCALATE),
            [],
        )
        assert auth.verdict is Verdict.HUMAN_REVIEW
        assert auth.requires_human_approval

    def test_asking_the_customer_a_question_does_not_need_a_human(self, policy):
        """Case C. A departure from a literal reading of the plan, argued in
        the policy module: requesting information is the safe non-action, not
        an action on the customer account."""
        auth = policy.evaluate(
            decision(
                classification=Classification.INSUFFICIENT_INFORMATION,
                action=RecommendedAction.REQUEST_INFORMATION,
            ),
            [],
        )
        assert auth.verdict is Verdict.AUTO

    @pytest.mark.parametrize(
        "classification",
        [Classification.UNKNOWN, Classification.INSUFFICIENT_INFORMATION],
    )
    def test_money_is_never_moved_on_an_unqualified_dispute(self, policy, classification):
        """Even if the engine asks for a credit note, and an amount exists."""
        auth = policy.evaluate(
            decision(classification=classification), [], NINETY_NINE
        )
        assert auth.verdict is Verdict.HUMAN_REVIEW


class TestAutonomyThresholds:
    """The mechanism exists, is configurable, and is deliberately withheld."""

    def test_disabled_by_default(self):
        assert AutonomyPolicy().enabled is False

    def test_even_a_perfect_self_reported_score_does_not_unlock_it(self, policy):
        auth = policy.evaluate(decision(confidence=1.0), [], SMALL)

        assert auth.verdict is Verdict.REQUIRES_APPROVAL
        assert auth.autonomy_available is True, "a threshold would have applied"

    def test_enabling_it_is_not_enough_while_confidence_is_self_reported(self):
        """The real lock is the *kind* of confidence, not the switch."""
        enabled = ActionPolicy(AutonomyPolicy(enabled=True))

        auth = enabled.evaluate(decision(confidence=1.0), [], SMALL)

        assert auth.verdict is Verdict.REQUIRES_APPROVAL
        assert "self_reported" in auth.reason

    def test_calibrated_confidence_under_the_thresholds_runs_unattended(self):
        """What the evaluation suite would eventually buy."""
        enabled = ActionPolicy(AutonomyPolicy(enabled=True))

        auth = enabled.evaluate(
            decision(confidence=0.99, confidence_kind="calibrated"), [], SMALL
        )

        assert auth.verdict is Verdict.AUTO

    def test_a_large_amount_still_needs_a_human_even_when_calibrated(self):
        enabled = ActionPolicy(AutonomyPolicy(enabled=True))

        auth = enabled.evaluate(
            decision(confidence=0.99, confidence_kind="calibrated"), [], NINETY_NINE
        )

        assert auth.verdict is Verdict.REQUIRES_APPROVAL

    def test_low_confidence_still_needs_a_human_even_when_calibrated(self):
        enabled = ActionPolicy(AutonomyPolicy(enabled=True))

        auth = enabled.evaluate(
            decision(confidence=0.60, confidence_kind="calibrated"), [], SMALL
        )

        # Below the confidence floor, not even an approval is proposed: a
        # person reviews the case itself (taxonomy S8).
        assert auth.verdict is Verdict.HUMAN_REVIEW
        assert auth.requires_human_approval


class TestConfidenceFloor:
    """Taxonomy S8: below 0.70 a classification is not used, whatever it is."""

    @pytest.mark.parametrize(
        "action",
        [RecommendedAction.PROPOSE_CREDIT_NOTE, RecommendedAction.EXPLAIN_INVOICE],
    )
    def test_below_the_floor_a_person_reviews(self, policy, action):
        auth = policy.evaluate(decision(action=action, confidence=0.69), [], NINETY_NINE)
        assert auth.verdict is Verdict.HUMAN_REVIEW
        assert "0.70" in auth.reason

    def test_at_the_floor_the_classification_is_used(self, policy):
        auth = policy.evaluate(
            decision(action=RecommendedAction.EXPLAIN_INVOICE, confidence=0.70), [], None
        )
        assert auth.verdict is Verdict.AUTO

    def test_a_blocking_finding_still_comes_first(self, policy):
        auth = policy.evaluate(decision(confidence=0.10), [injection_finding()], NINETY_NINE)
        assert "Constat bloquant" in auth.reason


class TestUsageNobodyChecked:
    """Taxonomy S7: "the invoice is correct" cannot rest on unchecked usage."""

    def usage_unavailable(self):
        return RuleFinding(
            code=RuleCode.USAGE_UNAVAILABLE, severity=Severity.LOW, description="", evidence={}
        )

    def test_a_valid_invoice_with_unchecked_usage_goes_to_a_person(self, policy):
        auth = policy.evaluate(
            decision(Classification.VALID_INVOICE, RecommendedAction.EXPLAIN_INVOICE),
            [self.usage_unavailable()],
        )
        assert auth.verdict is Verdict.HUMAN_REVIEW

    def test_other_classifications_are_unchanged(self, policy):
        auth = policy.evaluate(
            decision(
                Classification.INSUFFICIENT_INFORMATION, RecommendedAction.REQUEST_INFORMATION
            ),
            [self.usage_unavailable()],
        )
        assert auth.verdict is Verdict.AUTO


class TestWithheld:
    def test_the_reason_for_no_amount_never_changes_the_verdict(self, policy):
        """`withheld` words a refusal; it cannot turn one into a permission."""
        plain = policy.evaluate(decision(), [], amount=None)
        worded = policy.evaluate(decision(), [], amount=None, withheld="correction partielle")
        assert plain.verdict is worded.verdict is Verdict.HUMAN_REVIEW
        assert "correction partielle" in worded.reason

    def test_a_reason_alongside_an_amount_is_ignored(self, policy):
        auth = policy.evaluate(decision(), [], NINETY_NINE, withheld="ignored")
        assert auth.verdict is Verdict.REQUIRES_APPROVAL
        assert "ignored" not in auth.reason


class TestIsolation:
    def test_the_policy_never_reads_the_customer_message(self):
        """Checked by signature: evaluate() has no way to see it.

        Nothing a customer writes should be able to widen what the system is
        permitted to do to their account. `withheld` is no exception: it is
        the reason `correction_for` gives, computed from the records, and it
        only ever words a refusal.
        """
        import inspect

        params = set(inspect.signature(ActionPolicy.evaluate).parameters)
        assert params == {"self", "decision", "findings", "amount", "withheld"}
