"""Data a platform holds but the domain cannot represent, on every surface.

specs/001-stripe-read-adapter, FR-002 and FR-003: the investigation decides
nothing and records no run; the approval surfaces answer 422 with a category
and change no state; execution on a platform that cannot write is refused
before anything, with 409 -- never the 504 "unknown outcome".
"""

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.application.execute_action import ApprovalService
from app.application.investigate_dispute import DisputeInvestigator
from app.decision.mock import MockDecisionEngine
from app.domain.enums import ActionState
from app.main import app
from app.providers.base import ProviderUnsupported
from app.providers.fake.adapter import FakeBillingProvider
from tests.integration._auth import APPROVER, SERVICE, basic
from tests.integration._proposal import shown

CASE_A = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}
EXPECTED = {"category": "provider_unsupported", "reason": "mixed_item_periods"}
SENTINEL = "SENTINEL-platform-text"


class Unsupported(FakeBillingProvider):
    """Answers, but with data the domain cannot represent."""

    def __init__(self, can_write: bool = True) -> None:
        super().__init__()
        self.can_write = can_write

    async def get_invoice(self, invoice_id):
        raise ProviderUnsupported("mixed_item_periods")

    async def find_invoice(self, invoice_id):
        # The read behind the proposal fingerprint and the execution guard:
        # it happens before any claim, so a failure leaves the state as is.
        raise ProviderUnsupported("mixed_item_periods")

    async def list_subscriptions(self, customer_id):
        raise ProviderUnsupported("mixed_item_periods")


@pytest.fixture
def client():
    yield TestClient(app, headers=basic(APPROVER))
    app.dependency_overrides.clear()


@pytest.fixture
def action_id(client) -> str:
    response = client.post("/api/v1/disputes/investigate", json=CASE_A, headers=SERVICE)
    return response.json()["action_id"]


def on_unsupported(can_write: bool = True) -> None:
    provider = Unsupported(can_write)
    app.dependency_overrides[deps.get_approvals] = lambda: ApprovalService(
        provider=provider, actions=deps.get_actions()
    )


def assert_unsupported(response) -> None:
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": EXPECTED}
    assert SENTINEL not in response.text


class TestTheInvestigation:
    def test_decides_nothing_and_records_no_run(self, client):
        app.dependency_overrides[deps.get_investigator] = lambda: DisputeInvestigator(
            provider=Unsupported(),
            engine=MockDecisionEngine(),
            audit=deps.get_audit(),
            actions=deps.get_actions(),
        )
        runs = deps.get_audit().count()

        response = client.post("/api/v1/disputes/investigate", json=CASE_A, headers=SERVICE)

        assert_unsupported(response)
        assert deps.get_audit().count() == runs


class TestTheApprovalSurfaces:
    def test_reading_the_action(self, client, action_id):
        on_unsupported()
        assert_unsupported(client.get(f"/api/v1/actions/{action_id}"))
        assert deps.get_actions().get(action_id).state is ActionState.PENDING_APPROVAL

    def test_approving(self, client, action_id):
        proposal = shown(client, action_id)
        on_unsupported()
        response = client.post(f"/api/v1/actions/{action_id}/approve", json={"proposal": proposal})
        assert_unsupported(response)
        assert deps.get_actions().get(action_id).state is ActionState.PENDING_APPROVAL

    def test_the_review_page(self, client, action_id):
        on_unsupported()
        assert_unsupported(client.get(f"/api/v1/actions/{action_id}/review"))

    def test_executing_on_a_writable_platform_is_never_an_unknown_outcome(self, client, action_id):
        deps.get_actions().transition(action_id, ActionState.APPROVED, approved_by="approver")
        on_unsupported(can_write=True)
        response = client.post(f"/api/v1/actions/{action_id}/execute", headers=SERVICE)
        assert_unsupported(response)
        assert deps.get_actions().get(action_id).state is ActionState.APPROVED


class TestExecutionOnAReadOnlyPlatform:
    def test_is_refused_with_409_before_anything(self, client, action_id):
        deps.get_actions().transition(action_id, ActionState.APPROVED, approved_by="approver")
        on_unsupported(can_write=False)
        response = client.post(f"/api/v1/actions/{action_id}/execute", headers=SERVICE)
        assert response.status_code == 409, response.text
        assert "cannot write" in response.json()["detail"]
        assert deps.get_actions().get(action_id).state is ActionState.APPROVED


class TestTheValidationAnswerKeepsItsShape:
    def test_a_malformed_request_is_still_the_s11_422(self, client):
        response = client.post("/api/v1/disputes/investigate", json={}, headers=SERVICE)
        assert response.status_code == 422
        assert "category" not in response.text


class TestTheMcpServer:
    async def test_a_read_tool_fails_with_the_category(self, monkeypatch):
        pytest.importorskip("mcp", reason="the MCP server is an optional extra")
        from app.mcp import server as mcp_server

        monkeypatch.setattr(mcp_server, "_provider", lambda: Unsupported())
        try:
            result = await mcp_server.server.call_tool("get_invoice", {"invoice_id": "inv_002"})
        except Exception as error:  # noqa: BLE001 -- whichever way the SDK reports it
            text = str(error)
        else:
            text = str(result)
        assert "mixed_item_periods" in text
        assert SENTINEL not in text
