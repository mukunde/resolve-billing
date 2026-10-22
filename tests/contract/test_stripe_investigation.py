"""SC-001: a dispute on a seeded Stripe world is investigated as on the fake.

Each supported world reproduces a development case. Its target invoice,
investigated with the mock engine at the instant the seed recorded, gives the
same findings, classification and proposed action as the case on the fake,
at the evaluation date. Each unsupported world decides nothing and names its
reason. Runs locally, with the read-only key (see conftest).
"""

from __future__ import annotations

import json

import pytest

from app.application.investigate_dispute import DisputeInvestigator
from app.decision.mock import MockDecisionEngine
from app.domain.models import Dispute
from app.providers.base import ProviderUnsupported
from app.providers.fake.adapter import FakeBillingProvider
from evals.runner import CASES_PATH, EVALUATION_DATE, header_pairs
from scripts import stripe_worlds as worlds

pytestmark = pytest.mark.contract

CASES = {c["id"]: c for c in json.loads(CASES_PATH.read_text(encoding="utf-8"))}


async def outcome(provider, case, email, invoice_id, now):
    dispute = Dispute(
        id=f"sc001_{case['id']}",
        customer_message=case["message"],
        customer_email=email,
        invoice_id=invoice_id,
        headers=header_pairs(case.get("headers")),
    )
    investigator = DisputeInvestigator(provider=provider, engine=MockDecisionEngine())
    result = await investigator.investigate(dispute, now=now)
    return (
        sorted(f.code.value for f in result.findings),
        result.classification,
        result.proposed_action.type,
    )


@pytest.mark.parametrize("world", worlds.SUPPORTED, ids=lambda w: w.name)
async def test_a_world_is_investigated_as_its_fake_counterpart(stripe_account, world):
    case = CASES[world.counterpart]
    seeded = stripe_account.seeded[world.name]

    on_fake = await outcome(
        FakeBillingProvider(), case, case["customer_email"], case["invoice_id"], EVALUATION_DATE
    )
    on_stripe = await outcome(
        stripe_account.provider, case, world.email, seeded.target_invoice, seeded.investigate_at
    )

    assert on_stripe == on_fake


@pytest.mark.parametrize("world", worlds.UNSUPPORTED, ids=lambda w: w.unsupported)
async def test_an_unsupported_world_decides_nothing_and_says_why(stripe_account, world):
    seeded = stripe_account.seeded[world.name]
    case = {"id": world.name, "message": "Pouvez-vous verifier ma facture ?"}
    with pytest.raises(ProviderUnsupported) as raised:
        await outcome(
            stripe_account.provider, case, world.email, seeded.target_invoice, seeded.investigate_at
        )
    assert raised.value.reason == world.unsupported
