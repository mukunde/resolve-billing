"""Pennylane's configuration: the tokens, the declared sandbox, the errors.

The adapter reads with `PENNYLANE_API_TOKEN`; it writes credit notes only when
`PENNYLANE_WRITES` is exactly `enabled`, with `PENNYLANE_WRITE_TOKEN`, loaded
for writes alone (specs/015-pennylane-credit-notes, FR-002). The seed writes
with `PENNYLANE_SEED_TOKEN`, never loaded by the adapter. All may reach only
the company the maintainer declared, `PENNYLANE_SANDBOX_COMPANY_ID` (spec 014,
Q2): a real company is a later decision. No message carries a token.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.providers.pennylane.client import PennylaneWriter


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


def writes_enabled() -> bool:
    return os.environ.get("PENNYLANE_WRITES") == "enabled"


def writer() -> PennylaneWriter | None:
    """The writer when writes are enabled, by this exact word; else None,
    and the platform writes nothing. Enabled without its token fails
    visibly, never falling back to another token."""
    if not writes_enabled():
        return None
    from app.providers.pennylane.client import PennylaneWriter

    return PennylaneWriter(token("PENNYLANE_WRITE_TOKEN"), sandbox_company_id())
