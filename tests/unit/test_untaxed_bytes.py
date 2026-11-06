"""An untaxed invoice is read, fingerprinted and sent to the engine exactly as
before tax came into the domain (specs/013-invoice-tax, FR-008, FR-009, SC-002).

The snapshot was taken at `f5c3732`, before any change of spec 013: the hash of
each fixture invoice's dump (what the proposal fingerprint is built from) and
of the engine payload of every development case of that commit. Regenerate it
only on purpose, with the maintainer's approval: `python -m
tests.unit.test_untaxed_bytes` writes it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from app.decision.anonymise import anonymise
from app.decision.mock import MockDecisionEngine
from app.domain.models import Dispute
from app.providers.fake.adapter import FakeBillingProvider
from app.providers.fake.fixtures import load_all
from evals.runner import CASES_PATH, EVALUATION_DATE, header_pairs

SNAPSHOT = Path(__file__).parent / "snapshots" / "untaxed_bytes_f5c3732.json"


def _sha(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class _Recording(MockDecisionEngine):
    """The mock, recording what a hosted engine would have been sent."""

    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[dict] = []

    async def decide(self, dispute, context, findings):
        payload, _ = anonymise(context, findings)
        self.payloads.append(payload)
        return await super().decide(dispute, context, findings)


async def _payloads(case_ids: list[str] | None) -> dict[str, str]:
    from app.application.investigate_dispute import DisputeInvestigator

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if case_ids is not None:
        cases = [c for c in cases if c["id"] in case_ids]
    provider = FakeBillingProvider()
    engine = _Recording()
    investigator = DisputeInvestigator(provider=provider, engine=engine)
    hashes = {}
    for case in cases:
        engine.payloads.clear()
        dispute = Dispute(
            id=f"eval_{case['id']}",
            customer_message=case["message"],
            customer_email=case["customer_email"],
            customer_id=case.get("customer_id"),
            invoice_id=case["invoice_id"],
            headers=header_pairs(case.get("headers")),
        )
        await investigator.investigate(dispute, now=EVALUATION_DATE)
        hashes[case["id"]] = _sha(engine.payloads)
    return hashes


def capture(case_ids: list[str] | None = None) -> dict:
    invoices = {
        invoice.id: _sha(invoice.model_dump(mode="json")) for invoice in load_all().invoices
    }
    return {"invoices": invoices, "payloads": asyncio.run(_payloads(case_ids))}


def test_untaxed_invoices_and_payloads_are_unchanged():
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    now = capture(list(expected["payloads"]))
    assert {k: now["invoices"].get(k) for k in expected["invoices"]} == expected["invoices"]
    assert now["payloads"] == expected["payloads"]


if __name__ == "__main__":
    SNAPSHOT.write_text(json.dumps(capture(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
