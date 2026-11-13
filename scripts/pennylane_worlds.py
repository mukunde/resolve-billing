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
    # Attempted and reported: the documented API makes no partial payment and no
    # charge-back, so these need the maintainer's exclusion once reported.
    World("partially_paid", "refused", reason="invoice_status"),
    World("charged_back", "refused", reason="payment_status"),
    World("taxed_duplicate", "compared", counterpart="taxed_duplicate_fr"),
    World("taxed_billed_after_cancellation", "compared",
          counterpart="taxed_billed_after_cancellation_en"),
)

#: Worlds the maintainer approved leaving out, each with why. Only an approved
#: exclusion enters here; a world neither built nor excluded fails readiness.
#: Approved on 2026-11-12, after the seed reported each on the sandbox.
NOT_SEEDED: dict[str, str] = {
    "mixed_vat": "Pennylane refuses a mixed VAT rate on a customer invoice (422: only "
                 "supplier invoices can have one); the refusal stays for a code it may send",
    "partially_paid": "the documented API marks an invoice paid in full only",
    "charged_back": "the documented API cannot make a charged-back payment",
    "taxed_duplicate": "no documented way to attach a hand-made invoice to a subscription "
                       "(SC-002 as amended)",
    "taxed_billed_after_cancellation": "a subscription's invoice issued after its stop is not "
                                       "controllable through the documented API (SC-002 as "
                                       "amended)",
}

#: What the seed writes on every invoice it makes, and the read-back of
#: `taxed_invoice` must find: fixed dates, so a reader can check them exactly.
TAXED_INVOICE = {
    "date": "2026-10-01",
    "deadline": "2026-10-31",
    "start_date": "2026-10-01",
    "end_date": "2026-10-31",
    "quantity": "1",
    "unit_minor": 10000,
    "excluding_tax_minor": 10000,
    "tax_minor": 2000,
    "rate": "20",
    "total_minor": 12000,
}

#: The write contract's worlds (specs/015-pennylane-credit-notes, SC-002). A run of
#: the contract consumes them -- a credited invoice is refused afterwards -- so the
#: seed builds them afresh, one generation at a time (`--write-run`). Each line:
#: (unit price excluding tax, Pennylane's VAT code, quantity); then whether paid.
WRITE_WORLDS: dict[str, tuple[tuple[tuple[str, str, int], ...], bool]] = {
    "open": ((("100.00", "FR_200", 1),), False),
    "paid": ((("100.00", "FR_200", 1),), True),
    # Mixed rates, an exempt line and rounding edges (5.5 % of 33.33; 0.05 at 20 %).
    "mixed": ((("33.33", "FR_55", 1), ("19.99", "FR_200", 3), ("10.00", "exempt", 1),
               ("0.05", "FR_200", 1)), False),
    "untaxed": ((("100.00", "exempt", 1),), False),
    # For a credit beyond the total, refused at linking.
    "cap": ((("100.00", "exempt", 1),), False),
}


def write_world(generation: int, name: str) -> World:
    return World(f"w{generation}_{name}", "write")
