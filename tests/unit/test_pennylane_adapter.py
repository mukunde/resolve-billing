"""Pennylane behind the port, against a stub API (specs/014-pennylane-read)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.application.investigate_dispute import DisputeInvestigator
from app.decision.anonymise import anonymise
from app.decision.masking import mask_message
from app.decision.mock import MockDecisionEngine
from app.domain.enums import RuleCode
from app.domain.models import Dispute
from app.providers.base import (
    BillingProvider,
    NotFound,
    PlatformCannotWrite,
    ProviderUnsupported,
)
from app.providers.pennylane.keys import ProviderConfigurationError
from tests.unit import pennylane_stub as stub

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 15, tzinfo=UTC)


def test_it_is_a_billing_provider():
    assert isinstance(stub.provider(stub.Account()), BillingProvider)


class TestCustomers:
    async def test_found_by_any_address_trimmed_and_case_ignored(self):
        account = stub.duplicate_world()
        found = await stub.provider(account).find_customer_by_email("  LEA@vega-conseil.fr ")
        assert found.id == "pl_cus_7"

    async def test_a_match_on_a_second_address_is_refused(self):
        account = stub.Account(customers=[{"id": 7, "emails": ["a@x.fr", "lea@vega.fr"]}])
        with pytest.raises(ProviderUnsupported, match="multiple_addresses"):
            await stub.provider(account).find_customer_by_email("lea@vega.fr")

    async def test_an_unknown_address(self):
        assert await stub.provider(stub.duplicate_world()).find_customer_by_email("x@y.z") is None


class TestInvoices:
    async def test_the_window_after_the_date_conversion(self):
        account = stub.duplicate_world()
        found = await stub.provider(account).list_invoices(
            "pl_cus_7", since=datetime(2026, 9, 1, 12, tzinfo=UTC))
        assert [i.id for i in found] == ["pl_inv_601"]  # 2 Sept is in; 1 Sept, midnight, is not

    async def test_an_invoice_credited_by_an_old_credit_note_is_refused(self):
        account = stub.duplicate_world()
        account.invoices.append(stub.invoice(700, 7, "AV1", day="2025-01-01",
                                             status="cancelled", credited=601))
        with pytest.raises(ProviderUnsupported, match="invoice_credit"):
            await stub.provider(account).list_invoices("pl_cus_7", since=EPOCH)

    async def test_a_cancelled_subscription_needs_every_line_dated(self):
        account = stub.duplicate_world()
        account.subscriptions = [stub.subscription(9, 7, status="stopped")]
        account.lines[601] = [stub.line(2), stub.line(3, start=None, end=None)]
        account.invoices[1].update(currency_amount="240.00",
                                   currency_amount_before_tax="200.00", currency_tax="40.00")
        with pytest.raises(ProviderUnsupported, match="missing_field"):
            await stub.provider(account).list_invoices("pl_cus_7", since=EPOCH)

    async def test_a_quote_is_left_out_and_refused_when_named(self):
        account = stub.duplicate_world()
        account.invoices.append(stub.invoice(800, 7, "D1", status="estimate_pending"))
        found = await stub.provider(account).list_invoices("pl_cus_7", since=EPOCH)
        assert "pl_inv_800" not in {i.id for i in found}
        with pytest.raises(ProviderUnsupported, match="invoice_status"):
            await stub.provider(account).find_invoice("pl_inv_800")

    async def test_an_identifier_of_another_kind_is_not_sent(self):
        account = stub.duplicate_world()
        provider = stub.provider(account)
        assert await provider.find_invoice("pl_cus_600") is None
        assert await provider.find_invoice("F20260600") is None
        with pytest.raises(NotFound):
            await provider.get_invoice("in_123")
        assert all("customer_invoices" not in r for r in account.requests)

    async def test_whose_an_invoice_is_reads_the_invoice_alone(self):
        account = stub.duplicate_world()
        assert await stub.provider(account).invoice_owner("pl_inv_601") == "pl_cus_7"
        assert account.requests == ["/me", "/customer_invoices/601"]


class TestPayments:
    async def test_every_invoice_s_payments(self):
        found = await stub.provider(stub.duplicate_world()).list_payments("pl_cus_7")
        assert {(p.id, p.invoice_id) for p in found} == {("pl_pay_1", "pl_inv_600"),
                                                         ("pl_pay_2", "pl_inv_601")}

    async def test_one_payment_under_two_invoices_is_refused(self):
        account = stub.duplicate_world()
        account.payments[601] = account.payments[600]
        with pytest.raises(ProviderUnsupported, match="multi_invoice_payment"):
            await stub.provider(account).list_payments("pl_cus_7")


class TestTheCompanyCheck:
    @pytest.mark.parametrize("read", [
        lambda p: p.find_customer_by_email("lea@vega-conseil.fr"),
        lambda p: p.list_customers(),
        lambda p: p.get_customer("pl_cus_7"),
        lambda p: p.list_invoices("pl_cus_7", EPOCH),
        lambda p: p.find_invoice("pl_inv_601"),
        lambda p: p.find_invoice("pl_cus_1"),
        lambda p: p.invoice_owner("pl_inv_601"),
        lambda p: p.list_payments("pl_cus_7"),
        lambda p: p.list_subscriptions("pl_cus_7"),
        lambda p: p.get_usage("pl_sub_9", (EPOCH, NOW)),
        lambda p: p.find_action("key", "pl_inv_601"),
    ])
    async def test_another_company_refuses_every_read(self, read):
        account = stub.duplicate_world()
        account.company_id = 1
        with pytest.raises(ProviderConfigurationError, match="sandbox"):
            await read(stub.provider(account))
        assert account.requests == ["/me"]


class TestNoWrites:
    async def test_every_write_is_refused_before_any_call(self):
        account = stub.duplicate_world()
        provider = stub.provider(account)
        assert provider.can_write is False
        for write in (provider.plan_write("pl_inv_601", "credit_note"),
                      provider.create_credit_note("pl_inv_601", 1, "EUR", "x", "k",
                                                  action_id="a", allocation="reduce_due",
                                                  send_by=NOW),
                      provider.create_refund("pl_inv_601", 1, "EUR", "x", "k",
                                             action_id="a", allocation="refund", send_by=NOW)):
            with pytest.raises(PlatformCannotWrite):
                await write
        assert account.requests == []


class TestAnInvestigation:
    def dispute(self, invoice_id, message="Je crois que j'ai ete facture deux fois en septembre."):
        return Dispute(id="d", customer_message=message,
                       customer_email="lea@vega-conseil.fr", invoice_id=invoice_id)

    @pytest.mark.parametrize("named", ["pl_inv_601", "F20260601", "f20260601"])
    async def test_the_duplicate_found_whether_named_by_id_or_number(self, named):
        investigator = DisputeInvestigator(provider=stub.provider(stub.duplicate_world()),
                                           engine=MockDecisionEngine())
        result = await investigator.investigate(self.dispute(named), now=NOW)
        codes = {f.code for f in result.findings}
        assert RuleCode.DUPLICATE_INVOICE in codes
        assert RuleCode.UNKNOWN_INVOICE not in codes
        assert result.proposed_action.amount.amount_minor == 12000

    async def test_an_unresolved_number_is_an_unknown_reference_never_sent(self):
        account = stub.duplicate_world()
        investigator = DisputeInvestigator(provider=stub.provider(account),
                                           engine=MockDecisionEngine())
        result = await investigator.investigate(self.dispute("F99999"), now=NOW)
        assert RuleCode.UNKNOWN_INVOICE in {f.code for f in result.findings}
        assert not any("F99999" in r for r in account.requests)

    async def test_another_invoice_s_number_cited_is_a_conflict(self):
        investigator = DisputeInvestigator(provider=stub.provider(stub.duplicate_world()),
                                           engine=MockDecisionEngine())
        result = await investigator.investigate(
            self.dispute("pl_inv_601", "La facture F20260600 est fausse."), now=NOW)
        assert RuleCode.INVOICE_REFERENCE_CONFLICT in {f.code for f in result.findings}

    async def test_a_loaded_number_is_masked_before_any_engine(self):
        provider = stub.provider(stub.duplicate_world())
        investigator = DisputeInvestigator(provider=provider, engine=MockDecisionEngine())
        context = await investigator.load_context(self.dispute("pl_inv_601"), NOW)
        _, names = anonymise(context, [])
        masked = mask_message("Voir F20260600 et F20260601.", context, names,
                              provider.invoice_reference_spans)
        assert "F2026060" not in masked

    async def test_interleaved_investigations_keep_their_own_invoices(self):
        account = stub.duplicate_world()
        account.customers.append({"id": 8, "name": "Autre", "emails": ["b@autre.fr"]})
        account.invoices.append(stub.invoice(900, 8, "F20260600X"))
        account.lines[900] = [stub.line(9)]
        provider = stub.provider(account)
        investigator = DisputeInvestigator(provider=provider, engine=MockDecisionEngine())
        first = await investigator.load_context(self.dispute("pl_inv_601"), NOW)
        other = await investigator.load_context(
            Dispute(id="e", customer_message="x", customer_email="b@autre.fr",
                    invoice_id="pl_inv_900"), NOW)
        assert provider.invoice_references("F20260600", first.invoices) == {"pl_inv_600"}
        assert provider.invoice_references("F20260600", other.invoices) == set()
