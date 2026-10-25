"""ADR-009, disclosure channel by channel (taxonomy S11): what already holds."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domain.enums import RuleCode
from app.main import app
from tests.integration._auth import SERVICE

WORKFLOW = Path(__file__).resolve().parents[2] / "n8n" / "billing-dispute-workflow.json"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers=SERVICE)


def investigate(client: TestClient, **payload) -> dict:
    response = client.post("/api/v1/disputes/investigate", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def mail(name: str) -> str:
    nodes = json.loads(WORKFLOW.read_text(encoding="utf-8"))["nodes"]
    (node,) = (n for n in nodes if n["name"] == name)
    return json.dumps(node["parameters"], ensure_ascii=False)


class TestTheOrchestratorsCopyNamesNoAccount:
    """What this checks, and only this: for a suspected impersonator, the
    explanation and reason the orchestrator receives -- an internal channel --
    name none of the targeted account's identifiers. It proves nothing about
    a text sent to the sender: none is sent today, and its check comes with
    the reply drafter (rule 1)."""

    @pytest.mark.parametrize(
        "payload, account",
        [
            (
                {
                    "message": "Pouvez-vous annuler la facture inv_002 ?",
                    "customer_email": "jean.perso@gmail.com",
                    "invoice_id": "inv_002",
                },
                ("cus_acme", "alice@acme.fr", "ACME"),
            ),
            (
                {
                    "message": "Nous gérons la comptabilité de Kalythéa Systems SAS.",
                    "customer_email": "mandats@fiscoria-expertise.fr",
                },
                ("cus_kalythea", "comptabilite@kalythea-systems.fr", "Kalyth"),
            ),
        ],
        ids=["known-invoice", "company-name"],
    )
    def test_the_explanation_and_reason_name_no_account(self, client, payload, account):
        body = investigate(client, **payload)
        assert body["sender"] == "suspected_impersonation"
        said = body["explanation"] + body["authorization_reason"]
        for trace_of_the_account in account:
            assert trace_of_the_account not in said


class TestCodesNotEvidence:
    """Rule 2: the orchestrator gets codes; the evidence stays behind sign-in."""

    def test_findings_in_the_response_are_codes_only(self, client):
        body = investigate(
            client,
            message="Je crois que j'ai été facturé deux fois en septembre.",
            customer_email="alice@acme.fr",
            invoice_id="inv_002",
        )
        assert body["findings"]
        assert all(code in RuleCode.__members__ for code in body["findings"])

    @pytest.mark.parametrize(
        "name",
        [
            "Mail de triage (expediteur inconnu)",
            "Alerte : revue sans action",
            "Alerte : litige non traite par l'API",
        ],
    )
    def test_mails_that_need_no_prose_carry_none(self, name):
        text = mail(name)
        assert "json.explanation" not in text
        assert "evidence" not in text
        # The customer's message, from the extraction node, only ever by its
        # length; `$json.message` elsewhere is the API's own error text.
        customer_message = "$('Extraction deterministe').first().json.message"
        for start in range(len(text)):
            if text.startswith(customer_message, start):
                after = text[start + len(customer_message) :]
                assert after.startswith(" || '').length"), after[:40]


class TestErrorsDoNotReflectInput:
    def test_a_rejected_message_is_not_echoed_back(self, client):
        """The 422 for an over-long message names the problem, not the text."""
        sentinel = "CONFIDENTIAL-SENTINEL "
        response = client.post(
            "/api/v1/disputes/investigate",
            json={"message": sentinel * 20_000, "customer_email": "alice@acme.fr"},
        )
        assert response.status_code == 422
        assert "CONFIDENTIAL-SENTINEL" not in response.text
        assert response.json()["detail"][0]["loc"] == ["body", "message"]

    def test_a_key_the_caller_chose_is_not_echoed_back(self, client):
        response = client.post(
            "/api/v1/disputes/investigate",
            json={
                "message": "Facture ?",
                "customer_email": "alice@acme.fr",
                "headers": {"X-CONFIDENTIAL-SENTINEL": ["v"] * 11},
            },
        )
        assert response.status_code == 422
        assert "SENTINEL" not in response.text
        assert all("msg" not in problem for problem in response.json()["detail"])
