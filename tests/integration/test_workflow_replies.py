"""ADR-011 in the workflow: one authority on automatic replies, and no sender from text."""

import json
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / "n8n" / "billing-dispute-workflow.json"
REPLY = "Reponse automatique (aucun mouvement d'argent)"
GATE = "Reponse automatique permise ?"


def workflow() -> dict:
    return json.loads(WORKFLOW.read_text(encoding="utf-8"))


def node(name: str) -> dict:
    (found,) = (n for n in workflow()["nodes"] if n["name"] == name)
    return found


def targets(source: str, branch: int) -> list[str]:
    return [t["node"] for t in workflow()["connections"][source]["main"][branch]]


def sources_of(target: str) -> list[tuple[str, int]]:
    found = []
    for source, outputs in workflow()["connections"].items():
        for branch, links in enumerate(outputs["main"]):
            if any(link["node"] == target for link in links):
                found.append((source, branch))
    return found


def test_the_automatic_reply_is_reached_only_through_its_permission():
    """`requires_human_approval: false` alone is not permission to reply."""
    assert sources_of(REPLY) == [(GATE, 0)]
    condition = node(GATE)["parameters"]["conditions"]["conditions"][0]
    assert condition["leftValue"] == "={{ $json.automatic_reply_allowed }}"
    assert condition["operator"]["operation"] == "true"


def test_no_approval_needed_leads_to_the_permission_check():
    assert targets("Validation humaine requise ?", 1) == [GATE]


def test_unknown_senders_go_to_triage_and_robots_to_nobody():
    assert targets(GATE, 1) == ["Expediteur inconnu ?"]
    assert targets("Expediteur inconnu ?", 0) == ["Mail de triage (expediteur inconnu)"]
    assert targets("Expediteur inconnu ?", 1) == ["Aucune reponse (message automatique)"]
    assert node("Aucune reponse (message automatique)")["type"].endswith("noOp")


def test_triage_is_a_separate_mailbox():
    triage = node("Mail de triage (expediteur inconnu)")["parameters"]
    assert "json.triage_to" in triage["toEmail"]
    assignments = node("Configuration")["parameters"]["assignments"]["assignments"]
    values = {a["name"]: a["value"] for a in assignments}
    assert values["triage_to"] != values["notify_to"]


def test_the_sender_is_never_read_from_the_message_text():
    code = node("Extraction deterministe")["parameters"]["jsCode"]
    assert "@" not in code.split("const email", 1)[1].split(";", 1)[0]
    assert "body.customer_email ?? body.from ?? null" in code


def test_headers_are_forwarded():
    body = node("POST /disputes/investigate")["parameters"]["jsonBody"]
    assert "headers: $json.headers" in body
