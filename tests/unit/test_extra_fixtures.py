"""RESOLVE_EXTRA_FIXTURES_DIR: a second fixture directory, appended, never substituted."""

import json
from decimal import Decimal

import pytest

from app.domain.enums import InvoiceStatus
from app.domain.money import Money
from app.providers.fake import fixtures
from app.providers.fake.fixtures import EXTRA_FIXTURES_ENV, FixtureError, load_all
from evals import check

CUSTOMER = {"id": "cus_extra", "email": "extra@example.test", "currency": "EUR"}
SUBSCRIPTION = {
    "id": "sub_extra",
    "customer_id": "cus_extra",
    "state": "active",
    "plan": "pro",
    "period_start": "2026-09-01T00:00:00",
    "period_end": "2026-10-01T00:00:00",
}
INVOICE = {
    "id": "inv_extra",
    "customer_id": "cus_extra",
    "subscription_id": "sub_extra",
    "amount": "49.00",
    "currency": "EUR",
    "state": "paid",
    "issued_at": "2026-09-01T09:00:00",
    "lines": [],
}
PAYMENT = {
    "id": "pay_extra",
    "invoice_id": "inv_extra",
    "amount": "49.00",
    "currency": "EUR",
    "state": "succeeded",
}
USAGE = {
    "subscription_id": "sub_extra",
    "metric": "api_calls",
    "quantity": "1000",
    "period_start": "2026-09-01T00:00:00",
    "period_end": "2026-10-01T00:00:00",
}
ONE_OF_EACH = {
    "customers.json": [CUSTOMER],
    "subscriptions.json": [SUBSCRIPTION],
    "invoices.json": [INVOICE],
    "payments.json": [PAYMENT],
    "usage.json": [USAGE],
}


@pytest.fixture(autouse=True)
def fresh_load(monkeypatch):
    """The variable is read on the first load, which is cached."""
    monkeypatch.delenv(EXTRA_FIXTURES_ENV, raising=False)
    load_all.cache_clear()
    yield
    load_all.cache_clear()


def write(directory, files):
    directory.mkdir(exist_ok=True)
    for name, entries in files.items():
        (directory / name).write_text(json.dumps(entries), encoding="utf-8")
    return directory


def counts(data):
    return (
        len(data.customers),
        len(data.subscriptions),
        len(data.invoices),
        len(data.payments),
        len(data.usage),
    )


class TestLoader:
    def test_without_the_variable_nothing_changes(self, monkeypatch):
        before = counts(load_all())
        monkeypatch.setenv(EXTRA_FIXTURES_ENV, "  ")
        load_all.cache_clear()
        assert counts(load_all()) == before

    def test_extra_entries_are_added_and_mapped(self, monkeypatch, tmp_path):
        before = counts(load_all())
        monkeypatch.setenv(EXTRA_FIXTURES_ENV, str(write(tmp_path / "extra", ONE_OF_EACH)))
        load_all.cache_clear()
        data = load_all()

        assert counts(data) == tuple(n + 1 for n in before)
        invoice = next(i for i in data.invoices if i.id == "inv_extra")
        assert invoice.total == Money(amount_minor=4900, currency="EUR")
        assert invoice.status is InvoiceStatus.PAID
        assert next(u for u in data.usage if u.subscription_id == "sub_extra").quantity == Decimal(
            "1000"
        )

    def test_a_missing_file_in_the_extra_directory_is_ignored(self, monkeypatch, tmp_path):
        before = counts(load_all())
        extra = write(tmp_path / "extra", {"customers.json": [CUSTOMER]})
        monkeypatch.setenv(EXTRA_FIXTURES_ENV, str(extra))
        load_all.cache_clear()
        assert counts(load_all()) == (before[0] + 1, *before[1:])

    def test_a_directory_that_does_not_exist_is_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv(EXTRA_FIXTURES_ENV, str(tmp_path / "nowhere"))
        with pytest.raises(FixtureError, match="not a directory"):
            load_all()

    def test_an_id_already_defined_is_an_error_naming_file_and_id(self, monkeypatch, tmp_path):
        clash = dict(INVOICE, id="inv_001")
        monkeypatch.setenv(
            EXTRA_FIXTURES_ENV, str(write(tmp_path / "extra", {"invoices.json": [clash]}))
        )
        with pytest.raises(FixtureError, match=r"invoices\.json: id 'inv_001'"):
            load_all()

    def test_an_id_repeated_inside_the_extra_directory_is_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            EXTRA_FIXTURES_ENV,
            str(write(tmp_path / "extra", {"payments.json": [PAYMENT, PAYMENT]})),
        )
        with pytest.raises(FixtureError, match="pay_extra"):
            load_all()

    def test_usage_has_no_id_and_is_only_appended(self, monkeypatch, tmp_path):
        before = counts(load_all())
        monkeypatch.setenv(
            EXTRA_FIXTURES_ENV, str(write(tmp_path / "extra", {"usage.json": [USAGE, USAGE]}))
        )
        load_all.cache_clear()
        assert counts(load_all())[4] == before[4] + 2

    def test_the_repository_fixtures_are_never_written(self, monkeypatch, tmp_path):
        snapshot = {p.name: p.read_bytes() for p in fixtures.FIXTURES_DIR.glob("*.json")}
        monkeypatch.setenv(EXTRA_FIXTURES_ENV, str(write(tmp_path / "extra", ONE_OF_EACH)))
        load_all.cache_clear()
        load_all()
        assert {p.name: p.read_bytes() for p in fixtures.FIXTURES_DIR.glob("*.json")} == snapshot


def case(**overrides):
    base = {
        "id": "extra_case",
        "why": "test",
        "message": "Ma facture de septembre ?",
        "customer_email": "extra@example.test",
        "invoice_id": "inv_extra",
        "expect_classification": "valid_invoice",
        "expect_action": "reply",
        "expect_human_approval": False,
        "expect_findings": [],
    }
    return base | overrides


class TestCheckOnly:
    """Plumbing only, totals only, no engine."""

    def run(self, monkeypatch, tmp_path, cases, files=ONE_OF_EACH):
        monkeypatch.setenv(EXTRA_FIXTURES_ENV, str(write(tmp_path / "extra", files)))
        load_all.cache_clear()
        path = tmp_path / "cases.json"
        path.write_text(json.dumps(cases), encoding="utf-8")
        return check.check(path)

    def test_a_case_whose_data_lives_in_the_extra_directory(self, monkeypatch, tmp_path, capsys):
        assert self.run(monkeypatch, tmp_path, [case()]) == 0
        out = capsys.readouterr().out
        assert "references as expected        : 1" in out
        assert "extra_case" not in out and "inv_extra" not in out

    def test_without_the_extra_data_the_references_do_not_fit(self, monkeypatch, tmp_path, capsys):
        assert self.run(monkeypatch, tmp_path, [case()], files={}) == 1
        assert "references not as expected    : 1" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "bad",
        [
            case(customer_email="nobody@example.test"),
            case(invoice_id="inv_001"),
            case(expect_findings=["UNKNOWN_INVOICE"]),
            case(expect_sender="unknown"),
        ],
        ids=["unknown-sender", "foreign-invoice", "unknown-invoice-expected", "sender-known"],
    )
    def test_references_that_contradict_the_expectations(self, monkeypatch, tmp_path, bad):
        assert self.run(monkeypatch, tmp_path, [bad]) == 1

    @pytest.mark.parametrize(
        "good",
        [
            case(invoice_id="inv_001", expect_findings=["UNKNOWN_INVOICE"]),
            case(customer_email="nobody@example.test", expect_sender="unknown"),
            case(invoice_id=None),
        ],
        ids=["foreign-invoice-expected", "unknown-sender-expected", "no-invoice"],
    )
    def test_references_that_match_the_expectations(self, monkeypatch, tmp_path, good):
        assert self.run(monkeypatch, tmp_path, [good]) == 0

    def test_missing_fields_and_duplicate_ids(self, monkeypatch, tmp_path, capsys):
        cases = [case(), case(), {"id": "half"}]
        assert self.run(monkeypatch, tmp_path, cases) == 1
        out = capsys.readouterr().out
        assert "cases missing a field         : 1" in out
        assert "duplicate case ids            : 1" in out

    def test_broken_fixtures_are_reported_not_raised(self, monkeypatch, tmp_path, capsys):
        clash = {"customers.json": [dict(CUSTOMER, id="cus_acme")]}
        assert self.run(monkeypatch, tmp_path, [case()], files=clash) == 1
        assert "fixtures                      : error" in capsys.readouterr().out

    def test_the_development_set_fits_its_fixtures(self, capsys):
        from evals.runner import CASES_PATH

        assert check.check(CASES_PATH) == 0


def test_no_holdout_file_in_this_repository():
    """Blind cases live outside the repository that every session reads."""
    from evals.runner import CASES_PATH

    assert not (CASES_PATH.parent / "holdout_cases.json").exists()
