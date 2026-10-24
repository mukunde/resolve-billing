"""Test isolation.

Two things bite otherwise. The audit sink would write a database into the
repository, and the cached singletons in ``app.api.deps`` would carry state
between tests -- which matters more than it sounds, because the idempotency
key is derived from the action rather than generated: investigating case A in
two different tests legitimately yields the *same* action, so without a reset
one test approves what another expects to find pending.
"""

import os

# Set before anything imports app.api.deps, whose sinks read it once.
os.environ.setdefault("AUDIT_DB", ":memory:")
# The suite runs on the fake whatever the environment says: Stripe is reached
# only by the contract tests, which build it themselves, with its own key.
os.environ["BILLING_PROVIDER"] = "fake"

import pytest  # noqa: E402

from app.api import deps  # noqa: E402

_SINGLETONS = (deps.get_provider, deps.get_engine, deps.get_audit, deps.get_actions)


@pytest.fixture(autouse=True)
def fresh_state():
    """Give every test its own provider, engine and in-memory database."""
    for singleton in _SINGLETONS:
        singleton.cache_clear()
    yield
    for singleton in _SINGLETONS:
        singleton.cache_clear()


@pytest.fixture(autouse=True)
def account_state(tmp_path_factory, monkeypatch):
    """The vendor accounts' state -- run locks, pacing, the Mistral ledger --
    in a directory of the test's own, never the maintainer's."""
    from evals import state

    monkeypatch.setattr(state, "STATE_DIR", tmp_path_factory.mktemp("account-state"))
