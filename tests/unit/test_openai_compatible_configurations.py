"""The three configurations of spec 007, closed, and the four of spec 004
left as they were (specs/007-openai-qwen-engines, T020)."""

import json
from dataclasses import asdict, fields

import pytest

from app.api import deps
from app.decision.hosted import CONFIGURATIONS, Configuration, configuration
from evals import state
from evals.first_calls import FirstCalls, fingerprint

#: The four existing configurations' fingerprints, computed on `main`
#: (`14e7c9a`) before spec 007: their accepted first calls hold only while
#: these do not move.
PINNED = {
    "gemini-3.8-flash": "6fc2ab41043d7ce9690c7499a7f95dc2dfd0b2278ebcc85a022ac723c90ee5e0",
    "gemini-3.5-flash-lite": "a3e7d509eed1b5b0c279873491352031ae09ef260e9a9d852a4ad88cdc74bde8",
    "mistral-medium-3-5": "06167c9ee742329dfb120577c0fb49b4ac1b998e57175357f49f392037c782e8",
    "mistral-medium-3-5-reasoning": (
        "2170572347a547185c3e78ba6f43856556e9258618b4cbe53e1e3a1f6e79bb10"
    ),
}

#: The fields `Configuration` had before spec 007.
SPEC_004_FIELDS = {
    "name",
    "vendor",
    "developer",
    "endpoint",
    "api_version",
    "key_variable",
    "model",
    "temperature",
    "max_output_tokens",
    "reasoning",
    "safety",
    "tokens_per_minute",
    "requests_per_second",
    "requests_per_day",
    "quota_timezone",
    "attempts",
    "stop_on_outage",
    "limits_source",
    "shared_limits",
    "price_per_mtok",
    "price_source",
    "reservation",
    "reservation_estimator",
}

NEW = ("gpt-6-luna", "gpt-6.1-sol", "qwen3.5-397b-a17b")

KEYS = {"gemini": "gemini-key-set-by-the-test", "mistral": "mistral-key-set-by-the-test"}


def legacy_identity(name: str) -> dict:
    """An account identity as spec 004 wrote it -- the first 12 hex digits
    of the key's SHA-256, and the Gemini project -- built here, not by the
    code under test."""
    import hashlib

    vendor = name.split("-")[0]
    identity = {"key": hashlib.sha256(KEYS[vendor].encode("utf-8")).hexdigest()[:12]}
    if vendor == "gemini":
        identity["project"] = "resolve-billing-eval"
    return identity


class TestTheExistingFourDoNotMove:
    @pytest.mark.parametrize("name", sorted(PINNED))
    def test_their_fingerprints_are_the_ones_computed_on_main(self, name):
        assert fingerprint(configuration(name)) == PINNED[name]

    @pytest.mark.parametrize("name", sorted(PINNED))
    def test_the_new_fields_keep_their_defaults(self, name):
        config = configuration(name)
        for field in fields(Configuration):
            if field.name not in SPEC_004_FIELDS:
                assert getattr(config, field.name) == field.default, field.name

    def test_a_new_field_set_away_from_its_default_moves_the_fingerprint(self):
        from dataclasses import replace

        flash = configuration("gemini-3.8-flash")

        assert fingerprint(replace(flash, check_identity=True)) != PINNED["gemini-3.8-flash"]

    def test_old_acceptance_records_still_admit_their_full_runs(self, tmp_path, monkeypatch):
        """End to end, through the real gate: records written before spec
        007, with their evidence unchanged, still open the full runs --
        Mistral's sibling requirement included."""
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-set-by-the-test")
        monkeypatch.setenv("MISTRAL_API_KEY", "mistral-key-set-by-the-test")
        monkeypatch.setenv("GEMINI_QUOTA_PROJECT", "resolve-billing-eval")
        records = {}
        for name, value in PINNED.items():
            evidence = tmp_path / f"{name}.json"
            evidence.write_text("{}", encoding="utf-8")
            records[name] = {
                "accepted_at": "2026-10-25T00:00:00+00:00",
                "model_version": "v",
                "result": str(evidence.resolve()),
                "result_sha256": __import__("hashlib").sha256(b"{}").hexdigest(),
                "account": legacy_identity(name),
                "fingerprint": value,
            }
        state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (state.STATE_DIR / "first-calls.json").write_text(
            json.dumps({"first_calls": records}), encoding="utf-8"
        )

        for name in PINNED:
            FirstCalls().admit_full_run(configuration(name))  # no refusal


class TestTheNewConfigurations:
    @pytest.mark.parametrize("name", NEW)
    def test_each_is_closed_in_the_registry(self, name):
        config = CONFIGURATIONS[name]

        assert config.name == config.model == name
        assert config.reasoning == "low"
        assert config.max_output_tokens == 4096
        assert config.temperature is None
        assert config.attempts == 3
        assert config.stop_on_outage is False
        assert config.check_identity is True
        assert config.reservation == "estimated"
        assert config.reservation_estimator == "utf8-bytes-v1"
        assert config.requests_per_day is None
        assert config.price_source and config.limits_source
        assert config.processing_location

    @pytest.mark.parametrize("name", ["gpt-6-luna", "gpt-6.1-sol"])
    def test_openai_s(self, name):
        config = CONFIGURATIONS[name]

        assert config.vendor == "openai"
        assert config.developer == "OpenAI"
        assert config.endpoint == "https://api.openai.com/v1"
        assert config.key_variable == "OPENAI_API_KEY"
        assert config.account_variables == ("OPENAI_ORG_ID", "OPENAI_PROJECT_ID")
        assert config.currency == "USD"
        assert config.store is False
        assert config.processing_location == "global, no regional commitment"

    def test_openai_limits_as_the_maintainer_read_them(self):
        luna, sol = CONFIGURATIONS["gpt-6-luna"], CONFIGURATIONS["gpt-6.1-sol"]

        assert (luna.tokens_per_minute, luna.requests_per_second) == (2_000_000, 5_000 / 60)
        assert (sol.tokens_per_minute, sol.requests_per_second) == (1_000_000, 5_000 / 60)
        for config in (luna, sol):
            assert "read 2026-10-27" in config.limits_source
            assert "provisional" not in config.limits_source

    def test_scaleway_limits_as_documented_for_the_maintainer_s_account(self):
        qwen = CONFIGURATIONS["qwen3.5-397b-a17b"]

        assert (qwen.tokens_per_minute, qwen.requests_per_second) == (200_000, 300 / 60)
        assert "payment method validated" in qwen.limits_source
        assert "read 2026-10-27" in qwen.limits_source

    def test_no_configuration_keeps_provisional_limits(self):
        from app.decision.hosted import PROVISIONAL_LIMITS

        assert all(c.limits_source != PROVISIONAL_LIMITS for c in CONFIGURATIONS.values())

    def test_openai_prices(self):
        luna, sol = CONFIGURATIONS["gpt-6-luna"], CONFIGURATIONS["gpt-6.1-sol"]

        assert (luna.price_per_mtok, luna.cached_input_price_per_mtok) == ((0.10, 0.50), 0.01)
        assert luna.cache_write_price_per_mtok == 0.125
        assert (sol.price_per_mtok, sol.cached_input_price_per_mtok) == ((2.0, 10.0), 0.10)
        assert sol.cache_write_price_per_mtok == 2.50

    def test_qwen_on_scaleway(self):
        config = CONFIGURATIONS["qwen3.5-397b-a17b"]

        assert config.vendor == "scaleway"
        assert config.developer == "Alibaba (Qwen)"
        assert config.endpoint == "https://api.scaleway.ai/{project}/v1"
        assert config.key_variable == "SCALEWAY_API_KEY"
        assert config.account_variables == ("SCALEWAY_ORGANIZATION_ID", "SCALEWAY_PROJECT_ID")
        assert config.currency == "EUR"
        assert config.price_per_mtok == (0.60, 3.60)
        assert config.cached_input_price_per_mtok is None
        assert config.cache_write_price_per_mtok is None
        # Scaleway does not support `store`: never sent, so never set.
        assert config.store is None
        assert config.processing_location == "Paris, France"
        assert config.quantisation == "int4 (catalogue, serverless table)"
        assert "similar to `medium`" in config.reasoning_note

    @pytest.mark.parametrize("name", NEW)
    def test_the_service_refuses_each(self, name, monkeypatch):
        monkeypatch.setenv("DECISION_ENGINE", name)
        deps.get_engine.cache_clear()

        with pytest.raises(RuntimeError, match="unknown decision engine"):
            deps.get_engine()

        deps.get_engine.cache_clear()
        assert name not in deps.ENGINES

    @pytest.mark.parametrize("name", NEW)
    def test_the_result_carries_the_whole_configuration(self, name):
        recorded = asdict(CONFIGURATIONS[name])

        assert {"currency", "processing_location", "account_variables"} <= set(recorded)
