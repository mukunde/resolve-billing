"""D8. The server calls back only the orchestrator, at an address it builds.

Before: the review page accepted a `resume` URL from the browser, stored it,
and the server called it once a person had decided -- whatever host it
named. A link that reached a browser could make this service issue a request
to an internal address of an attacker's choosing. ADR-009.
"""

import pytest
from fastapi.testclient import TestClient

from app.api import review as review_module
from app.api.deps import get_actions
from app.main import app
from tests.integration._auth import APPROVER, SERVICE, basic
from tests.integration._proposal import shown

CASE_A = {
    "message": "Je crois que j'ai ete facture deux fois en septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}
GOOD = "http://orchestrator.test/webhook-waiting/25?signature=5168279ccd"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers=basic(APPROVER))


@pytest.fixture
def action_id(client) -> str:
    return client.post(
        "/api/v1/disputes/investigate", json=CASE_A, headers=SERVICE
    ).json()["action_id"]


@pytest.fixture
def calls(monkeypatch):
    """Every URL the server tries to open, instead of opening it."""
    opened: list[str] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(url, timeout=None):
        opened.append(url)
        return Response()

    monkeypatch.setattr(review_module.urllib.request, "urlopen", fake_urlopen)
    return opened


def visit_then_approve(client, action_id, resume):
    client.get(f"/api/v1/actions/{action_id}/review", params={"resume": resume})
    return client.post(
        f"/api/v1/actions/{action_id}/review",
        data={"decision": "approve", "proposal": shown(client, action_id)},
    )


class TestOnlyTheOrchestratorIsCalledBack:
    def test_the_orchestrator_s_own_callback_is_kept_and_called(self, client, action_id, calls):
        visit_then_approve(client, action_id, GOOD)

        assert get_actions().get(action_id).resume_url == GOOD
        assert len(calls) == 1 and calls[0].startswith(GOOD)

    @pytest.mark.parametrize(
        "resume",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://internal.service/webhook-waiting/25?signature=ab",
            "http://orchestrator.test.evil.test/webhook-waiting/25?signature=ab",
            "http://orchestrator.test@evil.test/webhook-waiting/25?signature=ab",
            "http://orchestrator.test:8080/webhook-waiting/25?signature=ab",
            "https://orchestrator.test/webhook-waiting/25?signature=ab",
            "http://orchestrator.test/webhook/anything",
            "http://orchestrator.test/webhook-waiting/25/../../admin",
            "http://orchestrator.test/webhook-waiting/25?signature=ab&next=http://evil.test",
            "file:///etc/passwd",
        ],
    )
    def test_anything_else_is_neither_stored_nor_called(self, client, action_id, calls, resume):
        response = visit_then_approve(client, action_id, resume)

        assert response.status_code == 200, "the decision itself still counts"
        assert get_actions().get(action_id).state.value == "approved"
        assert get_actions().get(action_id).resume_url is None
        assert calls == []

    def test_with_no_orchestrator_configured_nothing_is_called(
        self, client, action_id, calls, monkeypatch
    ):
        monkeypatch.delenv("RESOLVE_ORCHESTRATOR_URL")

        response = visit_then_approve(client, action_id, GOOD)

        assert response.status_code == 200
        assert get_actions().get(action_id).resume_url is None
        assert calls == []
