"""The service as a real operating-system process, for end-to-end tests
(specs/005-restart-pending-approval).

`ServiceProcess` starts `uvicorn app.main:app` with an environment built from
scratch -- the mock engine, the fake billing platform, a temporary database,
the tests' made-up credentials, and nothing from the maintainer's `.env` --
waits for it to answer, and stops it: gracefully, proven by uvicorn's own
shutdown line, or by a hard kill. Whatever happens, no process is left
behind.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from tests.integration._auth import APPROVERS, SERVICE_KEY

REPOSITORY = Path(__file__).resolve().parents[2]

#: How long a start, a stop and a kill may take.
START_DEADLINE = 20.0
STOP_DEADLINE = 10.0

#: What uvicorn writes once it has shut down cleanly.
SHUTDOWN_LINE = "Finished server process"

WINDOWS = sys.platform == "win32"


class ServiceFailed(AssertionError):
    """The service did not start or stop as it must; the message carries its
    last output."""


def client(base_url: str) -> httpx.Client:
    """An HTTP client for the service alone: no proxy or other setting taken
    from the environment, and every request bounded."""
    return httpx.Client(base_url=base_url, trust_env=False, timeout=10.0)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _port_taken(output: str) -> bool:
    """uvicorn's report of a port in use: POSIX's words, or Windows' error."""
    lowered = output.lower()
    return "address already in use" in lowered or "10048" in lowered


def _environment(database: Path) -> dict[str, str]:
    """Only what the service needs: no inherited key can reach it."""
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")
    environment = {name: os.environ[name] for name in keep if name in os.environ}
    environment |= {
        "PYTHONPATH": str(REPOSITORY),
        "PYTHONUNBUFFERED": "1",
        "AUDIT_DB": str(database),
        "BILLING_PROVIDER": "fake",
        "DECISION_ENGINE": "mock",
        "RESOLVE_SERVICE_KEY": SERVICE_KEY,
        "RESOLVE_APPROVERS": APPROVERS,
        "RESOLVE_ORCHESTRATOR_URL": "http://orchestrator.test",
    }
    return environment


class ServiceProcess:
    """One run of the service on `database`; a context manager."""

    def __init__(self, database: Path, log: Path) -> None:
        self.database = database
        self.log_path = log
        self.process: subprocess.Popen | None = None
        self.port: int | None = None
        self._log = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def pid(self) -> int:
        return self.process.pid

    def __enter__(self) -> ServiceProcess:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.reap()

    def start(self) -> None:
        """Start, and wait until it answers; on any failure, the child is
        killed and reaped here -- `__exit__` never runs when `__enter__`
        raises."""
        for attempt in (1, 2):
            self.port = _free_port()
            self._spawn()
            try:
                self._wait_ready()
                return
            except ServiceFailed as failure:
                output = self.output()
                self.reap()
                # The port was taken between the probe and the bind: one more.
                if attempt == 1 and _port_taken(output):
                    continue
                raise failure from None
            except BaseException:
                # Anything else -- an interruption included -- still leaves
                # no process behind.
                self.reap()
                raise

    def _spawn(self) -> None:
        self._log = open(self.log_path, "ab")  # noqa: SIM115 -- closed in reap
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--workers",
                "1",
                "--no-access-log",
            ],
            cwd=REPOSITORY,
            env=_environment(self.database),
            stdout=self._log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + START_DEADLINE
        with client(self.base_url) as http:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise ServiceFailed(f"the service exited at start\n{self.output()}")
                try:
                    health = http.get("/health", timeout=1.0)
                except httpx.TransportError:
                    time.sleep(0.1)
                    continue
                if health.status_code != 200:
                    raise ServiceFailed(
                        f"the health route answered {health.status_code}\n{self.output()}"
                    )
                body = health.json()
                if body.get("billing_provider") != "fake" or body.get("decision_engine") != "mock":
                    raise ServiceFailed(f"the service is not the one expected: {body}")
                return
        raise ServiceFailed(
            f"the service did not answer within {START_DEADLINE} s\n{self.output()}"
        )

    def stop_gracefully(self) -> None:
        """Ask it to shut down, and prove it did: uvicorn's shutdown line
        within the deadline. A kill never satisfies this; the exit code need
        not be zero (uvicorn may re-raise the signal it handled)."""
        if WINDOWS:
            self.process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(STOP_DEADLINE)
        except subprocess.TimeoutExpired:
            raise ServiceFailed(
                f"the service did not shut down within {STOP_DEADLINE} s "
                f"(console events delivered?)\n{self.output()}"
            ) from None
        if SHUTDOWN_LINE not in self.output():
            raise ServiceFailed(f"the service exited without shutting down\n{self.output()}")

    def kill(self) -> None:
        self.process.kill()
        try:
            self.process.wait(STOP_DEADLINE)
        except subprocess.TimeoutExpired:
            raise ServiceFailed(f"the service survived a kill\n{self.output()}") from None

    def exited(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def reap(self) -> None:
        """Whatever state it is in, end it and close its log."""
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(STOP_DEADLINE)
        if self._log is not None:
            self._log.close()
            self._log = None

    def output(self, limit: int = 4096) -> str:
        """The last of what the process wrote."""
        if self._log is not None:
            self._log.flush()
        try:
            data = self.log_path.read_bytes()
        except OSError:
            return ""
        return data[-limit:].decode("utf-8", errors="replace")
