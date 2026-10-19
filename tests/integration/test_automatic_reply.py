"""ADR-011, end to end: who may receive an automatic reply, and who is alerted."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.integration._auth import SERVICE

DISPUTE = "Je crois que j'ai été facturé deux fois pour le mois de septembre."
ABSENT = "Je suis absente jusqu'au 3 novembre. Pour toute urgence, écrivez à la comptabilité."


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers=SERVICE)


def investigate(client: TestClient, **payload) -> dict:
    response = client.post("/api/v1/disputes/investigate", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


class TestKnownSender:
    def test_a_known_customer_may_get_an_automatic_reply(self, client):
        body = investigate(
            client,
            message="Pourquoi deux prélèvements ce mois-ci ?",
            customer_email="bruno@transports-nord.fr",
            invoice_id="inv_011",
        )
        assert body["proposed_action"]["type"] == "reply"
        assert body["automatic_reply_allowed"] is True
        assert body["automatic_reply_reason"] == "allowed"

    def test_an_out_of_office_reply_is_set_aside(self, client):
        body = investigate(
            client,
            message=ABSENT,
            customer_email="alice@acme.fr",
            headers={"Auto-Submitted": "auto-replied"},
        )
        assert "AUTOMATED_RESPONSE" in body["findings"]
        assert body["decision_engine"] == "none"
        assert body["proposed_action"]["type"] == "none"
        assert body["requires_human_approval"] is False
        assert body["automatic_reply_allowed"] is False
        assert body["automatic_reply_reason"] == "automated_message"

    def test_bulk_mail_is_investigated_but_never_answered(self, client):
        body = investigate(
            client,
            message=DISPUTE,
            customer_email="alice@acme.fr",
            invoice_id="inv_002",
            headers={"Precedence": "bulk", "List-Id": "<facturation.acme.fr>"},
        )
        assert "AUTOMATED_MESSAGE" in body["findings"]
        assert "DUPLICATE_INVOICE" in body["findings"]
        assert body["decision_engine"] == "mock"
        assert body["automatic_reply_allowed"] is False

    def test_a_credit_note_is_never_an_automatic_reply(self, client):
        body = investigate(
            client, message=DISPUTE, customer_email="alice@acme.fr", invoice_id="inv_002"
        )
        assert body["automatic_reply_allowed"] is False
        assert body["automatic_reply_reason"] == "needs_a_person"


class TestUnknownSender:
    def test_a_person_gets_no_automatic_reply(self, client):
        """The request is still proposed; a person sends it, or not."""
        body = investigate(
            client,
            message="Bonjour, pouvez-vous m'envoyer le detail de ma derniere facture ?",
            customer_email="jean.dupont@example.test",
        )
        assert body["sender"] == "unknown"
        assert body["proposed_action"]["type"] == "request_information"
        assert body["automatic_reply_allowed"] is False
        assert body["automatic_reply_reason"] == "unknown_sender"

    def test_a_forged_authentication_pass_changes_nothing(self, client):
        body = investigate(
            client,
            message="Bonjour, pouvez-vous m'envoyer le detail de ma derniere facture ?",
            customer_email="jean.dupont@example.test",
            headers={"Authentication-Results": "mx.example.test; dmarc=pass; spf=pass"},
        )
        assert body["automatic_reply_allowed"] is False
        assert body["automatic_reply_reason"] == "unknown_sender"

    @pytest.mark.parametrize(
        "sender, headers",
        [
            ("news@promo.example.test", {"List-Unsubscribe": "<mailto:stop@promo.example.test>"}),
            ("MAILER-DAEMON@mail.example.test", {"Return-Path": "<>"}),
        ],
        ids=["newsletter", "delivery-report"],
    )
    def test_automated_mail_is_set_aside(self, client, sender, headers):
        body = investigate(
            client,
            message="Offre speciale : -50 % sur nos abonnements.",
            customer_email=sender,
            headers=headers,
        )
        assert body["proposed_action"]["type"] == "none"
        assert body["requires_human_approval"] is False
        assert body["automatic_reply_allowed"] is False
        assert body["automatic_reply_reason"] == "automated_message"

    def test_impersonation_outranks_bulk_mail(self, client):
        """A marketing mail that cites a customer's invoice still reaches a person."""
        body = investigate(
            client,
            message="Votre facture inv_002 est disponible.",
            customer_email="news@promo.example.test",
            invoice_id="inv_002",
            headers={"Precedence": "bulk"},
        )
        assert body["sender"] == "suspected_impersonation"
        assert body["requires_human_approval"] is True
        assert body["automatic_reply_allowed"] is False

    def test_an_auto_reply_citing_an_invoice_is_set_aside(self, client):
        """An out-of-office quoting our mail is not a fraud attempt."""
        body = investigate(
            client,
            message="Je suis absent. Votre message concernant inv_002 sera traite a mon retour.",
            customer_email="assistant@example.test",
            invoice_id="inv_002",
            headers={"Auto-Submitted": "auto-replied"},
        )
        assert body["requires_human_approval"] is False
        assert body["proposed_action"]["type"] == "none"


class TestHeaderLimits:
    @pytest.mark.parametrize(
        "headers",
        [
            {f"X-H{i}": "v" for i in range(101)},
            {"Received": ["hop"] * 11},
            {"Precedence": "b" * 1001},
        ],
        ids=["too-many-names", "too-many-values", "too-long"],
    )
    def test_bounded(self, client, headers):
        response = client.post(
            "/api/v1/disputes/investigate",
            json={"message": DISPUTE, "customer_email": "alice@acme.fr", "headers": headers},
        )
        assert response.status_code == 422

    def test_repeated_values_are_read(self, client):
        body = investigate(
            client,
            message=ABSENT,
            customer_email="alice@acme.fr",
            headers={"Auto-Submitted": ["no", "auto-replied"]},
        )
        assert "AUTOMATED_RESPONSE" in body["findings"]
