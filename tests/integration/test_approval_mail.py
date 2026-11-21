"""The mails n8n sends carry no model prose (ADR-009, 2026-11-21).

The engine's explanation is text a customer can steer. A mail client renders
it beyond our control -- a link written in plain text is clickable, a line
that reads like a finding looks like one -- so no mail carries it. The
review page shows it, escaped and labelled an opinion, not evidence.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.integration._auth import APPROVER, SERVICE, basic

WORKFLOW = Path(__file__).resolve().parents[2] / "n8n" / "billing-dispute-workflow.json"


def mails() -> list[dict]:
    nodes = json.loads(WORKFLOW.read_text(encoding="utf-8"))["nodes"]
    return [n for n in nodes if n["type"] == "n8n-nodes-base.emailSend"]


REVIEW_LINK = (
    "{{ $('Configuration').first().json.public_api }}/api/v1/actions/"
    "{{ $('POST /disputes/investigate').item.json.action_id }}"
    "/review?resume={{ encodeURIComponent($execution.resumeUrl) }}"
)


@pytest.mark.parametrize("mail", mails(), ids=lambda n: n["name"])
def test_no_mail_carries_the_explanation(mail):
    parameters = json.dumps(mail["parameters"], ensure_ascii=False)
    assert "explanation" not in parameters
    # Nor the whole answer at once, where the explanation would ride along.
    for whole in (".json }}", ".json) }}", "JSON.stringify", "Object.values"):
        assert whole not in parameters


def test_the_approval_mail_still_links_to_the_review_page():
    mail = next(n for n in mails() if n["name"] == "Mail au valideur")
    for part in ("text", "html"):
        assert REVIEW_LINK in mail["parameters"][part]


class TestTheReviewPage:
    def test_it_labels_the_explanation_an_opinion_not_evidence(self):
        client = TestClient(app, headers=basic(APPROVER))
        body = client.post(
            "/api/v1/disputes/investigate",
            headers=SERVICE,
            json={
                "message": "Je crois que j'ai été facturé deux fois pour septembre.",
                "customer_email": "alice@acme.fr",
                "invoice_id": "inv_002",
            },
        ).json()

        page = client.get(f"/api/v1/actions/{body['action_id']}/review").text

        assert "un avis, pas une preuve" in page

    def test_a_link_in_the_explanation_is_shown_as_text(self, monkeypatch):
        """The explanation itself carries the payload (the mock engine gives
        none, so the assembled one is replaced): escaping is what is tested."""
        from app.application import investigate_dispute

        payload = '<a href="https://evil.example/login">Examiner et valider</a>'
        monkeypatch.setattr(
            investigate_dispute, "deterministic_explanation", lambda findings: payload
        )
        client = TestClient(app, headers=basic(APPROVER))
        body = client.post(
            "/api/v1/disputes/investigate",
            headers=SERVICE,
            json={
                "message": "Je crois que j'ai été facturé deux fois pour septembre.",
                "customer_email": "alice@acme.fr",
                "invoice_id": "inv_002",
            },
        ).json()
        assert body["explanation"] == payload  # it did reach the action

        page = client.get(f"/api/v1/actions/{body['action_id']}/review").text

        assert "&lt;a href=&quot;https://evil.example/login&quot;&gt;" in page
        assert '<a href="https://evil.example' not in page
