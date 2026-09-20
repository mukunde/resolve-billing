"""ADR-005: integer minor units, one rounding function, no implicit FX."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.exceptions import CurrencyMismatch, UnsupportedCurrency
from app.domain.money import Money, exponent_for


class TestConstruction:
    def test_currency_is_normalised_to_uppercase(self):
        assert Money(amount_minor=9900, currency="eur").currency == "EUR"

    @pytest.mark.parametrize("bad", ["EURO", "E", "12A", ""])
    def test_invalid_currency_is_rejected(self, bad):
        with pytest.raises(UnsupportedCurrency):
            Money(amount_minor=1, currency=bad)

    def test_money_is_immutable(self):
        amount = Money(amount_minor=9900, currency="EUR")
        with pytest.raises(ValidationError):
            amount.amount_minor = 1


class TestRounding:
    """One conversion function, one documented mode: half-up."""

    @pytest.mark.parametrize(
        ("value", "expected_minor"),
        [
            ("99.00", 9900),
            ("99.004", 9900),
            ("99.005", 9901),  # half-up, not banker's rounding
            ("99.006", 9901),
            # Half-up rounds away from zero, so it is symmetric in sign: a
            # 99.005 credit and a 99.005 charge round to the same magnitude.
            ("-99.005", -9901),
            ("0", 0),
        ],
    )
    def test_from_decimal_rounds_half_up(self, value, expected_minor):
        assert Money.from_decimal(Decimal(value), "EUR").amount_minor == expected_minor

    def test_round_trip_through_decimal(self):
        amount = Money(amount_minor=9900, currency="EUR")
        assert amount.to_decimal() == Decimal("99.00")

    def test_float_is_refused(self):
        """0.1 + 0.2 != 0.3 has no place anywhere near an invoice."""
        with pytest.raises(TypeError, match="must not be a float"):
            Money.from_decimal(99.99, "EUR")

    def test_float_factor_is_refused(self):
        with pytest.raises(TypeError, match="must not be a float"):
            Money(amount_minor=9900, currency="EUR").multiply(0.5)


class TestCurrencyExponents:
    """Hard-coding two decimals is the classic billing bug."""

    @pytest.mark.parametrize(
        ("currency", "exponent"),
        [("EUR", 2), ("USD", 2), ("JPY", 0), ("KRW", 0), ("KWD", 3), ("BHD", 3)],
    )
    def test_known_exponents(self, currency, exponent):
        assert exponent_for(currency) == exponent

    def test_zero_decimal_currency_has_no_minor_unit(self):
        """1000 JPY is one thousand yen, not ten."""
        assert Money.from_decimal(Decimal("1000"), "JPY").amount_minor == 1000

    def test_three_decimal_currency(self):
        assert Money.from_decimal(Decimal("1.234"), "KWD").amount_minor == 1234

    def test_display_uses_the_currency_exponent(self):
        assert str(Money(amount_minor=9900, currency="EUR")) == "99.00 EUR"
        assert str(Money(amount_minor=1000, currency="JPY")) == "1000 JPY"


class TestArithmetic:
    def test_addition_and_subtraction(self):
        a = Money(amount_minor=9900, currency="EUR")
        b = Money(amount_minor=100, currency="EUR")
        assert (a + b).amount_minor == 10000
        assert (a - b).amount_minor == 9800

    def test_negation_and_absolute_value(self):
        a = Money(amount_minor=9900, currency="EUR")
        assert (-a).amount_minor == -9900
        assert abs(-a) == a

    def test_multiply_rounds_once(self):
        """A third of 100.00 EUR is 33.33, not 33.333333."""
        third = Money(amount_minor=10000, currency="EUR").multiply(Decimal(1) / Decimal(3))
        assert third.amount_minor == 3333


class TestCurrencyMismatch:
    """The rule that stops the system inventing money."""

    @pytest.fixture
    def eur(self):
        return Money(amount_minor=10000, currency="EUR")

    @pytest.fixture
    def usd(self):
        return Money(amount_minor=10000, currency="USD")

    def test_addition_raises(self, eur, usd):
        with pytest.raises(CurrencyMismatch):
            eur + usd

    def test_subtraction_raises(self, eur, usd):
        with pytest.raises(CurrencyMismatch):
            eur - usd

    @pytest.mark.parametrize("op", ["__lt__", "__le__", "__gt__", "__ge__"])
    def test_ordering_raises(self, eur, usd, op):
        """'Is 100 EUR more than 100 USD' has no answer without a rate and a date."""
        with pytest.raises(CurrencyMismatch):
            getattr(eur, op)(usd)

    def test_equality_is_false_rather_than_an_error(self, eur, usd):
        """Deliberate exception to the rule above.

        Raising on `==` would break sets, dict keys and `in` checks -- which
        the reply-draft guard relies on. Two amounts in different currencies
        are simply not equal.
        """
        assert eur != usd
        assert eur in {Money(amount_minor=10000, currency="EUR")}
