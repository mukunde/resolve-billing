"""Which bank, if any, the service reads for evidence that a payment arrived
(specs/016-bank-evidence). None until a bank is configured: the investigation then runs
exactly as before. Filled by the Revolut adapter (`BANK_EVIDENCE=revolut`)."""

from __future__ import annotations

from app.providers.bank_base import BankEvidence


def bank_evidence() -> BankEvidence | None:
    return None
