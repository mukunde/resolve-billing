"""What we assume about the Anthropic SDK, asserted against the SDK itself.

The engine's own tests use a stub client, which proves the logic around the
call but would happily keep passing if a parameter were renamed upstream. These
checks close that gap without a credential and without spending anything: an
SDK upgrade that moves something breaks a test here rather than the first real
run.

They are skipped when ``anthropic`` is not installed, since it is an optional
dependency -- the demo runs on the mock engine.
"""

import inspect

import pytest

anthropic = pytest.importorskip("anthropic")

from app.decision.claude import PRICING_PER_MTOK, _response_schema  # noqa: E402


class TestRequestShape:
    def test_the_parameters_we_send_exist(self):
        from anthropic.resources.messages import AsyncMessages

        accepted = set(inspect.signature(AsyncMessages.create).parameters)

        assert {"model", "max_tokens", "system", "messages", "output_config"} <= accepted

    def test_output_config_carries_effort_and_format(self):
        from anthropic.types.output_config_param import OutputConfigParam

        assert {"effort", "format"} <= set(OutputConfigParam.__annotations__)

    def test_our_effort_level_is_one_the_api_accepts(self):
        """Classification does not repay deep thinking; the money is in the
        deterministic findings, which have already run."""
        from anthropic.types.output_config_param import OutputConfigParam

        annotation = str(OutputConfigParam.__annotations__["effort"])

        assert "'low'" in annotation

    def test_the_json_schema_format_has_the_shape_we_build(self):
        from anthropic.types.json_output_format_param import JSONOutputFormatParam

        assert set(JSONOutputFormatParam.__annotations__) == {"type", "schema"}

        ours = _response_schema()
        assert ours["type"] == "json_schema"
        assert isinstance(ours["schema"], dict)


class TestResponseShape:
    def test_usage_exposes_the_fields_the_audit_row_needs(self):
        from anthropic.types import Usage

        assert {
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
        } <= set(Usage.model_fields)

    def test_a_message_carries_a_stop_reason(self):
        from anthropic.types import Message

        assert "stop_reason" in Message.model_fields


class TestModelIds:
    @pytest.mark.parametrize("model", sorted(PRICING_PER_MTOK))
    def test_priced_models_have_no_date_suffix(self, model):
        """Model ids are complete as written. A remembered date suffix is the
        classic way to get a 404 on the first real call."""
        assert not model[-1].isdigit() or "-2" not in model[-9:]

    def test_the_default_model_is_priced(self):
        from app.decision.claude import DEFAULT_MODEL

        assert DEFAULT_MODEL in PRICING_PER_MTOK, (
            "an unpriced default would silently record every run as costing zero"
        )
