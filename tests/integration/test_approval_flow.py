"""The human-in-the-loop round trip, and what protects it.

This is the sequence the Loom shows: a duplicate is found, nothing happens, a
person approves, the credit note is issued, the audit says who and when.
"""

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_actions, get_provider
from app.main import app
from tests.integration._auth import APPROVER, SERVICE
from tests.integration._proposal import shown

CASE_A = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers=SERVICE)


@pytest.fixture
def action_id(client) -> str:
    body = client.post("/api/v1/disputes/investigate", json=CASE_A).json()
    assert body["action_id"], "a financial action should be queued for approval"
    return body["action_id"]


def approve(client, action_id):
    return client.post(
        f"/api/v1/actions/{action_id}/approve",
        json={"proposal": shown(client, action_id)},
        auth=APPROVER,
    )


class TestQueueing:
    def test_a_credit_note_is_queued_pending_approval(self, client, action_id):
        action = client.get(f"/api/v1/actions/{action_id}").json()

        assert action["state"] == "pending_approval"
        assert action["action_type"] == "credit_note"
        assert action["amount_minor"] == 9900
        assert action["currency"] == "EUR"

    def test_nothing_is_queued_when_no_money_moves(self, client):
        """Case B. No approval to chase for an invoice that is correct."""
        body = client.post(
            "/api/v1/disputes/investigate",
            json={
                "message": "Pourquoi ai-je deux prélèvements ce mois-ci ?",
                "customer_email": "bruno@transports-nord.fr",
                "invoice_id": "inv_011",
            },
        ).json()

        assert body["action_id"] is None

    def test_investigating_twice_does_not_queue_two_approvals(self, client, action_id):
        """The key is derived from the action, so a replay finds the original."""
        again = client.post("/api/v1/disputes/investigate", json=CASE_A).json()

        assert again["action_id"] == action_id


class TestApproveThenExecute:
    def test_approval_records_who_without_executing(self, client, action_id):
        """Two steps on purpose: 'who authorised this' is a different question
        from 'did it go through'."""
        approved = client.post(
            f"/api/v1/actions/{action_id}/approve",
            json={"proposal": shown(client, action_id)},
            auth=APPROVER,
        ).json()

        assert approved["state"] == "approved"
        assert approved["approved_by"] == "m.dupont@acme.fr"
        assert approved["provider_action_id"] is None

    def test_execution_issues_the_credit_note(self, client, action_id):
        approve(client, action_id)

        executed = client.post(f"/api/v1/actions/{action_id}/execute").json()

        assert executed["state"] == "executed"
        assert executed["provider_action_id"] is not None

    def test_the_provider_really_was_called_once(self, client, action_id):
        before = len(get_provider().executed_actions)

        approve(client, action_id)
        client.post(f"/api/v1/actions/{action_id}/execute")

        assert len(get_provider().executed_actions) == before + 1


class TestTheGuards:
    def test_executing_without_approval_is_refused(self, client, action_id):
        """The whole point of ADR-004, in one request."""
        response = client.post(f"/api/v1/actions/{action_id}/execute")

        assert response.status_code == 409
        assert "pending_approval" in response.text
        assert get_actions().get(action_id).state.value == "pending_approval"

    def test_a_rejected_action_can_never_be_executed(self, client, action_id):
        client.post(f"/api/v1/actions/{action_id}/reject", auth=APPROVER)

        response = client.post(f"/api/v1/actions/{action_id}/execute")

        assert response.status_code == 409
        assert get_actions().get(action_id).state.value == "rejected"

    def test_a_rejected_action_cannot_be_approved_afterwards(self, client, action_id):
        client.post(f"/api/v1/actions/{action_id}/reject", auth=APPROVER)

        response = client.post(
            f"/api/v1/actions/{action_id}/approve",
            json={"proposal": "anything"},
            auth=APPROVER,
        )

        assert response.status_code == 409

    def test_executing_twice_does_not_issue_two_credit_notes(self, client, action_id):
        """A retry that lost the first response must not cost the company
        twice. Same idempotency key, same provider action."""
        approve(client, action_id)
        first = client.post(f"/api/v1/actions/{action_id}/execute").json()

        before = len(get_provider().executed_actions)
        second = client.post(f"/api/v1/actions/{action_id}/execute").json()

        assert second["provider_action_id"] == first["provider_action_id"]
        assert len(get_provider().executed_actions) == before

    def test_an_unknown_action_is_a_404(self, client):
        assert client.post("/api/v1/actions/act_nope/execute").status_code == 404

    def test_an_approval_without_an_approver_is_refused(self, client, action_id):
        """The orchestrator's key does not approve: only a person does."""
        assert client.post(f"/api/v1/actions/{action_id}/approve").status_code == 403

    def test_a_name_in_the_body_is_not_the_identity(self, client, action_id):
        approved = client.post(
            f"/api/v1/actions/{action_id}/approve",
            json={"by": "someone.else@acme.fr", "proposal": shown(client, action_id)},
            auth=APPROVER,
        ).json()

        assert approved["approved_by"] == APPROVER[0]


class TestResumability:
    def test_a_run_can_be_read_back_in_full(self, client, action_id):
        """What makes the flow resumable without n8n holding any state."""
        run_id = get_actions().get(action_id).run_id

        body = client.get(f"/api/v1/runs/{run_id}").json()

        assert body["run"]["classification"] == "duplicate_billing"
        assert body["run"]["verdict"] == "requires_approval"
        assert [e["event"] for e in body["events"]][0] == "run_started"
        assert body["actions"][0]["action_id"] == action_id

    def test_the_trace_records_the_queued_action(self, client, action_id):
        run_id = get_actions().get(action_id).run_id

        events = [e["event"] for e in client.get(f"/api/v1/runs/{run_id}").json()["events"]]

        assert "action_queued" in events
        assert "approval_requested" in events

    def test_an_unknown_run_is_a_404(self, client):
        assert client.get("/api/v1/runs/run_nope").status_code == 404
