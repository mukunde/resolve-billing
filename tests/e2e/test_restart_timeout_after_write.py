"""A4 -- a timeout after a write, then a real restart
(specs/006-restart-failures).

The billing platform writes the credit note, but its answer never reaches the
service: the service records an unknown outcome. The service is killed and a
new process starts, on the same database and the same platform. The next
execution asks the platform what it holds before writing anything, finds the
write, and records it -- never a second one.

The platform here is a test double that outlives the service
(`tests/e2e/_persistent_fake.py`, served through `tests/e2e/_launcher.py`);
its ledger, read directly, is the independent count of writes.

What this proves, and what it does not: recovery of a lost answer across a
restart, on the fake, at the service boundary. Not a crash mid-flight that
leaves the action `executing` (reconciled only after five minutes), not the
orchestrator's resumption, not Stripe.
"""

import pytest

from tests.integration._auth import APPROVER, SERVICE

pytest.importorskip("uvicorn", reason="the `api` extra is not installed")

from tests.e2e._persistent_fake import (  # noqa: E402
    TIMEOUT_AFTER_WRITE_ONCE,
    PersistentFake,
    read_calls,
)
from tests.e2e._service import ServiceProcess, client  # noqa: E402

pytestmark = pytest.mark.e2e

CASE = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}


def test_a_timeout_after_a_write_is_reconciled_after_a_restart(tmp_path):
    database, ledger = tmp_path / "audit.db", tmp_path / "platform.json"
    journal = tmp_path / "platform-calls.txt"
    platform = {
        "E2E_FAKE_LEDGER": str(ledger),
        "E2E_FAULT": TIMEOUT_AFTER_WRITE_ONCE,
        "E2E_FAKE_CALLS": str(journal),
    }

    def service(log: str) -> ServiceProcess:
        return ServiceProcess(
            database, tmp_path / log, launcher="tests.e2e._launcher", extra_env=platform
        )

    # The write lands; its answer is lost.
    with service("first.log") as first:
        with client(first.base_url) as http:
            action_id = http.post(
                "/api/v1/disputes/investigate", json=CASE, headers=SERVICE
            ).json()["action_id"]
            url = f"/api/v1/actions/{action_id}"
            proposal = http.get(url, headers=SERVICE).json()["proposal"]
            approved = http.post(f"{url}/approve", json={"proposal": proposal}, auth=APPROVER)
            assert approved.status_code == 200, approved.text

            lost = http.post(f"{url}/execute", headers=SERVICE)
            assert lost.status_code == 504, lost.text
            unknown = http.get(url, headers=SERVICE).json()
            assert unknown["state"] == "unknown_outcome"
            assert unknown["provider_action_id"] is None

        (written,) = PersistentFake(ledger).writes()
        assert written.action_id == action_id
        first.kill()
        assert first.exited()
        first_pid = first.pid

    # A new process, the same database and platform; the fault already spent.
    with service("second.log") as second, client(second.base_url) as http:
        assert second.pid != first_pid
        url = f"/api/v1/actions/{action_id}"

        reconciled = http.post(f"{url}/execute", headers=SERVICE)
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["state"] == "executed"
        assert reconciled.json()["provider_action_id"] == written.provider_action_id
        assert "reconciled" in reconciled.json()["resolution"]

        again = http.post(f"{url}/execute", headers=SERVICE)
        assert again.status_code == 200, again.text
        assert again.json()["provider_action_id"] == written.provider_action_id
        second_pid = second.pid

    # Recovered by asking the platform, not by sending the write again: the
    # new process looked the write up and made no write call at all.
    calls = read_calls(journal)
    first_calls = [call for pid, call, _ in calls if pid == first_pid]
    second_calls = [call for pid, call, _ in calls if pid == second_pid]
    assert first_calls.count("create_credit_note") == 1
    assert "find_action" in second_calls
    assert not any(call.startswith("create_") for call in second_calls), second_calls

    # The platform's own count: still the one write.
    assert PersistentFake(ledger).writes() == [written]
