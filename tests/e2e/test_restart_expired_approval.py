"""A2 -- an approval expires across a real restart
(specs/006-restart-failures).

A credit note waits for a person; the service stops; its deadline passes
while it is down; a new process starts on the same database. The action is
expired, and nobody can approve or execute it any more -- not through the
API, not through the review page's form.

What this proves, and what it does not: the deadline survives the restart and
is applied on access. Investigating the same dispute again does not reopen an
expired correction (the queue finds it by its economic key whatever its
state): a person reassesses it -- a known limitation, not tested here.
"""

import time
from datetime import UTC, datetime, timedelta

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

#: Long enough that the first assertions cannot race it on a loaded machine,
#: short enough for CI.
DEADLINE = {"RESOLVE_APPROVAL_TTL_HOURS": str(20 / 3600)}
#: The longest the test waits for that deadline to pass.
WAIT_BOUND = 60.0


def _wait_past(instant: datetime) -> None:
    """Until the wall clock is a second past `instant`, under a bound."""
    give_up = time.monotonic() + WAIT_BOUND
    while datetime.now(UTC) <= instant + timedelta(seconds=1):
        assert time.monotonic() < give_up, f"{instant} did not pass within {WAIT_BOUND} s"
        time.sleep(0.2)


def test_an_approval_expires_across_a_restart(tmp_path):
    database = tmp_path / "audit.db"

    with ServiceProcess(database, tmp_path / "first.log", extra_env=DEADLINE) as first:
        with client(first.base_url) as http:
            queued = http.post("/api/v1/disputes/investigate", json=CASE, headers=SERVICE)
            assert queued.status_code == 200, queued.text
            action_id = queued.json()["action_id"]
            before = http.get(f"/api/v1/actions/{action_id}", headers=SERVICE).json()
        assert before["state"] == "pending_approval"
        assert before["expires_at"] and before["proposal"]
        first.stop_gracefully()

    _wait_past(datetime.fromisoformat(before["expires_at"]))

    with (
        ServiceProcess(database, tmp_path / "second.log", extra_env=DEADLINE) as second,
        client(second.base_url) as http,
    ):
        url = f"/api/v1/actions/{action_id}"
        after = http.get(url, headers=SERVICE).json()
        assert after["state"] == "expired"
        assert after["expires_at"] == before["expires_at"]
        assert after["provider_action_id"] is None

        approved = http.post(f"{url}/approve", json={"proposal": before["proposal"]}, auth=APPROVER)
        assert approved.status_code == 409, approved.text

        reviewed = http.post(
            f"{url}/review",
            data={"decision": "approve", "proposal": before["proposal"]},
            auth=APPROVER,
        )
        assert reviewed.status_code == 409, reviewed.text

        executed = http.post(f"{url}/execute", headers=SERVICE)
        assert executed.status_code == 409, executed.text

        final = http.get(url, headers=SERVICE).json()
        assert final["state"] == "expired"
        assert final["provider_action_id"] is None
