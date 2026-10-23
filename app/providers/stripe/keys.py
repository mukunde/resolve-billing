"""Which Stripe keys may be used, and the configuration errors.

The adapter takes a restricted test key (`rk_test_...`) and nothing else; the
seed script, the secret test key (`sk_test_...`). Both refuse a live key, and
both checks run before any call -- before the SDK is even loaded. A key's
permissions cannot be read from the key itself: the maintainer creates the
adapter's with read permissions only, and the adapter calls no mutating
endpoint whatever the key allows (FR-021). No message carries a key.
"""

from __future__ import annotations


class ProviderConfigurationError(Exception):
    """The platform cannot be used as configured: a wrong key, or one Stripe
    refuses (authentication) or limits (permission). Not an outage -- trying
    later changes nothing -- so it fails the request visibly, by category,
    never with the platform's text (FR-020)."""

    def __init__(self, category: str) -> None:
        super().__init__(f"stripe configuration error: {category}")
        self.category = category


def check_read_key(key: str | None) -> None:
    """The adapter's key: restricted, test mode."""
    _refuse_missing_or_live(key)
    if key.startswith("sk_test_"):
        raise ProviderConfigurationError("secret_key")
    if not key.startswith("rk_test_"):
        raise ProviderConfigurationError("not_a_restricted_test_key")


def check_seed_key(key: str | None) -> None:
    """The seed script's key: secret, test mode."""
    _refuse_missing_or_live(key)
    if not key.startswith("sk_test_"):
        raise ProviderConfigurationError("not_a_secret_test_key")


def _refuse_missing_or_live(key: str | None) -> None:
    if not key:
        raise ProviderConfigurationError("missing_key")
    if "_live_" in key:
        raise ProviderConfigurationError("live_key")
