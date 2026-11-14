"""The bank check (specs/016-bank-evidence, FR-003, FR-003a/b): each outcome, its
precedence, every exception reason, the window, and the record it keeps."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.decision.base import findings_for_engine
from app.domain.bank import BankCredit, BankRead, BankReturn, CheckedCredit, DeclaredAccount
from app.domain.enums import InvoiceStatus, RuleCode, Severity
from app.domain.models import Invoice, RuleFinding
from app.domain.money import Money
from app.domain.rules import (
    BANK_LIMITATIONS,
    bank_credit_check,
    is_bank_finding,
)

ISSUED = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 15, tzinfo=UTC)
EUR = DeclaredAccount(id="acc_eur", currency="EUR")


def invoice(id_="inv_1", amount=5900, currency="EUR", number=None) -> Invoice:
    return Invoice(
        id=id_, customer_id="cus_1", subscription_id=None,
        total=Money(amount_minor=amount, currency=currency), status=InvoiceStatus.OPEN,
        issued_at=ISSUED, number=number,
    )


def credit(tx="tx_1", amount=5900, reference="Facture inv_1", **overrides) -> BankCredit:
    fields = {
        "account_id": "acc_eur", "transaction_id": tx, "type": "transfer",
        "state": "completed", "observed_at": NOW,
        "amount": Money(amount_minor=amount, currency=overrides.pop("currency", "EUR")),
        "created_at": ISSUED + timedelta(days=3),
        "completed_at": ISSUED + timedelta(days=3, seconds=5),
        "reference": reference, "origin": "external",
    }
    fields.update(overrides)
    return BankCredit(**fields)


def read(*credits, returns=(), accounts=(EUR,), **overrides) -> BankRead:
    fields = {
        "accounts": accounts, "credits": credits, "returns": returns,
        "window_start": ISSUED, "window_end": NOW, "margin_days": 7,
        "complete": True, "coverage_established": True,
    }
    fields.update(overrides)
    return BankRead(**fields)


def check(*credits, invoices=None, target=None, **read_overrides):
    target = target or invoice()
    return bank_credit_check(
        target, read(*credits, **read_overrides), invoices or [target], NOW
    )


class TestObserved:
    def test_one_qualifying_credit_is_observed(self):
        finding, record = check(credit())
        assert finding.code is RuleCode.BANK_CREDIT_OBSERVED
        assert finding.severity is Severity.MEDIUM
        assert finding.evidence == {
            "check": "bank", "invoice_id": "inv_1", "transaction_ids": ["tx_1"], "reasons": [],
        }
        assert record.outcome == "observed"
        (item,) = record.items
        assert isinstance(item, CheckedCredit)
        assert item.reasons == ("reference_matched", "amount_equal")
        assert item.transaction_id == "tx_1" and item.observed_at == NOW

    def test_a_printed_number_is_cited_as_the_id_is(self):
        target = invoice(number="F-2026-0042")
        finding, _ = check(credit(reference="Paiement F-2026-0042"), target=target)
        assert finding.code is RuleCode.BANK_CREDIT_OBSERVED

    def test_a_top_up_from_an_outside_payer_qualifies_too(self):
        finding, _ = check(credit(type="topup"))
        assert finding.code is RuleCode.BANK_CREDIT_OBSERVED

    def test_the_limitations_are_said_every_time(self):
        for credits in ((credit(),), (credit(amount=1),), ()):
            _, record = check(*credits)
            assert record.limitations == BANK_LIMITATIONS

    def test_the_record_carries_the_window_and_the_read(self):
        _, record = check(credit())
        assert (record.window_start, record.window_end) == (ISSUED, NOW)
        assert record.margin_days == 7
        assert record.complete is True and record.coverage_established is True
        assert record.accounts == (EUR,)
        assert record.invoice_id == "inv_1"


class TestToReconcile:
    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"state": "reverted"}, "reversed"),
            ({"amount": 3000}, "amount_partial"),
            ({"amount": 9900}, "amount_over"),
            ({"currency": "GBP"}, "currency_differs"),
            ({"fx": True}, "fx_present"),
            ({"fee": True}, "fee_present"),
            ({"origin": "unknown"}, "origin_unknown"),
            ({"origin": "internal"}, "origin_internal"),
            ({"origin": "internal", "type": "topup"}, "origin_internal"),
            ({"origin": "unknown", "type": "topup"}, "origin_unknown"),
        ],
    )
    def test_each_referenced_exception(self, overrides, reason):
        finding, record = check(credit(**overrides))
        assert finding.code is RuleCode.BANK_CREDIT_TO_RECONCILE
        assert record.outcome == "to_reconcile"
        assert reason in record.items[0].reasons
        assert reason in record.reasons
        assert reason in finding.evidence["reasons"]

    def test_fx_by_billed_amount_or_currency_is_one_flag_the_mapper_sets(self):
        # The mapper sets `fx` for a billed amount or a billed currency differing from
        # the leg's (2a); the rule reads the flag alone.
        finding, _ = check(credit(fx=True))
        assert finding.code is RuleCode.BANK_CREDIT_TO_RECONCILE

    def test_a_reference_citing_two_loaded_invoices(self):
        other = invoice("inv_2")
        finding, record = check(
            credit(reference="inv_1 et inv_2"), invoices=[invoice(), other]
        )
        assert finding.code is RuleCode.BANK_CREDIT_TO_RECONCILE
        assert "several_invoices_cited" in record.reasons

    def test_an_unloaded_invoice_is_not_a_second_citation(self):
        finding, _ = check(credit(reference="inv_1 et inv_999"))
        assert finding.code is RuleCode.BANK_CREDIT_OBSERVED

    def test_two_qualifying_credits(self):
        finding, record = check(credit("tx_1"), credit("tx_2"))
        assert finding.code is RuleCode.BANK_CREDIT_TO_RECONCILE
        assert record.reasons == ("several_candidates",)
        assert finding.evidence["transaction_ids"] == ["tx_1", "tx_2"]

    def test_an_exception_beside_a_qualifying_credit_still_reconciles(self):
        finding, record = check(credit("tx_1"), credit("tx_2", amount=100))
        assert finding.code is RuleCode.BANK_CREDIT_TO_RECONCILE
        assert record.reasons == ("amount_partial",)

    def test_a_referenced_internal_transfer_beside_a_qualifying_credit(self):
        finding, _ = check(credit("tx_1"), credit("tx_2", origin="internal"))
        assert finding.code is RuleCode.BANK_CREDIT_TO_RECONCILE

    def test_a_return_naming_the_credit(self):
        ret = BankReturn(
            id="tx_r", type="topup_return", amount=Money(amount_minor=-5900, currency="EUR"),
            created_at=ISSUED + timedelta(days=5), related_credit_id="tx_1",
        )
        finding, record = check(credit(), returns=(ret,))
        assert finding.code is RuleCode.BANK_CREDIT_TO_RECONCILE
        returned = record.items[1]
        assert returned.kind == "return" and returned.related_credit_id == "tx_1"
        assert returned.instant_is == "created"
        assert returned.instant == ISSUED + timedelta(days=5)

    def test_a_return_s_completion_instant_is_kept_when_present(self):
        done = ISSUED + timedelta(days=6)
        ret = BankReturn(
            id="tx_r", type="refund", amount=Money(amount_minor=5900, currency="EUR"),
            created_at=ISSUED + timedelta(days=5), completed_at=done, related_credit_id="tx_1",
        )
        _, record = check(credit(), returns=(ret,))
        assert record.items[1].instant == done and record.items[1].instant_is == "completed"

    def test_a_return_naming_no_credit_for_the_same_amount_after_it(self):
        ret = BankReturn(
            id="tx_r", type="topup_return", amount=Money(amount_minor=-5900, currency="EUR"),
            created_at=ISSUED + timedelta(days=9),
        )
        finding, record = check(credit(), returns=(ret,))
        assert finding.code is RuleCode.BANK_CREDIT_TO_RECONCILE
        assert "related_return_found" in record.reasons

    def test_an_unrelated_return_of_another_amount_does_not_count(self):
        ret = BankReturn(
            id="tx_r", type="topup_return", amount=Money(amount_minor=-100, currency="EUR"),
            created_at=ISSUED + timedelta(days=9),
        )
        finding, record = check(credit(), returns=(ret,))
        assert finding.code is RuleCode.BANK_CREDIT_OBSERVED
        assert len(record.items) == 1

    def test_a_reverted_credit_with_no_completion_created_inside_is_kept(self):
        finding, _ = check(credit(state="reverted", completed_at=None))
        assert finding.code is RuleCode.BANK_CREDIT_TO_RECONCILE

    def test_a_reverted_credit_with_no_completion_created_after_the_end_is_not(self):
        late = credit(state="reverted", completed_at=None, created_at=NOW + timedelta(seconds=1))
        finding, record = check(late)
        assert finding is None and record.outcome == "nothing"


class TestNothing:
    def test_no_reference_is_never_a_candidate(self):
        finding, record = check(credit(reference=None))
        assert finding is None
        assert record.outcome == "nothing" and record.items == ()

    def test_a_reference_citing_another_invoice(self):
        other = invoice("inv_2")
        finding, _ = check(credit(reference="inv_2"), invoices=[invoice(), other])
        assert finding is None

    def test_an_empty_read(self):
        finding, record = check()
        assert finding is None and record.outcome == "nothing"

    def test_a_type_outside_the_allowlist_is_not_read_as_a_payment(self):
        finding, _ = check(credit(type="card_refund"))
        assert finding is None

    def test_a_credit_on_an_undeclared_account_is_left_out(self):
        finding, _ = check(credit(account_id="acc_other"))
        assert finding is None


class TestTheWindow:
    @pytest.mark.parametrize(
        ("completed", "inside"),
        [
            (ISSUED, True),
            (NOW, True),
            (ISSUED - timedelta(seconds=1), False),
            (NOW + timedelta(seconds=1), False),
        ],
    )
    def test_completion_from_issue_to_the_frozen_instant_inclusive(self, completed, inside):
        finding, _ = check(credit(completed_at=completed, created_at=ISSUED - timedelta(days=1)))
        assert (finding is not None) is inside


class TestNotEvaluable:
    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"complete": False, "incompleteness": ("page_boundary_tie",)}, "read_incomplete"),
            ({"complete": False, "incompleteness": ("budget_spent",)}, "read_incomplete"),
            ({"coverage_established": False}, "coverage_not_established"),
            ({"accounts": (DeclaredAccount(id="acc_eur", currency="EUR", visible=False),)},
             "account_not_visible"),
            ({"accounts": (DeclaredAccount(id="acc_gbp", currency="GBP"),)},
             "no_account_in_currency"),
            ({"window_start": ISSUED + timedelta(days=1)}, "read_incomplete"),
            ({"window_end": NOW - timedelta(days=1)}, "read_incomplete"),
        ],
    )
    def test_each_reason_with_no_transaction(self, overrides, reason):
        finding, record = check(**overrides)
        assert finding.code is RuleCode.NOT_EVALUABLE
        assert finding.evidence == {"check": "bank", "invoice_id": "inv_1", "reason": reason}
        assert record.outcome == "not_evaluable" and record.reason == reason
        assert record.items == ()

    def test_it_outranks_a_qualifying_credit_and_an_exception(self):
        finding, _ = check(credit(), credit("tx_2", amount=1), coverage_established=False)
        assert finding.code is RuleCode.NOT_EVALUABLE


class TestWhatTheEngineSees:
    def test_a_bank_finding_is_its_code_alone(self):
        finding, _ = check(credit())
        other = RuleFinding(
            code=RuleCode.NOT_EVALUABLE, severity=Severity.LOW, description="d",
            evidence={"invoice_id": "inv_1", "checks": []},
        )
        unusable, _ = check(coverage_established=False)
        shown = findings_for_engine([finding, other, unusable])
        assert shown[0].evidence == {} and shown[2].evidence == {}
        assert shown[0].code is RuleCode.BANK_CREDIT_OBSERVED
        assert shown[1] is other  # not the bank's: unchanged
        assert "tx_1" not in str([f.model_dump() for f in shown])

    def test_only_the_bank_s_findings_are_bank_findings(self):
        finding, _ = check(credit())
        assert is_bank_finding(finding)
        assert not is_bank_finding(
            RuleFinding(code=RuleCode.NOT_EVALUABLE, severity=Severity.LOW,
                        description="d", evidence={"checks": []})
        )

    def test_no_bank_text_reaches_the_record_or_the_finding(self):
        finding, record = check(credit(reference="Facture inv_1 SECRET PAYER TEXT"))
        assert "SECRET" not in str(finding.model_dump())
        assert "SECRET" not in str(record.to_record())


class TestThePolicies:
    """FR-004: either bank finding, and a bank-derived NOT_EVALUABLE, block every
    correction and send the case to a person; the blocking findings keep precedence."""

    @staticmethod
    def bank_findings():
        observed, _ = check(credit())
        reconcile, _ = check(credit(amount=1))
        unusable, _ = check(coverage_established=False)
        return [observed, reconcile, unusable]

    def test_no_correction_has_an_amount(self):
        from app.policies.correction import correction_for

        dup = RuleFinding(code=RuleCode.DUPLICATE_INVOICE, severity=Severity.HIGH,
                          description="d", evidence={})
        for finding in self.bank_findings():
            for kind in ("credit_note", "refund"):
                assert correction_for(kind, invoice(), [dup, finding], []).amount is None

    def test_whatever_the_engine_says_a_person_reviews(self):
        from app.decision.base import Classification, Decision, RecommendedAction
        from app.policies.action_policy import ActionPolicy, AutonomyPolicy, Verdict

        policy = ActionPolicy(AutonomyPolicy(enabled=True, min_confidence=0.0))
        for finding in self.bank_findings():
            for action in RecommendedAction:
                decision = Decision(
                    classification=Classification.VALID_INVOICE, confidence=0.99,
                    confidence_kind="calibrated", recommended_action=action,
                )
                one = Money(amount_minor=1, currency="EUR")
                verdict = policy.evaluate(decision, [finding], one)
                assert verdict.verdict is Verdict.HUMAN_REVIEW

    def test_a_blocking_finding_keeps_its_precedence(self):
        from app.decision.base import Classification, Decision, RecommendedAction
        from app.policies.action_policy import ActionPolicy

        injection = RuleFinding(code=RuleCode.SUSPECTED_PROMPT_INJECTION,
                                severity=Severity.HIGH, description="d", evidence={})
        decision = Decision(classification=Classification.UNKNOWN, confidence=0.9,
                            confidence_kind="self_reported",
                            recommended_action=RecommendedAction.ESCALATE)
        verdict = ActionPolicy().evaluate(decision, [*self.bank_findings(), injection])
        assert "SUSPECTED_PROMPT_INJECTION" in verdict.reason
