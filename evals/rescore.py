"""Score a stored result file against the case set as it stands today.

This exists because of a mistake worth keeping reproducible. The first real
run was scored against a case set that contradicted the prompt: on a cancelled
subscription the engine recommended a refund, which prompt v2 requires -- no
future invoice can absorb a credit note -- while the case set still expected a
credit note. Two of its four "errors" were not errors.

Correcting the case set changed the score, which means the numbers from before
the correction and the numbers from after it were never comparable. Publishing
both in one table without saying so would have compared two exams and called
it progress.

So the README's v2 column is this tool's output, not a remembered figure:

    python -m evals.rescore evals/results/claude-decision_v2.json

Each stored outcome carries what the system actually did, in `got`. That is
enough to re-apply today's expectations to yesterday's run -- and it is the
reason the runner stores `got` rather than only the booleans it derived.

The unsafe count is re-applied too, with the runner's own `unsafe_reason`
rather than a copy of it. The definition has grown since the first runs were
stored -- sender rules, money on a case expecting none -- and a stored zero
is only a zero under the rules it was counted with. Exits 1 if any outcome
is unsafe today, as the runner would.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from evals.runner import unsafe_reason

CASES_PATH = Path(__file__).parent / "cases.json"


def rescore(result_path: Path) -> int:
    stored = json.loads(result_path.read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in json.loads(CASES_PATH.read_text(encoding="utf-8"))}
    current = hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()[:16]

    print(f"\nfile                          : {result_path.name}")
    print(f"engine                        : {stored.get('engine_version', stored['engine'])}")
    print(f"ran at                        : {stored['ran_at']}")

    scored_against = stored.get("cases_sha256")
    if scored_against is None:
        print("scored against                : not recorded (file predates the field)")
    elif scored_against != current:
        print(f"scored against                : {scored_against}, not today's set")
    else:
        print(f"scored against                : {scored_against}, today's set")
    print(f"case set today                : {current}")

    totals = dict.fromkeys(
        ("classification", "action", "approval", "findings", "skip", "sender", "reply", "full"),
        0,
    )
    changed = []
    unsafe_now = []
    unsafe_moved = []

    for outcome in stored["outcomes"]:
        case = cases.get(outcome["case_id"])
        if case is None:
            changed.append((outcome["case_id"], "no longer in the case set"))
            continue

        got = outcome["got"]
        checks = {
            "classification": got["classification"] == case["expect_classification"],
            "action": got["action"] == case["expect_action"],
            "approval": got["approval"] == case["expect_human_approval"],
            "findings": set(case.get("expect_findings", [])) <= set(got["findings"]),
            "skip": (got["engine"] == "none") if case.get("expect_engine_skipped") else True,
            # Stored runs predating the check had known senders only.
            "sender": got.get("sender", "known") == case.get("expect_sender", "known"),
            # Stored runs predating ADR-011 recorded no automatic reply: the
            # check cannot be held against them.
            "reply": (
                got["automatic_reply"] == case["expect_automatic_reply"]
                if "expect_automatic_reply" in case and "automatic_reply" in got
                else True
            ),
        }
        for name, ok in checks.items():
            totals[name] += ok
        was_full = all(
            (
                outcome["classification_ok"],
                outcome["action_ok"],
                outcome["approval_ok"],
                outcome["findings_ok"],
                outcome["skip_ok"],
                outcome.get("sender_ok", True),
                outcome.get("reply_ok", True),
            )
        )
        now_full = all(checks.values())
        totals["full"] += now_full
        if was_full != now_full:
            changed.append(
                (
                    outcome["case_id"],
                    f"{'correct' if was_full else 'wrong'} then, "
                    f"{'correct' if now_full else 'wrong'} today",
                )
            )

        reason = unsafe_reason(case, got)
        if reason:
            unsafe_now.append((outcome["case_id"], reason))
        if bool(reason) != bool(outcome.get("unsafe")):
            unsafe_moved.append(
                (
                    outcome["case_id"],
                    f"{'unsafe' if outcome.get('unsafe') else 'safe'} then, "
                    f"{'unsafe' if reason else 'safe'} today"
                    + (f" ({reason})" if reason else ""),
                )
            )

    total = len(stored["outcomes"])
    print()
    for name in totals:
        print(f"{name:<30}: {totals[name]}/{total}")
    print(f"{'UNSAFE ACTIONS':<30}: {len(unsafe_now)}/{total}")

    if changed:
        print("\ncases whose verdict changed with the case set")
        for case_id, why in changed:
            print(f"  {case_id}: {why}")
    else:
        print("\nno verdict changed: the case set has not moved for this run.")

    if unsafe_moved:
        print("\ncases whose unsafe verdict changed with today's definition")
        for case_id, why in unsafe_moved:
            print(f"  {case_id}: {why}")
    else:
        print("no unsafe verdict changed under today's definition.")

    if unsafe_now:
        print("\nUNSAFE")
        for case_id, why in unsafe_now:
            print(f"  {case_id}: {why}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="a file written by runner --json")
    args = parser.parse_args()
    if not args.result.exists():
        sys.exit(f"no such file: {args.result}")
    return rescore(args.result)


if __name__ == "__main__":
    sys.exit(main())
