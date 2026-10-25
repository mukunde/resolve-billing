"""A3 -- the same correction delivered again after a real restart
(specs/006-restart-failures).

The same dispute arrives once, the service restarts, and it arrives again.
Each investigation is a run of its own; the correction is one action, found
again by its economic key -- one approval, one write.

What this proves, and what it does not: deduplication of the correction (its
idempotency key: platform, invoice, type, amount, currency), not of every
delivery -- the second investigation is still a run, with its own trace.
"""

import sqlite3

import pytest

from tests.integration._auth import APPROVER, SERVICE

pytest.importorskip("uvicorn", reason="the `api` extra is not installed")

from tests.e2e._service import ServiceProcess, client  # noqa: E402

pytestmark = pytest.mark.e2e

CASE = {
    "message": "Je crois que j'ai été facturé deux fois pour le mois de septembre.",
    "customer_email": "alice@acme.fr",
    "invoice_id": "inv_002",
}


def _stored_actions(database, key: str) -> int:
    """How many actions the database holds under `key`, read-only."""
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        return connection.execute(
            "SELECT count(*) FROM pending_actions WHERE idempotency_key = ?", (key,)
        ).fetchone()[0]
    finally:
        connection.close()


def test_the_same_correction_delivered_again_after_a_restart_is_one_action(tmp_path):
    database = tmp_path / "audit.db"

    with ServiceProcess(database, tmp_path / "first.log") as first:
        with client(first.base_url) as http:
            one = http.post("/api/v1/disputes/investigate", json=CASE, headers=SERVICE)
            assert one.status_code == 200, one.text
            first_run, action_id = one.json()["run_id"], one.json()["action_id"]
            before = http.get(f"/api/v1/actions/{action_id}", headers=SERVICE).json()
        first.stop_gracefully()

    with (
        ServiceProcess(database, tmp_path / "second.log") as second,
        client(second.base_url) as http,
    ):
        two = http.post("/api/v1/disputes/investigate", json=CASE, headers=SERVICE)
        assert two.status_code == 200, two.text
        assert two.json()["run_id"] != first_run, "each investigation is a run"
        assert two.json()["action_id"] == action_id, "the correction is one action"

        url = f"/api/v1/actions/{action_id}"
        after = http.get(url, headers=SERVICE).json()
        assert after["idempotency_key"] == before["idempotency_key"]
        assert after["expires_at"] == before["expires_at"]
        assert after["run_id"] == first_run
        assert _stored_actions(database, before["idempotency_key"]) == 1

        approved = http.post(f"{url}/approve", json={"proposal": after["proposal"]}, auth=APPROVER)
        assert approved.status_code == 200, approved.text
        executed = http.post(f"{url}/execute", headers=SERVICE)
        again = http.post(f"{url}/execute", headers=SERVICE)
        assert executed.status_code == again.status_code == 200
        reference = executed.json()["provider_action_id"]
        assert reference and again.json()["provider_action_id"] == reference

        status = http.post(f"{url}/status", auth=APPROVER).json()
        assert status["state"] == "executed"
        assert status["provider_action_id"] == reference
        assert status["attention"] is None
        assert status["provider_status"] == "succeeded"
        assert status["provider_status_at"]
