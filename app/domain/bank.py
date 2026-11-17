"""What a bank says about money received (specs/016-bank-evidence).

Observations of the merchant's own declared accounts, read through the `BankEvidence`
port: never `Payment`s -- they feed no billing figure and no duplicate-payment rule. Bank
text (a transfer's reference) is untrusted: it is held in memory for the check and never
serialised; payer names, account numbers and descriptions are never mapped at all.
"""

from __future__ import annotations

from typing import Annotated, Literal

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
    model_config = ConfigDict(frozen=True, extra="forbid")

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


#: Why a credit or return is in the record -- the framing's closed set.
CheckReason = Literal[
    "reference_matched",
    "amount_equal",
    "amount_partial",
    "amount_over",
    "currency_differs",
    "reversed",
    "several_invoices_cited",
    "several_candidates",
    "fx_present",
    "fee_present",
    "related_return_found",
    "origin_unknown",
    "origin_internal",
]

#: Why the check concluded nothing it could stand on (`not_evaluable`).
NotEvaluableReason = Literal[
    "read_incomplete",
    "coverage_not_established",
    "account_not_visible",
    "no_account_in_currency",
    "bank_unavailable",
]

#: Why the check did not run (`not_run`).
NotRunReason = Literal[
    "no_bank",
    "no_invoice",
    "paid_on_platform",
    "short_circuit",
]

#: Said every time the check runs.
Limitation = Literal[
    "allocation_unverified_beyond_loaded_invoices",
    "coverage_declared_accounts_from_issue",
    "later_reversal_not_followed",
]

_CLOSED = ConfigDict(frozen=True, extra="forbid")


class CheckedCredit(BaseModel):
    """A credit as the record keeps it: the allowlist, nothing else."""

    model_config = _CLOSED

    kind: Literal["credit"] = "credit"
    account_id: str
    transaction_id: str
    amount: Money
    completed_at: AwareDatetime | None = None
    state: str
    observed_at: AwareDatetime
    reasons: tuple[CheckReason, ...] = ()


class CheckedReturn(BaseModel):
    """A return as the record keeps it: its instant labelled completed or created."""

    model_config = _CLOSED

    kind: Literal["return"] = "return"
    id: str
    type: str
    amount: Money  # signed
    related_credit_id: str | None = None
    instant: AwareDatetime
    instant_is: Literal["completed", "created"]


class BankCheck(BaseModel):
    """The record of the bank check on one run, kept whatever it found (plan 016, the
    audit allowlist). Built by the rule; closed types only, so no bank text can enter:
    an unknown field or reason is refused."""

    model_config = _CLOSED

    outcome: Outcome
    reason: NotRunReason | NotEvaluableReason | None = None
    invoice_id: str | None = None
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    margin_days: int | None = None
    accounts: tuple[DeclaredAccount, ...] = ()
    complete: bool | None = None
    coverage_established: bool | None = None
    items: tuple[Annotated[CheckedCredit | CheckedReturn, Field(discriminator="kind")], ...] = ()
    #: Reasons for the outcome as a whole (several candidates, several invoices cited).
    reasons: tuple[CheckReason, ...] = ()
    limitations: tuple[Limitation, ...] = ()

    def to_record(self) -> dict:
        return self.model_dump(mode="json")

    @classmethod
    def from_record(cls, record: dict | None) -> BankCheck | None:
        return None if record is None else cls.model_validate(record)
