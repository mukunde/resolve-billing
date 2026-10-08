"""Every API test runs with authentication configured, as production does."""

import pytest

from tests.integration._auth import APPROVERS, SERVICE_KEY


@pytest.fixture(autouse=True)
def configured_authentication(monkeypatch):
    monkeypatch.setenv("RESOLVE_SERVICE_KEY", SERVICE_KEY)
    monkeypatch.setenv("RESOLVE_APPROVERS", APPROVERS)
