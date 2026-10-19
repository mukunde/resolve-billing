"""Taxonomy S7, S8, S10: when the evidence cannot settle a dispute, say so."""

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.application.investigate_dispute import DisputeInvestigator
from app.decision.base import EngineUnavailable
from app.main import app
from app.providers.base import ProviderUnavailable
from tests.integration._auth import SERVICE


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers=SERVICE)


def investigate(client: TestClient, **payload) -> dict:
    response = client.post("/api/v1/disputes/investigate", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


class TestOldInvoice:
    def test_the_customers_old_invoice_goes_to_a_person(self, client):
        body = investigate(
            client,
            message="La facture inv_299 de fevrier comportait une erreur.",
            customer_email="compta@sirius-labs.fr",
            invoice_id="inv_299",
        )
        assert body["findings"] == ["INVOICE_OUTSIDE_WINDOW"]
        assert body["decision_engine"] == "none"
        assert body["requires_human_approval"] is True
        assert "fenêtre d'analyse" in body["authorization_reason"]

    def test_another_customers_invoice_is_still_unknown(self, client):
        """Looked up directly, but nothing about another account is kept."""
        body = investigate(
            client,
            message="Ma facture inv_299 est fausse.",
            customer_email="alice@acme.fr",
            invoice_id="inv_299",
        )
        assert "UNKNOWN_INVOICE" in body["findings"]
        assert "INVOICE_OUTSIDE_WINDOW" not in body["findings"]
        assert "sirius" not in body["explanation"].lower()

    def test_a_reference_that_exists_nowhere_is_still_unknown(self, client):
        body = investigate(
            client,
            message="Ma facture inv_999 est fausse.",
            customer_email="alice@acme.fr",
            invoice_id="inv_999",
        )
        assert "UNKNOWN_INVOICE" in body["findings"]


class TestCurrencies:
    def test_a_duplicate_across_currencies_is_not_concluded(self, client):
        body = investigate(
            client,
            message="Facturés deux fois pour septembre, en euros et en dollars.",
            customer_email="compta@sirius-labs.fr",
            invoice_id="inv_301",
        )
        assert "CURRENCY_MISMATCH" in body["findings"]
        assert "DUPLICATE_INVOICE" not in body["findings"]
        assert body["decision_engine"] == "none"
        assert body["proposed_action"]["type"] == "none"
        assert body["requires_human_approval"] is True

    def test_one_currency_on_the_other_invoice_is_not_an_anomaly(self, client):
        body = investigate(
            client,
            message="Pouvez-vous m'expliquer ma facture de septembre ?",
            customer_email="compta@sirius-labs.fr",
            invoice_id="inv_299",
        )
        assert "CURRENCY_MISMATCH" not in body["findings"]


class TestUsageNobodyChecked:
    def test_a_metered_line_without_usage_is_said_and_reviewed(self, client):
        body = investigate(
            client,
            message="Nous n'avons pas fait autant d'appels en septembre.",
            customer_email="facturation@meridien-data.fr",
            invoice_id="inv_310",
        )
        assert "USAGE_UNAVAILABLE" in body["findings"]
        assert body["classification"] == "valid_invoice"
        assert body["requires_human_approval"] is True
        assert body["automatic_reply_allowed"] is False


class Outage:
    name = "outage"
    version = "0"

    async def decide(self, dispute, context, findings):
        raise EngineUnavailable("APITimeoutError")


class TestEngineOutage:
    def test_an_unreachable_engine_sends_the_dispute_to_a_person(self, client):
        app.dependency_overrides[deps.get_investigator] = lambda: DisputeInvestigator(
            provider=deps.get_provider(),
            engine=Outage(),
            audit=deps.get_audit(),
            actions=deps.get_actions(),
        )
        try:
            body = investigate(
                client,
                message="Je crois que j'ai été facturé deux fois en septembre.",
                customer_email="alice@acme.fr",
                invoice_id="inv_002",
            )
        finally:
            app.dependency_overrides.pop(deps.get_investigator, None)
        assert body["classification"] == "unknown"
        assert body["proposed_action"]["type"] == "none"
        assert body["requires_human_approval"] is True
        assert "indisponible" in body["explanation"]
        trace = client.get(f"/api/v1/runs/{body['run_id']}").json()
        kinds = [e["event"] for e in trace.get("events", [])]
        assert "decision_engine_unavailable" in kinds


class TestProviderOutage:
    def test_a_failed_read_decides_nothing_and_says_so(self, client, monkeypatch):
        provider = deps.get_provider()

        async def down(*args, **kwargs):
            raise ProviderUnavailable("timeout")

        monkeypatch.setattr(provider, "list_invoices", down)
        response = client.post(
            "/api/v1/disputes/investigate",
            json={
                "message": "Facture de septembre en double ?",
                "customer_email": "alice@acme.fr",
                "invoice_id": "inv_002",
            },
        )
        assert response.status_code == 503
        assert "nothing was decided" in response.json()["detail"]
