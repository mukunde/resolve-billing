"""The worlds the Pennylane seed builds on the declared sandbox
(specs/014-pennylane-read, FR-011, SC-002 as amended). Shared by the seed and
the contract tests; nothing here talks to Pennylane."""

from __future__ import annotations

from dataclasses import dataclass

#: Every world's address is under this domain, reserved for tests (RFC 2606).
DOMAIN = "worlds.resolve-billing.test"


@dataclass(frozen=True)
class World:
    name: str
    #: "read" (read back field by field), "observation" (what Pennylane does,
    #: recorded), "refused" (read refused with `reason`), "compared" (equal to
    #: its fake `counterpart`, where the sandbox lets it be built).
    kind: str
    reason: str | None = None
    counterpart: str | None = None

    @property
    def email(self) -> str:
        return f"{self.name.replace('_', '-')}@{DOMAIN}"

    @property
    def reference(self) -> str:
        return f"rb:{self.name}"


WORLDS = (
    World("taxed_invoice", "read"),
    World("paid_invoice", "observation"),
    World("subscription", "observation"),
    World("credited_invoice", "refused", reason="invoice_credit"),
    World("two_addresses", "refused", reason="multiple_addresses"),
    World("mixed_vat", "refused", reason="tax_code"),
    World("invoice_discount", "refused", reason="invoice_discount"),
    World("fractional_unit_price", "refused", reason="fractional_unit_price"),
    World("taxed_duplicate", "compared", counterpart="taxed_duplicate_fr"),
    World("taxed_billed_after_cancellation", "compared",
          counterpart="taxed_billed_after_cancellation_en"),
)

#: Worlds the maintainer approved leaving out, each with why. Only an approved
#: exclusion enters here; a world neither built nor excluded fails readiness.
NOT_SEEDED: dict[str, str] = {}

#: What the read-back of `taxed_invoice` must find (what the seed writes).
TAXED_INVOICE = {
    "excluding_tax_minor": 10000,
    "tax_minor": 2000,
    "rate": "20",
    "total_minor": 12000,
}
