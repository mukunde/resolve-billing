"""What a proposal on a taxed invoice is made of, recorded and shown, and an
approval bound to it (specs/013-invoice-tax, FR-007, FR-008, US2)."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_actions, get_provider
from app.domain.models import LineTax
from app.domain.money import Money
from app.main import app
from tests.integration._auth import APPROVER, SERVICE, basic
from tests.integration._proposal import shown

TAXED = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "lea@vega-conseil.fr",
    "invoice_id": "inv_601",
}
UNTAXED = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}
BREAKDOWN = {
    "currency": "EUR",
    "excluding_tax_minor": 10000,
    "taxes": [{"rate": "20", "amount_minor": 2000}],
    "total_minor": 12000,
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers=basic(APPROVER))


def investigate(client, case) -> dict:
    response = client.post("/api/v1/disputes/investigate", json=case, headers=SERVICE)
    assert response.status_code == 200, response.text
    return response.json()


def change_tax(invoice_id: str, rate: str, amount_minor: int) -> None:
    provider = get_provider()
    invoice = next(i for i in provider._invoices if i.id == invoice_id)
    lines = [
        line.model_copy(
            update={
                "taxes": (
                    LineTax(
                        amount=Money(amount_minor=amount_minor, currency="EUR"),
                        rate=Decimal(rate),
                    ),
                )
            }
        )
        for line in invoice.lines
    ]
    provider.change_invoice(invoice_id, lines=lines, total_minor=10000 + amount_minor)


class TestTheBreakdownIsRecordedAndShown:
    def test_the_proposal_carries_it(self, client):
        result = investigate(client, TAXED)
        assert result["proposed_action"]["amount_minor"] == 12000
        assert result["proposed_action"]["tax_breakdown"] == BREAKDOWN

    def test_the_action_carries_it(self, client):
        action_id = investigate(client, TAXED)["action_id"]
        assert client.get(f"/api/v1/actions/{action_id}").json()["tax_breakdown"] == BREAKDOWN
        assert get_actions().get(action_id).tax_breakdown is not None

    def test_the_review_page_shows_its_parts(self, client):
        action_id = investigate(client, TAXED)["action_id"]
        page = client.get(f"/api/v1/actions/{action_id}/review").text
        assert "HT : 100.00 EUR" in page
        assert "TVA 20 % : 20.00 EUR" in page
        assert "TTC : 120.00 EUR" in page

    def test_the_run_page_shows_it(self, client):
        run_id = investigate(client, TAXED)["run_id"]
        page = client.get(f"/api/v1/runs/{run_id}/view").text
        assert "TTC : 120.00 EUR" in page

    def test_an_untaxed_proposal_shows_nothing_new(self, client):
        result = investigate(client, UNTAXED)
        assert "tax_breakdown" not in result["proposed_action"]
        action = client.get(f"/api/v1/actions/{result['action_id']}").json()
        assert "tax_breakdown" not in action
        page = client.get(f"/api/v1/actions/{result['action_id']}/review").text
        assert "TVA" not in page


class TestAnApprovalHoldsForTheTaxShown:
    def test_a_tax_changed_before_the_page_is_opened_offers_nothing(self, client):
        """US2-2, before presentation: the breakdown recorded is not the
        invoice's any more."""
        action_id = investigate(client, TAXED)["action_id"]
        change_tax("inv_601", "5.5", 550)

        page = client.get(f"/api/v1/actions/{action_id}/review")

        assert page.status_code == 409
        assert "Proposition périmée" in page.text

    def test_a_tax_changed_before_approval_refuses_it(self, client):
        action_id = investigate(client, TAXED)["action_id"]
        proposal = shown(client, action_id)
        change_tax("inv_601", "5.5", 550)

        response = client.post(
            f"/api/v1/actions/{action_id}/approve", json={"proposal": proposal}
        )

        assert response.status_code == 409
        assert get_actions().get(action_id).state.value == "expired"
        assert get_actions().get(action_id).tax_breakdown is not None

    def test_a_tax_changed_after_approval_is_not_written(self, client):
        action_id = investigate(client, TAXED)["action_id"]
        proposal = shown(client, action_id)
        client.post(f"/api/v1/actions/{action_id}/approve", json={"proposal": proposal})
        change_tax("inv_601", "5.5", 550)

        response = client.post(f"/api/v1/actions/{action_id}/execute", headers=SERVICE)

        assert response.status_code == 409
        assert get_provider().executed_actions == []
