"""Spec 016's foundation (plan section 1): the domain recogniser, printed numbers on every
platform, the bank types and the fake bank, the investigator's optional bank."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.domain.bank import BankCheck, BankCredit, DeclaredAccount
from app.domain.models import Invoice
from app.domain.money import Money
from app.domain.references import cited_invoices, merge_spans, references
from app.providers.fake.adapter import FakeBillingProvider
from app.providers.fake.bank import FakeBank

T = datetime(2026, 10, 1, tzinfo=UTC)


def invoice(id_: str, number: str | None = None) -> Invoice:
    return Invoice(id=id_, customer_id="cus_1", subscription_id=None,
                   total=Money(amount_minor=4900, currency="EUR"),
                   status="open", issued_at=T, lines=[], number=number)


class TestCitedInvoices:
    def test_a_printed_number_and_an_id_are_both_cited(self):
        loaded = [invoice("inv_1", "F-2026-11-16"), invoice("inv_2")]
        assert cited_invoices("Paiement F-2026-11-16 et inv_2", loaded) == {"inv_1", "inv_2"}

    def test_an_invoice_not_loaded_is_not_cited(self):
        assert cited_invoices("Paiement F-2026-11-99", [invoice("inv_1", "F-2026-11-16")]) == set()

    def test_case_folded_and_bounded(self):
        loaded = [invoice("inv_1", "F-2026-11-16")]
        assert cited_invoices("f-2026-11-16", loaded) == {"inv_1"}
        assert cited_invoices("XF-2026-11-16", loaded) == set()

    def test_no_text_cites_nothing(self):
        assert cited_invoices(None, [invoice("inv_1", "F-1")]) == set()

    def test_merge_keeps_the_longest_where_spans_overlap(self):
        assert merge_spans([(0, 5, "a")], [(0, 8, "b")]) == [(0, 8, "b")]


def test_pennylane_still_reads_through_the_moved_recogniser():
    from app.providers.pennylane import mapping

    assert mapping.references is references


class TestPrintedNumbers:
    def test_the_fake_recognises_and_masks_a_printed_number(self):
        fake = FakeBillingProvider()
        loaded = [invoice("inv_900", "RB-0042")]
        spans = fake.invoice_reference_spans("J'ai payé RB-0042", loaded)
        assert spans == [(10, 17, "inv_900")]
        assert fake.invoice_references("J'ai payé RB-0042", loaded) == {"inv_900"}

    def test_the_fake_still_reads_its_ids_without_invoices(self):
        assert FakeBillingProvider().invoice_references("voir inv_060") == {"inv_060"}

    def test_stripe_maps_the_printed_number(self):
        from app.providers.stripe import mapping
        from tests.unit import stripe_payloads as stripe

        prices = {stripe.PRICE: stripe.price()}
        assert mapping.invoice(stripe.invoice(number="ABCD-0001"), prices, {}).number == (
            "ABCD-0001")
        assert mapping.invoice(stripe.invoice(number=None), prices, {}).number is None

    def test_stripe_masks_a_printed_number_beside_its_ids(self):
        from app.providers.stripe.adapter import StripeBillingProvider

        provider = StripeBillingProvider(object())
        loaded = [invoice("in_1AbC", "ABCD-0001")]
        spans = provider.invoice_reference_spans("payé ABCD-0001", loaded)
        assert (5, 14, "in_1AbC") in spans


class TestFakeBank:
    def world(self, tmp_path, **extra):
        data = {"worlds": {"w": {
            "accounts": [{"id": "acc", "currency": "EUR", "visible": True}],
            "credits": [{"account_id": "acc", "transaction_id": "tx_1", "type": "transfer",
                         "state": "completed", "amount": 4900, "currency": "EUR",
                         "created_at": "2026-10-03T09:00:00Z",
                         "completed_at": "2026-10-03T09:00:01Z",
                         "reference": "inv_060", "origin": "external"},
                        {"account_id": "acc", "transaction_id": "tx_old", "type": "topup",
                         "state": "completed", "amount": 100, "currency": "EUR",
                         "created_at": "2026-01-01T00:00:00Z"}],
            "controlled": True, **extra}}}
        path = tmp_path / "bank.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return FakeBank("w", path)

    async def test_it_reads_by_creation_inside_the_margin(self, tmp_path):
        read = await self.world(tmp_path).read(
            T, datetime(2026, 10, 31, tzinfo=UTC), 7, datetime(2026, 11, 1, tzinfo=UTC))
        assert [c.transaction_id for c in read.credits] == ["tx_1"]
        assert read.complete and read.coverage_established
        assert read.credits[0].reference == "inv_060"

    async def test_an_incomplete_or_invisible_world_is_said_so(self, tmp_path):
        bank = self.world(tmp_path, incomplete=["page_boundary_tie"],
                          accounts=[{"id": "acc", "currency": "EUR", "visible": False}])
        read = await bank.read(T, T, 7, T)
        assert not read.complete
        assert set(read.incompleteness) == {"page_boundary_tie", "account_not_visible"}


def test_a_credit_s_reference_is_never_serialised():
    credit = BankCredit(account_id="acc", transaction_id="tx", type="topup", state="completed",
                        observed_at=T, amount=Money(amount_minor=1, currency="EUR"),
                        created_at=T, reference="secret payer text")
    assert "secret" not in json.dumps(credit.model_dump(mode="json"))
    assert "secret" not in repr(credit)


def test_a_bank_check_round_trips():
    check = BankCheck(outcome="not_run", reason="no_bank",
                      accounts=(DeclaredAccount(id="acc", currency="EUR"),))
    assert BankCheck.from_record(check.to_record()) == check
    assert BankCheck.from_record(None) is None


class TestTheInvestigatorAndTheEvaluations:
    def test_the_investigator_takes_an_optional_bank(self):
        from app.application.investigate_dispute import DisputeInvestigator
        from app.decision.mock import MockDecisionEngine

        investigator = DisputeInvestigator(FakeBillingProvider(), MockDecisionEngine())
        assert investigator._bank is None

    def test_a_case_without_a_bank_world_has_no_bank(self):
        from app.decision.mock import MockDecisionEngine
        from evals.investigators import investigator_for

        built = investigator_for({"id": "x"}, FakeBillingProvider(), MockDecisionEngine())
        assert built._bank is None

    def test_the_bank_config_gives_none_until_a_bank_is_wired(self):
        from app.api.bank_config import bank_evidence

        assert bank_evidence() is None

    def test_the_bank_record_is_not_in_the_api_answer(self):
        from app.application.investigate_dispute import InvestigationResult

        assert InvestigationResult.model_fields["bank_check"].exclude is True
