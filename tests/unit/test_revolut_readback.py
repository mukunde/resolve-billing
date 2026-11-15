"""The Sandbox read-back prints structure only (specs/016-bank-evidence, SC-002)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.bank import BankCredit, BankRead, BankReturn, DeclaredAccount
from app.domain.money import Money
from scripts import revolut_readback

T = datetime(2026, 11, 15, tzinfo=UTC)
ARGS = revolut_readback.arguments([
    "--invoice-number", "RB-SC002-01", "--total", "49.00",
    "--other-number", "RB-SC002-02", "--issued-at", "2026-11-15T00:00:00Z",
])


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
