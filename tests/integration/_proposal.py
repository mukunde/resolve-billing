"""What an approver is shown, as the value an approval names. D13."""


def shown(client, action_id: str) -> str:
    """The proposal fingerprint of an action awaiting a decision."""
    proposal = client.get(f"/api/v1/actions/{action_id}").json()["proposal"]
    assert proposal, "an action awaiting a decision carries its proposal"
    return proposal
