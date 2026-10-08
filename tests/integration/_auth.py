"""Test credentials for the API's two kinds of caller. ADR-009.

Values made up for the tests; they authenticate nothing outside them.
"""

import base64

SERVICE_KEY = "test-service-key"
SERVICE = {"Authorization": f"Bearer {SERVICE_KEY}"}

APPROVER = ("m.dupont@acme.fr", "test-approver-secret")
SECOND_APPROVER = ("j.martin@acme.fr", "test-second-secret")

APPROVERS = ",".join(f"{name}:{secret}" for name, secret in (APPROVER, SECOND_APPROVER))


def basic(credentials: tuple[str, str]) -> dict[str, str]:
    """The Authorization header a person's browser sends after signing in."""
    token = base64.b64encode(":".join(credentials).encode()).decode()
    return {"Authorization": f"Basic {token}"}
