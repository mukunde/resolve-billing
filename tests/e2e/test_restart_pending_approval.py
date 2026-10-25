"""A pending approval survives a real service restart
(specs/005-restart-pending-approval).

The service runs as its own process. A credit note is queued and waits for a
person; the process is stopped -- cleanly, or killed -- and a new one starts
on the same database. The person approves, the orchestrator executes, twice,
and the credit note is written once. Everything goes through HTTP: what the
test sees of the billing platform is what the service reads of it.

What this proves, and what it does not: the local service boundary. Not the
milestone, not exactly-once on a real platform, not recovery from a crash
inside a database transaction, not the orchestrator's resumption.
"""

import pytest

from tests.integration._auth import APPROVER, SERVICE

pytest.importorskip("uvicorn", reason="the `api` extra is not installed")

from tests.e2e._service import ServiceProcess, client  # noqa: E402

pytestmark = pytest.mark.e2e

#: The synthetic dispute the integration tests use: a duplicate charge the
#: mock engine turns into a credit note of EUR 99 awaiting approval.
CASE = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}


@pytest.mark.parametrize("stop", ["graceful", "kill"])
def test_a_pending_approval_survives_a_restart(tmp_path, stop):
    database = tmp_path / "audit.db"

    # The first process: the credit note is queued and waits for a person.
    with ServiceProcess(database, tmp_path / "first.log") as first:
        with client(first.base_url) as http:
            queued = http.post("/api/v1/disputes/investigate", json=CASE, headers=SERVICE)
            assert queued.status_code == 200, queued.text
            action_id = queued.json()["action_id"]
            run_id = queued.json()["run_id"]
            assert action_id, "a credit note should wait for approval"
            before = http.get(f"/api/v1/actions/{action_id}", headers=SERVICE).json()
        assert before["state"] == "pending_approval"
        assert before["action_type"] == "credit_note"
        assert (before["amount_minor"], before["currency"]) == (9900, "EUR")
        assert before["provider_action_id"] is None
        assert before["proposal"]
        assert before["expires_at"], "a request for approval has a deadline"
        assert before["run_id"] == run_id

        if stop == "graceful":
            first.stop_gracefully()
        else:
            first.kill()
        assert first.exited(), "the first process must be gone before the second starts"
        first_pid = first.pid

    # The replacement: same database, same configuration.
    with ServiceProcess(database, tmp_path / "second.log") as second:
        assert second.pid != first_pid
        with client(second.base_url) as http:
            url = f"/api/v1/actions/{action_id}"
            after = http.get(url, headers=SERVICE).json()
            assert after["state"] == "pending_approval"
            assert after["action_id"] == action_id
            assert after["proposal"] == before["proposal"]
            assert after["provider_action_id"] is None
            # The deadline is the one set at queueing, not reset by the restart.
            assert after["expires_at"] == before["expires_at"]

            # The run is still there, its action with it.
            run = http.get(f"/api/v1/runs/{run_id}", headers=SERVICE)
            assert run.status_code == 200, run.text
            assert [a["action_id"] for a in run.json()["actions"]] == [action_id]

            # Nothing moves before a person decides.
            premature = http.post(f"{url}/execute", headers=SERVICE)
            assert premature.status_code == 409, premature.text
            assert http.get(url, headers=SERVICE).json()["state"] == "pending_approval"

            anonymous = http.post(f"{url}/approve", json={"proposal": before["proposal"]})
            assert anonymous.status_code == 401, anonymous.text
            assert http.get(url, headers=SERVICE).json()["state"] == "pending_approval"

            approved = http.post(
                f"{url}/approve", json={"proposal": before["proposal"]}, auth=APPROVER
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["state"] == "approved"
            assert approved.json()["approved_by"] == APPROVER[0]

            executed = http.post(f"{url}/execute", headers=SERVICE)
            again = http.post(f"{url}/execute", headers=SERVICE)
            assert executed.status_code == again.status_code == 200, (executed.text, again.text)
            reference = executed.json()["provider_action_id"]
            assert executed.json()["state"] == again.json()["state"] == "executed"
            assert reference and again.json()["provider_action_id"] == reference

            # One matching write under the action's key, as the platform answers
            # the service -- not a count of every write on the platform.
            status = http.post(f"{url}/status", auth=APPROVER)
            assert status.status_code == 200, status.text
            observed = status.json()
            assert observed["state"] == "executed"
            assert observed["provider_action_id"] == reference
            assert observed["attention"] is None
            assert observed["provider_status"] in {"issued", "succeeded"}
            assert observed["provider_status_at"]
