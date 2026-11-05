"""The engine's own recommendation, scored apart from the system's action
(specs/012-prompt-v4-usage, FR-001a).

The action policy can turn a money recommendation on `unknown` into no action,
so the system's action alone cannot show that the engine escalated."""

import json
from pathlib import Path

from app.application.investigate_dispute import DisputeInvestigator
from app.decision.base import Classification, Decision, RecommendedAction
from app.domain.models import Dispute
from app.providers.fake.adapter import FakeBillingProvider
from evals import rescore, runner, variance

CASES = json.loads(Path("evals/cases.json").read_text(encoding="utf-8"))


def base_case(**extra):
    case = next(c for c in CASES if c["id"] == "legitimate_overage")
    return case | extra


class Answering:
    name = "answering"
    version = "answering/v1"

    def __init__(self, classification, action):
        self._decision = Decision(
            classification=classification,
            confidence=0.9,
            confidence_kind="self_reported",
            recommended_action=action,
        )

    async def decide(self, dispute, context, findings):
        return self._decision


async def evaluate(case, engine):
    dispute = Dispute(
        id="eval_case",
        customer_message=case["message"],
        customer_email=case["customer_email"],
        customer_id=case.get("customer_id"),
        invoice_id=case["invoice_id"],
        headers=runner.header_pairs(case.get("headers")),
    )
    investigator = DisputeInvestigator(provider=FakeBillingProvider(), engine=engine)
    result = await investigator.investigate(dispute, now=runner.EVALUATION_DATE)
    return runner._evaluate(case, result)


class TestTheRunner:
    async def test_a_suppressed_credit_note_is_not_an_escalation(self):
        case = base_case(
            expect_classification="unknown",
            expect_engine_action="escalate",
            expect_action="none",
            expect_human_approval=True,
            expect_automatic_reply=False,
        )
        outcome = await evaluate(
            case, Answering(Classification.UNKNOWN, RecommendedAction.PROPOSE_CREDIT_NOTE)
        )
        assert outcome.got["action"] == "none"
        assert outcome.action_ok
        assert outcome.recommendation_ok is False
        assert outcome.ok is False

    async def test_the_expected_recommendation_passes(self):
        case = base_case(
            expect_classification="unknown",
            expect_engine_action="escalate",
            expect_action="none",
            expect_human_approval=True,
            expect_automatic_reply=False,
        )
        outcome = await evaluate(
            case, Answering(Classification.UNKNOWN, RecommendedAction.ESCALATE)
        )
        assert outcome.recommendation_ok
        assert outcome.ok

    async def test_no_recommendation_fails_an_expectation(self):
        # The injection screen settles the case before any engine is asked.
        injection = next(c for c in CASES if c["id"] == "injection_refund")
        outcome = await evaluate(
            injection | {"expect_engine_action": "escalate"},
            Answering(Classification.UNKNOWN, RecommendedAction.ESCALATE),
        )
        assert outcome.got["recommended_action"] is None
        assert outcome.recommendation_ok is False

    async def test_a_case_declaring_nothing_scores_as_today(self):
        outcome = await evaluate(
            base_case(), Answering(Classification.VALID_INVOICE, RecommendedAction.EXPLAIN_INVOICE)
        )
        assert outcome.recommendation_ok
        assert outcome.ok


class TestTheBaseline:
    def outcome(self, recommendation_ok):
        return runner.CaseOutcome(
            case_id="c",
            classification_ok=True,
            action_ok=True,
            approval_ok=True,
            findings_ok=True,
            skip_ok=True,
            unsafe=None,
            confidence=0.9,
            confidence_kind="self_reported",
            cost_micros=None,
            latency_ms=None,
            recommendation_ok=recommendation_ok,
        )

    def test_it_records_the_check(self):
        assert runner.baseline_of([self.outcome(True)])["c"]["recommendation"] is True

    def test_a_moved_recommendation_is_reported(self):
        baseline = runner.baseline_of([self.outcome(True)])
        diffs = runner.compare_to_baseline(baseline, [self.outcome(False)])
        assert diffs == ["c: recommendation passed in the baseline, fails now"]


def stored(tmp_path, recommended, *, recommendation_ok=None):
    outcome = {
        "case_id": "usage_case",
        "classification_ok": True,
        "action_ok": True,
        "approval_ok": True,
        "findings_ok": True,
        "skip_ok": True,
        "unsafe": None,
        "got": {
            "classification": "unknown",
            "action": "none",
            "recommended_action": recommended,
            "approval": True,
            "findings": [],
            "engine": "x",
        },
    }
    if recommendation_ok is not None:
        outcome["recommendation_ok"] = recommendation_ok
    path = tmp_path / "run.json"
    path.write_text(
        json.dumps({"engine": "x", "ran_at": "2026-11-05T00:00:00", "outcomes": [outcome]}),
        encoding="utf-8",
    )
    return path


class TestRescore:
    def cases(self, tmp_path, monkeypatch):
        case = base_case(
            id="usage_case",
            expect_classification="unknown",
            expect_engine_action="escalate",
            expect_action="none",
            expect_human_approval=True,
            expect_findings=[],
        )
        case.pop("expect_automatic_reply", None)
        path = tmp_path / "cases.json"
        path.write_text(json.dumps([case]), encoding="utf-8")
        monkeypatch.setattr(rescore, "CASES_PATH", path)

    def test_it_recomputes_the_check(self, tmp_path, monkeypatch, capsys):
        self.cases(tmp_path, monkeypatch)
        rescore.rescore(stored(tmp_path, "propose_credit_note", recommendation_ok=True))
        out = capsys.readouterr().out
        assert "recommendation" in out
        assert "usage_case: correct then, wrong today" in out

    def test_the_expected_recommendation_is_full(self, tmp_path, monkeypatch, capsys):
        self.cases(tmp_path, monkeypatch)
        rescore.rescore(stored(tmp_path, "escalate"))
        out = capsys.readouterr().out
        assert "recommendation                : 1/1 declared" in out
        assert "full                          : 1/1" in out


class TestStoredResults:
    def test_variance_reads_a_missing_check_as_passed(self):
        outcome = {name: True for name in variance.CHECKS} | {"case_id": "c"}
        outcome.pop("recommendation_ok", None)
        assert variance.fully_correct(outcome)

    def test_variance_holds_it_when_present(self):
        outcome = {name: True for name in variance.CHECKS} | {"case_id": "c"}
        outcome["recommendation_ok"] = False
        assert not variance.fully_correct(outcome)
