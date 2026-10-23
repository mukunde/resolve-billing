"""An invoice the dispute cites but the customer's window does not hold.

Only whose it is gets extracted (ADR-001, 2026-10-22). So another customer's
invoice that the platform cannot represent -- taxed, say -- answers exactly as
one it can: unknown to this customer. Nothing of its content is mapped, so
nothing of it can surface, not even as a refusal.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.application.investigate_dispute import DisputeInvestigator
from app.decision.mock import MockDecisionEngine
from app.domain.models import Dispute
from app.providers.base import ProviderUnsupported
from app.providers.fake.adapter import FakeBillingProvider

NOW = datetime(2026, 10, 15, tzinfo=UTC)


class Taxed(FakeBillingProvider):
    """Holds inv_010 (Transports Nord's) and inv_299 (Sirius', old) with data
    the domain cannot represent; records every read that maps an invoice."""

    def __init__(self) -> None:
        super().__init__()
        self.full_reads: list[str] = []

    async def find_invoice(self, invoice_id):
        self.full_reads.append(invoice_id)
        if invoice_id in ("inv_010", "inv_299"):
            raise ProviderUnsupported("invoice_tax")
        return await super().find_invoice(invoice_id)

    async def get_invoice(self, invoice_id):
        self.full_reads.append(invoice_id)
        return await super().get_invoice(invoice_id)


async def investigate(provider, email: str, invoice_id: str):
    dispute = Dispute(
        id="dsp_foreign",
        customer_message=f"La facture {invoice_id} est fausse.",
        customer_email=email,
        invoice_id=invoice_id,
    )
    investigator = DisputeInvestigator(provider=provider, engine=MockDecisionEngine())
    return await investigator.investigate(dispute, now=NOW)


def codes(result) -> set[str]:
    return {f.code.value for f in result.findings}


async def test_another_customers_unrepresentable_invoice_is_unknown_like_any_other():
    taxed, plain = Taxed(), FakeBillingProvider()
    on_taxed = await investigate(taxed, "alice@acme.fr", "inv_010")
    on_plain = await investigate(plain, "alice@acme.fr", "inv_010")
    assert "UNKNOWN_INVOICE" in codes(on_taxed)
    assert codes(on_taxed) == codes(on_plain)
    assert on_taxed.proposed_action == on_plain.proposed_action
    assert taxed.full_reads == []


async def test_the_customers_own_old_invoice_is_outside_the_window_without_being_read():
    taxed = Taxed()
    result = await investigate(taxed, "compta@sirius-labs.fr", "inv_299")
    assert codes(result) == {"INVOICE_OUTSIDE_WINDOW"}
    assert taxed.full_reads == []


async def test_an_unknown_sender_citing_it_is_still_reaching_for_that_account():
    taxed = Taxed()
    result = await investigate(taxed, "jean.perso@gmail.com", "inv_010")
    impersonation = next(f for f in result.findings if f.code.value == "SUSPECTED_IMPERSONATION")
    assert {"signal": "known_invoice", "customer_id": "cus_nord", "invoice_id": "inv_010"} in (
        impersonation.evidence["signals"]
    )
    assert taxed.full_reads == []


async def test_an_invoice_nobody_holds_is_unknown():
    result = await investigate(Taxed(), "alice@acme.fr", "inv_999")
    assert "UNKNOWN_INVOICE" in codes(result)
