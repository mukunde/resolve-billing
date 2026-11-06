"""What the engine receives for a taxed invoice (specs/013-invoice-tax, FR-009).
The untaxed side is `test_untaxed_bytes`."""

import json

from app.application.investigate_dispute import DisputeInvestigator
from app.decision.anonymise import anonymise
from app.decision.mock import MockDecisionEngine
from app.domain.models import Dispute
from app.providers.fake.adapter import FakeBillingProvider
from evals.runner import EVALUATION_DATE


async def test_a_taxed_invoice_reaches_the_engine_with_its_tax():
    investigator = DisputeInvestigator(provider=FakeBillingProvider(), engine=MockDecisionEngine())
    dispute = Dispute(
        id="d",
        customer_message="Facturé deux fois ?",
        customer_email="lea@vega-conseil.fr",
        invoice_id="inv_601",
    )
    context = await investigator.load_context(dispute, EVALUATION_DATE)
    payload, _ = anonymise(context, [])
    invoices = payload["invoices"]
    assert all(i["excluding_tax_minor"] == 10000 for i in invoices)
    assert all(i["taxes"] == [{"rate": "20", "amount_minor": 2000}] for i in invoices)
    assert all(i["total_minor"] == 12000 for i in invoices)
    assert invoices[0]["lines"][0]["taxes"] == [{"rate": "20", "amount_minor": 2000}]
    # Amounts and rates only: nothing that names the customer.
    assert "vega" not in json.dumps(payload).lower()
