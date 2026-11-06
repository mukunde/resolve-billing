"""The comparison of prompt v3 and v4, applied as declared before the runs
(specs/012-prompt-v4-usage, FR-007). Synthetic result files; no network."""

import json
from dataclasses import asdict

import pytest

from app.api.deps import FAILOVER
from app.decision.bounded import Bounds
from app.decision.failover import SECONDARY_BOUNDS
from app.decision.masking import MASKING_VERSION
from evals import engines, prompt_compare
from evals.first_calls import fingerprint
from evals.service_run import ENGINE, HAIKU, measured

CASE_IDS = [c["id"] for c in json.loads(engines.DEVELOPMENT_SET.read_bytes())]
NEW = prompt_compare.NEW_CASES
EXPECTED = {c["id"]: c for c in json.loads(engines.DEVELOPMENT_SET.read_bytes())}


def outcome(case_id, *, right=True, unsafe=None, recommendation=None):
    case = EXPECTED[case_id]
    got = {
        "classification": case["expect_classification"],
        "recommended_action": recommendation or case.get("expect_engine_action", "escalate"),
        "action": case["expect_action"],
        "approval": case["expect_human_approval"],
        "automatic_reply": case.get("expect_automatic_reply", False),
    }
    checks = dict.fromkeys(
        (
            "classification_ok",
            "action_ok",
            "approval_ok",
            "findings_ok",
            "skip_ok",
            "sender_ok",
            "reply_ok",
            "recommendation_ok",
        ),
        True,
    )
    if not right:
        checks["classification_ok"] = False
        got["classification"] = "something_else"
    if recommendation is not None and recommendation != case.get("expect_engine_action"):
        checks["recommendation_ok"] = False
    return {"case_id": case_id, "unsafe": unsafe, "got": got, **checks}


def run(leg, prompt, n, *, overrides=None, wrong=(), unsafe=(), recommendations=None):
    name = HAIKU if leg == "haiku" else ENGINE
    record = {
        "engine": FAILOVER if leg == "haiku" else ENGINE,
        "engine_version": f"x/{prompt}",
        "primary_disabled": leg == "haiku",
        "run_kind": "service run",
        "complete": True,
        "stop_reason": None,
        "not_attempted": [],
        "cases_sha256": engines.DEVELOPMENT_SET_SHA256[:16],
        "ran_at": f"2026-11-06T0{n}:00:00+00:00-{leg}-{prompt}",
        "bounds": asdict(SECONDARY_BOUNDS if leg == "haiku" else Bounds()),
        "configuration_fingerprint": fingerprint(measured(name, prompt)),
        "prompt_version": prompt,
        "masking_version": MASKING_VERSION,
        "schema_sha256": prompt_compare._schema_hash(),
        "commit": "abc1234",
        "clean_tree": True,
        "attempts": [{"attempt_id": f"{leg}-{prompt}-{n}-{i}"} for i in range(2)],
        "outcomes": [
            outcome(
                c,
                right=c not in wrong,
                unsafe="money" if c in unsafe else None,
                recommendation=(recommendations or {}).get(c),
            )
            for c in CASE_IDS
        ],
    }
    record.update(overrides or {})
    return record


def files(tmp_path, records):
    paths = []
    for index, record in enumerate(records):
        path = tmp_path / f"run-{index}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        paths.append(path)
    return paths


def twelve(**v4):
    """Three runs per leg and prompt; `v4` changes every v4 run of both legs."""
    records = []
    for leg in ("luna", "haiku"):
        for prompt in ("decision_v3", "decision_v4"):
            for n in range(3):
                records.append(run(leg, prompt, n, **(v4 if prompt == "decision_v4" else {})))
    return records


class TestTheVerdicts:
    def test_adopt(self, tmp_path):
        report = prompt_compare.compare(files(tmp_path, twelve()))
        assert report["legs"]["luna"]["verdict"] == "adopt"
        assert report["legs"]["haiku"]["verdict"] == "adopt"

    def test_an_unsafe_action_under_v4_blocks(self, tmp_path):
        report = prompt_compare.compare(files(tmp_path, twelve(unsafe={"duplicate_fr"})))
        assert report["legs"]["luna"]["verdict"] == "do not adopt"

    def test_an_unsafe_action_under_v3_blocks_too(self, tmp_path):
        """FR-007: no unsafe action on every run, whichever prompt."""
        records = twelve()
        records[0] = run("luna", "decision_v3", 0, unsafe={"duplicate_fr"})
        report = prompt_compare.compare(files(tmp_path, records))
        assert report["legs"]["luna"]["verdict"] == "do not adopt"

    def test_a_change_in_frequency_is_listed(self, tmp_path):
        records = twelve()
        case = CASE_IDS[0]
        # v3: right twice, wrong once; v4: right once, wrong twice. Same kinds.
        records[0]["outcomes"][0] = outcome(case, right=False)
        records[3]["outcomes"][0] = outcome(case, right=False)
        records[4]["outcomes"][0] = outcome(case, right=False)
        report = prompt_compare.compare(files(tmp_path, records))
        assert case in report["legs"]["luna"]["changed"]

    def test_the_report_shows_each_new_case_run_by_run(self, tmp_path):
        text = prompt_compare.markdown(prompt_compare.compare(files(tmp_path, twelve())))
        assert "`usage_overbilled_fr`: unknown / escalate / none; unknown" in text

    def test_a_new_case_missed_blocks(self, tmp_path):
        report = prompt_compare.compare(files(tmp_path, twelve(wrong={NEW[0]})))
        assert report["legs"]["haiku"]["verdict"] == "do not adopt"
        assert NEW[0] in report["legs"]["haiku"]["new_cases_missed"]

    def test_a_suppressed_credit_note_is_a_miss(self, tmp_path):
        records = twelve(recommendations={"usage_overbilled_fr": "propose_credit_note"})
        report = prompt_compare.compare(files(tmp_path, records))
        assert report["legs"]["luna"]["verdict"] == "do not adopt"

    def test_a_regression_goes_to_the_maintainer(self, tmp_path):
        records = twelve()
        records[3]["outcomes"][0] = outcome(CASE_IDS[0], right=False)  # Luna v4, run 1
        report = prompt_compare.compare(files(tmp_path, records))
        leg = report["legs"]["luna"]
        assert leg["verdict"] == "regression: the maintainer decides"
        assert leg["regressions"] == [CASE_IDS[0]]
        assert CASE_IDS[0] in leg["changed"]

    def test_a_case_v3_already_missed_is_no_regression(self, tmp_path):
        records = twelve()
        records[0]["outcomes"][0] = outcome(CASE_IDS[0], right=False)  # Luna v3, run 1
        records[3]["outcomes"][0] = outcome(CASE_IDS[0], right=False)  # Luna v4, run 1
        report = prompt_compare.compare(files(tmp_path, records))
        assert report["legs"]["luna"]["regressions"] == []

    def test_the_report_says_what_three_runs_show(self, tmp_path):
        text = prompt_compare.markdown(prompt_compare.compare(files(tmp_path, twelve())))
        assert "do not establish non-degradation" in text


class TestTheRefusals:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"complete": False},
            {"stop_reason": "RunStopped"},
            {"not_attempted": ["duplicate_fr"]},
            {"clean_tree": False},
            {"commit": "other"},
            {"cases_sha256": "other"},
            {"masking_version": "m2"},
            {"schema_sha256": "s2"},
            {"commit": "abc1235"},
            {"run_kind": "hosted run"},
            {"prompt_version": None},
            {"bounds": {"attempts": 9}},
            {"configuration_fingerprint": "f00"},
        ],
    )
    def test_a_run_out_of_line(self, overrides, tmp_path):
        records = twelve()
        records[5].update(overrides)
        with pytest.raises(prompt_compare.Refused):
            prompt_compare.compare(files(tmp_path, records))

    def test_a_copied_run_with_another_start(self, tmp_path):
        records = twelve()
        copy = json.loads(json.dumps(records[0]))
        copy["ran_at"] = "2026-11-06T09:00:00+00:00"
        records[1] = copy
        with pytest.raises(prompt_compare.Refused, match="distinct"):
            prompt_compare.compare(files(tmp_path, records))

    @pytest.mark.parametrize("key", ["commit", "masking_version", "schema_sha256"])
    def test_evidence_missing_everywhere(self, key, tmp_path):
        records = twelve()
        for record in records:
            record.pop(key)
        with pytest.raises(prompt_compare.Refused, match=key):
            prompt_compare.compare(files(tmp_path, records))

    @pytest.mark.parametrize(
        ("key", "value"),
        [("masking_version", "m1"), ("schema_sha256", "s1"), ("commit", "not-a-commit")],
    )
    def test_the_same_wrong_evidence_everywhere(self, key, value, tmp_path):
        records = twelve()
        for record in records:
            record[key] = value
        with pytest.raises(prompt_compare.Refused, match=key.split("_")[0]):
            prompt_compare.compare(files(tmp_path, records))

    def test_a_run_without_attempts(self, tmp_path):
        records = twelve()
        records[4]["attempts"] = []
        with pytest.raises(prompt_compare.Refused, match="attempt"):
            prompt_compare.compare(files(tmp_path, records))

    @pytest.mark.parametrize("drop", ["recommendation_ok", "got"])
    def test_an_outcome_without_its_checks(self, drop, tmp_path):
        records = twelve()
        records[3]["outcomes"][0].pop(drop)
        with pytest.raises(prompt_compare.Refused, match="outcome"):
            prompt_compare.compare(files(tmp_path, records))

    def test_a_duplicated_run(self, tmp_path):
        records = twelve()
        records[1] = dict(records[0])
        with pytest.raises(prompt_compare.Refused, match="distinct"):
            prompt_compare.compare(files(tmp_path, records))

    def test_a_missing_case(self, tmp_path):
        records = twelve()
        records[2]["outcomes"] = records[2]["outcomes"][1:]
        with pytest.raises(prompt_compare.Refused, match="cases"):
            prompt_compare.compare(files(tmp_path, records))

    def test_a_repeated_case(self, tmp_path):
        records = twelve()
        records[2]["outcomes"] = records[2]["outcomes"][:-1] + [records[2]["outcomes"][0]]
        with pytest.raises(prompt_compare.Refused, match="cases"):
            prompt_compare.compare(files(tmp_path, records))

    def test_eleven_runs(self, tmp_path):
        with pytest.raises(prompt_compare.Refused, match="three"):
            prompt_compare.compare(files(tmp_path, twelve()[:-1]))

    def test_a_leg_with_the_wrong_engine(self, tmp_path):
        records = twelve()
        records[6]["primary_disabled"] = False
        with pytest.raises(prompt_compare.Refused):
            prompt_compare.compare(files(tmp_path, records))


class TestTheCommand:
    def test_it_writes_the_report(self, tmp_path):
        paths = files(tmp_path, twelve())
        target = tmp_path / "report.md"
        assert prompt_compare.main([*map(str, paths), "--report", str(target)]) == 0
        assert "adopt" in target.read_text(encoding="utf-8")

    def test_a_refusal_writes_nothing(self, tmp_path):
        paths = files(tmp_path, twelve()[:-1])
        target = tmp_path / "report.md"
        assert prompt_compare.main([*map(str, paths), "--report", str(target)]) == 2
        assert not target.exists()
