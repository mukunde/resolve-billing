"""Keep the test suite from writing an audit database into the repository."""

import os

# Set before anything imports app.api.deps, whose audit sink reads it once.
os.environ.setdefault("AUDIT_DB", ":memory:")
