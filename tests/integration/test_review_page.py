"""The approval page: what it shows, what it refuses, and what it survives.

The interesting tests are not the rendering ones. They are: a GET never moves
money, the page cannot be made to carry injected markup, and a decision
survives the orchestrator being unreachable.
"""

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_actions, get_audit, get_provider
from app.main import app

CASE_A = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def action_id(client) -> str:
    body = client.post("/api/v1/disputes/investigate", json=CASE_A).json()
    return body["action_id"]


def review(client, action_id, **params):
    return client.get(f"/api/v1/actions/{action_id}/review", params=params)


class TestThePage:
    def test_it_shows_the_amount_and_the_invoice(self, client, action_id):
        page = review(client, action_id).text

        assert "99.00 EUR" in page
        assert "inv_002" in page

    def test_it_shows_both_buttons(self, client, action_id):
        page = review(client, action_id).text

        assert 'value="approve"' in page
        assert 'value="reject"' in page

    def test_it_shows_the_engine_and_the_kind_of_confidence(self, client, action_id):
        """A reviewer being shown a score is told what kind of number it is."""
        page = review(client, action_id).text

        assert "mock (1.0.0)" in page
        assert "self_reported" in page
        assert "non mesuré" in page

    def test_it_says_the_amount_did_not_come_from_the_model(self, client, action_id):
        assert "pas par le moteur de décision" in review(client, action_id).text

    def test_it_admits_the_identifier_is_not_authentication(self, client, action_id):
        """Stating the limit is stronger than hiding it."""
        assert "SSO" in review(client, action_id).text

    def test_an_unknown_action_is_a_404(self, client):
        assert review(client, "act_nope").status_code == 404


class TestGetMovesNothing:
    def test_loading_the_page_does_not_decide_anything(self, client, action_id):
        """The reason this page exists rather than two links in the email.

        Outlook Safe Links and antivirus gateways follow links to scan them.
        If a GET could approve, a scanner would issue credit notes.
        """
        before = len(get_provider().executed_actions)

        review(client, action_id)
        review(client, action_id)

        assert get_actions().get(action_id).state.value == "pending_approval"
        assert len(get_provider().executed_actions) == before

    def test_the_page_can_be_reloaded_without_losing_the_callback(
        self, client, action_id
    ):
        review(client, action_id, resume="http://orchestrator.test/resume/1")
        review(client, action_id)

        assert get_actions().get(action_id).resume_url == (
            "http://orchestrator.test/resume/1"
        )


class TestDeciding:
    def test_approving_records_who_and_does_not_execute(self, client, action_id):
        response = client.post(
            f"/api/v1/actions/{action_id}/review",
            data={"decision": "approve", "by": "gael@inetum.test"},
        )

        assert response.status_code == 200
        action = get_actions().get(action_id)
        assert action.state.value == "approved"
        assert action.approved_by == "gael@inetum.test"
        assert action.provider_action_id is None, "execution belongs to the next step"

    def test_rejecting_closes_the_action(self, client, action_id):
        client.post(
            f"/api/v1/actions/{action_id}/review",
            data={"decision": "reject", "by": "gael@inetum.test"},
        )

        assert get_actions().get(action_id).state.value == "rejected"

    def test_an_anonymous_decision_is_refused(self, client, action_id):
        response = client.post(
            f"/api/v1/actions/{action_id}/review", data={"decision": "approve", "by": ""}
        )

        assert response.status_code == 422
        assert get_actions().get(action_id).state.value == "pending_approval"

    def test_an_invented_decision_is_refused(self, client, action_id):
        response = client.post(
            f"/api/v1/actions/{action_id}/review",
            data={"decision": "execute_everything", "by": "gael"},
        )

        assert response.status_code == 422
        assert get_actions().get(action_id).state.value == "pending_approval"

    def test_deciding_twice_shows_the_outcome_instead_of_failing(
        self, client, action_id
    ):
        """A reviewer double-checking, or a second approver arriving late, is
        not an error worth a stack trace."""
        client.post(
            f"/api/v1/actions/{action_id}/review",
            data={"decision": "approve", "by": "first"},
        )

        second = client.post(
            f"/api/v1/actions/{action_id}/review",
            data={"decision": "reject", "by": "second"},
        )

        assert second.status_code == 409
        assert "Décision déjà enregistrée" in second.text
        assert get_actions().get(action_id).approved_by == "first"

    def test_reopening_a_settled_link_shows_the_outcome(self, client, action_id):
        client.post(
            f"/api/v1/actions/{action_id}/review",
            data={"decision": "approve", "by": "gael"},
        )

        page = review(client, action_id)

        assert page.status_code == 409
        assert "approved" in page.text


class TestTheOrchestratorIsNotLoadBearing:
    def test_a_decision_survives_an_unreachable_orchestrator(self, client, action_id):
        """The whole reason the state does not live in n8n.

        The callback points nowhere; the human's click must still count.
        """
        review(client, action_id, resume="http://127.0.0.1:9/never-listens")

        response = client.post(
            f"/api/v1/actions/{action_id}/review",
            data={"decision": "approve", "by": "gael"},
        )

        assert response.status_code == 200
        assert get_actions().get(action_id).state.value == "approved"
        # Apostrophes come back escaped: the renderer escapes every value
        # uniformly rather than keeping a list of trusted ones.
        assert "pas pu être joint" in response.text

    def test_it_works_with_no_callback_at_all(self, client, action_id):
        response = client.post(
            f"/api/v1/actions/{action_id}/review",
            data={"decision": "approve", "by": "gael"},
        )

        assert response.status_code == 200
        assert get_actions().get(action_id).state.value == "approved"


class TestInjectedMarkup:
    def test_the_dispute_message_cannot_inject_markup(self, client):
        """The explanation reaches this page from the findings, which quote
        evidence. A page that rendered it raw would be a costly irony on a
        system whose threat model opens with untrusted input.
        """
        payload = "<script>alert('xss')</script>"
        body = client.post(
            "/api/v1/disputes/investigate",
            json={
                "message": f"Facture inv_002 fausse {payload}",
                "customer_email": "alice@acme.fr",
                "invoice_id": "inv_002",
            },
        ).json()

        page = review(client, body["action_id"]).text

        assert "<script>" not in page

    def test_the_run_row_carries_the_payload_verbatim(self, client):
        """Escaped for display, stored raw: the audit records what arrived."""
        body = client.post(
            "/api/v1/disputes/investigate",
            json={
                "message": "Facture inv_002 fausse <b>gras</b>",
                "customer_email": "alice@acme.fr",
                "invoice_id": "inv_002",
            },
        ).json()

        assert "<b>gras</b>" in get_audit().run(body["run_id"])["input_message"]
