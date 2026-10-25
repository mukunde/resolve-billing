"""D7. Every route that acts requires an authenticated caller of the right kind.

Before: no route asked who was calling. Anyone who could reach the API and
knew an action id could approve it, then execute it, and the approver's
identity was whatever name the caller typed. ADR-009.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.integration._auth import APPROVER, SERVICE, basic

CASE_A = {
    "message": "Je crois que j'ai ete facture deux fois en septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}

ACTING_ROUTES = [
    ("post", "/api/v1/disputes/investigate"),
    ("post", "/api/v1/actions/act_any/approve"),
    ("post", "/api/v1/actions/act_any/reject"),
    ("post", "/api/v1/actions/act_any/execute"),
    ("get", "/api/v1/actions/act_any"),
    ("get", "/api/v1/actions/act_any/review"),
    ("post", "/api/v1/actions/act_any/review"),
    ("get", "/api/v1/runs/run_any"),
    ("get", "/api/v1/runs/run_any/view"),
]


@pytest.fixture
def anonymous() -> TestClient:
    return TestClient(app)


@pytest.fixture
def action_id() -> str:
    body = TestClient(app, headers=SERVICE).post(
        "/api/v1/disputes/investigate", json=CASE_A
    ).json()
    return body["action_id"]


class TestNobodyActsAnonymously:
    @pytest.mark.parametrize(("method", "path"), ACTING_ROUTES)
    def test_every_acting_route_refuses_an_anonymous_caller(self, anonymous, method, path):
        assert getattr(anonymous, method)(path).status_code == 401

    @pytest.mark.parametrize(("method", "path"), ACTING_ROUTES)
    def test_a_wrong_key_is_no_key(self, method, path):
        client = TestClient(app, headers={"Authorization": "Bearer not-the-key"})
        assert getattr(client, method)(path).status_code == 401

    def test_an_unknown_approver_is_refused(self, action_id):
        client = TestClient(app, headers=basic(("intruder@acme.fr", "guess")))
        assert client.post(f"/api/v1/actions/{action_id}/approve").status_code == 401

    def test_a_known_approver_with_the_wrong_secret_is_refused(self, action_id):
        client = TestClient(app, headers=basic((APPROVER[0], "wrong")))
        assert client.post(f"/api/v1/actions/{action_id}/approve").status_code == 401

    def test_health_stays_open(self, anonymous):
        assert anonymous.get("/health").status_code == 200


class TestEachCallerDoesOnlyItsPart:
    def test_the_orchestrator_cannot_approve(self, action_id):
        client = TestClient(app, headers=SERVICE)
        assert client.post(f"/api/v1/actions/{action_id}/approve").status_code == 403

    def test_a_person_cannot_execute(self, action_id):
        client = TestClient(app, headers=basic(APPROVER))
        client.post(f"/api/v1/actions/{action_id}/approve")

        assert client.post(f"/api/v1/actions/{action_id}/execute").status_code == 403

    def test_a_person_cannot_start_an_investigation(self):
        client = TestClient(app, headers=basic(APPROVER))
        assert client.post("/api/v1/disputes/investigate", json=CASE_A).status_code == 403


class TestClosedByDefault:
    def test_with_no_configuration_nothing_acting_is_open(self, monkeypatch, anonymous):
        monkeypatch.delenv("RESOLVE_SERVICE_KEY")
        monkeypatch.delenv("RESOLVE_APPROVERS")

        assert anonymous.post("/api/v1/disputes/investigate", json=CASE_A).status_code == 503
        assert anonymous.post("/api/v1/actions/act_any/approve").status_code == 503
