"""The runner for the configurations on prepaid accounts: envelopes, account
identity, results (specs/007-openai-qwen-engines, T050)."""

import asyncio
import hashlib
import json

import pytest

from app.decision.base import EngineRejected
from app.decision.hosted import Answer
from evals import engines, runner, state
from tests.unit.test_claude_engine import VALID_VERDICT
from tests.unit.test_evaluation_limits import Vendor, _no_wait

KEY = "sk-SENTINEL-KEY-0123456789"
SCW_KEY = "scw-SENTINEL-KEY-9876543210"


def verdict(model, input_tokens=900, output_tokens=120):
    return Answer(json.dumps(VALID_VERDICT), "stop", input_tokens, output_tokens, model)


class Scripted(Vendor):
    """Answers every call the same way, or raises."""

    def __init__(self, name, outcome=None):
        super().__init__([], name=name)
        self.outcome = outcome if outcome is not None else verdict(name)

    async def _complete(self, system, user):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture(autouse=True)
def accounts(monkeypatch):
    monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
    for variable in ("OPENAI_CUSTOM_HEADERS", "OPENAI_LOG"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_ORG_ID", "org-test")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_test")
    monkeypatch.setenv("SCALEWAY_API_KEY", SCW_KEY)
    monkeypatch.setenv("SCALEWAY_ORGANIZATION_ID", "scw-org")
    monkeypatch.setenv("SCALEWAY_PROJECT_ID", "scw-project")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SENTINEL-KEY")
    monkeypatch.setenv("RESOLVE_ANTHROPIC_ORG_ID", "anth-org")
    for variable in ("ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_LOG"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-set-by-the-test")
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-key-set-by-the-test")
    monkeypatch.setenv("GEMINI_QUOTA_PROJECT", "resolve-billing-eval")
    monkeypatch.setattr("evals.limits.Limiter.acquire", _no_wait)
    monkeypatch.setattr("evals.state.AccountRun.wait", lambda self: 0.0)


def run(monkeypatch, tmp_path, name, *flags, outcome=None, vendor=None, refused=False):
    vendor = vendor or Scripted(name, outcome)

    def build():
        # A refusal comes before any client is built.
        assert not refused, "a client was built on a run that should be refused"
        return vendor

    monkeypatch.setitem(engines.HOSTED_ENGINES, name, build)
    target = tmp_path / f"{name}.json"
    before = set(tmp_path.glob(f"{name}-*.json"))
    argv = ["runner", "--engine", name, "--data-terms", "contractual", "--json", str(target)]
    monkeypatch.setattr("sys.argv", [*argv, *flags])
    status = runner.main()
    written = sorted(set(tmp_path.glob(f"{name}-*.json")) - before)
    result = json.loads(written[-1].read_text(encoding="utf-8")) if written else None
    return status, result, vendor


def envelope(account="openai-org-test") -> dict:
    return json.loads((state.STATE_DIR / f"envelope-{account}.json").read_text(encoding="utf-8"))


class TestTheEnvelopeFlag:
    def test_a_first_run_without_an_envelope_is_refused_before_any_call(
        self, tmp_path, monkeypatch, capsys
    ):
        status, result, vendor = run(
            monkeypatch, tmp_path, "gpt-6-luna", "--first-call-only", refused=True
        )

        assert status == 2
        assert result is None
        assert vendor.calls == 0
        assert "--envelope" in capsys.readouterr().err

    def test_the_envelope_is_declared_in_the_account_s_currency(self, tmp_path, monkeypatch):
        run(monkeypatch, tmp_path, "qwen3.5-397b-a17b", "--first-call-only", "--envelope", "2")

        held = envelope("scaleway-scw-org")
        assert held["currency"] == "EUR"
        assert held["declarations"][-1]["total"] == 2.0

    @pytest.mark.parametrize("name", ["gemini-3.8-flash", "mistral-medium-3-5"])
    def test_it_is_refused_for_spec_004_s_engines(self, name, tmp_path, monkeypatch):
        with pytest.raises(SystemExit):
            run(monkeypatch, tmp_path, name, "--first-call-only", "--envelope", "5")

    @pytest.mark.parametrize(
        "flags",
        [("--mistral-allowance-usd", "10"), ("--gemini-requests-used-today", "0")],
    )
    def test_spec_004_s_flags_are_refused_for_the_new_vendors(self, flags, tmp_path, monkeypatch):
        with pytest.raises(SystemExit):
            run(monkeypatch, tmp_path, "gpt-6-luna", "--first-call-only", "--envelope", "5", *flags)


class TestTheAccount:
    @pytest.mark.parametrize("missing", ["OPENAI_ORG_ID", "OPENAI_PROJECT_ID"])
    def test_a_missing_account_variable_is_named_and_nothing_runs(self, missing, monkeypatch):
        monkeypatch.delenv(missing)

        assert missing in runner.missing_credential("gpt-6-luna")

    @pytest.mark.parametrize("variable", ["OPENAI_CUSTOM_HEADERS", "OPENAI_LOG"])
    def test_an_ambient_sdk_setting_is_refused_before_any_client(
        self, variable, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(variable, "debug")

        assert variable in runner.missing_credential("qwen3.5-397b-a17b")
        with pytest.raises(SystemExit, match=variable):
            run(
                monkeypatch,
                tmp_path,
                "qwen3.5-397b-a17b",
                "--first-call-only",
                "--envelope",
                "2",
                refused=True,
            )

    def test_a_missing_account_variable_refuses_the_run_before_any_client(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("OPENAI_ORG_ID")

        with pytest.raises(SystemExit, match="OPENAI_ORG_ID"):
            run(monkeypatch, tmp_path, "gpt-6-luna", "--first-call-only", refused=True)

    def test_a_malformed_account_id_is_refused_cleanly(self, tmp_path, monkeypatch):
        run(monkeypatch, tmp_path, "gpt-6-luna", "--first-call-only", "--envelope", "5")
        monkeypatch.setenv("OPENAI_PROJECT_ID", "../elsewhere")

        with pytest.raises(SystemExit, match="OPENAI_PROJECT_ID"):
            run(monkeypatch, tmp_path, "gpt-6-luna", refused=True)

    def test_a_full_run_without_its_first_call_builds_no_client(self, tmp_path, monkeypatch):
        status, _, _ = run(monkeypatch, tmp_path, "gpt-6-luna", "--envelope", "5", refused=True)

        assert status == 2

    def test_one_run_per_billing_organisation_at_a_time(self, tmp_path, monkeypatch, capsys):
        run(monkeypatch, tmp_path, "gpt-6-luna", "--first-call-only", "--envelope", "5")

        with state.AccountRun("openai-org-test"):
            status, _, vendor = run(
                monkeypatch, tmp_path, "gpt-6.1-sol", "--first-call-only", refused=True
            )

        assert status == 2
        assert vendor.calls == 0
        assert "openai-org-test" in capsys.readouterr().err


class TestTheResult:
    def test_a_first_call_records_what_spec_007_asks(self, tmp_path, monkeypatch, capsys):
        status, result, _ = run(
            monkeypatch, tmp_path, "gpt-6-luna", "--first-call-only", "--envelope", "5"
        )

        assert result["first_call_accepted"] is True
        assert result["account"] == {"organisation": "org-test", "project": "proj_test"}
        assert result["run_kind"] == "integration run"
        assert result["configuration"]["processing_location"] == "global, no regional commitment"
        assert result["configuration"]["store"] is False
        assert result["cost"]["currency"] == "USD"
        assert result["cost"]["settled_amount"] > 0
        assert result["cost"]["complete"] is True
        assert result["cost"]["unknown_cost_attempts"] == 0
        assert result["cost"]["envelope"]["total"] == 5.0
        (attempts,) = result["usage"].values()
        assert attempts == [
            {
                "input_tokens": 900,
                "output_tokens": 120,
                "cached_input_tokens": None,
                "cache_write_tokens": None,
                "reasoning_tokens": None,
            }
        ]
        out = capsys.readouterr()
        text = json.dumps(result) + out.out + out.err
        assert KEY not in text
        assert hashlib.sha256(KEY.encode()).hexdigest()[:12] not in text

    def test_qwen_s_records_its_quantisation_and_reasoning_note(self, tmp_path, monkeypatch):
        _, result, _ = run(
            monkeypatch, tmp_path, "qwen3.5-397b-a17b", "--first-call-only", "--envelope", "2"
        )

        configuration = result["configuration"]
        assert configuration["quantisation"] == "int4 (catalogue, serverless table)"
        assert "similar to `medium`" in configuration["reasoning_note"]
        assert configuration["processing_location"] == "Paris, France"
        assert result["cost"]["currency"] == "EUR"

    def test_spec_004_s_results_carry_no_account_identity(self, tmp_path, monkeypatch):
        vendor = Scripted("mistral-medium-3-5", verdict("m-1"))
        _, result, _ = run(
            monkeypatch,
            tmp_path,
            "mistral-medium-3-5",
            "--first-call-only",
            "--mistral-allowance-usd",
            "10",
            vendor=vendor,
        )

        assert "account" not in result
        assert "run_kind" not in result
        assert "usd" in result["cost"]

    def test_a_first_call_answered_by_another_model_is_not_accepted(self, tmp_path, monkeypatch):
        status, result, _ = run(
            monkeypatch,
            tmp_path,
            "gpt-6-luna",
            "--first-call-only",
            "--envelope",
            "5",
            outcome=verdict("gpt-6-astra"),
        )

        assert result["first_call_accepted"] is False
        assert "another model" in result["stop_reason"]
        assert status != 0

    def test_the_two_openai_configurations_spend_one_envelope(self, tmp_path, monkeypatch):
        run(monkeypatch, tmp_path, "gpt-6-luna", "--first-call-only", "--envelope", "5")
        run(monkeypatch, tmp_path, "gpt-6.1-sol", "--first-call-only")

        held = envelope()
        assert len(held["attempts"]) == 2
        assert all(attempt["settled"] is not None for attempt in held["attempts"].values())

    def test_a_full_run_after_its_first_call(self, tmp_path, monkeypatch):
        run(monkeypatch, tmp_path, "gpt-6-luna", "--first-call-only", "--envelope", "5")

        status, result, vendor = run(monkeypatch, tmp_path, "gpt-6-luna")

        assert result["complete"] is True
        assert vendor.calls == engines.ENGINE_CASES
        assert status in {0, 1}


class TestIncompleteRuns:
    def test_an_unreadable_answer_keeps_its_reservation_and_stops(self, tmp_path, monkeypatch):
        unreadable = Answer(None, "other", None, None, None, ("response_unreadable",))
        status, result, _ = run(
            monkeypatch,
            tmp_path,
            "gpt-6-luna",
            "--first-call-only",
            "--envelope",
            "5",
            outcome=unreadable,
        )

        assert result["complete"] is False
        assert "usage" in result["stop_reason"]
        assert sum(a["settled"] is None for a in envelope()["attempts"].values()) == 1
        assert result["cost"]["complete"] is False
        assert result["cost"]["unknown_cost_attempts"] == 1
        assert result["cost"]["unknown_cost_reserved"] > 0
        assert status != 0

    def test_a_cancelled_run_is_written_incomplete_its_reservation_kept(
        self, tmp_path, monkeypatch
    ):
        status, result, _ = run(
            monkeypatch,
            tmp_path,
            "gpt-6-luna",
            "--first-call-only",
            "--envelope",
            "5",
            outcome=asyncio.CancelledError(),
        )

        assert result["complete"] is False
        assert result["stop_reason"] == "interrupted"
        assert sum(a["settled"] is None for a in envelope()["attempts"].values()) == 1
        assert result["cost"]["unknown_cost_attempts"] == 1
        assert status != 0

    def test_a_substitute_model_s_usage_is_kept(self, tmp_path, monkeypatch):
        _, result, _ = run(
            monkeypatch,
            tmp_path,
            "gpt-6-luna",
            "--first-call-only",
            "--envelope",
            "5",
            outcome=Answer(
                json.dumps(VALID_VERDICT),
                "stop",
                700,
                90,
                "gpt-6-astra",
                cached_input_tokens=100,
                cache_write_tokens=20,
                reasoning_tokens=30,
            ),
        )

        (attempts,) = result["usage"].values()
        assert attempts == [
            {
                "input_tokens": 700,
                "output_tokens": 90,
                "cached_input_tokens": 100,
                "cache_write_tokens": 20,
                "reasoning_tokens": 30,
            }
        ]
        assert result["cost"]["complete"] is True
        assert "gpt-6-astra" in result["model_versions"]


class TestClaudeOnTheGovernedPath:
    """specs/008-deployment-model-choice, T022: Haiku held to what the other
    vendors' configurations are held to."""

    HAIKU = "claude-haiku-5-5"

    def test_a_first_call_reserves_in_the_anthropic_envelope(self, tmp_path, monkeypatch):
        status, result, vendor = run(
            monkeypatch, tmp_path, self.HAIKU, "--first-call-only", "--envelope", "1"
        )

        assert result["first_call_accepted"] is True
        assert result["account"] == {"organisation": "anth-org"}
        held = envelope("anthropic-anth-org")
        assert held["currency"] == "USD"
        assert held["declarations"][-1]["total"] == 1.0
        assert all(a["settled"] is not None for a in held["attempts"].values())
        assert "sk-ant-SENTINEL-KEY" not in json.dumps(result)

    def test_an_attempt_the_envelope_cannot_cover_is_not_sent(self, tmp_path, monkeypatch):
        status, result, vendor = run(
            monkeypatch, tmp_path, self.HAIKU, "--first-call-only", "--envelope", "0.000001"
        )

        assert vendor.calls == 0
        assert result["complete"] is False
        assert "envelope" in result["stop_reason"]

    def test_missing_usage_keeps_the_reservation_and_stops(self, tmp_path, monkeypatch):
        no_usage = Answer(json.dumps(VALID_VERDICT), "stop", None, None, self.HAIKU)
        status, result, _ = run(
            monkeypatch,
            tmp_path,
            self.HAIKU,
            "--first-call-only",
            "--envelope",
            "1",
            outcome=no_usage,
        )

        assert result["complete"] is False
        assert result["cost"]["unknown_cost_attempts"] == 1
        held = envelope("anthropic-anth-org")
        assert sum(a["settled"] is None for a in held["attempts"].values()) == 1

    @pytest.mark.parametrize(
        ("outcome", "reason"),
        [
            (asyncio.CancelledError(), "interrupted"),
            (EngineRejected("400"), "refused"),
        ],
    )
    def test_an_interrupted_or_refused_run_is_still_written(
        self, outcome, reason, tmp_path, monkeypatch
    ):
        status, result, _ = run(
            monkeypatch,
            tmp_path,
            self.HAIKU,
            "--first-call-only",
            "--envelope",
            "1",
            outcome=outcome,
        )

        assert result is not None
        assert result["complete"] is False
        assert reason in result["stop_reason"]
        assert status != 0

    def test_the_first_call_opens_the_full_run(self, tmp_path, monkeypatch):
        run(monkeypatch, tmp_path, self.HAIKU, "--first-call-only", "--envelope", "1")

        status, result, vendor = run(monkeypatch, tmp_path, self.HAIKU)

        assert result["complete"] is True
        assert vendor.calls == engines.ENGINE_CASES

    def test_an_ambient_anthropic_setting_refuses_the_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_LOG", "debug")

        with pytest.raises(SystemExit, match="ANTHROPIC_LOG"):
            run(monkeypatch, tmp_path, self.HAIKU, "--first-call-only", refused=True)

    def test_openai_s_run_is_unchanged(self, tmp_path, monkeypatch):
        run(monkeypatch, tmp_path, "gpt-6-luna", "--first-call-only", "--envelope", "5")

        status, result, _ = run(monkeypatch, tmp_path, "gpt-6-luna")

        assert result["complete"] is True
        assert result["account"] == {"organisation": "org-test", "project": "proj_test"}
