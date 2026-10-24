"""The daily request quota of an engine evaluated on a free tier
(specs/004-second-engines, amendment of 2026-10-24).

Google's free tier allows `gemini-3.8-flash` twenty requests a day per
project, reset at midnight Pacific time; twenty of the development cases
reach the engine, so a full run takes the whole day. This module keeps the
day's count, per project, in the accounts' state outside every checkout
(`evals/state.py`), under one lock every key and checkout of the project
share.

- On each quota day, before its first request, the maintainer declares the
  requests already used, as the console shows them: a missing count proves
  nothing. A day is declared once; a count is never lowered.
- Each request is counted before it is sent, and never refunded: a failed,
  refused, cancelled or timed-out attempt counts. A request that would
  exceed the limit is not sent and the run stops; so does one on a day not
  declared -- a run crossing Pacific midnight.
- A full run starts only on a day with nothing used.
- A clock gone back, or a state that cannot be read, is refused before
  anything changes, and never reset.

The project is assumed dedicated, with no other caller: neither the console
nor this count can account for one. That the key belongs to the project is
the maintainer's declaration, not verified here.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from evals import state
from evals.limits import RunStopped

#: A Google Cloud project id: 6 to 30 lowercase letters, digits and hyphens,
#: starting with a letter, not ending with a hyphen. It names a file.
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")


class QuotaRefused(Exception):  # noqa: N818 -- a refusal, not a defect
    """The quota cannot be used as it stands; nothing was sent or changed."""


class DailyQuota:
    def __init__(
        self,
        project: str,
        limit: int,
        timezone: str,
        now: Callable[[], float] = time.time,
        lock_timeout: float = 30.0,
    ) -> None:
        if not PROJECT_ID.fullmatch(project or ""):
            raise QuotaRefused(f"{project!r} is not a Google Cloud project id")
        self.project = project
        self.limit = limit
        self.timezone = timezone
        self._zone = ZoneInfo(timezone)
        self._now = now
        self._lock_timeout = lock_timeout

    @property
    def path(self) -> Path:
        return state.STATE_DIR / f"quota-{self.project}.json"

    def declare(self, used: int) -> None:
        """Today's requests already used, as the console shows them."""
        if not (isinstance(used, int) and 0 <= used <= self.limit):
            raise QuotaRefused(f"the requests used must be between 0 and {self.limit}")
        with self._operation() as (held, day):
            if day in held["days"]:
                raise QuotaRefused(
                    f"quota day {day} is already declared "
                    f"({held['days'][day]['declared']} used); a count is never lowered"
                )
            held["days"][day] = {"declared": used, "sent": 0}

    def admit_full_run(self) -> None:
        """A full run needs the whole day: refused if anything was used."""
        with self._operation() as (held, day):
            counts = held["days"].get(day)
            if counts is None:
                raise QuotaRefused(f"quota day {day} is not declared")
            if counts["declared"] + counts["sent"]:
                raise QuotaRefused(
                    f"a full run needs all {self.limit} requests of quota day {day}; "
                    f"{counts['declared'] + counts['sent']} are already used"
                )

    def take(self) -> None:
        """Count one request before it is sent, or stop the run."""
        with self._operation() as (held, day):
            counts = held["days"].get(day)
            if counts is None:
                raise RunStopped(f"quota day {day} not declared; nothing sent")
            if counts["declared"] + counts["sent"] + 1 > self.limit:
                raise RunStopped(
                    f"daily quota exhausted (local count: {self.limit} requests on {day})"
                )
            counts["sent"] += 1

    def state(self) -> dict:
        with self._locked():
            held = self._read()
        return {
            "project": self.project,
            "limit": self.limit,
            "timezone": self.timezone,
            "days": held["days"],
        }

    @contextmanager
    def _operation(self) -> Iterator[tuple[dict, str]]:
        """The lock held, the clock read once and checked, the state read;
        written back -- `last_seen` advanced -- only if the body succeeds."""
        with self._locked():
            now = self._now()
            held = self._read()
            if now < held["last_seen"]:
                raise QuotaRefused(
                    "the clock is behind the quota's last record; nothing was changed"
                )
            day = datetime.fromtimestamp(now, UTC).astimezone(self._zone).date().isoformat()
            yield held, day
            held["last_seen"] = now
            state.write_atomically(self.path, held)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"project": self.project, "days": {}, "last_seen": 0.0}
        try:
            held = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise QuotaRefused(
                f"the quota {self.path} cannot be read ({type(error).__name__})"
            ) from None
        if not self._consistent(held):
            raise QuotaRefused(f"the quota {self.path} is inconsistent; it is never reset")
        return held

    def _consistent(self, held) -> bool:
        if not isinstance(held, dict) or set(held) != {"project", "days", "last_seen"}:
            return False
        if held["project"] != self.project or not isinstance(held["days"], dict):
            return False
        last = held["last_seen"]
        if isinstance(last, bool) or not isinstance(last, int | float) or not math.isfinite(last):
            return False
        for day, counts in held["days"].items():
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except (TypeError, ValueError):
                return False
            if not isinstance(counts, dict) or set(counts) != {"declared", "sent"}:
                return False
            if not all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in counts.values()
            ):
                return False
        return True

    @contextmanager
    def _locked(self) -> Iterator[None]:
        state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(state.STATE_DIR / f"quota-{self.project}.lock", "a+b") as lock:
            deadline = time.monotonic() + self._lock_timeout
            while not state.try_lock(lock):
                if time.monotonic() > deadline:
                    raise QuotaRefused(f"the quota of {self.project} is held by another run")
                time.sleep(0.02)
            try:
                yield
            finally:
                state.unlock(lock)
