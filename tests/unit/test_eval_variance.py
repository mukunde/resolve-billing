"""Run-to-run aggregation of eval outcomes, and the runner's multi-run path."""

import json
import sys

import pytest

from evals import runner, variance


def outcome(case_id, *, ok=True, classification="duplicate_billing", unsafe=None):
    return {
        "case_id": case_id,
        "classification_ok": ok,
        "action_ok": ok,
        "approval_ok": True,
        "findings_ok": True,
        "skip_ok": True,
        "unsafe": unsafe,
        "confidence": 0.9,
        "confidence_kind": "self_reported",
        "cost_micros": 1500,
        "latency_ms": 900,
        "got": {"classification": classification, "action": "credit_note"},
    }


def test_pass_rate_is_counted_per_case_across_runs():
    runs = [
        [outcome("a"), outcome("b")],
        [outcome("a"), outcome("b", ok=False, classification="unknown")],
        [outcome("a"), outcome("b")],
    ]

    a, b = variance.by_case(runs)

    assert (a.fully_correct, a.runs, a.stable) == (3, 3, True)
    assert (b.fully_correct, b.classification_ok, b.stable) == (2, 2, False)
    assert b.classifications == {"duplicate_billing": 2, "unknown": 1}


def test_a_case_wrong_on_every_run_is_stable():
    """Stable means the verdict does not move, not that it is right."""
    (row,) = variance.by_case([[outcome("a", ok=False)]] * 3)
    assert row.fully_correct == 0 and row.stable


def test_runs_over_different_case_sets_are_refused():
    with pytest.raises(ValueError, match="run 2"):
        variance.by_case([[outcome("a")], [outcome("b")]])


def test_one_unsafe_draw_fails_the_report(capsys):
    runs = [[outcome("a")]] * 4 + [[outcome("a", unsafe="refund proposed without approval")]]
    assert variance.report("x", runs) == 1
    assert "UNSAFE FINANCIAL ACTIONS      : 1/5" in capsys.readouterr().out


def test_the_floor_holds_against_the_worst_run():
    good = [outcome("a"), outcome("b")]
    bad = [outcome("a"), outcome("b", ok=False)]
    assert variance.report("x", [good, good], min_fully_correct=2) == 0
    assert variance.report("x", [good, bad, good], min_fully_correct=2) == 1


def test_a_single_run_file_loads_as_one_run(tmp_path):
    path = tmp_path / "single.json"
    path.write_text(json.dumps({"engine": "mock", "outcomes": [outcome("a")]}))

    label, runs = variance.load(path)

    assert label == "mock" and len(runs) == 1


def test_the_runner_stores_every_run_and_reports_variance(tmp_path, monkeypatch, capsys):
    out = tmp_path / "mock-x3.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "--runs", "3", "--json", str(out), "--min-fully-correct", "9"],
    )

    assert runner.main() == 0

    stored = json.loads(out.read_text(encoding="utf-8"))
    assert len(stored["runs"]) == 3
    assert stored["cases_file"] == "cases.json"
    assert stored["cases_sha256"] == runner._case_set_fingerprint()
    # The mock is deterministic: three draws, one verdict per case.
    label, runs = variance.load(out)
    assert all(row.stable for row in variance.by_case(runs))
    assert "fully correct, run by run" in capsys.readouterr().out


def test_a_single_run_keeps_the_stored_shape(tmp_path, monkeypatch):
    out = tmp_path / "mock.json"
    monkeypatch.setattr(sys, "argv", ["runner", "--json", str(out)])

    assert runner.main() == 0

    stored = json.loads(out.read_text(encoding="utf-8"))
    assert "outcomes" in stored and "runs" not in stored
