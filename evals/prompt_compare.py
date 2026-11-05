"""Prompt v3 against v4, by the rule declared before the runs
(specs/012-prompt-v4-usage, FR-007).

Twelve service runs on the development set, on one commit: Luna alone and the
composite with Luna's leg disabled (Haiku), each under v3 and v4, three times.
The rule, per leg:

- no unsafe action on any v4 run;
- every new usage case meets its classification, the engine's recommendation
  and the system's action on every v4 run;
- on the other cases, every outcome that differs between v3 and v4 is listed;
  a case all three v3 runs got fully right and any v4 run did not is a
  regression, and goes to the maintainer.

Three runs show variance; they do not establish non-degradation.

    python -m evals.prompt_compare RESULT.json ... --report evals/results/prompt-v4-comparison.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from app.api.deps import FAILOVER, HAIKU
from app.decision.bounded import Bounds
from app.decision.failover import SECONDARY_BOUNDS
from evals import engines
from evals.first_calls import fingerprint
from evals.runner import CHECK_NAMES
from evals.service_run import ENGINE, RUN_KIND, measured

PROMPTS = ("decision_v3", "decision_v4")
RUNS = 3

#: The usage cases spec 012 added, labels adjudicated by the maintainer on 2026-11-05.
NEW_CASES = (
    "usage_overbilled_fr",
    "usage_overbilled_en",
    "usage_overbilled_terse",
    "usage_corroborated_control",
    "usage_overbilled_other_complaint",
    "usage_underbilled_explanation",
    "usage_unavailable_disputed",
)

#: What a new case must meet on every v4 run.
NEW_CASE_CHECKS = ("classification_ok", "recommendation_ok", "action_ok")

#: What an outcome is, for telling v3 and v4 apart.
OUTCOME = ("classification", "recommended_action", "action", "approval", "automatic_reply")

#: Every check a stored outcome carries; all must be there, as booleans.
CHECKS = tuple(f"{name}_ok" for name in CHECK_NAMES)

STATEMENT = "Three runs show variance; they do not establish non-degradation."


class Refused(Exception):  # noqa: N818 -- a refusal, not a defect
    """The result files are not the twelve runs the rule was declared for."""


def _leg(record: dict) -> str:
    if record.get("engine") == ENGINE and record.get("primary_disabled") is False:
        return "luna"
    if record.get("engine") == FAILOVER and record.get("primary_disabled") is True:
        return "haiku"
    raise Refused(f"{record.get('_file')}: neither Luna alone nor Haiku's leg")


def _expected(leg: str, prompt: str) -> tuple[dict, str]:
    """The bounds as a result file holds them (through JSON), and the fingerprint."""
    if leg == "luna":
        bounds, name = Bounds(), ENGINE
    else:
        bounds, name = SECONDARY_BOUNDS, HAIKU
    return json.loads(json.dumps(asdict(bounds))), fingerprint(measured(name, prompt))


def _fully_correct(outcome: dict) -> bool:
    return all(outcome[check] for check in CHECKS)


def _valid_outcome(outcome) -> bool:
    """Every check, a boolean; the unsafe flag; what the system did."""
    return (
        isinstance(outcome, dict)
        and all(isinstance(outcome.get(check), bool) for check in CHECKS)
        and (outcome.get("unsafe") is None or isinstance(outcome.get("unsafe"), str))
        and isinstance(outcome.get("got"), dict)
        and all(key in outcome["got"] for key in OUTCOME)
    )


def load(paths: list[Path]) -> dict[tuple[str, str], list[dict]]:
    """The runs by (leg, prompt), or a refusal."""
    case_ids = sorted(c["id"] for c in json.loads(engines.DEVELOPMENT_SET.read_bytes()))
    records = []
    for path in paths:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        record["_file"] = Path(path).name
        records.append(record)
    if len({r.get("ran_at") for r in records}) != len(records):
        raise Refused("the runs are not distinct: two share a start")
    if len({str(Path(p).resolve()) for p in paths}) != len(paths):
        raise Refused("the runs are not distinct: a file is given twice")
    shared = ("commit", "cases_sha256", "masking_version", "schema_sha256")
    for key in shared:
        values = {r.get(key) for r in records}
        if len(values) != 1 or not all(isinstance(v, str) and v for v in values):
            raise Refused(f"the runs do not share one {key}")
    # A run is its attempts: a copy with another start is still the same run.
    attempt_ids = []
    for record in records:
        attempts = record.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            raise Refused(f"{record['_file']}: records no attempt")
        for attempt in attempts:
            ident = attempt.get("attempt_id") if isinstance(attempt, dict) else None
            if not isinstance(ident, str) or not ident:
                raise Refused(f"{record['_file']}: an attempt without its id")
            attempt_ids.append(ident)
    if len(set(attempt_ids)) != len(attempt_ids):
        raise Refused("the runs are not distinct: two share an attempt")
    if records and records[0].get("cases_sha256") != engines.DEVELOPMENT_SET_SHA256[:16]:
        raise Refused("the runs are not on today's development set")
    grouped: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        name = record["_file"]
        if record.get("run_kind") != RUN_KIND:
            raise Refused(f"{name}: not a service run")
        if not (
            record.get("complete") is True
            and record.get("stop_reason") is None
            and record.get("not_attempted") == []
        ):
            raise Refused(f"{name}: not a complete run")
        if record.get("clean_tree") is not True:
            raise Refused(f"{name}: not on a clean tree")
        prompt = record.get("prompt_version")
        if prompt not in PROMPTS:
            raise Refused(f"{name}: names no prompt of the comparison")
        leg = _leg(record)
        bounds, expected = _expected(leg, prompt)
        if record.get("bounds") != bounds:
            raise Refused(f"{name}: not the leg's bounds")
        if record.get("configuration_fingerprint") != expected:
            raise Refused(f"{name}: not the configuration this leg sends under {prompt}")
        outcomes = record.get("outcomes", [])
        if not all(_valid_outcome(o) for o in outcomes):
            raise Refused(f"{name}: an outcome without its checks or what the system did")
        ids = [o.get("case_id") for o in outcomes]
        if sorted(ids) != case_ids:
            raise Refused(f"{name}: does not hold the development cases, each once")
        grouped.setdefault((leg, prompt), []).append(record)
    for leg in ("luna", "haiku"):
        for prompt in PROMPTS:
            if len(grouped.get((leg, prompt), [])) != RUNS:
                raise Refused(f"{leg} under {prompt}: not three runs")
    return grouped


def _by_case(runs: list[dict]) -> dict[str, list[dict]]:
    held: dict[str, list[dict]] = {}
    for record in runs:
        for o in record["outcomes"]:
            held.setdefault(o["case_id"], []).append(o)
    return held


def _seen(outcomes: list[dict]) -> Counter:
    """What the system did, with how often: frequencies count, not only kinds."""
    return Counter(tuple(o["got"][k] for k in OUTCOME) for o in outcomes)


def _shown(seen: Counter) -> list[str]:
    return [f"{n}x {' / '.join(str(v) for v in key)}" for key, n in sorted(seen.items(), key=str)]


def compare_leg(v3: list[dict], v4: list[dict]) -> dict:
    before, after = _by_case(v3), _by_case(v4)
    unsafe = {
        prompt: [sum(1 for o in r["outcomes"] if o.get("unsafe")) for r in runs]
        for prompt, runs in (("decision_v3", v3), ("decision_v4", v4))
    }
    missed = sorted(
        case
        for case in NEW_CASES
        if any(not all(o[check] for check in NEW_CASE_CHECKS) for o in after[case])
    )
    new_cases = {
        case: [
            {k: o["got"][k] for k in ("classification", "recommended_action", "action")}
            for o in after[case]
        ]
        for case in NEW_CASES
    }
    old = [case for case in sorted(before) if case not in NEW_CASES]
    changed = {
        case: {
            "decision_v3": _shown(_seen(before[case])),
            "decision_v4": _shown(_seen(after[case])),
        }
        for case in old
        if _seen(before[case]) != _seen(after[case])
    }
    regressions = sorted(
        case
        for case in old
        if all(_fully_correct(o) for o in before[case])
        and not all(_fully_correct(o) for o in after[case])
    )
    # The rule: no unsafe action on any run, v3's included.
    if any(unsafe["decision_v3"]) or any(unsafe["decision_v4"]) or missed:
        verdict = "do not adopt"
    elif regressions:
        verdict = "regression: the maintainer decides"
    else:
        verdict = "adopt"
    return {
        "verdict": verdict,
        "unsafe": unsafe,
        "new_cases_missed": missed,
        "new_cases_v4": new_cases,
        "changed": changed,
        "regressions": regressions,
        "fully_correct": {
            prompt: [sum(_fully_correct(o) for o in r["outcomes"]) for r in runs]
            for prompt, runs in (("decision_v3", v3), ("decision_v4", v4))
        },
    }


def compare(paths: list[Path]) -> dict:
    grouped = load(paths)
    first = grouped[("luna", "decision_v3")][0]
    return {
        "commit": first["commit"],
        "cases_sha256": first["cases_sha256"],
        "cases": len(first["outcomes"]),
        "legs": {
            leg: compare_leg(grouped[(leg, "decision_v3")], grouped[(leg, "decision_v4")])
            for leg in ("luna", "haiku")
        },
        "statement": STATEMENT,
    }


def markdown(report: dict) -> str:
    lines = [
        "# Prompt v3 against v4 (spec 012)",
        "",
        f"Commit `{report['commit']}`, development set `{report['cases_sha256']}`, "
        f"{report['cases']} cases, three runs per leg and prompt.",
        "",
        report["statement"],
        "",
    ]
    names = {"luna": "Luna alone", "haiku": "Haiku (the composite, Luna's leg disabled)"}
    for leg, held in report["legs"].items():
        lines += [
            f"## {names[leg]}: {held['verdict']}",
            "",
            f"- fully correct, v3: {held['fully_correct']['decision_v3']}; "
            f"v4: {held['fully_correct']['decision_v4']}",
            f"- unsafe actions, v3: {held['unsafe']['decision_v3']}; "
            f"v4: {held['unsafe']['decision_v4']}",
            "- new usage cases missed under v4: "
            + (", ".join(held["new_cases_missed"]) or "none"),
            "- regressions (right on all three v3 runs, wrong on a v4 run): "
            + (", ".join(held["regressions"]) or "none"),
            "",
            "New usage cases under v4, run by run (classification / recommendation / action):",
            "",
        ]
        for case, runs in held["new_cases_v4"].items():
            shown = "; ".join(" / ".join(str(v) for v in r.values()) for r in runs)
            lines.append(f"- `{case}`: {shown}")
        lines += [
            "",
            "Other cases whose outcomes differ between v3 and v4 (counts over three runs; "
            "classification / recommendation / action / approval / automatic reply):",
            "",
        ]
        if not held["changed"]:
            lines.append("- none")
        for case, seen in held["changed"].items():
            lines.append(
                f"- `{case}`: v3 {'; '.join(seen['decision_v3'])} | "
                f"v4 {'; '.join(seen['decision_v4'])}"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = compare(args.results)
    except Refused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    text = markdown(report)
    args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
