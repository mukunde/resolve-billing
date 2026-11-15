"""What the evaluation asserts beyond a superset of findings (specs/016-bank-evidence,
adjudicated 2026-11-15): absences a label rests on, and the bank check's outcome."""

from __future__ import annotations

import json

import pytest

from evals.check import _well_formed
from evals.runner import CASES_PATH, findings_hold


def got(findings=(), bank=None):
    return {"findings": list(findings), "bank_check": bank}


class TestFindingsHold:
    def test_a_superset_still_holds(self):
        assert findings_hold({"expect_findings": ["A"]}, got(["A", "B"]))

    def test_a_forbidden_finding_fails(self):
        case = {"expect_findings": [], "forbid_findings": ["DUPLICATE_PAYMENT"]}
        assert not findings_hold(case, got(["DUPLICATE_PAYMENT"]))
        assert findings_hold(case, got(["NOT_EVALUABLE"]))

    def test_the_bank_outcome_and_reason_must_match(self):
        case = {"expect_bank_check": {"outcome": "not_run", "reason": "paid_on_platform"}}
        assert findings_hold(case, got(bank={"outcome": "not_run", "reason": "paid_on_platform"}))
        assert not findings_hold(case, got(bank={"outcome": "not_run", "reason": "no_bank"}))
        assert not findings_hold(case, got(bank={"outcome": "nothing", "reason": None}))

    def test_a_run_that_recorded_no_bank_check_fails_an_expectation_of_one(self):
        assert not findings_hold({"expect_bank_check": {"outcome": "nothing"}}, got())
        assert not findings_hold({"expect_bank_check": {"outcome": "nothing"}}, {"findings": []})


def base_case(**extra):
    case = {
        "id": "c", "why": "w", "message": "m", "customer_email": "a@b.fr",
        "invoice_id": None, "expect_classification": "unknown", "expect_action": "none",
        "expect_human_approval": True, "expect_findings": [],
    }
    return case | extra


class TestTheCaseFile:
    @pytest.mark.parametrize(
        "extra",
        [
            {"forbid_findings": ["NOT_A_RULE"]},
            {"forbid_findings": "DUPLICATE_PAYMENT"},
            {"expect_findings": ["NOT_EVALUABLE"], "forbid_findings": ["NOT_EVALUABLE"]},
            {"expect_bank_check": {"outcome": "paid"}},
            {"expect_bank_check": {"outcome": "not_run", "reason": "SECRET"}},
            {"expect_bank_check": {"outcome": "nothing", "amount": 1}},
            {"bank_world": "no_such_world"},
        ],
    )
    def test_malformed_evidence_is_refused(self, extra):
        assert not _well_formed(base_case(**extra))

    def test_well_formed_evidence_is_accepted(self):
        assert _well_formed(base_case(
            forbid_findings=["DUPLICATE_PAYMENT"], bank_world="no_credit",
            expect_bank_check={"outcome": "not_run", "reason": "paid_on_platform"},
        ))

    def test_every_bank_case_names_its_outcome_and_an_absence(self):
        cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        bank = [c for c in cases if c.get("bank_world")]
        assert len(bank) == 18
        for case in bank:
            assert "expect_bank_check" in case, case["id"]
            assert case.get("forbid_findings"), case["id"]
            assert case["label_source"] == "maintainer, 2026-11-15", case["id"]
        paid = next(c for c in bank if c["id"] == "bank_payment_recorded_on_platform")
        assert "DUPLICATE_PAYMENT" in paid["forbid_findings"]
        assert paid["expect_bank_check"] == {"outcome": "not_run", "reason": "paid_on_platform"}
