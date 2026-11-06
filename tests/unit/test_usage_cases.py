"""The usage cases of the development set (specs/012-prompt-v4-usage, FR-001,
FR-002): what the runner's required-subset findings cannot assert."""

import json
from pathlib import Path

from app.application.investigate_dispute import DisputeInvestigator
from app.decision.base import Classification, Decision, RecommendedAction
from app.domain.enums import RuleCode
from app.domain.models import Dispute
from app.providers.fake.adapter import FakeBillingProvider
from evals import runner

CASES = {c["id"]: c for c in json.loads(Path("evals/cases.json").read_text(encoding="utf-8"))}


class Recording:
    """An engine that records the findings it was given."""

    name = "recording"
    version = "recording/v1"

    def __init__(self):
        self.seen = None

    async def decide(self, dispute, context, findings):
        self.seen = [f.code for f in findings]
        return Decision(
            classification=Classification.VALID_INVOICE,
            confidence=0.9,
            confidence_kind="self_reported",
            recommended_action=RecommendedAction.EXPLAIN_INVOICE,
        )


async def investigate(case_id):
    case = CASES[case_id]
    engine = Recording()
    dispute = Dispute(
        id="eval_case",
        customer_message=case["message"],
        customer_email=case["customer_email"],
        invoice_id=case["invoice_id"],
    )
    investigator = DisputeInvestigator(provider=FakeBillingProvider(), engine=engine)
    return await investigator.investigate(dispute, now=runner.EVALUATION_DATE), engine


class TestUnderBilling:
    async def test_no_usage_finding_reaches_the_engine_or_the_result(self):
        result, engine = await investigate("usage_underbilled_explanation")
        usage = {RuleCode.SUSPICIOUS_USAGE_DELTA, RuleCode.USAGE_UNAVAILABLE}
        assert engine.seen is not None
        assert not usage & set(engine.seen)
        assert not usage & {f.code for f in result.findings}

    async def test_the_audit_note_is_written(self):
        result, _ = await investigate("usage_underbilled_explanation")
        notes = [e for e in result.events if e.event == "usage_under_billed"]
        assert len(notes) == 1
        assert notes[0].payload == {"expected": "60", "billed": "20"}


class TestTheWorlds:
    async def test_each_over_billing_puts_the_finding_before_the_engine(self):
        for case_id in (
            "usage_overbilled_fr",
            "usage_overbilled_en",
            "usage_overbilled_terse",
            "usage_overbilled_other_complaint",
        ):
            _, engine = await investigate(case_id)
            assert RuleCode.SUSPICIOUS_USAGE_DELTA in engine.seen, case_id

    async def test_the_control_has_no_usage_finding(self):
        _, engine = await investigate("usage_corroborated_control")
        assert engine.seen == []

    async def test_the_control_differs_from_its_twin_by_the_usage_alone(self):
        twin, control = CASES["usage_overbilled_fr"], CASES["usage_corroborated_control"]
        assert control["message"] == twin["message"]

    async def test_unchecked_usage_is_said(self):
        _, engine = await investigate("usage_unavailable_disputed")
        assert RuleCode.USAGE_UNAVAILABLE in engine.seen
