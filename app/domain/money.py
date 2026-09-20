"""Money as integer minor units. See ADR-005.

Three rules, enforced here and nowhere else:

1. An amount is an integer count of minor units plus a currency. Never a float.
2. There is exactly one function that turns a decimal into minor units
   (:meth:`Money.from_decimal`), with one documented rounding mode.
3. Two amounts in different currencies are never combined or ordered. The
   domain raises instead of converting.

``Decimal`` remains legal for *quantities* and for intermediate proration
maths, but it must cross back into ``Money`` through ``from_decimal``.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator

from app.domain.exceptions import CurrencyMismatch, UnsupportedCurrency

#: Rounding mode for every decimal-to-minor-unit conversion in the system.
#: Half-up is what invoicing platforms use for customer-facing amounts; the
#: point is less which mode than that there is only one, applied in one place.
ROUNDING = ROUND_HALF_UP

_ISO_4217 = re.compile(r"^[A-Z]{3}$")

_DEFAULT_EXPONENT = 2

#: Currencies whose minor unit is not 1/100. Anything absent uses two decimals.
_EXPONENT_OVERRIDES: dict[str, int] = {
    # Zero-decimal currencies: the minor unit *is* the major unit.
    "JPY": 0,
    "KRW": 0,
    "VND": 0,
    "CLP": 0,
    "ISK": 0,
    # Three-decimal currencies.
    "BHD": 3,
    "JOD": 3,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
}


def exponent_for(currency: str) -> int:
    """Number of decimal places in `currency`.

    Hard-coding two decimals is the classic billing bug: ``JPY 1000`` means one
    thousand yen, not ten.
    """
    return _EXPONENT_OVERRIDES.get(currency.upper(), _DEFAULT_EXPONENT)


def _reject_float(value: object, what: str) -> None:
    if isinstance(value, float):
        raise TypeError(
            f"{what} must not be a float: use Decimal, int or str. "
            "Binary floats cannot represent 0.01 exactly (ADR-005)."
        )


class Money(BaseModel):
    """An exact amount of money.

    Immutable, so an amount can be shared across a decision, an action and an
    audit row without any of them mutating it.
    """

    model_config = ConfigDict(frozen=True)

    amount_minor: int
    currency: str

    @field_validator("currency")
    @classmethod
    def _normalise_currency(cls, value: str) -> str:
        upper = value.upper()
        if not _ISO_4217.match(upper):
            raise UnsupportedCurrency(value)
        return upper

    # -- construction ------------------------------------------------------

    @classmethod
    def zero(cls, currency: str) -> Self:
        return cls(amount_minor=0, currency=currency)

    @classmethod
    def from_decimal(cls, value: Decimal | int | str, currency: str) -> Self:
        """Convert a major-unit decimal into minor units.

        The single rounding point of the whole system. Every proration, every
        percentage, every provider payload in major units comes through here.
        """
        _reject_float(value, "a monetary value")
        exponent = exponent_for(currency)
        quantum = Decimal(1).scaleb(-exponent)
        rounded = Decimal(value).quantize(quantum, rounding=ROUNDING)
        return cls(amount_minor=int(rounded.scaleb(exponent)), currency=currency)

    # -- conversion --------------------------------------------------------

    def to_decimal(self) -> Decimal:
        """Exact major-unit value. For display and for intermediate maths."""
        return Decimal(self.amount_minor).scaleb(-exponent_for(self.currency))

    # -- arithmetic --------------------------------------------------------

    def _assert_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(self.currency, other.currency)

    def __add__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(amount_minor=self.amount_minor + other.amount_minor, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(amount_minor=self.amount_minor - other.amount_minor, currency=self.currency)

    def __neg__(self) -> Money:
        return Money(amount_minor=-self.amount_minor, currency=self.currency)

    def __abs__(self) -> Money:
        return Money(amount_minor=abs(self.amount_minor), currency=self.currency)

    def multiply(self, factor: Decimal | int | str) -> Money:
        """Scale by a ratio, rounding once through :meth:`from_decimal`.

        Used by proration. Takes no float, for the same reason as the rest.
        """
        _reject_float(factor, "a multiplication factor")
        return Money.from_decimal(self.to_decimal() * Decimal(factor), self.currency)

    # -- ordering ----------------------------------------------------------
    #
    # Ordering across currencies raises: "is 100 EUR more than 100 USD" has no
    # answer without a rate and a date, and guessing one inside a dispute would
    # invent money.
    #
    # Equality is deliberately *not* an error. Two amounts in different
    # currencies are simply not equal, which keeps Money usable in sets, dict
    # keys and `in` checks -- as the reply-draft guard needs.

    def __lt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.amount_minor < other.amount_minor

    def __le__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.amount_minor <= other.amount_minor

    def __gt__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.amount_minor > other.amount_minor

    def __ge__(self, other: Money) -> bool:
        self._assert_same_currency(other)
        return self.amount_minor >= other.amount_minor

    def __hash__(self) -> int:
        return hash((self.amount_minor, self.currency))

    # -- display -----------------------------------------------------------

    def __str__(self) -> str:
        exponent = exponent_for(self.currency)
        return f"{self.to_decimal():.{exponent}f} {self.currency}"
