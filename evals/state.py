"""What the engines evaluated on free tiers keep about the maintainer's
vendor accounts, across runs and checkouts (specs/004-second-engines).

An allowance and a rate limit belong to an account, not to a checkout: two
worktrees running on one key spend one envelope and share one per-minute
quota. So their state lives in one place outside every checkout,
`~/.resolve-billing/`: the Mistral ledger (`evals/ledger.py`), and per
account a run lock and the time of its last request.

One run per account at a time: a second is refused while the first holds
the lock, which the operating system releases if the process dies. A run
starts no sooner than a minute after the account's last request, so the
quota window a previous run left is never counted twice.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

#: The accounts' state, outside every checkout. Tests point it elsewhere.
STATE_DIR = Path.home() / ".resolve-billing"

#: How long a previous run's requests still count against a per-minute quota.
WINDOW_SECONDS = 60.0


class AccountBusy(Exception):  # noqa: N818 -- a refusal, not a defect
    """Another run on the same vendor account is in progress."""


class AccountRun:
    """Holds an account for one run; `record_send` notes each request."""

    def __init__(self, account: str, now: Callable[[], float] = time.time) -> None:
        self.account = account
        self._now = now
        self._lock = None

    def __enter__(self) -> AccountRun:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = open(STATE_DIR / f"{self.account}.run.lock", "a+b")  # noqa: SIM115
        if not try_lock(self._lock):
            self._lock.close()
            raise AccountBusy(f"another run on the {self.account} account is in progress")
        return self

    def __exit__(self, *exc) -> None:
        unlock(self._lock)
        self._lock.close()

    @property
    def _pace(self) -> Path:
        return STATE_DIR / f"{self.account}.pace.json"

    def wait(self) -> float:
        """Seconds until the previous run's last request leaves the window."""
        try:
            last = float(json.loads(self._pace.read_text(encoding="utf-8"))["last_send"])
        except FileNotFoundError:
            return 0.0
        except (OSError, ValueError, KeyError, TypeError):
            # Unreadable: assume a request was just sent.
            return WINDOW_SECONDS
        if not math.isfinite(last) or last > self._now() + WINDOW_SECONDS:
            return WINDOW_SECONDS
        return min(max(0.0, last + WINDOW_SECONDS - self._now()), WINDOW_SECONDS)

    def record_send(self) -> None:
        write_atomically(self._pace, {"last_send": self._now()})


def write_atomically(path: Path, content: dict) -> None:
    """A temporary file beside `path`, flushed to disk, then replaced."""
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(content, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


if sys.platform == "win32":
    import msvcrt

    def try_lock(lock) -> bool:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def unlock(lock) -> None:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def try_lock(lock) -> bool:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def unlock(lock) -> None:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
