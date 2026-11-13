"""The API and the review page for spec 015's execution core: a type the
platform does not write, a person's resolution of a partial write, and the
customer the investigation passes to the store
(specs/015-pennylane-credit-notes, FR-001, FR-009, FR-009a)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.application.execute_action import ApprovalService
from app.domain.enums import ActionState
from app.domain.money import Money
from app.infrastructure.actions import NeedsAPerson
from app.main import app
from app.providers.base import PartialWrite
from app.providers.fake.adapter import FakeBillingProvider
from tests.integration._auth import APPROVER, SECOND_APPROVER, SERVICE, basic

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
CASE_A = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}


class Clock:
    def __init__(self) -> None:
        self.at = T0

    def __call__(self) -> datetime:
        return self.at


class CreditNotesOnly(FakeBillingProvider):
    writes = frozenset({"credit_note"})


class LeftHalfway(FakeBillingProvider):
    """Makes a draft, then stops: the write is left half-way."""

    idempotency_window = None

    def __init__(self) -> None:
        super().__init__()
        self.sends = 0

    async def create_credit_note(self, **kwargs):
        self.sends += 1
        await kwargs["before_request"]()
        raise PartialWrite("doc_7", "linking")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


def serve(provider, clock=None) -> ApprovalService:
    service = ApprovalService(provider=provider, actions=deps.get_actions(), now=clock)
    app.dependency_overrides[deps.get_approvals] = lambda: service
    return service


def queued(action_type="credit_note", invoice_id="inv_002", amount=9900):
    return deps.get_actions().create(
        run_id="run_test",
        action_type=action_type,
        invoice_id=invoice_id,
        amount=Money(amount_minor=amount, currency="EUR"),
        reason="duplicate invoice",
        provider="fake",
        customer_id="cus_acme",
    )


async def left_halfway(clock) -> tuple[ApprovalService, str, LeftHalfway]:
    provider = LeftHalfway()
    service = serve(provider, clock)
    action = queued()
    deps.get_actions().transition(action.action_id, ActionState.APPROVED, approved_by="a")
    with pytest.raises(NeedsAPerson):
        await service.execute(action.action_id)
    assert deps.get_actions().get(action.action_id).attention == "partial_write"
    return service, action.action_id, provider


class TestATypeNotWritten:
    def test_the_api_says_a_person_carries_it_out(self, client):
        serve(CreditNotesOnly())
        refund = queued(action_type="refund")
        credit = queued(invoice_id="inv_001")

        said = client.get(f"/api/v1/actions/{refund.action_id}", headers=SERVICE).json()
        silent = client.get(f"/api/v1/actions/{credit.action_id}", headers=SERVICE).json()

        assert "outside ResolveBilling" in said["notice"]
        assert said["proposal"], "approving it still records the decision"
        assert "notice" not in silent

    def test_the_review_page_says_so_and_keeps_the_approve_button(self, client):
        serve(CreditNotesOnly())
        refund = queued(action_type="refund")

        page = client.get(
            f"/api/v1/actions/{refund.action_id}/review", headers=basic(APPROVER)
        ).text

        assert "hors de ResolveBilling" in page
        assert 'value="approve"' in page

    def test_executing_it_answers_409_and_keeps_its_state(self, client):
        serve(CreditNotesOnly())
        refund = queued(action_type="refund")
        deps.get_actions().transition(refund.action_id, ActionState.APPROVED, approved_by="a")

        response = client.post(f"/api/v1/actions/{refund.action_id}/execute", headers=SERVICE)

        assert response.status_code == 409
        assert "outside ResolveBilling" in response.json()["detail"]
        assert deps.get_actions().get(refund.action_id).state is ActionState.APPROVED


class TestTheInvestigationPassesTheCustomer:
    def test_a_queued_action_carries_the_verified_customer(self, client):
        body = client.post("/api/v1/disputes/investigate", json=CASE_A, headers=SERVICE).json()

        assert deps.get_actions().get(body["action_id"]).customer_id == "cus_acme"


class TestTheResolutionRoute:
    async def test_it_takes_an_authenticated_approver_by_post_only(self, client, clock):
        _, action_id, _ = await left_halfway(clock)
        path = f"/api/v1/actions/{action_id}/resolution"

        assert client.post(path, json={"note": "x"}).status_code == 401
        assert client.post(path, json={"note": "x"}, headers=SERVICE).status_code == 403
        assert client.get(path, headers=basic(APPROVER)).status_code == 405

    async def test_it_is_refused_before_a_late_enough_status_read(self, client, clock):
        _, action_id, _ = await left_halfway(clock)
        clock.at = T0 + timedelta(minutes=9)
        client.post(f"/api/v1/actions/{action_id}/status", headers=basic(APPROVER))

        response = client.post(
            f"/api/v1/actions/{action_id}/resolution",
            json={"note": "deleted the draft"},
            headers=basic(APPROVER),
        )

        assert response.status_code == 409
        assert "ten minutes" in response.json()["detail"]

    async def test_it_records_the_authenticated_identity_once(self, client, clock):
        service, action_id, provider = await left_halfway(clock)
        before = client.get(f"/api/v1/actions/{action_id}", headers=SERVICE).json()
        assert "in flight" in before["warning"]
        assert before["partial_document"] == {"document_id": "doc_7", "stage": "linking"}
        clock.at = T0 + timedelta(minutes=11)
        read = client.post(f"/api/v1/actions/{action_id}/status", headers=basic(APPROVER))
        assert read.json()["status_read_found"] == "nothing under this idempotency key"

        recorded = client.post(
            f"/api/v1/actions/{action_id}/resolution",
            json={"note": "deleted the draft", "resolved_by": "someone else"},
            headers=basic(APPROVER),
        )
        again = client.post(
            f"/api/v1/actions/{action_id}/resolution",
            json={"note": "another account"},
            headers=basic(SECOND_APPROVER),
        )

        assert recorded.status_code == 200
        body = recorded.json()
        assert body["resolved_by"] == APPROVER[0]
        assert body["resolution_note"] == "deleted the draft"
        assert body["attention"] == "partial_write"
        assert "warning" not in body
        assert again.status_code == 409
        executed = client.post(f"/api/v1/actions/{action_id}/execute", headers=SERVICE)
        assert executed.status_code == 409
        assert provider.sends == 1

    async def test_an_empty_or_missing_account_is_refused(self, client, clock):
        _, action_id, _ = await left_halfway(clock)
        path = f"/api/v1/actions/{action_id}/resolution"

        assert client.post(path, headers=basic(APPROVER)).status_code == 422
        assert client.post(path, json={"note": ""}, headers=basic(APPROVER)).status_code == 422
        assert client.post(path, json={"note": "  "}, headers=basic(APPROVER)).status_code == 422


class TestTheResolutionPage:
    async def test_it_shows_the_warning_and_the_last_status_read_first(self, client, clock):
        _, action_id, _ = await left_halfway(clock)

        response = client.get(f"/api/v1/actions/{action_id}/review", headers=basic(APPROVER))

        assert response.status_code == 200
        page = response.text
        assert "en vol" in page
        assert "doc_7" in page and "linking" in page
        assert f"/api/v1/actions/{action_id}/review/resolution" in page
        assert page.index("en vol") < page.index('name="note"')

    async def test_its_form_records_the_resolution(self, client, clock):
        _, action_id, _ = await left_halfway(clock)
        path = f"/api/v1/actions/{action_id}/review/resolution"
        early = client.post(path, data={"note": "deleted"}, headers=basic(APPROVER))
        assert early.status_code == 409
        clock.at = T0 + timedelta(minutes=11)
        client.post(f"/api/v1/actions/{action_id}/status", headers=basic(APPROVER))

        done = client.post(path, data={"note": "deleted the draft"}, headers=basic(APPROVER))

        assert done.status_code == 200
        assert "Résolution enregistrée" in done.text
        assert deps.get_actions().get(action_id).resolved_by == APPROVER[0]
        reopened = client.get(f"/api/v1/actions/{action_id}/review", headers=basic(APPROVER))
        assert reopened.status_code == 409

    async def test_the_form_takes_an_authenticated_approver(self, client, clock):
        _, action_id, _ = await left_halfway(clock)
        path = f"/api/v1/actions/{action_id}/review/resolution"

        assert client.post(path, data={"note": "x"}).status_code == 401
        assert client.post(path, data={"note": "x"}, headers=SERVICE).status_code == 403
