"""The Sandbox read-back prints structure only (specs/016-bank-evidence, SC-002)."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from app.domain.bank import BankCredit, BankRead, BankReturn, DeclaredAccount
from app.domain.money import Money
from scripts import revolut_readback

T = datetime(2026, 11, 15, tzinfo=UTC)
ARGV = [
    "--invoice-number", "RB-SC002-01", "--total", "49.00",
    "--other-number", "RB-SC002-02", "--issued-at", "2026-11-15T00:00:00Z",
]
ARGS = revolut_readback.arguments(ARGV)


def credit(tx: str, reference: str | None, amount: int = 4900) -> BankCredit:
    return BankCredit(account_id="acc-secret-id", transaction_id=tx, type="topup",
                      state="completed", observed_at=T,
                      amount=Money(amount_minor=amount, currency="EUR"),
                      created_at=T, completed_at=T, reference=reference)


def test_structure_only_never_a_reference_or_an_id():
    read = BankRead(
        accounts=(DeclaredAccount(id="acc-secret-id", currency="EUR"),),
        credits=(credit("tx-secret-1", "Facture RB-SC002-01 payer Dupont"),
                 credit("tx-secret-2", "RB-SC002-02", 2000),
                 credit("tx-secret-3", None)),
        returns=(BankReturn(id="tx-secret-4", type="topup_return",
                            amount=Money(amount_minor=-4900, currency="EUR"),
                            created_at=T, related_credit_id="tx-secret-1"),),
        window_start=T, window_end=T, margin_days=7, complete=True, coverage_established=True,
    )
    out = "\n".join(revolut_readback.describe(read, revolut_readback.invoices(ARGS)))
    for secret in ("Dupont", "Facture", "secret"):
        assert secret not in out
    assert "cites=['the invoice']" in out
    assert "cites=['the other number']" in out
    assert "reference=absent cites=[]" in out
    assert "amount=49 EUR" in out or "amount=49.00 EUR" in out
    assert "related_to_a_credit_read=True" in out


def test_it_refuses_the_seed_key(monkeypatch, capsys):
    monkeypatch.setenv("REVOLUT_SEED_KEY", "SEEDKEYVALUE")
    monkeypatch.setenv("REVOLUT_API_KEY", "k")
    monkeypatch.setenv("REVOLUT_SIGNING_KEY_PATH", "p")
    monkeypatch.setenv("REVOLUT_SANDBOX_ACCOUNT_ID", "a")
    assert revolut_readback.main([
        "--invoice-number", "RB-SC002-01", "--total", "49.00",
        "--other-number", "RB-SC002-02", "--issued-at", "2026-11-15T00:00:00Z",
    ]) == 2
    out = capsys.readouterr().out
    assert "seed_key_in_service_environment" in out
    assert "SEEDKEYVALUE" not in out


def test_a_bank_authored_type_or_state_prints_as_other():
    odd = BankReturn(id="r", type="SECRET-PAYER \x1b[31m", created_at=T,
                     amount=Money(amount_minor=-1, currency="EUR"), related_credit_id="tx")
    weird = credit("tx", None).model_copy(update={"state": "SECRET-STATE", "type": "SECRET-TYPE"})
    read = BankRead(accounts=(), credits=(weird,), returns=(odd,), window_start=T,
                    window_end=T, margin_days=7, complete=True)
    out = "\n".join(revolut_readback.describe(read, revolut_readback.invoices(ARGS)))
    assert "SECRET" not in out and "\x1b" not in out
    assert "type=other state=other" in out and "return type=other" in out


class FakeBank:
    def __init__(self):
        self.calls = []

    async def read(self, start, end, margin, now):
        self.calls.append((start, end, margin, now))
        return BankRead(accounts=(DeclaredAccount(id="a", currency="EUR"),), window_start=start,
                        window_end=end, margin_days=margin, complete=True,
                        coverage_established=True)


def test_a_read_through_the_composition_controlled_for_this_run(monkeypatch, capsys):
    from app.api import bank_config

    bank, seen = FakeBank(), {}

    def compose():
        seen.update(bank=os.environ.get("BANK_EVIDENCE"),
                    controlled=os.environ.get("REVOLUT_CONTROLLED_WORLDS"))
        return bank

    monkeypatch.delenv("BANK_EVIDENCE", raising=False)
    monkeypatch.delenv("REVOLUT_CONTROLLED_WORLDS", raising=False)
    monkeypatch.setattr(bank_config, "bank_evidence", compose)
    assert revolut_readback.main(ARGV) == 0
    assert seen == {"bank": "revolut", "controlled": "enabled"}
    ((start, end, margin, now),) = bank.calls
    assert start == T and margin == 7 and end == now and now >= T
    assert "coverage_established=True" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["transport", "malformed"])
def test_a_failure_prints_its_type_and_category_only(monkeypatch, capsys, failure):
    from app.api import bank_config
    from app.providers.bank_base import BankUnavailable

    class Failing:
        async def read(self, *args):
            if failure == "transport":
                raise OSError("SECRET-HOST SECRET-BODY")
            raise BankUnavailable("malformed_answer")

    monkeypatch.setattr(bank_config, "bank_evidence", Failing)
    assert revolut_readback.main(ARGV) == 1
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err
