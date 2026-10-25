"""The service on a platform that outlives it, for A4
(specs/006-restart-failures). Test code: run by the harness as
`python -m tests.e2e._launcher --port <port>`, never in production.

It builds one `PersistentFake` from its own environment -- `E2E_FAKE_LEDGER`,
the ledger's path, and `E2E_FAULT`, the one fault armed, if any -- and makes
every use of the billing provider reach it: `app.api.deps.get_provider` and
`app.api.health.get_provider` are replaced by name, since `get_engine`,
`get_approvals`, `get_investigator` and the health route call it directly,
and a route depending on it is overridden on the original callable, saved
before the replacement. All of it before the application serves. Then
uvicorn serves it in the main thread, so graceful stops and uvicorn's
shutdown line behave as for `uvicorn app.main:app`.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from app.api import deps, health
from app.main import app
from tests.e2e._persistent_fake import PersistentFake


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    port = parser.parse_args().port

    platform = PersistentFake(
        Path(os.environ["E2E_FAKE_LEDGER"]), fault=os.environ.get("E2E_FAULT") or None
    )
    original = deps.get_provider
    deps.get_provider = lambda: platform
    health.get_provider = lambda: platform
    app.dependency_overrides[original] = lambda: platform

    uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, workers=1, access_log=False)
    ).run()


if __name__ == "__main__":
    main()
