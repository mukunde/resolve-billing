"""Pennylane's configuration: the tokens, the declared sandbox, the errors.

The adapter reads with `PENNYLANE_API_TOKEN`; the seed writes with
`PENNYLANE_SEED_TOKEN`, never loaded by the adapter. Both may reach only the
company the maintainer declared, `PENNYLANE_SANDBOX_COMPANY_ID` (spec 014, Q2):
reading a real company is a later decision. No message carries a token.
"""

from __future__ import annotations

import os


class ProviderConfigurationError(Exception):
    """Pennylane cannot be used as configured: a missing or refused token, a
    missing scope, or a company other than the declared sandbox. Not an outage
    -- trying later changes nothing -- so it fails visibly, by category, never
    with the platform's text."""

    def __init__(self, category: str) -> None:
        super().__init__(f"pennylane configuration error: {category}")
        self.category = category


def token(variable: str) -> str:
    value = os.environ.get(variable)
    if not value:
        raise ProviderConfigurationError("missing_token")
    return value


def sandbox_company_id() -> int:
    value = os.environ.get("PENNYLANE_SANDBOX_COMPANY_ID", "")
    if not value.isdigit():
        raise ProviderConfigurationError("sandbox")
    return int(value)
