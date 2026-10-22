"""The worlds the seed script builds on a Stripe test account.

specs/001-stripe-read-adapter, plan section 6. Shared by the seed script and
the contract tests, so nothing here imports Stripe. A supported world
reproduces a development case, its fake counterpart (SC-001); an unsupported
one produces a reason the adapter refuses with (FR-002, FR-032).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Metadata the seed sets on each world's customer, read back by the tests:
#: the world's name, the invoice a dispute targets, and the instant to
#: investigate it at (right after that invoice is finalised).
WORLD = "rb_world"
TARGET_INVOICE = "rb_target_invoice"
INVESTIGATE_AT = "rb_investigate_at"

#: Every world's address is under this domain, reserved for tests (RFC 2606).
DOMAIN = "worlds.resolve-billing.test"


@dataclass(frozen=True)
class World:
    name: str
    #: The development case the world reproduces (evals/cases.json).
    counterpart: str | None = None
    #: The `ProviderUnsupported` reason reading the world produces.
    unsupported: str | None = None

    @property
    def email(self) -> str:
        return f"{self.name.replace('_', '-')}@{DOMAIN}"


SUPPORTED = (
    World("duplicate", counterpart="duplicate_fr"),
    World("proration", counterpart="proration"),
    World("billed_after_cancellation", counterpart="billed_after_cancellation"),
    World("failed_payment", counterpart="failed_payment_valid_invoice"),
    World("usage", counterpart="legitimate_overage"),
)

UNSUPPORTED = tuple(
    World(f"unsupported_{reason}", unsupported=reason)
    for reason in (
        "mixed_item_periods",
        "multiple_prices",
        "subscription_status",
        "invoice_tax",
        "invoice_discount",
        "negative_line",
        "fractional_unit_price",
        "partial_refund",
        "multi_invoice_payment",
        "unrepresentable_period",
    )
)

#: Reasons no world produces, and why: the mapping tests cover them alone.
NOT_SEEDED = {
    "cancellation_without_end": "a subscription the seed cancels is expected to carry `ended_at`.",
    "unplaced_payment": "an invoice payment without a payment intent is not built here.",
    "missing_field": "the seed fills every field the mapping needs; absences are mapping tests.",
    "invoice_credit": "billing credits are not built by the seed in this slice.",
}

WORLDS = SUPPORTED + UNSUPPORTED
