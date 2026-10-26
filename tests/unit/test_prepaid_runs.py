"""A configuration on a prepaid account, held to its envelope and to its
model's identity on every answer (specs/007-openai-qwen-engines, T044)."""

# ruff: noqa: F811 -- the fixtures imported below are requested by name

import json

import pytest

from app.decision.hosted import Answer
from evals.governed import Governed
from evals.ledger import PrepaidLedger
from evals.limits import Limiter, RunStopped
from tests.unit.test_claude_engine import (  # noqa: F401 -- the fixtures are used by name
    VALID_VERDICT,
    context,
    dispute,
    findings,
)
from tests.unit.test_evaluation_limits import Clock, Vendor


def answered(model="gpt-6-luna", input_tokens=900, output_tokens=120, **details):
    return Answer(json.dumps(VALID_VERDICT), "stop", input_tokens, output_tokens, model, **details)


def prepaid(tmp_path, vendor, total=5.0):
    clock = Clock()
    ledger = PrepaidLedger("openai-org-test", "USD", path=tmp_path / "envelope.json")
    ledger.declare(total)
    limiter = Limiter(
        vendor.config.tokens_per_minute,
        vendor.config.requests_per_second,
        clock=clock,
        sleep=clock.sleep,
    )
    engine = Governed(vendor, [limiter], ledger, run_id="r", clock=clock, sleep=clock.sleep)
    engine.begin("case")
    return engine, ledger


class TestTheEnvelope:
    def test_a_priced_configuration_needs_its_ledger(self):
        with pytest.raises(ValueError, match="ledger"):
            Governed(Vendor([], name="gpt-6-luna"), [], None)

    async def test_the_attempt_is_reserved_then_settled(self, tmp_path, context, findings, dispute):
        vendor = Vendor([answered(cached_input_tokens=0, cache_write_tokens=0)], name="gpt-6-luna")
        engine, ledger = prepaid(tmp_path, vendor)

        await engine.decide(dispute, context, findings)

        held = ledger.state()
        assert held["unsettled"] == 0
        assert held["spent"] == pytest.approx((900 * 0.10 + 120 * 0.50) / 1e6)
        assert engine.cost == pytest.approx(held["spent"])

    async def test_an_inconsistent_usage_keeps_the_reservation_and_stops(
        self, tmp_path, context, findings, dispute
    ):
        vendor = Vendor([answered(cached_input_tokens=5000)], name="gpt-6-luna")
        engine, ledger = prepaid(tmp_path, vendor)

        with pytest.raises(RunStopped, match="inconsistent"):
            await engine.decide(dispute, context, findings)

        assert ledger.state()["unsettled"] == 1

    async def test_an_attempt_the_envelope_cannot_cover_is_not_sent(
        self, tmp_path, context, findings, dispute
    ):
        vendor = Vendor([answered()], name="gpt-6.1-sol", reserved=10_000)
        engine, ledger = prepaid(tmp_path, vendor, total=0.0001)

        with pytest.raises(RunStopped, match="envelope"):
            await engine.decide(dispute, context, findings)

        assert vendor.calls == 0


class TestTheModelsIdentity:
    async def test_the_configured_model_is_scored(self, tmp_path, context, findings, dispute):
        vendor = Vendor([answered()], name="gpt-6-luna")
        engine, _ = prepaid(tmp_path, vendor)

        await engine.decide(dispute, context, findings)

        assert engine.statuses["case"] == "decided"

    @pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-luna-2026-10-01", None])
    async def test_another_or_no_model_stops_the_run_after_settling(
        self, model, tmp_path, context, findings, dispute
    ):
        vendor = Vendor([answered(model=model)], name="gpt-6-luna")
        engine, ledger = prepaid(tmp_path, vendor)

        with pytest.raises(RunStopped, match="model"):
            await engine.decide(dispute, context, findings)

        assert ledger.state()["unsettled"] == 0
        assert ledger.state()["spent"] > 0
        assert engine.statuses["case"] == "unavailable"
        if model is not None:
            assert model in engine.engine.model_versions

    async def test_spec_004_configurations_are_not_held_to_it(
        self, tmp_path, context, findings, dispute
    ):
        from tests.unit.test_evaluation_limits import governed

        vendor = Vendor([answered(model="mistral-medium-2604")], name="mistral-medium-3-5")
        engine, _ = governed(tmp_path, vendor)
        engine.begin("case")

        await engine.decide(dispute, context, findings)

        assert engine.statuses["case"] == "decided"
