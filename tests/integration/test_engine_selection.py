"""Choosing the engine and model per run, and the boundary around it.

The feature exists so the demo can replay one dispute across the cost ladder
of ADR-003. The tests that matter are the ones establishing that it is an
allow-list rather than a free string: on a system that proposes credit notes,
letting the caller pick the judge is privilege escalation dressed as a
configuration option.
"""

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import get_audit
from app.main import app

CASE_A = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def post(client, **extra):
    return client.post("/api/v1/disputes/investigate", json=CASE_A | extra)


class TestDefaults:
    def test_no_preference_uses_the_configured_engine(self, client):
        body = post(client).json()

        assert body["decision_engine"] == "mock"

    def test_an_explicit_choice_is_honoured(self, client):
        body = post(client, decision_engine="mock").json()

        assert body["decision_engine"] == "mock"


class TestTheAllowList:
    def test_an_unknown_engine_is_refused(self, client):
        response = post(client, decision_engine="definitely-not-an-engine")

        assert response.status_code == 422
        assert "allowed" in response.text

    def test_an_unknown_model_is_refused(self, client):
        response = post(client, decision_engine="claude", model="gpt-4")

        assert response.status_code == 422

    def test_a_model_with_a_remembered_date_suffix_is_refused(self, client):
        """The classic way to 404 on a first real call, caught at the edge."""
        response = post(client, decision_engine="claude", model="claude-opus-5-20260401")

        assert response.status_code == 422

    def test_an_unpriced_model_is_refused(self, client):
        """An unpriced model would record every run as costing nothing, which
        is worse than refusing it."""
        response = post(client, decision_engine="claude", model="claude-fable-5-1")

        assert response.status_code == 422

    def test_an_engine_that_takes_no_model_says_so(self, client):
        response = post(client, decision_engine="mock", model="claude-opus-5")

        assert response.status_code == 422
        assert "does not take a model" in response.text

    def test_refusal_is_not_a_silent_fallback(self, client):
        """The run must not proceed on the default engine after a rejected
        choice: the audit row would then name an engine the caller did not
        get."""
        before = get_audit().count()

        post(client, decision_engine="nope")

        assert get_audit().count() == before

    def test_the_allowlist_can_be_narrowed_by_configuration(self, client, monkeypatch):
        """In a real deployment the list would not contain `mock`."""
        monkeypatch.setenv("ENGINE_ALLOWLIST", "claude")
        deps.get_engine.cache_clear()

        assert deps.allowed_engines() == ["claude"]
        assert post(client, decision_engine="mock").status_code == 422


class TestItCannotWidenWhatMayHappen:
    def test_the_same_decision_authorises_identically_whatever_model_made_it(self):
        """The guarantee behind the feature, tested as behaviour.

        Note it is behavioural, not structural: ``Decision`` does carry
        ``telemetry.model_id``, so the policy *could* read which model
        answered. It does not, and this is what says so -- an identical
        decision from the cheapest and the dearest model authorises the same.
        Choosing a different judge changes who answers, not what may be done
        with the answer.
        """
        from app.decision.base import (
            Classification,
            Decision,
            EngineTelemetry,
            RecommendedAction,
        )
        from app.domain.money import Money
        from app.policies.action_policy import ActionPolicy

        def decided_by(model_id: str) -> Decision:
            return Decision(
                classification=Classification.DUPLICATE_BILLING,
                confidence=0.99,
                confidence_kind="self_reported",
                recommended_action=RecommendedAction.PROPOSE_CREDIT_NOTE,
                telemetry=EngineTelemetry(model_id=model_id, prompt_version="decision_v1"),
            )

        policy = ActionPolicy()
        amount = Money(amount_minor=9900, currency="EUR")

        cheapest = policy.evaluate(decided_by("claude-haiku-4-5"), [], amount)
        dearest = policy.evaluate(decided_by("claude-opus-5"), [], amount)

        assert cheapest == dearest
        assert cheapest.verdict.value == "requires_approval"

    def test_a_chosen_engine_still_cannot_execute_without_a_human(self, client):
        body = post(client, decision_engine="mock").json()

        assert body["requires_human_approval"] is True


class TestAuditability:
    def test_the_engine_that_actually_ran_is_recorded(self, client):
        """Which is what makes a cost comparison between two runs meaningful."""
        body = post(client, decision_engine="mock").json()

        row = get_audit().run(body["run_id"])

        assert row["decision_engine"] == "mock"
        assert row["decision_engine_version"] == "1.0.0"
