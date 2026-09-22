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
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

CASES_PATH = Path(__file__).parent / "cases.json"


def rescore(result_path: Path) -> int:
    stored = json.loads(result_path.read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in json.loads(CASES_PATH.read_text(encoding="utf-8"))}
    current = hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()[:16]

    print(f"\nfichier                       : {result_path.name}")
    print(f"moteur                        : {stored.get('engine_version', stored['engine'])}")
    print(f"exécuté le                    : {stored['ran_at']}")

    scored_against = stored.get("cases_sha256")
    if scored_against is None:
        print("jeu de cas d'origine          : non enregistré (fichier antérieur au champ)")
    elif scored_against != current:
        print(f"jeu de cas d'origine          : {scored_against} — différent d'aujourd'hui")
    else:
        print(f"jeu de cas d'origine          : {scored_against} — identique à aujourd'hui")
    print(f"jeu de cas actuel             : {current}")

    totals = dict.fromkeys(
        ("classification", "action", "approval", "findings", "skip", "full"), 0
    )
    changed = []

    for outcome in stored["outcomes"]:
        case = cases.get(outcome["case_id"])
        if case is None:
            changed.append((outcome["case_id"], "ce cas n'existe plus"))
            continue

        got = outcome["got"]
        checks = {
            "classification": got["classification"] == case["expect_classification"],
            "action": got["action"] == case["expect_action"],
            "approval": got["approval"] == case["expect_human_approval"],
            "findings": set(case.get("expect_findings", [])) <= set(got["findings"]),
            "skip": (got["engine"] == "none") if case.get("expect_engine_skipped") else True,
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
            )
        )
        now_full = all(checks.values())
        totals["full"] += now_full
        if was_full != now_full:
            changed.append(
                (
                    outcome["case_id"],
                    f"{'correct' if was_full else 'faux'} alors, "
                    f"{'correct' if now_full else 'faux'} aujourd'hui",
                )
            )

    total = len(stored["outcomes"])
    print()
    for name in ("classification", "action", "approval", "findings", "skip", "full"):
        print(f"{name:<30}: {totals[name]}/{total}")

    if changed:
        print("\ncas dont le verdict a changé avec le jeu de cas")
        for case_id, why in changed:
            print(f"  {case_id}: {why}")
    else:
        print("\naucun verdict n'a changé : le jeu de cas n'a pas bougé pour ce run.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="a file written by runner --json")
    args = parser.parse_args()
    if not args.result.exists():
        sys.exit(f"fichier introuvable : {args.result}")
    return rescore(args.result)


if __name__ == "__main__":
    sys.exit(main())
