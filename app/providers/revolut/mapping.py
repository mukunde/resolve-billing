"""Revolut's transactions as the domain's bank observations (specs/016-bank-evidence).

The only place Revolut's vocabulary is read. What the mapper keeps, and what it drops:

- **A credit**: one leg, on a declared account, a positive amount, a `type` on the
  allowlist (`topup`, `transfer` -- the convention the maintainer approved on 2026-11-14,
  until Revolut answers), `state` `completed` or `reverted`. Its origin is *reported*, not
  judged: an `external` counterparty is external; `self` or `revolut` internal; absent or
  anything else unknown. The rule, not the mapper, decides what qualifies.
- **A return**: a transaction whose `related_transaction_id` points at a credit read, or of
  a return type (`topup_return`, `refund`), whatever its sign, state or reference; one per
  leg on a declared account, signed as Revolut says it.
- **Kept**: ids, type, state, instants, amount and currency, whether the leg is billed in
  another amount or currency (FX), whether it carries a fee; a credit's `reference` in
  memory only (the domain type never serialises it).
- **Never kept**: `description`, the counterparty's ids and names, `merchant`, `card`,
  balances -- read here to decide nothing, and not copied anywhere.

Anything unreadable in what is mapped is `Malformed`, which the adapter turns into a
fixed category; nothing of the answer travels with it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.domain.bank import BankCredit, BankReturn, DeclaredAccount, Origin
from app.domain.exceptions import UnsupportedCurrency
from app.domain.money import Money, exponent_for

#: Credit types, by convention until Revolut answers (spec 016, clarification 2026-11-14).
CREDIT_TYPES = frozenset({"topup", "transfer"})
#: Return types read whatever their relation (framing, design point 1).
RETURN_TYPES = frozenset({"topup_return", "refund"})
#: The states a credit is kept in.
CREDIT_STATES = frozenset({"completed", "reverted"})
#: Counterparty account types that are the merchant's own or the bank's.
INTERNAL = frozenset({"self", "revolut"})


class Malformed(Exception):  # noqa: N818 -- a state of the answer
    """An answer the mapper cannot read; carries no text of it."""


def instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise Malformed
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Malformed from None
    if parsed.tzinfo is None:
        raise Malformed
    return parsed.astimezone(UTC)


def _optional_instant(value: Any) -> datetime | None:
    return None if value is None else instant(value)


def _number(value: Any) -> Decimal:
    # JSON numbers arrive as int or Decimal (the client parses floats as Decimal); a bool
    # is an int in Python and is refused, as is anything else.
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise Malformed
    number = Decimal(value)
    if not number.is_finite():
        raise Malformed
    return number


def money(value: Any, currency: Any) -> Money:
    """An exact amount: more decimals than the currency's minor unit is malformed, never
    rounded (IV)."""
    if not isinstance(currency, str):
        raise Malformed
    number = _number(value)
    try:
        exponent = exponent_for(currency)
        if number != number.quantize(Decimal(1).scaleb(-exponent)):
            raise Malformed
        return Money.from_decimal(number, currency)
    except (UnsupportedCurrency, ArithmeticError):
        raise Malformed from None


def accounts(payload: Any, declared: tuple[str, ...]) -> tuple[DeclaredAccount, ...]:
    """Each declared account as `/accounts` shows it: visible when exactly one account
    carries its id, with its currency; otherwise not visible."""
    if not isinstance(payload, list) or not all(isinstance(a, dict) for a in payload):
        raise Malformed
    result = []
    for account_id in declared:
        found = [a for a in payload if a.get("id") == account_id]
        if len(found) != 1:
            result.append(DeclaredAccount(id=account_id, currency=None, visible=False))
            continue
        currency = found[0].get("currency")
        try:
            Money.zero(currency if isinstance(currency, str) else "")
        except UnsupportedCurrency:
            raise Malformed from None
        result.append(DeclaredAccount(id=account_id, currency=currency.upper(), visible=True))
    return tuple(result)


def page(payload: Any) -> list[tuple[dict, str, datetime]]:
    """A `/transactions` page: each item with its id and `created_at`, both required."""
    if not isinstance(payload, list):
        raise Malformed
    items = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise Malformed
        items.append((item, item["id"], instant(item.get("created_at"))))
    return items


def _legs(tx: dict) -> list[dict]:
    legs = tx.get("legs")
    if not isinstance(legs, list) or not all(isinstance(leg, dict) for leg in legs):
        raise Malformed
    return legs


def _origin(leg: dict) -> Origin:
    counterparty = leg.get("counterparty")
    kind = counterparty.get("account_type") if isinstance(counterparty, dict) else None
    if kind == "external":
        return "external"
    if kind in INTERNAL:
        return "internal"
    return "unknown"


def _fx(leg: dict, amount: Decimal, currency: str) -> bool:
    billed_currency = leg.get("bill_currency")
    billed_amount = leg.get("bill_amount")
    if billed_currency is not None:
        if not isinstance(billed_currency, str):
            raise Malformed
        if billed_currency.upper() != currency:
            return True
    return billed_amount is not None and _number(billed_amount) != amount


def _fee(leg: dict) -> bool:
    fee = leg.get("fee")
    return fee is not None and _number(fee) != 0


def credit(tx: dict, declared: frozenset[str], observed_at: datetime) -> BankCredit | None:
    """The transaction as a credit on a declared account, or None when it is not one."""
    kind, state = tx.get("type"), tx.get("state")
    if kind not in CREDIT_TYPES or state not in CREDIT_STATES:
        return None
    legs = _legs(tx)
    if len(legs) != 1 or legs[0].get("account_id") not in declared:
        return None
    leg = legs[0]
    amount = money(leg.get("amount"), leg.get("currency"))
    if amount.amount_minor <= 0:
        return None
    reference = tx.get("reference")
    return BankCredit(
        account_id=leg["account_id"],
        transaction_id=tx["id"],
        type=kind,
        state=state,
        observed_at=observed_at,
        amount=amount,
        created_at=instant(tx.get("created_at")),
        completed_at=_optional_instant(tx.get("completed_at")),
        reference=reference if isinstance(reference, str) else None,
        origin=_origin(leg),
        fx=_fx(leg, _number(leg.get("amount")), amount.currency),
        fee=_fee(leg),
    )


def returns(tx: dict, declared: frozenset[str], credit_ids: set[str]) -> list[BankReturn]:
    """The transaction's legs on declared accounts as returns, when it is one."""
    related = tx.get("related_transaction_id")
    related = related if isinstance(related, str) and related in credit_ids else None
    kind = tx.get("type")
    if related is None and kind not in RETURN_TYPES:
        return []
    if not isinstance(kind, str):
        raise Malformed
    created_at = instant(tx.get("created_at"))
    completed_at = _optional_instant(tx.get("completed_at"))
    return [
        BankReturn(
            id=tx["id"],
            type=kind,
            amount=money(leg.get("amount"), leg.get("currency")),
            created_at=created_at,
            completed_at=completed_at,
            related_credit_id=related,
        )
        for leg in _legs(tx)
        if leg.get("account_id") in declared
    ]


def observations(
    items: list[dict], declared: frozenset[str], observed_at: datetime
) -> tuple[tuple[BankCredit, ...], tuple[BankReturn, ...]]:
    """Every credit, then every return -- a return may relate to a credit on any page."""
    credits = tuple(c for tx in items if (c := credit(tx, declared, observed_at)) is not None)
    ids = {c.transaction_id for c in credits}
    found = tuple(r for tx in items for r in returns(tx, declared, ids))
    return credits, found
