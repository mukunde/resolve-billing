"""What a bank says about money received (specs/016-bank-evidence).

Observations of the merchant's own declared accounts, read through the `BankEvidence`
port: never `Payment`s -- they feed no billing figure and no duplicate-payment rule. Bank
text (a transfer's reference) is untrusted: it is held in memory for the check and never
serialised; payer names, account numbers and descriptions are never mapped at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.money import Money

#: Where a credit came from, as far as the bank says: an outside payer, one of the
#: merchant's own or the bank's internal movements, or unknown (no counterparty).
Origin = Literal["external", "internal", "unknown"]

#: Why a read is not complete -- a closed set, never the bank's own text.
Incompleteness = Literal[
    "read_failed",          # a request failed or answered something unreadable
    "page_boundary_tie",    # a full page ended on an instant shared by several items
    "budget_spent",         # the time or request budget ran out before the end
    "account_not_visible",  # a declared account is not visible with the key
]

#: What the check concluded, recorded whatever it was.
Outcome = Literal["observed", "to_reconcile", "not_evaluable", "nothing", "not_run"]


class DeclaredAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    currency: str | None = None
    visible: bool = True


class BankCredit(BaseModel):
    """A credit on a declared account, as observed."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    transaction_id: str
    type: str
    state: str
    observed_at: AwareDatetime
    amount: Money
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    #: The transfer's reference: untrusted text, used by the check, never serialised.
    reference: str | None = Field(default=None, exclude=True, repr=False)
    origin: Origin = "unknown"
    #: A billed amount or currency differing from the leg's own.
    fx: bool = False
    #: A non-zero fee on the leg.
    fee: bool = False


class BankReturn(BaseModel):
    """A transaction that gives money back on a declared account: related to a credit,
    or of a return type."""

    model_config = ConfigDict(frozen=True)

    id: str
    type: str
    amount: Money  # signed, as the bank says it
    created_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    related_credit_id: str | None = None


class BankRead(BaseModel):
    """One read of the declared accounts over a window (`created_at` from the window's
    start minus the margin), and what can be trusted of it."""

    model_config = ConfigDict(frozen=True)

    accounts: tuple[DeclaredAccount, ...]
    credits: tuple[BankCredit, ...] = ()
    returns: tuple[BankReturn, ...] = ()
    window_start: AwareDatetime
    window_end: AwareDatetime
    margin_days: int
    complete: bool
    incompleteness: tuple[Incompleteness, ...] = ()
    #: Only when every credit could be in the read: controlled worlds, or a maximum
    #: creation-to-completion delay the bank states (FR-003b).
    coverage_established: bool = False


class BankCheck(BaseModel):
    """The record of the bank check on one run, kept whatever it found (plan 016, the
    audit allowlist). Built by the rule; nothing here carries bank text."""

    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    reason: str | None = None  # for `not_run`: why, from a closed set
    invoice_id: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    margin_days: int | None = None
    accounts: tuple[DeclaredAccount, ...] = ()
    complete: bool | None = None
    coverage_established: bool | None = None
    #: Per credit or return: allowlisted fields and reasons only.
    items: tuple[dict, ...] = ()
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def to_record(self) -> dict:
        return self.model_dump(mode="json")

    @classmethod
    def from_record(cls, record: dict | None) -> BankCheck | None:
        return None if record is None else cls.model_validate(record)
