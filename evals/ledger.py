"""The month's Mistral envelope, kept on disk (specs/004-second-engines).

Mistral's free level is an allowance in dollars a month, shared by every
model the key reaches. One ledger holds it, for both models and every run:
the maintainer declares the month's allowance once; each attempt reserves
its estimated maximum cost before it is sent, written durably (a temporary
file, then an atomic replace) under a lock held across reading, checking and
writing; after the answer it is settled at the cost reported. A reservation
never settled -- a run interrupted mid-call -- stays counted as spent.

The ledger lives with the accounts' state, outside every checkout
(`evals/state.py`): two worktrees on one key spend one envelope.
An unreadable or inconsistent ledger is refused, never reset. A new month
(UTC) starts a new ledger, the old one kept beside it under its month.
The costs are estimates (the maintainer's waiver, 2026-10-24): the console's
usage, read by the maintainer, is the account's own figure.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from evals import state
from evals.limits import RunStopped

#: The ledger's name in the accounts' state directory.
ENVELOPE_NAME = "mistral-envelope.json"


class LedgerRefused(Exception):  # noqa: N818 -- a refusal, not a defect
    """The ledger cannot be used as it stands; nothing was sent or changed."""


class Ledger:
    def __init__(
        self,
        path: Path | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        lock_timeout: float = 30.0,
    ) -> None:
        self.path = path if path is not None else state.STATE_DIR / ENVELOPE_NAME
        self._now = now
        self._lock_timeout = lock_timeout

    @property
    def month(self) -> str:
        return self._now().strftime("%Y-%m")

    def declare(self, allowance_usd: float) -> None:
        """The month's allowance, declared once."""
        if not (math.isfinite(allowance_usd) and allowance_usd > 0):
            raise LedgerRefused("the allowance must be a positive amount of dollars")
        with self._locked():
            held = self._read()
            if held is not None and held["month"] == self.month:
                raise LedgerRefused(
                    f"the allowance for {self.month} is already declared "
                    f"(${held['allowance_usd']}); it is declared once a month"
                )
            if held is not None:
                # A past month's ledger, kept for the record.
                self.path.replace(self.path.with_name(f"{self.path.stem}.{held['month']}.json"))
            self._write({"month": self.month, "allowance_usd": allowance_usd, "attempts": {}})

    def reserve(self, attempt: str, usd: float) -> None:
        """Reserve an attempt's maximum cost, on disk, before it is sent; an
        attempt that would exceed what remains stops the run."""
        with self._locked():
            held = self._current()
            if attempt in held["attempts"]:
                raise LedgerRefused(f"attempt {attempt!r} is already in the ledger")
            left = held["allowance_usd"] - _spent(held)
            if usd > left + 1e-9:
                raise RunStopped(
                    f"the Mistral envelope for {self.month} cannot cover this attempt "
                    f"(${usd:.4f} reserved, ${max(left, 0):.4f} left)"
                )
            held["attempts"][attempt] = {"reserved": round(usd, 6), "settled": None}
            self._write(held)

    def settle(self, attempt: str, usd: float) -> None:
        """The attempt's cost as reported, replacing its reservation."""
        with self._locked():
            held = self._current()
            if attempt not in held["attempts"]:
                raise LedgerRefused(f"attempt {attempt!r} was never reserved")
            held["attempts"][attempt]["settled"] = round(usd, 6)
            self._write(held)

    def state(self) -> dict:
        """The month, the allowance, what is spent and what is left."""
        with self._locked():
            held = self._current()
        spent = _spent(held)
        return {
            "month": held["month"],
            "allowance_usd": held["allowance_usd"],
            "spent_usd": round(spent, 6),
            "left_usd": round(held["allowance_usd"] - spent, 6),
            "unsettled": sum(1 for a in held["attempts"].values() if a["settled"] is None),
        }

    def _current(self) -> dict:
        held = self._read()
        if held is None or held["month"] != self.month:
            raise LedgerRefused(
                f"no Mistral allowance is declared for {self.month}: declare it once with "
                "--mistral-allowance-usd"
            )
        return held

    def _read(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            held = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise LedgerRefused(
                f"the ledger {self.path} cannot be read ({type(error).__name__})"
            ) from None
        if not self._consistent(held):
            raise LedgerRefused(f"the ledger {self.path} is inconsistent; it is never reset")
        return held

    @staticmethod
    def _consistent(held) -> bool:
        return _consistent(held)

    def _write(self, held: dict) -> None:
        state.write_atomically(self.path, held)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """An exclusive lock on a file beside the ledger, released by the
        operating system if the process dies holding it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "a+b") as lock:
            deadline = time.monotonic() + self._lock_timeout
            while not state.try_lock(lock):
                if time.monotonic() > deadline:
                    raise LedgerRefused(f"the ledger {self.path} is held by another run")
                time.sleep(0.05)
            try:
                yield
            finally:
                state.unlock(lock)


class PrepaidLedger(Ledger):
    """The envelope of a prepaid vendor account (specs/007-openai-qwen-engines).

    One per billing organisation, in the account's currency, shared by every
    configuration, run and checkout of that account under one lock. The
    maintainer declares its total once; a later declaration only raises it,
    recorded beside the earlier ones, never edited. Each attempt reserves
    its maximum estimated cost before it is sent and is settled at its
    reported usage's cost; a reservation never settled stays spent. It does
    not reset monthly, and it never deducts a vendor's free allowance: the
    vendor's console or prepaid credit is the account's own figure.
    """

    def __init__(
        self,
        account: str,
        currency: str,
        path: Path | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        lock_timeout: float = 30.0,
    ) -> None:
        super().__init__(
            path if path is not None else state.STATE_DIR / f"envelope-{account}.json",
            now=now,
            lock_timeout=lock_timeout,
        )
        self.account = account
        self.currency = currency

    def declare(self, total: float) -> None:
        """The envelope's total: once to open it, then only higher."""
        if not (math.isfinite(total) and total > 0):
            raise LedgerRefused(f"the envelope must be a positive amount ({self.currency})")
        with self._locked():
            held = self._read()
            if held is None:
                held = {
                    "account": self.account,
                    "currency": self.currency,
                    "declarations": [],
                    "attempts": {},
                }
            elif total <= held["declarations"][-1]["total"]:
                raise LedgerRefused(
                    f"the envelope of {self.account} is {held['declarations'][-1]['total']} "
                    f"{self.currency}; a declaration only raises it"
                )
            held["declarations"].append({"at": self._now().isoformat(), "total": total})
            self._write(held)

    def reserve(self, attempt: str, amount: float) -> None:
        """Reserve an attempt's maximum cost, on disk, before it is sent; an
        attempt that would exceed what remains stops the run."""
        _refuse_amount(amount)
        with self._locked():
            held = self._current()
            if attempt in held["attempts"]:
                raise LedgerRefused(f"attempt {attempt!r} is already in the ledger")
            left = held["declarations"][-1]["total"] - _spent(held)
            if amount > left + 1e-9:
                raise RunStopped(
                    f"the envelope of {self.account} cannot cover this attempt "
                    f"({amount:.4f} {self.currency} reserved, {max(left, 0):.4f} left)"
                )
            held["attempts"][attempt] = {"reserved": round(amount, 6), "settled": None}
            self._write(held)

    def settle(self, attempt: str, amount: float) -> None:
        """The attempt's cost as reported, replacing its reservation."""
        _refuse_amount(amount)
        super().settle(attempt, amount)

    def state(self) -> dict:
        """The total, its declarations, what is spent and what is left."""
        with self._locked():
            held = self._current()
        spent = _spent(held)
        total = held["declarations"][-1]["total"]
        return {
            "account": held["account"],
            "currency": held["currency"],
            "total": total,
            "declarations": held["declarations"],
            "spent": round(spent, 6),
            "left": round(total - spent, 6),
            "unsettled": sum(1 for a in held["attempts"].values() if a["settled"] is None),
        }

    def _current(self) -> dict:
        held = self._read()
        if held is None:
            raise LedgerRefused(
                f"no envelope is declared for {self.account}: declare it once with --envelope"
            )
        return held

    def _read(self) -> dict | None:
        held = super()._read()
        if held is not None and held["account"] != self.account:
            raise LedgerRefused(f"the ledger {self.path} belongs to another account")
        if held is not None and held["currency"] != self.currency:
            raise LedgerRefused(
                f"the ledger {self.path} is kept in another currency ({held['currency']}, "
                f"not {self.currency})"
            )
        return held

    @staticmethod
    def _consistent(held) -> bool:
        if not isinstance(held, dict) or set(held) != {
            "account",
            "currency",
            "declarations",
            "attempts",
        }:
            return False
        if not isinstance(held["account"], str) or not isinstance(held["currency"], str):
            return False
        if held["currency"] not in {"USD", "EUR"}:
            return False
        declarations = held["declarations"]
        if not isinstance(declarations, list) or not declarations:
            return False
        totals = []
        for entry in declarations:
            if not isinstance(entry, dict) or set(entry) != {"at", "total"}:
                return False
            if not (_amount(entry["total"]) and entry["total"] > 0):
                return False
            try:
                datetime.fromisoformat(entry["at"])
            except (TypeError, ValueError):
                return False
            totals.append(entry["total"])
        if any(later <= earlier for earlier, later in zip(totals, totals[1:], strict=False)):
            return False
        return _attempts_consistent(held["attempts"])


def _refuse_amount(amount) -> None:
    """An amount is a finite, non-negative number, or nothing is written."""
    if not (_amount(amount) and amount >= 0):
        raise LedgerRefused("an amount must be a finite, non-negative number")


def _spent(held: dict) -> float:
    return sum(
        a["reserved"] if a["settled"] is None else a["settled"] for a in held["attempts"].values()
    )


def _amount(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _consistent(held) -> bool:
    if not isinstance(held, dict) or set(held) != {"month", "allowance_usd", "attempts"}:
        return False
    try:
        datetime.strptime(held["month"], "%Y-%m")
    except (TypeError, ValueError):
        return False
    if not (_amount(held["allowance_usd"]) and held["allowance_usd"] > 0):
        return False
    return _attempts_consistent(held["attempts"])


def _attempts_consistent(attempts) -> bool:
    if not isinstance(attempts, dict):
        return False
    for entry in attempts.values():
        if not isinstance(entry, dict) or set(entry) != {"reserved", "settled"}:
            return False
        if not (_amount(entry["reserved"]) and entry["reserved"] >= 0):
            return False
        if entry["settled"] is not None and not (
            _amount(entry["settled"]) and entry["settled"] >= 0
        ):
            return False
    return True
