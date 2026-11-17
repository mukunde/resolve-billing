"""The third port: a bank's evidence that money arrived (specs/016-bank-evidence;
constitution III as amended, ADR-014).

Read-only by construction. The declared accounts come from configuration, never from a
dispute. A read that fails raises `BankUnavailable` with a fixed category, never the
bank's text; the check then answers *not evaluable*, and the investigation goes on.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.bank import BankRead


class BankUnavailable(Exception):  # noqa: N818 -- a state, not a programming error
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@runtime_checkable
class BankEvidence(Protocol):
    name: str

    async def read(
        self, window_start: datetime, window_end: datetime, margin_days: int, now: datetime
    ) -> BankRead:
        """Credits and returns on the declared accounts whose `created_at` falls from
        `window_start` minus `margin_days` to `window_end`, with whether the read is
        complete and whether its coverage is established."""
        ...
