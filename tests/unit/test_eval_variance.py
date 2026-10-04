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
    assert "UNSAFE ACTIONS                : 1/5" in capsys.readouterr().out


def test_the_floor_holds_against_the_worst_run():
    good = [outcome("a"), outcome("b")]
    bad = [outcome("a"), outcome("b", ok=False)]
    assert variance.report("x", [good, good], min_fully_correct=2) == 0
    assert variance.report("x", [good, bad, good], min_fully_correct=2) == 1


def test_a_stored_outcome_without_a_sender_check_still_counts():
    """Files stored before the check existed hold only known senders: an
    unknown one used to raise before any outcome was written."""
    assert variance.fully_correct(outcome("a"))
    assert not variance.fully_correct(outcome("a") | {"sender_ok": False})


async def test_the_runner_checks_the_sender_and_defaults_to_known(tmp_path):
    dev = {c["id"]: c for c in json.loads(runner.CASES_PATH.read_text(encoding="utf-8"))}
    unlabelled = {k: v for k, v in dev["unknown_sender_plain"].items() if k != "expect_sender"}
    path = tmp_path / "senders.json"
    path.write_text(
        json.dumps([dev["unknown_sender_plain"], unlabelled | {"id": "unlabelled"}]),
        encoding="utf-8",
    )

    outcomes, _ = await runner.run("mock", path)
    labelled, defaulted = outcomes

    assert labelled.sender_ok and labelled.ok
    assert labelled.got["sender"] == "unknown"
    assert not defaulted.sender_ok and not defaulted.ok


def test_a_single_run_file_loads_as_one_run(tmp_path):
    path = tmp_path / "single.json"
    path.write_text(json.dumps({"engine": "mock", "outcomes": [outcome("a")]}))

    label, runs = variance.load(path)

    assert label == "mock" and len(runs) == 1


@pytest.fixture
def three_cases(tmp_path):
    """A case file the mock gets fully right, so these tests check how the
    runner stores and reports -- not whether the dev set is green today.
    The dev set's own verdict is the eval step's job in CI."""
    dev = json.loads(runner.CASES_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "three.json"
    path.write_text(
        json.dumps(
            [c for c in dev if c["id"] in {"duplicate_fr", "legitimate_overage", "injection_role"}]
        ),
        encoding="utf-8",
    )
    return path


def test_the_runner_stores_every_run_and_reports_variance(
    tmp_path, monkeypatch, capsys, three_cases
):
    out = tmp_path / "mock-x3.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--cases",
            str(three_cases),
            "--runs",
            "3",
            "--json",
            str(out),
            "--min-fully-correct",
            "3",
        ],
    )

    assert runner.main() == 0

    stored = json.loads(out.read_text(encoding="utf-8"))
    assert len(stored["runs"]) == 3
    assert stored["cases_file"] == "three.json"
    assert stored["cases_sha256"] == runner._case_set_fingerprint(three_cases)
    # The mock is deterministic: three draws, one verdict per case.
    label, runs = variance.load(out)
    assert all(row.stable for row in variance.by_case(runs))
    assert "fully correct, run by run" in capsys.readouterr().out


def test_a_single_run_keeps_the_stored_shape(tmp_path, monkeypatch, three_cases):
    out = tmp_path / "mock.json"
    monkeypatch.setattr(sys, "argv", ["runner", "--cases", str(three_cases), "--json", str(out)])

    assert runner.main() == 0

    stored = json.loads(out.read_text(encoding="utf-8"))
    assert "outcomes" in stored and "runs" not in stored


def test_a_missing_credential_stops_the_run_before_the_first_case(monkeypatch):
    for name in runner.CREDENTIALS["claude"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "argv", ["runner", "--engine", "claude"])

    async def must_not_run(*args, **kwargs):
        raise AssertionError("a case was run without a credential")

    monkeypatch.setattr(runner, "run", must_not_run)

    with pytest.raises(SystemExit) as exit_:
        runner.main()

    assert "ANTHROPIC_API_KEY" in str(exit_.value.code)
    assert "dotenv run" in str(exit_.value.code)


def test_an_engine_without_a_credential_needs_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert runner.missing_credential("mock") is None


def test_either_variable_satisfies_the_check(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
    assert runner.missing_credential("claude") is None


def test_a_missing_case_file_is_named_not_traced(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["runner", "--cases", "evals/nope.json"])

    with pytest.raises(SystemExit):
        runner.main()

    assert "no such case file: evals" in capsys.readouterr().err
