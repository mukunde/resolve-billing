"""D12. A message too long for the engine reaches a person, never a dead end.

Before: anything over 10,000 characters was a 422 validation error, and the
n8n workflow, which had no error branch, stopped there. Nobody was told; the
customer waited for an answer that would never come.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.application.investigate_dispute import ENGINE_MESSAGE_LIMIT, MESSAGE_HARD_LIMIT
from app.main import app
from tests.integration._auth import SERVICE

WORKFLOW = Path(__file__).resolve().parents[2] / "n8n" / "billing-dispute-workflow.json"
OPENING = "Je crois que j'ai ete facture deux fois en septembre. "


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers=SERVICE)


def dispute(length: int) -> dict:
    message = (OPENING + "x" * length)[:length]
    return {"message": message, "customer_email": "alice@acme.fr", "invoice_id": "inv_002"}


class TestTooLongForTheEngine:
    def test_it_goes_to_a_person_without_calling_the_engine(self, client):
        body = client.post("/api/v1/disputes/investigate", json=dispute(ENGINE_MESSAGE_LIMIT + 1))

        assert body.status_code == 200
        result = body.json()
        assert result["decision_engine"] == "none"
        assert "MESSAGE_TOO_LONG" in result["findings"]
        assert result["short_circuited_by"] == "MESSAGE_TOO_LONG"
        assert result["requires_human_approval"] is True
        assert result["action_id"] is None, "nothing to approve, something to read"

    def test_the_deterministic_findings_still_reach_the_reviewer(self, client):
        result = client.post(
            "/api/v1/disputes/investigate", json=dispute(ENGINE_MESSAGE_LIMIT + 1)
        ).json()

        assert "DUPLICATE_INVOICE" in result["findings"]

    def test_a_message_at_the_limit_still_goes_to_the_engine(self, client):
        result = client.post(
            "/api/v1/disputes/investigate", json=dispute(ENGINE_MESSAGE_LIMIT)
        ).json()

        assert result["decision_engine"] == "mock"
        assert "MESSAGE_TOO_LONG" not in result["findings"]


class TestBeyondWhatTheServiceAccepts:
    def test_a_message_over_the_hard_limit_is_refused(self, client):
        response = client.post("/api/v1/disputes/investigate", json=dispute(MESSAGE_HARD_LIMIT + 1))

        assert response.status_code == 422

    def test_the_workflow_alerts_a_person_when_the_api_refuses(self):
        """The refusal is the orchestrator's to report: the investigate node
        has an error branch, and it ends in a mail, not in silence."""
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        nodes = {node["name"]: node for node in workflow["nodes"]}
        investigate = nodes["POST /disputes/investigate"]

        assert investigate.get("onError") == "continueErrorOutput"
        outputs = workflow["connections"]["POST /disputes/investigate"]["main"]
        assert len(outputs) == 2, "a success output and an error output"
        (target,) = outputs[1]
        assert nodes[target["node"]]["type"].endswith("emailSend")
