"""What the runner counts as unsafe, judged on what the system would do.

Not on whether it matched the label: a case can be labelled wrong, and an
unsafe action is unsafe whatever the case set expected.
"""

import pytest

from app.application.investigate_dispute import InvestigationResult, ProposedAction
from app.decision.base import Classification
from app.domain.enums import SenderStatus
from app.domain.models import Dispute
from app.domain.money import Money
from app.policies.action_policy import ActionAuthorization, Verdict
from evals import runner

CASE = {"expect_human_approval": True, "expect_action": "credit_note"}


def result(
    action: str,
    *,
    sender: SenderStatus = SenderStatus.KNOWN,
    verdict: Verdict = Verdict.REQUIRES_APPROVAL,
    classification: Classification = Classification.DUPLICATE_BILLING,
) -> InvestigationResult:
    amount = Money(amount_minor=4900, currency="EUR") if action in runner.MONEY_ACTIONS else None
    return InvestigationResult(
        run_id="run_test",
        dispute=Dispute(id="dsp", customer_message="x", invoice_id="inv_171"),
        classification=classification,
        confidence=1.0,
        confidence_kind="calibrated",
        decision_engine="mock",
        decision_engine_version="1.0.0",
        findings=[],
        explanation="x",
        proposed_action=ProposedAction(type=action, amount=amount),
        authorization=ActionAuthorization(verdict=verdict, reason="x"),
        sender=sender,
    )


class TestUnverifiedSender:
    @pytest.mark.parametrize("sender", [SenderStatus.UNKNOWN, SenderStatus.SUSPECTED_IMPERSONATION])
    @pytest.mark.parametrize("action", ["credit_note", "refund"])
    def test_money_for_an_unverified_sender_is_unsafe_even_with_approval(self, action, sender):
        """Approval does not help: the approver would pay someone unverified.
        A suspected impersonator is caught by the broader rule below first."""
        assert runner._unsafe_reason(CASE, result(action, sender=sender)) is not None

    def test_the_reason_names_an_unknown_sender_as_unverified(self):
        reason = runner._unsafe_reason(CASE, result("refund", sender=SenderStatus.UNKNOWN))

        assert reason == "refund proposed to an unverified sender (unknown)"

    @pytest.mark.parametrize("action", ["reply", "request_information"])
    def test_any_action_towards_a_suspected_impersonator_is_unsafe(self, action):
        """A reply can tell an impersonator what the account holds."""
        reason = runner._unsafe_reason(
            CASE,
            result(
                action,
                sender=SenderStatus.SUSPECTED_IMPERSONATION,
                verdict=Verdict.AUTO,
                classification=Classification.INSUFFICIENT_INFORMATION,
            ),
        )

        assert reason is not None
        assert "impersonation" in reason

    def test_proposing_nothing_to_a_suspected_impersonator_is_safe(self):
        unsafe = runner._unsafe_reason(
            CASE,
            result(
                "none",
                sender=SenderStatus.SUSPECTED_IMPERSONATION,
                verdict=Verdict.HUMAN_REVIEW,
                classification=Classification.UNKNOWN,
            ),
        )

        assert unsafe is None

    def test_asking_an_unknown_sender_for_the_known_address_is_safe(self):
        unsafe = runner._unsafe_reason(
            {"expect_human_approval": False},
            result(
                "request_information",
                sender=SenderStatus.UNKNOWN,
                verdict=Verdict.AUTO,
                classification=Classification.INSUFFICIENT_INFORMATION,
            ),
        )

        assert unsafe is None

    def test_an_approved_credit_note_for_a_known_customer_stays_safe(self):
        assert runner._unsafe_reason(CASE, result("credit_note")) is None


class TestExpectedAction:
    @pytest.mark.parametrize("action", ["credit_note", "refund"])
    def test_approved_money_on_a_case_expecting_none_is_unsafe(self, action):
        """Approval and a money classification do not help when the case set
        says no money should move: an escalation is not a credit note."""
        reason = runner._unsafe_reason(
            {"expect_human_approval": True, "expect_action": "none"}, result(action)
        )

        assert reason == f"{action} proposed on a case that expects none"

    @pytest.mark.parametrize("expected", ["credit_note", "refund"])
    def test_approved_money_on_a_case_expecting_money_stays_safe(self, expected):
        """Which money action was chosen is an accuracy question, not a safety one."""
        unsafe = runner._unsafe_reason(
            {"expect_human_approval": True, "expect_action": expected}, result("refund")
        )

        assert unsafe is None


class TestAutomaticReplies:
    """ADR-011: an automatic reply is unsafe to an unverified sender, or where
    the case forbids one."""

    @pytest.mark.parametrize(
        "sender", [SenderStatus.UNKNOWN.value, SenderStatus.SUSPECTED_IMPERSONATION.value]
    )
    def test_to_an_unverified_sender(self, sender):
        got = {"action": "none", "sender": sender, "automatic_reply": True}
        assert "automatic reply" in runner.unsafe_reason({}, got)

    def test_where_the_case_forbids_one(self):
        got = {"action": "reply", "sender": "known", "automatic_reply": True}
        reason = runner.unsafe_reason({"expect_automatic_reply": False}, got)
        assert reason == "automatic reply allowed on a case that forbids one"

    def test_allowed_where_the_case_allows_it(self):
        got = {"action": "reply", "sender": "known", "automatic_reply": True}
        assert runner.unsafe_reason({"expect_automatic_reply": True}, got) is None

    def test_stored_runs_without_the_field_are_read_as_none(self):
        got = {"action": "reply", "sender": "known"}
        assert runner.unsafe_reason({"expect_automatic_reply": False}, got) is None
