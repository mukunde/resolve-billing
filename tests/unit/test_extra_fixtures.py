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

    def test_usage_is_one_record_per_subscription(self, monkeypatch, tmp_path):
        """The platform returns one usage record per subscription."""
        monkeypatch.setenv(
            EXTRA_FIXTURES_ENV, str(write(tmp_path / "extra", {"usage.json": [USAGE, USAGE]}))
        )
        with pytest.raises(FixtureError, match="subscription_id 'sub_extra'"):
            load_all()

    def test_usage_for_a_subscription_already_metered_is_an_error(self, monkeypatch, tmp_path):
        clash = dict(USAGE, subscription_id="sub_88")
        monkeypatch.setenv(
            EXTRA_FIXTURES_ENV, str(write(tmp_path / "extra", {"usage.json": [clash]}))
        )
        with pytest.raises(FixtureError, match="sub_88"):
            load_all()

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
            case(customer_email="nobody@elsewhere.test"),
            case(invoice_id="inv_001"),
            case(expect_findings=["UNKNOWN_INVOICE"]),
            case(expect_sender="unknown"),
            case(customer_email="nobody@elsewhere.test", expect_sender="suspected_impersonation",
                 invoice_id=None),
            case(customer_email="nobody@elsewhere.test", expect_sender="unknown"),
        ],
        ids=[
            "unknown-sender",
            "foreign-invoice",
            "unknown-invoice-expected",
            "sender-known",
            "impersonation-without-signal",
            "unknown-sender-citing-a-known-invoice",
        ],
    )
    def test_references_that_contradict_the_expectations(self, monkeypatch, tmp_path, bad):
        assert self.run(monkeypatch, tmp_path, [bad]) == 1

    @pytest.mark.parametrize(
        "good",
        [
            case(invoice_id="inv_001", expect_findings=["UNKNOWN_INVOICE"]),
            case(customer_email="nobody@elsewhere.test", expect_sender="unknown", invoice_id=None),
            case(customer_email="nobody@elsewhere.test", expect_sender="suspected_impersonation"),
            case(invoice_id=None),
        ],
        ids=[
            "foreign-invoice-expected",
            "unknown-sender-expected",
            "impersonation-by-known-invoice",
            "no-invoice",
        ],
    )
    def test_references_that_match_the_expectations(self, monkeypatch, tmp_path, good):
        assert self.run(monkeypatch, tmp_path, [good]) == 0

    def test_missing_fields_and_duplicate_ids(self, monkeypatch, tmp_path, capsys):
        cases = [case(), case(), {"id": "half"}]
        assert self.run(monkeypatch, tmp_path, cases) == 1
        out = capsys.readouterr().out
        assert "cases malformed               : 1" in out
        assert "duplicate case ids            : 1" in out

    @pytest.mark.parametrize(
        "bad",
        [
            case(id=["not", "hashable"]),
            case(customer_email=None),
            case(expect_findings=None),
            case(expect_findings=["NOT_A_RULE"]),
            case(expect_classification="refund_please"),
            case(expect_action="wire_transfer"),
            case(expect_human_approval="yes"),
            case(expect_sender="somebody"),
            case(expect_classification=["valid_invoice"]),
            case(expect_action={"type": "reply"}),
            case(expect_findings=[["UNKNOWN_INVOICE"]]),
            case(expect_sender=["unknown"]),
            case(headers={"Auto-Submitted": []}),
            case(headers={"Received": ["hop"] * 11}),
            case(headers=["Precedence", "bulk"]),
        ],
    )
    def test_mistyped_cases_are_counted_not_raised(self, monkeypatch, tmp_path, capsys, bad):
        assert self.run(monkeypatch, tmp_path, [bad]) == 1
        assert "cases malformed               : 1" in capsys.readouterr().out

    def test_an_invoice_on_another_customers_subscription(self, monkeypatch, tmp_path, capsys):
        crossed = dict(ONE_OF_EACH, **{"invoices.json": [dict(INVOICE, subscription_id="sub_77")]})
        assert self.run(monkeypatch, tmp_path, [case()], files=crossed) == 1
        assert "fixture links not resolved    : 1" in capsys.readouterr().out

    def test_an_orphan_link_between_fixtures(self, monkeypatch, tmp_path, capsys):
        orphan = dict(ONE_OF_EACH, **{"payments.json": [dict(PAYMENT, invoice_id="inv_ghost")]})
        assert self.run(monkeypatch, tmp_path, [case()], files=orphan) == 1
        assert "fixture links not resolved    : 1" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "files, category",
        [
            ({"customers.json": [dict(CUSTOMER, id="cus_acme")]}, "defined twice"),
            ({"invoices.json": [dict(INVOICE, amount="SECRET-MARKER")]}, "invalid entry"),
            ({"payments.json": [dict(PAYMENT, state="SECRET-MARKER")]}, "invalid entry"),
        ],
        ids=["duplicate", "bad-amount", "bad-state"],
    )
    def test_broken_fixtures_print_a_category_and_nothing_of_the_data(
        self, monkeypatch, tmp_path, capsys, files, category
    ):
        assert self.run(monkeypatch, tmp_path, [case()], files=files) == 1
        out, err = capsys.readouterr()
        assert "fixtures                      : error -- " in out and category in out
        for leak in ("SECRET-MARKER", "cus_acme", str(tmp_path)):
            assert leak not in out and leak not in err

    def test_a_missing_extra_directory(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv(EXTRA_FIXTURES_ENV, str(tmp_path / "nowhere"))
        path = tmp_path / "cases.json"
        path.write_text(json.dumps([case()]), encoding="utf-8")
        assert check.check(path) == 1
        out = capsys.readouterr().out
        assert "extra directory not found" in out and "nowhere" not in out

    def test_the_runner_calls_no_engine(self, monkeypatch, tmp_path, capsys):
        """`--check-only` returns before any engine exists or any result is written."""
        from evals import runner

        def no_run(*args, **kwargs):
            raise AssertionError("an engine run was started")

        monkeypatch.setattr(runner, "run", no_run)
        monkeypatch.setenv(EXTRA_FIXTURES_ENV, str(write(tmp_path / "extra", ONE_OF_EACH)))
        load_all.cache_clear()
        path = tmp_path / "cases.json"
        path.write_text(json.dumps([case()]), encoding="utf-8")
        out_json = tmp_path / "out.json"
        monkeypatch.setattr(
            "sys.argv",
            ["runner", "--engine", "claude", "--check-only", "--cases", str(path),
             "--json", str(out_json)],
        )
        assert runner.main() == 0
        assert not out_json.exists()

    def test_the_development_set_fits_its_fixtures(self, capsys):
        from evals.runner import CASES_PATH

        assert check.check(CASES_PATH) == 0


def test_no_holdout_file_in_this_repository():
    """Blind cases live outside the repository that every session reads."""
    from evals.runner import CASES_PATH

    assert not (CASES_PATH.parent / "holdout_cases.json").exists()
