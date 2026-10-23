"""Domain-level errors.

These describe violations of billing invariants, never transport or provider
failures. Provider errors are translated at the adapter boundary.
"""


class BillingDomainError(Exception):
    """Base class for every domain invariant violation."""


class CurrencyMismatch(BillingDomainError):
    """Two amounts in different currencies were combined or compared.

    Raised rather than converting: an implicit FX conversion inside a billing
    dispute would silently invent money. See ADR-005.
    """

    def __init__(self, left: str, right: str) -> None:
        super().__init__(
            f"cannot combine amounts in {left} and {right}: currency conversion must be explicit"
        )
        self.left = left
        self.right = right


class UnsupportedCurrency(BillingDomainError):
    """A currency code is not a well-formed ISO 4217 alphabetic code."""

    def __init__(self, currency: str) -> None:
        super().__init__(f"{currency!r} is not a valid ISO 4217 alphabetic code")
        self.currency = currency
