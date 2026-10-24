"""A first call accepted, recorded, and the full run it opens
(specs/004-second-engines, second amendment; T103)."""

import hashlib
import json
from dataclasses import replace

import pytest

from app.decision.hosted import Answer, configuration
from evals import engines, first_calls, runner, state
from evals.first_calls import FirstCalls, FirstCallsRefused
from tests.unit.test_evaluation_limits import Vendor, _no_wait, ok

PROJECT = "resolve-billing-eval"
MISTRAL = ("mistral-medium-3-5", "mistral-medium-3-5-reasoning")


class Always(Vendor):
    def __init__(self, name, answer=None):
        super().__init__([], name=name)
        self.answer = answer

    async def _complete(self, system, user):
        self.calls += 1
        return self.answer or ok()


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-set-by-the-test")
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-key-set-by-the-test")
    monkeypatch.setenv("GEMINI_QUOTA_PROJECT", PROJECT)
    monkeypatch.setattr("evals.limits.Limiter.acquire", _no_wait)
    # Runs follow one another here: the minute between them is not the subject.
    monkeypatch.setattr("evals.state.AccountRun.wait", lambda self: 0.0)


def run(monkeypatch, tmp_path, name, *extra, answer=None):
    vendor = Always(name, answer)
    monkeypatch.setitem(engines.HOSTED_ENGINES, name, lambda: vendor)
    argv = ["runner", "--engine", name, "--data-terms", "unknown"]
    target = tmp_path / f"{name}.json"
    flags = list(extra)
    if name.startswith("mistral") and not (state.STATE_DIR / "mistral-envelope.json").exists():
        flags += ["--mistral-allowance-usd", "10"]
    if name.startswith("gemini") and "--gemini-requests-used-today" not in flags:
        held = state.STATE_DIR / f"quota-{PROJECT}.json"
        if not held.exists():
            flags += ["--gemini-requests-used-today", "0"]
    before = set(tmp_path.glob(f"{name}-*.json"))
    monkeypatch.setattr("sys.argv", [*argv, "--json", str(target), *flags])
    status = runner.main()
    written = sorted(set(tmp_path.glob(f"{name}-*.json")) - before)
    result = json.loads(written[-1].read_text(encoding="utf-8")) if written else None
    return status, result, vendor, (written[-1] if written else None)


def held() -> dict:
    return json.loads((state.STATE_DIR / "first-calls.json").read_text(encoding="utf-8"))


class TestAcceptance:
    def test_a_valid_verdict_with_its_usage_and_model_version_is_accepted(
        self, tmp_path, monkeypatch
    ):
        status, result, _, path = run(
            monkeypatch, tmp_path, "mistral-medium-3-5", "--first-call-only"
        )

        assert result["stop_reason"] == "first call accepted"
        assert result["first_call_accepted"] is True
        record = held()["first_calls"]["mistral-medium-3-5"]
        assert record["model_version"] == "m-1"
        assert record["result"] == str(path)
        assert record["result_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_an_invalid_answer_is_not_accepted(self, tmp_path, monkeypatch):
        status, result, _, _ = run(
            monkeypatch, tmp_path, "mistral-medium-3-5", "--first-call-only", answer=ok(text="{no")
        )

        assert result["stop_reason"] == "first call not accepted: invalid answer"
        assert result["first_call_accepted"] is False
        assert not (state.STATE_DIR / "first-calls.json").exists()

    def test_a_verdict_without_a_model_version_is_not_accepted(self, tmp_path, monkeypatch):
        bare = Answer(json.dumps(json.loads(ok().text)), "stop", 900, 120, None)

        status, result, _, _ = run(
            monkeypatch, tmp_path, "mistral-medium-3-5", "--first-call-only", answer=bare
        )

        assert result["stop_reason"] == "first call not accepted: no model version returned"
        assert result["first_call_accepted"] is False

    def test_missing_usage_is_not_accepted(self, tmp_path, monkeypatch):
        status, result, _, _ = run(
            monkeypatch,
            tmp_path,
            "mistral-medium-3-5",
            "--first-call-only",
            answer=ok(input_tokens=None),
        )

        assert "usage" in result["stop_reason"]
        assert result["first_call_accepted"] is False
        assert not (state.STATE_DIR / "first-calls.json").exists()

    def test_gemini_s_first_call_is_accepted_the_same_way(self, tmp_path, monkeypatch):
        status, result, _, _ = run(monkeypatch, tmp_path, "gemini-3.8-flash", "--first-call-only")

        assert result["stop_reason"] == "first call accepted"
        assert "gemini-3.8-flash" in held()["first_calls"]

    def test_a_full_run_records_no_acceptance_field_as_true(self, tmp_path, monkeypatch):
        run(monkeypatch, tmp_path, "gemini-3.8-flash", "--first-call-only")
        quota = state.STATE_DIR / f"quota-{PROJECT}.json"
        quota.unlink()  # a fresh quota day for the full run

        status, result, _, _ = run(monkeypatch, tmp_path, "gemini-3.8-flash")

        assert result["complete"] is True
        assert result["first_call_accepted"] is None


class TestEvidenceBeforeAcceptance:
    def test_nothing_is_recorded_when_the_result_cannot_be_written(self, tmp_path, monkeypatch):
        def unwritable(*_):
            raise OSError("disk full")

        monkeypatch.setattr("evals.hosted_run._new_file", unwritable)

        with pytest.raises(OSError):
            run(monkeypatch, tmp_path, "mistral-medium-3-5", "--first-call-only")
        assert not (state.STATE_DIR / "first-calls.json").exists()

    def test_an_interruption_after_the_result_leaves_it_unrecorded(self, tmp_path, monkeypatch):
        def interrupted(*_, **__):
            raise KeyboardInterrupt

        monkeypatch.setattr(FirstCalls, "record", interrupted)

        with pytest.raises(KeyboardInterrupt):
            run(monkeypatch, tmp_path, "mistral-medium-3-5", "--first-call-only")
        assert list(tmp_path.glob("mistral-medium-3-5-*.json"))
        assert not (state.STATE_DIR / "first-calls.json").exists()


class TestTheGate:
    def test_a_full_run_without_its_first_call_is_refused_before_any_client(
        self, tmp_path, monkeypatch, capsys
    ):
        built = []
        monkeypatch.setitem(
            engines.HOSTED_ENGINES,
            "gemini-3.8-flash",
            lambda: built.append(1) or Always("gemini-3.8-flash"),
        )
        argv = ["runner", "--engine", "gemini-3.8-flash", "--data-terms", "unknown"]
        extra = ["--json", str(tmp_path / "g.json"), "--gemini-requests-used-today", "0"]
        monkeypatch.setattr("sys.argv", [*argv, *extra])

        assert runner.main() == 2
        assert built == []
        assert "first call" in capsys.readouterr().err

    def test_a_mistral_full_run_needs_both_first_calls(self, tmp_path, monkeypatch, capsys):
        run(monkeypatch, tmp_path, "mistral-medium-3-5", "--first-call-only")

        status, result, vendor, _ = run(monkeypatch, tmp_path, "mistral-medium-3-5")
        assert status == 2
        assert vendor.calls == 0
        assert "mistral-medium-3-5-reasoning" in capsys.readouterr().err

        run(monkeypatch, tmp_path, "mistral-medium-3-5-reasoning", "--first-call-only")
        status, result, _, _ = run(monkeypatch, tmp_path, "mistral-medium-3-5")
        assert result["complete"] is True

    @pytest.mark.parametrize(
        "change",
        ["field", "prompt_version", "schema", "masking_version", "estimator"],
    )
    def test_any_change_to_the_fingerprinted_whole_calls_for_a_new_first_call(
        self, monkeypatch, change
    ):
        config = configuration("gemini-3.8-flash")
        book = FirstCalls()
        book.record(config, "m-1", _a_result())
        book.admit_full_run(config)

        if change == "field":
            config = replace(config, max_output_tokens=8192)
        elif change == "prompt_version":
            monkeypatch.setattr("app.decision.hosted.PROMPT_VERSION", "decision_v4")
        elif change == "schema":
            monkeypatch.setattr(first_calls, "verdict_schema", lambda: {"type": "object"})
        elif change == "masking_version":
            monkeypatch.setattr(first_calls, "MASKING_VERSION", "masking_v2")
        else:
            config = replace(config, reservation_estimator="utf8-bytes-v2")

        with pytest.raises(FirstCallsRefused, match="gemini-3.8-flash"):
            book.admit_full_run(config)

    @pytest.mark.parametrize(
        ("variable", "value"),
        [("GEMINI_API_KEY", "another-key"), ("GEMINI_QUOTA_PROJECT", "another-project")],
    )
    def test_another_key_or_project_calls_for_a_new_first_call(self, monkeypatch, variable, value):
        config = configuration("gemini-3.8-flash")
        book = FirstCalls()
        book.record(config, "m-1", _a_result())
        monkeypatch.setenv(variable, value)

        with pytest.raises(FirstCallsRefused):
            book.admit_full_run(config)

    @pytest.mark.parametrize("evidence", ["deleted", "modified"])
    def test_a_full_run_needs_its_first_call_s_evidence_intact(self, evidence):
        config = configuration("gemini-3.8-flash")
        result = _a_result()
        book = FirstCalls()
        book.record(config, "m-1", result)
        book.admit_full_run(config)

        if evidence == "deleted":
            result.unlink()
        else:
            result.write_text('{"edited": true}', encoding="utf-8")

        with pytest.raises(FirstCallsRefused, match="missing or changed"):
            book.admit_full_run(config)

    def test_the_key_never_reaches_the_state(self):
        FirstCalls().record(configuration("gemini-3.8-flash"), "m-1", _a_result())

        text = (state.STATE_DIR / "first-calls.json").read_text(encoding="utf-8")
        assert "gemini-key-set-by-the-test" not in text
        expected = hashlib.sha256(b"gemini-key-set-by-the-test").hexdigest()[:12]
        assert held()["first_calls"]["gemini-3.8-flash"]["account"]["key"] == expected

    @pytest.mark.parametrize("content", ["{nope", '{"first_calls": []}', '{"other": {}}'])
    def test_a_corrupt_state_is_refused_never_reset(self, content):
        state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = state.STATE_DIR / "first-calls.json"
        path.write_text(content, encoding="utf-8")

        with pytest.raises(FirstCallsRefused):
            FirstCalls().admit_full_run(configuration("gemini-3.8-flash"))
        assert path.read_text(encoding="utf-8") == content


def _a_result():
    state.STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = state.STATE_DIR.parent / "a-result.json"
    path.write_text("{}", encoding="utf-8")
    return path
