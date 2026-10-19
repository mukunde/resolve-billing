"""ClaudeDecisionEngine, driven by a stub client.

No API key, no network, no spend. What is under test is everything around the
call: what the model is allowed to see, what happens when it answers badly,
and what gets recorded.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.decision.anonymise import anonymise
from app.decision.base import Classification, EngineUnavailable, RecommendedAction
from app.decision.claude import (
    PRICING_AS_OF,
    PRICING_PER_MTOK,
    ClaudeDecisionEngine,
    estimate_cost_micros,
)
from app.domain.enums import InvoiceStatus, RuleCode, Severity, SubscriptionStatus
from app.domain.models import (
    BillingContext,
    Customer,
    Dispute,
    Invoice,
    InvoiceLine,
    Payment,
    RuleFinding,
    Subscription,
)
from app.domain.money import Money

SEPT = datetime(2026, 9, 1, tzinfo=UTC)
OCT = datetime(2026, 10, 1, tzinfo=UTC)

VALID_VERDICT = {
    "classification": "duplicate_billing",
    "confidence": 0.94,
    "recommended_action": "propose_credit_note",
    "explanation": "Deux factures couvrent septembre pour le même montant.",
    "evidence_refs": ["INVOICE_1", "INVOICE_2"],
}


# -- stub client -----------------------------------------------------------


class StubMessages:
    def __init__(self, outcome):
        self._outcome = outcome
        self.last_request: dict | None = None

    async def create(self, **kwargs):
        self.last_request = kwargs
        return self._outcome


class StubClient:
    def __init__(self, outcome):
        self.messages = StubMessages(outcome)


def reply(body: dict | str, *, stop_reason="end_turn", input_tokens=1200, output_tokens=180):
    text = body if isinstance(body, str) else json.dumps(body)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
        ),
    )


def engine_with(outcome) -> tuple[ClaudeDecisionEngine, StubClient]:
    client = StubClient(outcome)
    return ClaudeDecisionEngine(client=client, model="claude-opus-5"), client


# -- fixtures --------------------------------------------------------------


def eur(minor: int) -> Money:
    return Money(amount_minor=minor, currency="EUR")


def an_invoice(invoice_id: str) -> Invoice:
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


@pytest.fixture
def context() -> BillingContext:
    return BillingContext(
        customer=Customer(id="cus_acme", email="alice@acme.fr", currency="EUR"),
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
            Payment(id="pay_001", invoice_id="inv_001", amount=eur(9900), status="succeeded")
        ],
        target_invoice_id="inv_002",
    )


@pytest.fixture
def findings() -> list[RuleFinding]:
    return [
        RuleFinding(
            code=RuleCode.DUPLICATE_INVOICE,
            severity=Severity.HIGH,
            description="Deux factures couvrent la même période.",
            evidence={"invoice_1": "inv_001", "invoice_2": "inv_002"},
        )
    ]


@pytest.fixture
def dispute() -> Dispute:
    return Dispute(
        id="dsp_1",
        customer_message="J'ai été facturé deux fois en septembre.",
        customer_id="cus_acme",
        invoice_id="inv_002",
    )


# -- what the model is allowed to see --------------------------------------


class TestPseudonymisation:
    def test_no_real_identifier_reaches_the_payload(self, context, findings):
        payload, _ = anonymise(context, findings)
        serialised = json.dumps(payload)

        for real_id in ("cus_acme", "sub_77", "inv_001", "inv_002", "pay_001"):
            assert real_id not in serialised

    def test_findings_evidence_is_aliased_too(self, context, findings):
        """The raw ids in a finding's evidence are right for the audit log
        and wrong for the model."""
        payload, _ = anonymise(context, findings)
        evidence = payload["deterministic_findings"][0]["evidence"]

        assert evidence["invoice_1"].startswith("INVOICE_")
        assert evidence["invoice_2"].startswith("INVOICE_")

    def test_amounts_and_dates_pass_through(self, context, findings):
        payload, _ = anonymise(context, findings)
        assert payload["invoices"][0]["total_minor"] == 9900
        assert payload["invoices"][0]["currency"] == "EUR"

    def test_the_same_object_always_gets_the_same_alias(self, context, findings):
        payload, _ = anonymise(context, findings)
        disputed = payload["disputed_invoice_ref"]
        listed = [i["ref"] for i in payload["invoices"]]
        assert disputed in listed

    async def test_the_request_carries_no_real_identifier(
        self, context, findings, dispute
    ):
        engine, client = engine_with(reply(VALID_VERDICT))
        await engine.decide(dispute, context, findings)

        sent = json.dumps(client.messages.last_request, default=str)
        assert "cus_acme" not in sent
        assert "inv_001" not in sent


class TestProseIsReadable:
    async def test_aliases_are_put_back_into_the_explanation(
        self, context, findings, dispute
    ):
        """The explanation is what a human reads -- audit row, email,
        approval page. Mapping only evidence_refs back left a billing manager
        told that "INVOICE_1 and INVOICE_2 cover the same period", with no way
        to know which invoices those are once the alias map is discarded.
        """
        verdict = VALID_VERDICT | {
            "explanation": "INVOICE_1 et INVOICE_2 couvrent la meme periode."
        }
        engine, _ = engine_with(reply(verdict))

        decision = await engine.decide(dispute, context, findings)

        assert "INVOICE_" not in decision.explanation
        assert "inv_001" in decision.explanation
        assert "inv_002" in decision.explanation


class TestEvidenceMapping:
    async def test_aliases_are_mapped_back_to_real_ids(self, context, findings, dispute):
        engine, _ = engine_with(reply(VALID_VERDICT))

        decision = await engine.decide(dispute, context, findings)

        assert set(decision.evidence_refs) == {"inv_001", "inv_002"}

    async def test_a_reference_we_never_issued_is_dropped(self, context, findings, dispute):
        """The core of the control: the model can only cite what it was given.

        A coaxed or hallucinated `INVOICE_9` maps to nothing, so it cannot
        point the system at another customer's invoice.
        """
        verdict = VALID_VERDICT | {"evidence_refs": ["INVOICE_1", "INVOICE_9", "cus_other"]}
        engine, _ = engine_with(reply(verdict))

        decision = await engine.decide(dispute, context, findings)

        assert decision.evidence_refs == ["inv_001"]


# -- what happens when the model answers badly -----------------------------


class TestNonConformingOutput:
    """Every failure lands in the same place: a human."""

    @pytest.mark.parametrize(
        ("outcome", "why"),
        [
            (reply("this is not json at all"), "unparseable"),
            (reply({"classification": "duplicate_billing"}), "missing fields"),
            (reply(VALID_VERDICT | {"confidence": 4.2}), "confidence out of range"),
            (reply(VALID_VERDICT | {"classification": "refund_everything"}), "unknown class"),
            (reply(VALID_VERDICT, stop_reason="max_tokens"), "truncated"),
            (reply(VALID_VERDICT, stop_reason="refusal"), "declined"),
        ],
    )
    async def test_escalates_instead_of_guessing(
        self, context, findings, dispute, outcome, why
    ):
        engine, _ = engine_with(outcome)

        decision = await engine.decide(dispute, context, findings)

        assert decision.classification is Classification.UNKNOWN, why
        assert decision.recommended_action is RecommendedAction.ESCALATE, why
        assert decision.confidence == 0.0, why

    async def test_the_failed_call_is_still_costed(self, context, findings, dispute):
        """A run that produced nothing usable still spent money. Record it."""
        engine, _ = engine_with(reply("garbage"))

        decision = await engine.decide(dispute, context, findings)

        assert decision.telemetry is not None
        assert decision.telemetry.cost_micros > 0


# -- what gets recorded ----------------------------------------------------


class TestDecisionAndTelemetry:
    async def test_a_valid_verdict_becomes_a_decision(self, context, findings, dispute):
        engine, _ = engine_with(reply(VALID_VERDICT))

        decision = await engine.decide(dispute, context, findings)

        assert decision.classification is Classification.DUPLICATE_BILLING
        assert decision.recommended_action is RecommendedAction.PROPOSE_CREDIT_NOTE
        assert decision.confidence == 0.94
        assert decision.explanation.startswith("Deux factures")

    async def test_confidence_is_always_self_reported(self, context, findings, dispute):
        """The model does not get to call its own number calibrated."""
        engine, _ = engine_with(reply(VALID_VERDICT))

        decision = await engine.decide(dispute, context, findings)

        assert decision.confidence_kind == "self_reported"

    async def test_telemetry_carries_what_the_audit_row_needs(
        self, context, findings, dispute
    ):
        engine, _ = engine_with(reply(VALID_VERDICT))

        telemetry = (await engine.decide(dispute, context, findings)).telemetry

        assert telemetry.model_id == "claude-opus-5"
        assert telemetry.prompt_version == "decision_v3"
        assert telemetry.input_tokens == 1200
        assert telemetry.output_tokens == 180
        assert telemetry.latency_ms >= 0

    def test_engine_version_pins_model_and_prompt(self):
        engine, _ = engine_with(reply(VALID_VERDICT))
        assert engine.version == "claude-opus-5/decision_v3"


class TestCost:
    def test_priced_in_micro_dollars(self):
        """1M input tokens on Opus 5 is $5.00, i.e. 5_000_000 micro-dollars."""
        assert estimate_cost_micros("claude-opus-5", 1_000_000, 0) == 5_000_000
        assert estimate_cost_micros("claude-opus-5", 0, 1_000_000) == 25_000_000

    def test_an_unknown_model_reports_nothing_rather_than_zero(self):
        """Zero is a claim; None is the absence of one.

        Reporting zero would have the audit assert the run was free, and
        would silently empty out the figures the cost argument rests on.
        """
        assert estimate_cost_micros("some-future-model", 1000, 1000) is None

    def test_a_model_with_no_per_call_price_costs_zero(self, monkeypatch):
        """Self-hosted or flat-rate: genuinely free per call, and we know it.

        This is a result worth publishing -- "the sovereign on-premise option
        costs nothing per call" -- not a gap in the table.
        """
        monkeypatch.setitem(PRICING_PER_MTOK, "mistral-onprem", None)

        assert estimate_cost_micros("mistral-onprem", 1000, 1000) == 0

    async def test_a_priced_run_carries_the_date_of_the_price_list(
        self, context, findings, dispute
    ):
        """A cost without the grid it came from cannot be read six months on."""
        engine, _ = engine_with(reply(VALID_VERDICT))

        telemetry = (await engine.decide(dispute, context, findings)).telemetry

        assert telemetry.pricing_as_of == PRICING_AS_OF

    async def test_a_model_priced_later_carries_its_own_date(
        self, context, findings, dispute
    ):
        """Haiku 5.5 was priced on 2026-10-07, after the rest of the table.
        Stamping the table's date on its cost would cite a price list that
        never contained it."""
        engine, _ = engine_with(reply(VALID_VERDICT))
        engine.model = "claude-haiku-5-5"

        telemetry = (await engine.decide(dispute, context, findings)).telemetry

        assert telemetry.cost_micros is not None
        assert telemetry.pricing_as_of == "2026-10-07"

    def test_haiku_5_5_at_its_list_price(self):
        """$0.10 in and $0.50 out per million tokens, prompts up to 100k."""
        assert estimate_cost_micros("claude-haiku-5-5", 1_000_000, 0) == 100_000
        assert estimate_cost_micros("claude-haiku-5-5", 0, 1_000_000) == 500_000

    async def test_an_unpriced_run_quotes_no_date(self, context, findings, dispute):
        """Quoting the date of a table that does not contain this model would
        suggest the figure was derived from it."""
        engine, _ = engine_with(reply(VALID_VERDICT))
        engine.model = "some-future-model"

        telemetry = (await engine.decide(dispute, context, findings)).telemetry

        assert telemetry.cost_micros is None
        assert telemetry.pricing_as_of is None


class TestRequestShape:
    async def test_output_is_constrained_to_the_closed_classification_set(
        self, context, findings, dispute
    ):
        engine, client = engine_with(reply(VALID_VERDICT))
        await engine.decide(dispute, context, findings)

        schema = client.messages.last_request["output_config"]["format"]["schema"]
        enum = schema["properties"]["classification"]["enum"]

        assert set(enum) == {c.value for c in Classification}
        assert "unknown" in enum
        assert schema["additionalProperties"] is False

    async def test_the_customer_message_is_fenced_as_data(
        self, context, findings, dispute
    ):
        engine, client = engine_with(reply(VALID_VERDICT))
        await engine.decide(dispute, context, findings)

        content = client.messages.last_request["messages"][0]["content"]
        assert "<customer_message>" in content
        assert "<billing_context>" in content


class TestTheClientIsReleased:
    """Each evaluation run builds an engine, and with it an HTTP client. Left
    open, the client of one run was collected while the next run's event
    loop was running; the SDK's finaliser then scheduled its close on that
    loop, for connections bound to the previous one, already closed --
    "Event loop is closed" in the middle of `--runs`."""

    async def test_closing_the_engine_closes_its_client(self):
        closed = []

        class ClosableClient(StubClient):
            async def close(self):
                closed.append(True)

        engine = ClaudeDecisionEngine(client=ClosableClient(reply(VALID_VERDICT)), model="m")
        await engine.aclose()

        assert closed == [True]

    async def test_a_client_with_nothing_to_close_is_left_alone(self):
        engine, _ = engine_with(reply(VALID_VERDICT))

        await engine.aclose()


# -- outages (taxonomy S8) ---------------------------------------------------


class APIConnectionError(Exception):
    """Named like the SDK's: the engine tells outages apart by class name."""


class APITimeoutError(APIConnectionError):
    pass


class APIStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class Raising:
    def __init__(self, error: Exception):
        self.messages = SimpleNamespace(create=self._raise)
        self._error = error

    async def _raise(self, **kwargs):
        raise self._error


class TestOutages:
    @pytest.mark.parametrize(
        "error",
        [
            APIConnectionError(),
            APITimeoutError(),
            APIStatusError(500),
            APIStatusError(529),
            APIStatusError(429),
            APIStatusError(408),
        ],
        ids=["connection", "timeout", "500", "overloaded", "rate-limited", "request-timeout"],
    )
    async def test_an_outage_is_reported_as_one(self, context, findings, dispute, error):
        engine = ClaudeDecisionEngine(client=Raising(error), model="claude-opus-5")
        with pytest.raises(EngineUnavailable):
            await engine.decide(dispute, context, findings)

    @pytest.mark.parametrize(
        "error",
        [APIStatusError(400), APIStatusError(401), ValueError("a bug")],
        ids=["bad-request", "unauthorised", "programming-error"],
    )
    async def test_an_error_stays_an_error(self, context, findings, dispute, error):
        """A bad request or a missing key is not an outage: it fails visibly."""
        engine = ClaudeDecisionEngine(client=Raising(error), model="claude-opus-5")
        with pytest.raises(type(error)):
            await engine.decide(dispute, context, findings)
