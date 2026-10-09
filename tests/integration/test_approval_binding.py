"""D13. An approval holds for what was shown, and not forever.

Before: a request for approval stayed decidable indefinitely, and the
decision named only the action. If the invoice changed between the page
being shown and the click -- or between the click and the execution -- the
approval applied to facts nobody had looked at.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_actions, get_provider
from app.main import app
from tests.integration._auth import APPROVER, SERVICE, basic
from tests.integration._proposal import shown

WORKFLOW = Path(__file__).resolve().parents[2] / "n8n" / "billing-dispute-workflow.json"

CASE_A = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers=basic(APPROVER))


@pytest.fixture
def action_id(client) -> str:
    return client.post(
        "/api/v1/disputes/investigate", json=CASE_A, headers=SERVICE
    ).json()["action_id"]


def page_proposal(client, action_id) -> str:
    page = client.get(f"/api/v1/actions/{action_id}/review").text
    return re.search(r'name="proposal" value="([0-9a-f]+)"', page).group(1)


class TestTheDecisionNamesWhatWasShown:
    def test_the_page_carries_the_proposal_it_shows(self, client, action_id):
        assert page_proposal(client, action_id) == shown(client, action_id)

    def test_the_page_shows_the_deadline(self, client, action_id):
        assert "À décider avant" in client.get(f"/api/v1/actions/{action_id}/review").text

    def test_an_approval_that_names_nothing_is_refused(self, client, action_id):
        response = client.post(f"/api/v1/actions/{action_id}/approve", json={})
        form = client.post(f"/api/v1/actions/{action_id}/review", data={"decision": "approve"})

        assert response.status_code == 422
        assert form.status_code == 422
        assert get_actions().get(action_id).state.value == "pending_approval"

    def test_an_invoice_changed_while_the_page_was_open_expires_the_action(
        self, client, action_id
    ):
        proposal = page_proposal(client, action_id)
        get_provider().change_invoice("inv_002", total_minor=4900)

        response = client.post(
            f"/api/v1/actions/{action_id}/review",
            data={"decision": "approve", "proposal": proposal},
        )

        assert response.status_code == 409
        assert "la facture a changé" in response.text
        assert get_actions().get(action_id).state.value == "expired"

    def test_an_invoice_changed_after_approval_is_not_written(self, client, action_id):
        client.post(
            f"/api/v1/actions/{action_id}/approve",
            json={"proposal": shown(client, action_id)},
        )
        get_provider().change_invoice("inv_002", total_minor=4900)

        response = client.post(f"/api/v1/actions/{action_id}/execute", headers=SERVICE)

        assert response.status_code == 409
        assert get_actions().get(action_id).state.value == "expired"
        assert get_provider().executed_actions == []


class TestARequestHasADeadline:
    @pytest.fixture
    def overdue(self, client, monkeypatch) -> str:
        monkeypatch.setenv("RESOLVE_APPROVAL_TTL_HOURS", "-1")
        return client.post(
            "/api/v1/disputes/investigate", json=CASE_A, headers=SERVICE
        ).json()["action_id"]

    def test_the_action_reports_its_deadline(self, client, action_id):
        assert client.get(f"/api/v1/actions/{action_id}").json()["expires_at"]

    def test_an_overdue_request_is_shown_as_expired(self, client, overdue):
        page = client.get(f"/api/v1/actions/{overdue}/review")

        assert page.status_code == 409
        assert "expired" in page.text
        assert get_actions().get(overdue).state.value == "expired"

    def test_an_overdue_request_cannot_be_approved(self, client, overdue):
        response = client.post(
            f"/api/v1/actions/{overdue}/approve", json={"proposal": "anything"}
        )

        assert response.status_code == 409
        assert get_actions().get(overdue).state.value == "expired"

    def test_an_overdue_request_cannot_be_rejected_either(self, client, overdue):
        """Expired is the record that nobody decided in time, not a refusal."""
        response = client.post(f"/api/v1/actions/{overdue}/reject")

        assert response.status_code == 409
        assert get_actions().get(overdue).state.value == "expired"


class TestAnExecutionThatDoesNotGoThroughIsReported:
    """An approved action that expires at execution answers 409 -- as a
    refusal by the platform answers 502 and a lost answer 504. The workflow's
    execute node had no error branch: the run stopped there, and nobody was
    told that what a person had approved was not done."""

    def test_the_workflow_alerts_a_person_when_execution_fails(self):
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        nodes = {node["name"]: node for node in workflow["nodes"]}
        execute = nodes["POST /actions/{id}/execute"]

        assert execute.get("onError") == "continueErrorOutput"
        outputs = workflow["connections"]["POST /actions/{id}/execute"]["main"]
        assert len(outputs) == 2, "a success output and an error output"
        (target,) = outputs[1]
        alert = nodes[target["node"]]
        assert alert["type"].endswith("emailSend")
        # The person alerted can act: which action, and what the API said.
        assert "action_id" in alert["parameters"]["subject"]
        assert "$json.error" in alert["parameters"]["text"]
