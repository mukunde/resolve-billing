"""Every API test runs with authentication configured, as production does."""

import pytest

from tests.integration._auth import APPROVERS, SERVICE_KEY


@pytest.fixture(autouse=True)
def configured_authentication(monkeypatch):
    monkeypatch.setenv("RESOLVE_SERVICE_KEY", SERVICE_KEY)
    monkeypatch.setenv("RESOLVE_APPROVERS", APPROVERS)
    # Where the orchestrator's resume callbacks may point. ADR-009, D8.
    monkeypatch.setenv("RESOLVE_ORCHESTRATOR_URL", "http://orchestrator.test")
