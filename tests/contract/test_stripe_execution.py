"""SC-002: an approved correction is made on Stripe once, end to end.

On the latest write generation: the duplicate dispute investigated with the
mock engine at the instant the seed recorded, its proposal approved as shown
and executed twice, makes exactly one credit note for the approved amount; a
refund executed through the service makes one refunding credit note. Runs
locally, with the writing key (see conftest).
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.application.execute_action import ApprovalService
from app.application.investigate_dispute import DisputeInvestigator
from app.decision.mock import MockDecisionEngine
from app.domain.enums import ActionState
from app.domain.models import Dispute
from app.domain.money import Money
from app.infrastructure.actions import SqliteActionStore
from evals.runner import CASES_PATH, header_pairs
from scripts import stripe_worlds as worlds

pytestmark = pytest.mark.contract

CASES = {c["id"]: c for c in json.loads(CASES_PATH.read_text(encoding="utf-8"))}


async def approved_and_executed_twice(provider, store, action_id):
    service = ApprovalService(provider, store)
    action = store.get(action_id)
    await service.approve(action_id, "contract", await service.proposal_fingerprint(action))
    first = await service.execute(action_id)
    second = await service.execute(action_id)
    return first, second


async def test_a_dispute_investigated_approved_and_executed_makes_one_credit_note(
    stripe_writer,
):
    customer = stripe_writer.duplicate
    target = customer["metadata"][worlds.TARGET_INVOICE]
    if not await stripe_writer.untouched(target):
        pytest.skip(f"invoice {target} already carries a credit note: run --write-run")
    case = CASES["duplicate_fr"]
    store = SqliteActionStore(":memory:")
    investigator = DisputeInvestigator(
        provider=stripe_writer.provider, engine=MockDecisionEngine(), actions=store
    )
    result = await investigator.investigate(
        Dispute(
            id="sc002_duplicate",
            customer_message=case["message"],
            customer_email=customer["email"],
            invoice_id=target,
            headers=header_pairs(case.get("headers")),
        ),
        now=datetime.fromisoformat(customer["metadata"][worlds.INVESTIGATE_AT]),
    )
    assert result.action_id is not None, "the duplicate should queue a correction"

    first, second = await approved_and_executed_twice(
        stripe_writer.provider, store, result.action_id
    )

    assert first.state is second.state is ActionState.EXECUTED
    assert second.provider_action_id == first.provider_action_id
    (note,) = await stripe_writer.credit_notes(target)
    assert note["id"] == first.provider_action_id
    assert note["amount"] == result.proposed_action.amount.amount_minor
    assert note["metadata"]["rb_action_id"] == first.action_id


async def test_a_refund_executed_through_the_service_makes_one_refunding_credit_note(
    stripe_writer,
):
    invoice = stripe_writer.invoices["execute_refund"]
    if not await stripe_writer.untouched(invoice):
        pytest.skip(f"invoice {invoice} already carries a credit note: run --write-run")
    store = SqliteActionStore(":memory:")
    action = store.create(
        run_id="sc002_refund",
        action_type="refund",
        invoice_id=invoice,
        amount=Money(amount_minor=worlds.WRITE_TOTAL, currency="EUR"),
        reason="contract",
        provider="stripe",
    )

    first, second = await approved_and_executed_twice(
        stripe_writer.provider, store, action.action_id
    )

    assert first.state is second.state is ActionState.EXECUTED
    assert first.allocation == "refund"
    (note,) = await stripe_writer.credit_notes(invoice)
    assert note["id"] == first.provider_action_id
    assert sum(r["amount_refunded"] for r in note["refunds"]) == worlds.WRITE_TOTAL
