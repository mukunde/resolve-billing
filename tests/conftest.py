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
