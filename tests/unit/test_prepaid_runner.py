"""The runner for the configurations on prepaid accounts: envelopes, account
identity, results (specs/007-openai-qwen-engines, T050)."""

import asyncio
import hashlib
import json

import pytest

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
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-set-by-the-test")
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-key-set-by-the-test")
    monkeypatch.setenv("GEMINI_QUOTA_PROJECT", "resolve-billing-eval")
    monkeypatch.setattr("evals.limits.Limiter.acquire", _no_wait)
    monkeypatch.setattr("evals.state.AccountRun.wait", lambda self: 0.0)


def run(monkeypatch, tmp_path, name, *flags, outcome=None, vendor=None):
    vendor = vendor or Scripted(name, outcome)
    monkeypatch.setitem(engines.HOSTED_ENGINES, name, lambda: vendor)
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
        status, result, vendor = run(monkeypatch, tmp_path, "gpt-6-luna", "--first-call-only")

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
    def test_an_ambient_sdk_setting_is_refused_before_any_client(self, variable, monkeypatch):
        monkeypatch.setenv(variable, "debug")

        assert variable in runner.missing_credential("qwen3.5-397b-a17b")

    def test_one_run_per_billing_organisation_at_a_time(self, tmp_path, monkeypatch, capsys):
        run(monkeypatch, tmp_path, "gpt-6-luna", "--first-call-only", "--envelope", "5")

        with state.AccountRun("openai-org-test"):
            status, _, vendor = run(monkeypatch, tmp_path, "gpt-6.1-sol", "--first-call-only")

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
        assert result["cost"]["amount"] > 0
        assert result["cost"]["envelope"]["total"] == 5.0
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
        assert status != 0
