"""The workflow uses two addresses for the API, and keeps them apart.

n8n calls the API from where it runs -- inside Docker, `host.docker.internal`.
A person opens the links in a mail from a browser, where that address leads
nowhere. Built from the one `billing_api`, every link in every mail failed
for the person it was sent to.
"""

import json
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / "n8n" / "billing-dispute-workflow.json"


def nodes() -> list[dict]:
    return json.loads(WORKFLOW.read_text(encoding="utf-8"))["nodes"]


def text_of(node: dict) -> str:
    return json.dumps(node.get("parameters", {}), ensure_ascii=False)


def test_both_addresses_are_configured():
    (configuration,) = (n for n in nodes() if n["name"] == "Configuration")
    names = {a["name"] for a in configuration["parameters"]["assignments"]["assignments"]}

    assert {"billing_api", "public_api"} <= names


def test_n8n_calls_the_api_at_its_own_address():
    calls = [n for n in nodes() if n["type"].endswith("httpRequest")]

    assert calls
    for node in calls:
        assert "json.billing_api" in text_of(node), node["name"]


def test_every_link_in_a_mail_is_one_a_browser_can_open():
    mails = [n for n in nodes() if n["type"].endswith("emailSend")]
    linking = [n for n in mails if "/api/v1/" in text_of(n)]

    assert linking, "the approval mail at least carries a link"
    for node in linking:
        assert "json.billing_api" not in text_of(node), node["name"]
        assert "json.public_api" in text_of(node), node["name"]
