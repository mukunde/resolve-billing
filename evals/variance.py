"""How often each case comes out right, rather than whether it did once.

One run of a sampling model is one draw. Twelve out of twelve on one draw says
the engine *can* get every case right; it does not say it *does*. What a case
needs is its pass rate over repeated runs at unchanged settings -- and the
cases that pass only sometimes are exactly the ones a single run hides, in
whichever direction that draw happened to fall.

This module only aggregates. It reads outcome rows in the shape the runner
stores them, so a stored multi-run file can be reported again without paying
for the calls twice:

    python -m evals.variance evals/results/<file>.json

Five runs per case is a band, not a measurement: 4/5 and 5/5 are not
distinguishable at that size, and nothing here pretends otherwise. What five
runs do show is which cases move at all.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

CHECKS = (
    "classification_ok",
    "action_ok",
    "approval_ok",
    "findings_ok",
    "skip_ok",
    "sender_ok",
)

#: Checks added after some result files were stored. Absent means passed:
#: before the sender check existed, an unknown sender raised before any
#: outcome was written, so every stored outcome is about a known one.
ADDED_LATER = frozenset({"sender_ok"})


def fully_correct(outcome: dict) -> bool:
    return all(
        outcome.get(check, True) if check in ADDED_LATER else outcome[check]
        for check in CHECKS
    )


@dataclass
class CaseVariance:
    case_id: str
    runs: int
    fully_correct: int
    classification_ok: int
    unsafe: int
    classifications: Counter[str]
    actions: Counter[str]

    @property
    def stable(self) -> bool:
        """Same verdict on every run, whether that verdict is right or wrong."""
        return self.fully_correct in (0, self.runs)


def by_case(runs: list[list[dict]]) -> list[CaseVariance]:
    """One row per case, in the order the case set lists them.

    Refuses runs that were not made on the same cases: a pass rate over
    draws from two different exams is not a rate of anything.
    """
    if not runs:
        raise ValueError("no runs to aggregate")
    order = [o["case_id"] for o in runs[0]]
    for index, outcomes in enumerate(runs[1:], start=2):
        if [o["case_id"] for o in outcomes] != order:
            raise ValueError(f"run {index} was not made on the same cases as run 1")

    rows = []
    for position, case_id in enumerate(order):
        draws = [outcomes[position] for outcomes in runs]
        rows.append(
            CaseVariance(
                case_id=case_id,
                runs=len(draws),
                fully_correct=sum(fully_correct(o) for o in draws),
                classification_ok=sum(o["classification_ok"] for o in draws),
                unsafe=sum(1 for o in draws if o["unsafe"]),
                classifications=Counter(o["got"]["classification"] for o in draws),
                actions=Counter(o["got"]["action"] for o in draws),
            )
        )
    return rows


def _seen(counter: Counter[str]) -> str:
    return ", ".join(f"{value} x{count}" for value, count in counter.most_common())


def report(
    engine_label: str, runs: list[list[dict]], min_fully_correct: int | None = None
) -> int:
    rows = by_case(runs)
    cases = len(rows)
    per_run = [sum(fully_correct(o) for o in outcomes) for outcomes in runs]
    draws = [o for outcomes in runs for o in outcomes]
    unsafe = [o for o in draws if o["unsafe"]]
    costs = [o["cost_micros"] for o in draws if o["cost_micros"]]
    latencies = [o["latency_ms"] for o in draws if o["latency_ms"]]

    print(f"\nengine                        : {engine_label}")
    print(f"runs                          : {len(runs)} x {cases} cases")
    print(f"fully correct, run by run     : {', '.join(map(str, per_run))}")
    print(f"fully correct, worst run      : {min(per_run)}/{cases}")
    print(f"UNSAFE FINANCIAL ACTIONS      : {len(unsafe)}/{len(draws)}")
    print(f"cases right on every run      : {sum(r.fully_correct == r.runs for r in rows)}/{cases}")
    print(f"cases right on some runs only : {sum(not r.stable for r in rows)}/{cases}")
    print(f"cases right on no run         : {sum(r.fully_correct == 0 for r in rows)}/{cases}")

    if costs:
        # Same reasoning as the single-run report: cost is a mean because it
        # is summed, latency a median because a reviewer waits for the
        # typical call, not the average one.
        print(f"\nengine calls                  : {len(costs)}/{len(draws)}")
        print(f"cost of all runs              : ${sum(costs) / 1e6:.4f}")
        print(f"mean cost per dispute         : ${sum(costs) / 1e6 / len(draws):.5f}")
    if latencies:
        print(f"median latency, engine calls  : {int(statistics.median(latencies))} ms")

    width = max(len(r.case_id) for r in rows)
    print(f"\npass rate by case\n  {'case':<{width}}  fully  class.  classifications seen")
    for r in rows:
        mark = " " if r.stable else "~"
        seen = _seen(r.classifications)
        if len(r.actions) > 1:
            seen += f"  | actions: {_seen(r.actions)}"
        print(
            f"{mark} {r.case_id:<{width}}  {r.fully_correct}/{r.runs:<3}  "
            f"{r.classification_ok}/{r.runs:<4}  {seen}"
        )
    print("  ~ = the verdict changed between runs")

    if unsafe:
        print("\nUNSAFE")
        for o in unsafe:
            print(f"  {o['case_id']}: {o['unsafe']}")
        return 1

    # The floor holds against the worst run, not the mean of them: a build
    # that passes on average and fails one time in five has a floor that
    # fails one time in five.
    if min_fully_correct is not None:
        if min(per_run) < min_fully_correct:
            print(
                f"\nREGRESSION: worst run {min(per_run)}/{cases} fully correct, "
                f"floor is {min_fully_correct}"
            )
            return 1
        print(f"\nfloor of {min_fully_correct}/{cases} fully correct: held on every run")

    return 0


def load(path: Path) -> tuple[str, list[list[dict]]]:
    """The engine label and the runs of a stored file.

    Reads a single-run file too, as one run: the files written before
    multi-run existed stay readable rather than becoming a second format.
    """
    stored = json.loads(path.read_text(encoding="utf-8"))
    label = stored.get("engine_version", stored["engine"])
    if "runs" in stored:
        return label, [run["outcomes"] for run in stored["runs"]]
    return label, [stored["outcomes"]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="a file written by runner --json")
    args = parser.parse_args()
    if not args.result.exists():
        sys.exit(f"no such file: {args.result}")
    label, runs = load(args.result)
    return report(label, runs)


if __name__ == "__main__":
    sys.exit(main())
