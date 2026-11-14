"""Revolut Business behind the `BankEvidence` port (specs/016-bank-evidence, plan 2a).

One read:

1. `/accounts`, every time: each declared account's visibility and currency. An account
   not visible is not read, and the read is incomplete (`account_not_visible`).
2. `/transactions` for each visible declared account, paged by `created_at`: `from` is
   the window's start minus the margin (inclusive), `to` (exclusive) starts just past the
   window's end and moves to the last item's `created_at` as Revolut returns it. A page
   shorter than the page size ends the account. A full page must prove its boundary
   clean before the next one is asked for: its items in order, its last instant held by
   that item alone, and a second request over that instant (one millisecond wide, never
   past the current `to`) returning nothing unseen and less than a full page. Otherwise
   the read is incomplete (`page_boundary_tie`).
3. The mapper turns what was read into credits and returns; nothing else is kept.

**Coverage** (FR-003b): established only when the configuration declares the worlds
controlled (`REVOLUT_CONTROLLED_WORLDS=enabled`, for the seeded Sandbox run alone) **and**
no transaction read was created before the read's lower bound. Otherwise not established
-- on the Sandbox too -- until Revolut states a maximum creation-to-completion delay.

A failure is `BankUnavailable` with a fixed category; a time budget spent, an incomplete
read. Nothing of Revolut's text travels either way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.bank import BankRead, DeclaredAccount, Incompleteness
from app.providers.bank import mapping
from app.providers.bank.client import DEFAULT_BUDGET, BudgetSpent, RevolutReader
from app.providers.bank_base import BankUnavailable

#: Revolut's largest page.
PAGE_SIZE = 1000
#: The width of the request that proves a full page's boundary clean.
TIE_PROBE = timedelta(milliseconds=1)
#: How far past the window's end the first `to` is set (`to` is exclusive; items created
#: after the window's end are read and dropped).
END_SLACK = timedelta(seconds=1)


def wire_instant(value: datetime) -> str:
    """An instant as sent: UTC, to the millisecond, truncated."""
    value = value.astimezone(UTC)
    value = value.replace(microsecond=value.microsecond // 1000 * 1000)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def lower_bound(window_start: datetime, margin_days: int) -> datetime:
    """The read's lower bound, as sent: the margin before the window, truncated to the
    millisecond (never later than the bound asked for)."""
    return mapping.instant(wire_instant(window_start - timedelta(days=margin_days)))


def _keep(items: dict[str, tuple[dict, datetime]], tx_id: str, tx: dict,
          created: datetime) -> None:
    """Keep a transaction once; the same id answered twice with different content is
    malformed -- never the first answer silently trusted."""
    if tx_id in items and items[tx_id][0] != tx:
        raise mapping.Malformed
    items.setdefault(tx_id, (tx, created))


class _AccountRead:
    """Transactions read for one account: the items, and why the read stopped short."""

    def __init__(self) -> None:
        self.items: dict[str, tuple[dict, datetime]] = {}
        self.short: Incompleteness | None = None


class RevolutBank:
    name = "revolut"

    def __init__(
        self,
        reader: RevolutReader,
        accounts: tuple[str, ...],
        *,
        controlled: bool,
        budget: float = DEFAULT_BUDGET,
        page_size: int = PAGE_SIZE,
    ) -> None:
        if not accounts:
            raise ValueError("at least one declared account")
        self._reader = reader
        self._declared = tuple(accounts)
        self._controlled = controlled
        self._budget = budget
        self._page_size = page_size

    async def read(
        self, window_start: datetime, window_end: datetime, margin_days: int, now: datetime
    ) -> BankRead:
        lower = lower_bound(window_start, margin_days)
        deadline = self._reader.clock() + self._budget
        incomplete: list[Incompleteness] = []
        items: dict[str, tuple[dict, datetime]] = {}
        try:
            try:
                accounts = mapping.accounts(
                    await self._reader.get("/accounts", None, deadline), self._declared
                )
            except BudgetSpent:
                accounts = tuple(DeclaredAccount(id=a) for a in self._declared)
                incomplete.append("budget_spent")
            else:
                for account in accounts:
                    if not account.visible:
                        incomplete.append("account_not_visible")
                        continue
                    found = await self._transactions(account.id, lower, window_end, deadline)
                    for tx_id, (tx, created) in found.items.items():
                        _keep(items, tx_id, tx, created)
                    if found.short is not None:
                        incomplete.append(found.short)
                        if found.short == "budget_spent":
                            break
            kept = [tx for tx, created in items.values() if lower <= created <= window_end]
            credits, returns = mapping.observations(
                kept, frozenset(a.id for a in accounts if a.visible), now
            )
        except (mapping.Malformed, TypeError, ValueError, KeyError, AttributeError):
            # Whatever in the answer could not be read; nothing of it travels.
            raise BankUnavailable("malformed_answer") from None
        early = any(created < lower for _, created in items.values())
        reasons = tuple(dict.fromkeys(incomplete))
        return BankRead(
            accounts=accounts,
            credits=credits,
            returns=returns,
            window_start=window_start,
            window_end=window_end,
            margin_days=margin_days,
            complete=not reasons,
            incompleteness=reasons,
            coverage_established=self._controlled and not early,
        )

    async def _transactions(
        self, account: str, lower: datetime, window_end: datetime, deadline: float
    ) -> _AccountRead:
        read = _AccountRead()
        sent_from = wire_instant(lower)
        to_raw = wire_instant(window_end + END_SLACK)
        to = mapping.instant(to_raw)
        try:
            while True:
                found = mapping.page(await self._reader.get(
                    "/transactions",
                    {"account": account, "from": sent_from, "to": to_raw,
                     "count": str(self._page_size)},
                    deadline,
                ))
                for tx, tx_id, created in found:
                    _keep(read.items, tx_id, tx, created)
                if len(found) < self._page_size:
                    return read
                if not await self._clean_boundary(account, found, to_raw, to, read, deadline):
                    read.short = "page_boundary_tie"
                    return read
                to_raw, to = found[-1][0]["created_at"], found[-1][2]
        except BudgetSpent:
            read.short = "budget_spent"
            return read

    async def _clean_boundary(
        self,
        account: str,
        found: list[tuple[dict, str, datetime]],
        to_raw: str,
        to: datetime,
        read: _AccountRead,
        deadline: float,
    ) -> bool:
        """Whether a full page's last instant is proven to hold no item left out."""
        instants = [created for _, _, created in found]
        if any(newer < older for newer, older in zip(instants, instants[1:], strict=False)):
            return False  # not newest first: the boundary means nothing
        last = instants[-1]
        if instants.count(last) > 1 or last >= to:
            return False
        probe_to = last + TIE_PROBE
        probe_raw = to_raw if probe_to >= to else wire_instant(probe_to)
        probe = mapping.page(await self._reader.get(
            "/transactions",
            {"account": account, "from": found[-1][0]["created_at"], "to": probe_raw,
             "count": str(self._page_size)},
            deadline,
        ))
        for tx, tx_id, _ in probe:
            if tx_id not in read.items:
                return False  # left out of the page: the boundary is a tie
            if read.items[tx_id][0] != tx:
                raise mapping.Malformed
        return len(probe) < self._page_size
