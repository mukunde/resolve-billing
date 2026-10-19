"""Taxonomy S7, S8, S10: when the evidence cannot settle a dispute, say so."""

import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.application.investigate_dispute import DisputeInvestigator
from app.decision.base import EngineUnavailable
from app.decision.mock import MockDecisionEngine
from app.domain.enums import RuleCode
from app.domain.models import BillingContext, Dispute, Usage
from app.domain.money import Money
from app.main import app
from app.providers.base import ProviderUnavailable
from app.providers.fake.adapter import FakeBillingProvider
from tests.integration._auth import SERVICE


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers=SERVICE)


def investigate(client: TestClient, **payload) -> dict:
    response = client.post("/api/v1/disputes/investigate", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


NOW = datetime(2026, 10, 15, tzinfo=UTC)


def investigator() -> DisputeInvestigator:
    return DisputeInvestigator(provider=FakeBillingProvider(), engine=MockDecisionEngine())


async def run(message: str, email: str, invoice_id: str | None, now=NOW):
    dispute = Dispute(
        id="dsp_test", customer_message=message, customer_email=email, invoice_id=invoice_id
    )
    return await investigator().investigate(dispute, now=now)


class TestOldInvoice:
    """At a fixed date: the window is 180 days back from "now"."""

    async def test_the_customers_old_invoice_goes_to_a_person(self):
        result = await run("La facture inv_299 etait fausse.", "compta@sirius-labs.fr", "inv_299")
        assert [f.code.value for f in result.findings] == ["INVOICE_OUTSIDE_WINDOW"]
        assert result.decision_engine == "none"
        assert result.requires_human_approval is True
        assert "fenêtre d'analyse" in result.authorization.reason

    async def test_inside_the_window_it_is_investigated(self):
        result = await run(
            "La facture inv_299 etait fausse.",
            "compta@sirius-labs.fr",
            "inv_299",
            now=datetime(2026, 3, 1, tzinfo=UTC),
        )
        assert "INVOICE_OUTSIDE_WINDOW" not in {f.code.value for f in result.findings}
        assert result.decision_engine == "mock"

    async def test_another_customers_invoice_is_still_unknown(self):
        """Looked up directly, but nothing about another account is kept."""
        result = await run("Ma facture inv_299 est fausse.", "alice@acme.fr", "inv_299")
        codes = {f.code.value for f in result.findings}
        assert "UNKNOWN_INVOICE" in codes
        assert "INVOICE_OUTSIDE_WINDOW" not in codes
        assert "sirius" not in result.explanation.lower()

    async def test_a_reference_that_exists_nowhere_is_still_unknown(self):
        result = await run("Ma facture inv_999 est fausse.", "alice@acme.fr", "inv_999")
        assert "UNKNOWN_INVOICE" in {f.code.value for f in result.findings}


class TestCurrencies:
    def test_a_duplicate_across_currencies_is_not_concluded(self, client):
        body = investigate(
            client,
            message="Facturés deux fois pour septembre, en euros et en dollars.",
            customer_email="compta@sirius-labs.fr",
            invoice_id="inv_301",
        )
        assert "CURRENCY_MISMATCH" in body["findings"]
        assert "DUPLICATE_INVOICE" not in body["findings"]
        assert body["decision_engine"] == "none"
        assert body["proposed_action"]["type"] == "none"
        assert body["requires_human_approval"] is True

    def test_another_currency_outside_the_comparison_is_not_an_anomaly(self):
        """A multi-currency account investigates normally: only the
        comparisons the disputed invoice needs are checked."""
        data = FakeBillingProvider()
        sirius = next(c for c in data._customers if c.id == "cus_sirius")
        september = next(i for i in data._invoices if i.id == "inv_300")
        august = [
            line.model_copy(
                update={
                    "period_start": datetime(2026, 8, 1, tzinfo=UTC),
                    "period_end": datetime(2026, 9, 1, tzinfo=UTC),
                }
            )
            for line in september.lines
        ]
        august_usd = september.model_copy(
            update={
                "id": "inv_302",
                "total": Money(amount_minor=4900, currency="USD"),
                "lines": august,
            }
        )
        context = BillingContext(
            customer=sirius, invoices=[september, august_usd], target_invoice_id="inv_300"
        )
        dispute = Dispute(id="dsp", customer_message="Facture ?", invoice_id="inv_300")
        codes = {f.code for f in investigator().run_rules(dispute, context)}
        assert RuleCode.CURRENCY_MISMATCH not in codes


class TestUsageNobodyChecked:
    def test_a_metered_line_without_usage_is_said_and_reviewed(self, client):
        body = investigate(
            client,
            message="Nous n'avons pas fait autant d'appels en septembre.",
            customer_email="facturation@meridien-data.fr",
            invoice_id="inv_310",
        )
        assert "USAGE_UNAVAILABLE" in body["findings"]
        assert body["classification"] == "valid_invoice"
        assert body["requires_human_approval"] is True
        assert body["automatic_reply_allowed"] is False


class Outage:
    name = "outage"
    version = "0"

    async def decide(self, dispute, context, findings):
        raise EngineUnavailable("APITimeoutError")


class TestEngineOutage:
    def test_an_unreachable_engine_sends_the_dispute_to_a_person(self, client):
        app.dependency_overrides[deps.get_investigator] = lambda: DisputeInvestigator(
            provider=deps.get_provider(),
            engine=Outage(),
            audit=deps.get_audit(),
            actions=deps.get_actions(),
        )
        try:
            body = investigate(
                client,
                message="Je crois que j'ai été facturé deux fois en septembre.",
                customer_email="alice@acme.fr",
                invoice_id="inv_002",
            )
        finally:
            app.dependency_overrides.pop(deps.get_investigator, None)
        assert body["classification"] == "unknown"
        assert body["proposed_action"]["type"] == "none"
        assert body["requires_human_approval"] is True
        assert "indisponible" in body["explanation"]
        trace = client.get(f"/api/v1/runs/{body['run_id']}").json()
        kinds = [e["event"] for e in trace.get("events", [])]
        assert "decision_engine_unavailable" in kinds
        assert "decision_created" not in kinds  # no answer came: none is recorded


class TestProviderOutage:
    def test_a_failed_read_decides_nothing_and_says_so(self, client, monkeypatch, caplog):
        provider = deps.get_provider()

        async def down(*args, **kwargs):
            raise ProviderUnavailable("GET https://billing.example.test/v1?token=SECRET")

        monkeypatch.setattr(provider, "list_invoices", down)
        caplog.set_level(logging.WARNING)
        response = client.post(
            "/api/v1/disputes/investigate",
            json={
                "message": "Facture de septembre en double ?",
                "customer_email": "alice@acme.fr",
                "invoice_id": "inv_002",
            },
        )
        assert response.status_code == 503
        assert "nothing was decided" in response.json()["detail"]
        logged = [r.getMessage() for r in caplog.records if "unavailable" in r.getMessage()]
        assert logged, "the outage is recorded"
        assert all("SECRET" not in m and "https://" not in m for m in logged)
        assert "error=ProviderUnavailable" in logged[0]


class TestUsageLineByLine:
    """Every metered line is compared, or said not to be (taxonomy S7)."""

    def invoice(self):
        return next(i for i in FakeBillingProvider()._invoices if i.id == "inv_310")

    def metered(self, metric):
        line = self.invoice().lines[0]
        return line.model_copy(update={"metric": metric, "quantity": Decimal("1200")})

    def usage(self, metric):
        return Usage(
            subscription_id="sub_310",
            metric=metric,
            quantity=Decimal("1200"),
            period_start=datetime(2026, 9, 1, tzinfo=UTC),
            period_end=datetime(2026, 10, 1, tzinfo=UTC),
        )

    def findings(self, lines, usage):
        data = FakeBillingProvider()
        meridien = next(c for c in data._customers if c.id == "cus_meridien")
        target = self.invoice().model_copy(update={"lines": lines})
        context = BillingContext(
            customer=meridien, invoices=[target], usage=usage, target_invoice_id=target.id
        )
        dispute = Dispute(id="dsp", customer_message="Usage ?", invoice_id=target.id)
        return {f.code: f for f in investigator().run_rules(dispute, context)}

    def test_usage_for_another_metric_is_not_a_check(self):
        found = self.findings([self.metered("api_calls")], self.usage("storage_gb"))
        assert found[RuleCode.USAGE_UNAVAILABLE].evidence["metrics"] == ["api_calls"]

    def test_a_second_metered_line_without_usage_is_said(self):
        lines = [self.metered("api_calls"), self.metered("storage_gb")]
        found = self.findings(lines, self.usage("api_calls"))
        assert found[RuleCode.USAGE_UNAVAILABLE].evidence["metrics"] == ["storage_gb"]

    def test_a_checked_line_is_not_reported_unavailable(self):
        found = self.findings([self.metered("api_calls")], self.usage("api_calls"))
        assert RuleCode.USAGE_UNAVAILABLE not in found

    def test_a_metric_billed_on_two_lines_is_not_compared(self):
        """Adding lines up is not specified: both are said unchecked."""
        lines = [self.metered("api_calls"), self.metered("api_calls")]
        found = self.findings(lines, self.usage("api_calls"))
        assert found[RuleCode.USAGE_UNAVAILABLE].evidence["metrics"] == ["api_calls"]
        assert RuleCode.SUSPICIOUS_USAGE_DELTA not in found


class TestCitedInvoice:
    """Taxonomy S6, through the API."""

    def test_another_invoice_than_the_designated_one_asks_which(self, client):
        body = investigate(
            client,
            message="Ma facture inv_001 de septembre est en double.",
            customer_email="alice@acme.fr",
            invoice_id="inv_002",
        )
        assert body["findings"][0] == "INVOICE_REFERENCE_CONFLICT"
        assert body["classification"] == "insufficient_information"
        assert body["proposed_action"]["type"] == "request_information"
        assert body["decision_engine"] == "none"
        assert body["requires_human_approval"] is False


class TestUnderBilling:
    """Taxonomy S1: under-billing is an audit note, never a finding."""

    def test_under_billing_is_noted_not_put_to_the_customer(self):
        data = FakeBillingProvider()
        meridien = next(c for c in data._customers if c.id == "cus_meridien")
        invoice = next(i for i in data._invoices if i.id == "inv_310")
        usage = Usage(
            subscription_id="sub_310",
            metric="api_calls",
            quantity=Decimal("5000"),
            period_start=datetime(2026, 9, 1, tzinfo=UTC),
            period_end=datetime(2026, 10, 1, tzinfo=UTC),
        )

        class Provider(FakeBillingProvider):
            async def get_usage(self, subscription_id):
                return usage

        engine = MockDecisionEngine()
        investigator = DisputeInvestigator(provider=Provider(), engine=engine)
        dispute = Dispute(
            id="dsp",
            customer_message="Pouvez-vous m'expliquer ma facture ?",
            customer_email=meridien.email,
            invoice_id=invoice.id,
        )
        import asyncio

        result = asyncio.run(investigator.investigate(dispute, now=NOW))
        assert "SUSPICIOUS_USAGE_DELTA" not in {f.code.value for f in result.findings}
        assert "usage_under_billed" in [e.event for e in result.events]


class TestUnderBillingNeverMovesAnything:
    """An engine that tries to act on under-billing gets no money; a reply on
    another ground stays possible (taxonomy S1)."""

    def test_a_credit_note_on_under_billing_has_no_amount(self):
        from app.decision.base import Classification, Decision, RecommendedAction

        class Eager:
            name = "eager"
            version = "0"

            async def decide(self, dispute, context, findings):
                return Decision(
                    classification=Classification.DUPLICATE_BILLING,
                    confidence=0.99,
                    confidence_kind="self_reported",
                    recommended_action=RecommendedAction.PROPOSE_CREDIT_NOTE,
                )

        usage = Usage(
            subscription_id="sub_310",
            metric="api_calls",
            quantity=Decimal("5000"),
            period_start=datetime(2026, 9, 1, tzinfo=UTC),
            period_end=datetime(2026, 10, 1, tzinfo=UTC),
        )

        class Provider(FakeBillingProvider):
            async def get_usage(self, subscription_id):
                return usage

        import asyncio

        dispute = Dispute(
            id="dsp",
            customer_message="Pouvez-vous m'expliquer ma facture ?",
            customer_email="facturation@meridien-data.fr",
            invoice_id="inv_310",
        )
        result = asyncio.run(
            DisputeInvestigator(provider=Provider(), engine=Eager()).investigate(dispute, now=NOW)
        )
        assert result.proposed_action.type == "none"
        assert result.action_id is None
