"""D10: the customer's message is masked before it reaches the model provider.

Proven without a model: on the masking itself, then on the request the Claude
engine actually sends, captured by a stub client.
"""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api import deps
from app.decision.anonymise import Pseudonyms, anonymise
from app.decision.claude import ClaudeDecisionEngine
from app.decision.masking import _apply, mask_message
from app.decision.mock import MockDecisionEngine
from app.domain.enums import InvoiceStatus, PaymentStatus, SubscriptionStatus
from app.domain.models import (
    BillingContext,
    Customer,
    Dispute,
    Invoice,
    InvoiceLine,
    Payment,
    Subscription,
)
from app.domain.money import Money
from app.providers.fake.adapter import FakeBillingProvider

SEPT = datetime(2026, 9, 1, tzinfo=UTC)
OCT = datetime(2026, 10, 1, tzinfo=UTC)
IBAN = "FR76 3000 6000 0112 3456 7890 189"
CARD = "4111 1111 1111 1111"


def eur(minor):
    return Money(amount_minor=minor, currency="EUR")


def an_invoice(invoice_id):
    return Invoice(
        id=invoice_id,
        customer_id="cus_acme",
        subscription_id="sub_77",
        total=eur(9900),
        status=InvoiceStatus.PAID,
        issued_at=SEPT,
        lines=[
            InvoiceLine(
                description="Abonnement Enterprise",
                quantity=Decimal(1),
                unit_amount=eur(9900),
                amount=eur(9900),
                period_start=SEPT,
                period_end=OCT,
            )
        ],
    )


CONTEXT = BillingContext(
    customer=Customer(
        id="cus_acme", email="alice@acme.fr", currency="EUR", display_name="ACME SAS"
    ),
    subscriptions=[
        Subscription(
            id="sub_77",
            customer_id="cus_acme",
            status=SubscriptionStatus.ACTIVE,
            plan_code="enterprise_monthly",
            current_period_start=SEPT,
            current_period_end=OCT,
        )
    ],
    invoices=[an_invoice("inv_001"), an_invoice("inv_002")],
    payments=[
        Payment(
            id="pay_001", invoice_id="inv_001", amount=eur(9900), status=PaymentStatus.SUCCEEDED
        )
    ],
    target_invoice_id="inv_002",
)


def masked(message):
    _, names = anonymise(CONTEXT, [])
    return mask_message(
        message, CONTEXT, names, FakeBillingProvider().invoice_reference_spans
    ), names


class TestIdentifiers:
    @pytest.mark.parametrize("written", ["inv_002", "INV-002", "inv002"])
    def test_a_known_invoice_takes_its_context_alias(self, written):
        text, names = masked(f"La facture {written} est en double.")
        assert names.alias_of("inv_002") in text
        assert written not in text

    def test_invoices_outside_the_context_get_numbered_neutral_tokens(self):
        text, _ = masked("Voir inv_160, inv_170 et encore inv_160.")
        assert text == "Voir [INVOICE_REF_1], [INVOICE_REF_2] et encore [INVOICE_REF_1]."

    def test_other_context_identifiers_written_as_they_are(self):
        text, names = masked("Abonnement sub_77, paiement pay_001, client cus_acme.")
        for real in ("sub_77", "pay_001", "cus_acme"):
            assert real not in text
            assert names.alias_of(real) in text

    def test_a_token_that_looks_like_an_alias_is_neutralised(self):
        """A message cannot pass itself off as a reference we issued."""
        text, _ = masked("Comme dit dans INVOICE_7 et CUSTOMER_1.")
        assert text == "Comme dit dans [REF] et [REF]."

    def test_a_token_that_looks_like_a_placeholder_is_neutralised(self):
        text, _ = masked("Voir [INVOICE_REF_1], [IBAN] et [customer_name].")
        assert text == "Voir [REF], [REF] et [REF]."

    @pytest.mark.parametrize("written", ["inv_002x", "xinv_002", "pay_0011", "cus_acmex"])
    def test_an_identifier_inside_another_word_is_not_one(self, written):
        text, _ = masked(f"Code {written} ici.")
        assert text == f"Code {written} ici."


class TestPeopleAndNumbers:
    def test_addresses(self):
        text, _ = masked(
            "Écrivez à alice@acme.fr, ALICE@ACME.FR, bob.martin@exemple.org ou o'brien@exemple.ie."
        )
        assert text == "Écrivez à [CUSTOMER_EMAIL], [CUSTOMER_EMAIL], [EMAIL] ou [EMAIL]."

    @pytest.mark.parametrize(
        "written",
        [
            "ACME SAS",
            "Acme",
            "acmé",
            "ACME",
            "ACME-SAS",
            "ACME.SAS",
            "ACME  SAS",
            "AcmeSAS",
            "A\u0301CME",  # an accent written as a separate combining mark
            "\uff21\uff23\uff2d\uff25",  # full-width letters
        ],
    )
    def test_the_customers_name(self, written):
        text, _ = masked(f"Nous, {written}, contestons la facture.")
        assert text == "Nous, [CUSTOMER_NAME], contestons la facture."

    @pytest.mark.parametrize(
        "iban",
        [IBAN, IBAN.lower(), IBAN.replace(" ", "-"), IBAN.replace(" ", "")],
        ids=["spaced", "lower-case", "dashed", "compact"],
    )
    def test_ibans(self, iban):
        text, _ = masked(f"Remboursez sur {iban} merci.")
        assert text == "Remboursez sur [IBAN] merci."

    @pytest.mark.parametrize(
        "card",
        [CARD, CARD.replace(" ", "-"), CARD.replace(" ", "")],
        ids=["spaced", "dashed", "compact"],
    )
    def test_cards(self, card):
        text, _ = masked(f"Carte {card} débitée.")
        assert text == "Carte [CARD] débitée."

    @pytest.mark.parametrize(
        "phone",
        [
            "06 12 34 56 78",
            "06.12.34.56.78",
            "0612345678",
            "+33 6 12 34 56 78",
            "+33 (0)6 12 34 56 78",
            "+44 20 7946 0958",
            "+1 (415) 555-0132",
            "+4930123456",
        ],
    )
    def test_phones(self, phone):
        text, _ = masked(f"Appelez le {phone} demain.")
        assert text == "Appelez le [PHONE] demain."

    def test_a_long_number_that_passes_luhn_is_masked_as_a_card(self):
        """The deliberate trade-off: an order number lost, never a card sent."""
        text, _ = masked("Commande 4012888888881881 reçue.")
        assert text == "Commande [CARD] reçue."

    @pytest.mark.parametrize(
        "kept",
        [
            "49,00 €",
            "1 234,56 EUR",
            "le 2026-09-01",
            "le 01/09/2026",
            "1200 appels API",
            "FR77 0000 0000 0000 0000 0000 000",  # an IBAN whose key does not check
            "4111 1111 1111 1112",  # sixteen digits that fail Luhn
            "commande 4012888888881882",
            "Abonnement Enterprise",
        ],
    )
    def test_amounts_dates_and_lookalikes_pass_through(self, kept):
        text, _ = masked(f"Montant {kept} indiqué.")
        assert kept in text


class TestTheRequestSent:
    """What the provider actually receives, captured by a stub client."""

    def engine_and_request(self, message):
        sent = {}

        async def create(**request):
            sent.update(request)
            body = {
                "classification": "duplicate_billing",
                "confidence": 0.9,
                "recommended_action": "propose_credit_note",
                "explanation": "INVOICE_2 et [INVOICE_REF_1] : voir CUSTOMER_1.",
                "evidence_refs": ["INVOICE_2", "INVOICE_99"],
            }
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=json.dumps(body))],
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=10, output_tokens=10, cache_read_input_tokens=0),
            )

        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        engine = ClaudeDecisionEngine(
            client=client,
            model="claude-opus-5",
            invoice_references=FakeBillingProvider().invoice_reference_spans,
        )
        dispute = Dispute(
            id="dsp",
            customer_message=message,
            customer_email="alice@acme.fr",
            invoice_id="inv_002",
        )
        decision = asyncio.run(engine.decide(dispute, CONTEXT, []))
        return dispute, decision, json.dumps(sent, ensure_ascii=False, default=str)

    def test_no_sentinel_reaches_the_provider(self):
        message = (
            f"ACME SAS ici (alice@acme.fr, 06 12 34 56 78). inv_002 et INV-160 sont fausses ; "
            f"remboursez sur {IBAN} ou la carte {CARD}."
        )
        _, _, sent = self.engine_and_request(message)
        for sentinel in (
            "ACME SAS",
            "alice@acme.fr",
            "06 12 34 56 78",
            "inv_002",
            "INV-160",
            "FR76",
            "4111",
        ):
            assert sentinel not in sent, sentinel

    def test_the_dispute_itself_is_untouched(self):
        message = "La facture inv_002 est fausse."
        dispute, _, _ = self.engine_and_request(message)
        assert dispute.customer_message == message

    def test_only_issued_aliases_are_put_back(self):
        _, decision, _ = self.engine_and_request("La facture inv_002 est fausse.")
        assert "inv_002" in decision.explanation
        assert "[INVOICE_REF_1]" in decision.explanation  # a token, never resolved
        assert decision.evidence_refs == ["inv_002"]  # INVOICE_99 was never issued


class TestOverlaps:
    def test_a_short_match_starting_first_cannot_leave_a_longer_one_half_clear(self):
        # A five-character match, then a twelve-character one starting inside it.
        assert _apply("0123456789AB rest", [(0, 5, "[PHONE]"), (3, 12, "[IBAN]")]) == "[IBAN] rest"

    def test_chained_overlaps_are_one_zone(self):
        spans = [(0, 4, "[PHONE]"), (3, 8, "[CARD]"), (7, 10, "[EMAIL]")]
        assert _apply("abcdefghij!", spans) == "[CARD]!"

    def test_an_address_holding_the_customers_name_is_masked_whole(self):
        text, _ = masked("Écrivez à acme-billing@acme.fr.")
        assert text == "Écrivez à [EMAIL]."


class TestRestore:
    names = Pseudonyms()
    names.alias("inv_002", "INVOICE")
    names.alias("inv_003", "INVOICE")

    @pytest.mark.parametrize(
        "written",
        ["INVOICE_20", "XINVOICE_2", "INVOICE_2x", "[INVOICE_REF_1]", "INVOICE_3_b"],
    )
    def test_only_whole_issued_aliases_are_put_back(self, written):
        assert self.names.restore(f"voir {written}.") == f"voir {written}."

    def test_issued_aliases_are_put_back(self):
        assert self.names.restore("INVOICE_1 et INVOICE_2.") == "inv_002 et inv_003."


class Probe(MockDecisionEngine):
    """A mock engine with the hook, to see what wiring hands it."""

    invoice_references = None


class TestWiring:
    """Every path that builds an engine from configuration hands it the hook."""

    def test_the_api(self, monkeypatch):
        monkeypatch.setitem(deps.ENGINES, "probe", Probe)
        monkeypatch.setenv("DECISION_ENGINE", "probe")
        deps.get_engine.cache_clear()
        try:
            engine = deps.get_engine()
        finally:
            deps.get_engine.cache_clear()
        assert engine.invoice_references == deps.get_provider().invoice_reference_spans

    def test_the_evaluation_runner(self, monkeypatch):
        from evals import runner

        built = []

        def probe():
            engine = Probe()
            built.append(engine)
            return engine

        monkeypatch.setitem(deps.ENGINES, "probe", probe)
        asyncio.run(runner.run("probe"))
        assert built[0].invoice_references is not None
        assert built[0].invoice_references("inv_002") == [(0, 7, "inv_002")]

    def test_an_engine_built_by_hand_masks_only_literal_identifiers(self):
        """Without the provider's hook, `INV-160` is not known to be an invoice."""
        _, names = anonymise(CONTEXT, [])
        text = mask_message("inv_002 et INV-160.", CONTEXT, names)
        assert text == f"{names.alias_of('inv_002')} et INV-160."
