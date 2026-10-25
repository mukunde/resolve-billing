"""The launcher serves the service on the persistent fake
(specs/006-restart-failures, T021): a credit note executed through it lands
in the platform's own ledger, and the service stops as uvicorn does."""

import pytest

from tests.integration._auth import APPROVER, SERVICE

pytest.importorskip("uvicorn", reason="the `api` extra is not installed")

from tests.e2e._persistent_fake import PersistentFake  # noqa: E402
from tests.e2e._service import ServiceProcess, client  # noqa: E402

pytestmark = pytest.mark.e2e

CASE = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}


def test_the_launched_service_writes_on_the_persistent_fake(tmp_path):
    ledger = tmp_path / "platform.json"
    with ServiceProcess(
        tmp_path / "audit.db",
        tmp_path / "service.log",
        launcher="tests.e2e._launcher",
        extra_env={"E2E_FAKE_LEDGER": str(ledger)},
    ) as service:
        with client(service.base_url) as http:
            action_id = http.post(
                "/api/v1/disputes/investigate", json=CASE, headers=SERVICE
            ).json()["action_id"]
            url = f"/api/v1/actions/{action_id}"
            proposal = http.get(url, headers=SERVICE).json()["proposal"]
            assert (
                http.post(f"{url}/approve", json={"proposal": proposal}, auth=APPROVER).status_code
                == 200
            )
            executed = http.post(f"{url}/execute", headers=SERVICE)
            assert executed.status_code == 200, executed.text
        service.stop_gracefully()

    (write,) = PersistentFake(ledger).writes()
    assert write.provider_action_id == executed.json()["provider_action_id"]
    assert write.action_id == action_id
