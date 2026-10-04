"""The per-case baseline: a verdict that moves on any case fails the run.

A floor is a count, and a count lets one case break while another is fixed.
The baseline holds every check of every case, so neither goes unnoticed.
"""

import json
import sys

import pytest

from evals import runner


def outcome(case_id, **checks) -> runner.CaseOutcome:
    values = {f"{name}_ok": True for name in runner.CHECK_NAMES} | {
        f"{name}_ok": ok for name, ok in checks.items()
    }
    return runner.CaseOutcome(
        case_id=case_id,
        unsafe=None,
        confidence=1.0,
        confidence_kind="calibrated",
        cost_micros=None,
        latency_ms=None,
        **values,
    )


def recorded(*outcomes) -> dict:
    return runner.baseline_of(list(outcomes))


class TestCompare:
    def test_the_same_verdicts_match(self):
        same = [outcome("a"), outcome("b", action=False)]

        assert runner.compare_to_baseline(recorded(*same), same) == []

    def test_a_regression_is_named_with_its_check(self):
        baseline = recorded(outcome("a"))

        (diff,) = runner.compare_to_baseline(baseline, [outcome("a", classification=False)])

        assert diff == "a: classification passed in the baseline, fails now"

    def test_an_improvement_is_reported_too(self):
        """So the baseline is updated in the same commit, where review sees it."""
        baseline = recorded(outcome("a", action=False))

        (diff,) = runner.compare_to_baseline(baseline, [outcome("a")])

        assert diff == "a: action failed in the baseline, passes now"

    def test_a_case_already_failing_that_fails_one_more_check_is_caught(self):
        """What a fully-correct count cannot see."""
        baseline = recorded(outcome("a", action=False))

        diffs = runner.compare_to_baseline(baseline, [outcome("a", action=False, approval=False)])

        assert diffs == ["a: approval passed in the baseline, fails now"]

    def test_a_swap_between_two_cases_is_caught(self):
        """The count stays the same; the baseline does not."""
        baseline = recorded(outcome("a"), outcome("b", action=False))

        diffs = runner.compare_to_baseline(baseline, [outcome("a", action=False), outcome("b")])

        assert len(diffs) == 2

    def test_a_new_case_must_be_recorded(self):
        diffs = runner.compare_to_baseline(recorded(outcome("a")), [outcome("a"), outcome("b")])

        assert diffs == ["b: not in the baseline"]

    def test_a_removed_case_is_reported(self):
        diffs = runner.compare_to_baseline(recorded(outcome("a"), outcome("b")), [outcome("a")])

        assert diffs == ["b: in the baseline, no longer in the case set"]


@pytest.fixture
def three_cases(tmp_path):
    """Three dev cases, so these tests check how the baseline is written and
    read -- not whether the dev set is green today."""
    dev = json.loads(runner.CASES_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "three.json"
    path.write_text(
        json.dumps(
            [c for c in dev if c["id"] in {"duplicate_fr", "legitimate_overage", "injection_role"}]
        ),
        encoding="utf-8",
    )
    return path


def run_main(monkeypatch, *args) -> int:
    monkeypatch.setattr(sys, "argv", ["runner", *map(str, args)])
    return runner.main()


class TestRunner:
    def test_a_written_baseline_is_matched_by_the_next_run(
        self, tmp_path, monkeypatch, capsys, three_cases
    ):
        baseline = tmp_path / "mock.json"

        assert run_main(monkeypatch, "--cases", three_cases, "--write-baseline", baseline) == 0
        assert run_main(monkeypatch, "--cases", three_cases, "--baseline", baseline) == 0

        stored = json.loads(baseline.read_text(encoding="utf-8"))
        assert stored["engine"] == "mock"
        assert set(stored["cases"]) == {"duplicate_fr", "legitimate_overage", "injection_role"}
        assert "matched case by case" in capsys.readouterr().out

    def test_a_moved_verdict_fails_the_run_and_says_how_to_record_it(
        self, tmp_path, monkeypatch, capsys, three_cases
    ):
        baseline = tmp_path / "mock.json"
        run_main(monkeypatch, "--cases", three_cases, "--write-baseline", baseline)
        stored = json.loads(baseline.read_text(encoding="utf-8"))
        stored["cases"]["duplicate_fr"]["action"] = False
        baseline.write_text(json.dumps(stored), encoding="utf-8")

        assert run_main(monkeypatch, "--cases", three_cases, "--baseline", baseline) == 1

        out = capsys.readouterr().out
        assert "duplicate_fr: action failed in the baseline, passes now" in out
        assert "--write-baseline" in out

    def test_a_baseline_from_another_engine_is_refused(
        self, tmp_path, monkeypatch, capsys, three_cases
    ):
        baseline = tmp_path / "mock.json"
        run_main(monkeypatch, "--cases", three_cases, "--write-baseline", baseline)
        stored = json.loads(baseline.read_text(encoding="utf-8"))
        baseline.write_text(json.dumps(stored | {"engine": "claude"}), encoding="utf-8")

        assert run_main(monkeypatch, "--cases", three_cases, "--baseline", baseline) == 1
        assert "recorded with engine 'claude'" in capsys.readouterr().out

    def test_a_baseline_needs_a_single_run(self, tmp_path, monkeypatch, three_cases):
        """Several runs means a sampling engine, whose verdicts are not exact."""
        with pytest.raises(SystemExit):
            run_main(
                monkeypatch, "--cases", three_cases, "--runs", "2", "--baseline", tmp_path / "x"
            )
