"""One way to build the investigator for an evaluation case (specs/016-bank-evidence).

A case may select a bank world from `fixtures/bank.json` with `bank_world`; a case with
none has no bank, so every case written before the bank existed runs exactly as before.
Used by `evals.runner` and `evals.hosted_run` alike, so both read a case the same way.
"""

from __future__ import annotations

from app.application.investigate_dispute import DisputeInvestigator
from app.providers.fake.bank import FakeBank


def investigator_for(case: dict, provider, engine) -> DisputeInvestigator:
    world = case.get("bank_world")
    bank = FakeBank(world) if world else None
    return DisputeInvestigator(provider=provider, engine=engine, bank=bank)
