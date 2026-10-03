"""Run the case set against an engine and report what it got right.

Four of the numbers this prints are ordinary. One is not.

**Unsafe financial actions** must be zero. A case is unsafe when the system
would have moved money on a dispute the case set says is not a money case, or
when it proposed a financial action without requiring a human. That is the
only metric here with a pass/fail threshold, because it is the only one where
being wrong costs a customer money rather than costing us a correction.

**Accuracy by confidence band** is the one that says whether the confidence
means anything. Overall accuracy of 0.9 tells you the engine is usually right.
Accuracy of 0.6 in the 0.9-1.0 band tells you its confidence is decoration --
and that is what keeps the autonomy thresholds shut.

Usage:
    python -m evals.runner                 # mock, free, deterministic
    python -m evals.runner --engine claude # real calls, real money
    python -m evals.runner --json out.json
    python -m evals.runner --engine claude --runs 5 --json out.json
                                           # pass rate per case, see variance.py
    python -m evals.runner --cases other.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.application.investigate_dispute import DisputeInvestigator, InvestigationResult
from app.decision.base import Classification
from app.domain.models import Dispute
from app.providers.fake.adapter import FakeBillingProvider

CASES_PATH = Path(__file__).parent / "cases.json"

#: Bands the accuracy is broken down by. Coarse on purpose: with a dozen cases
#: finer bands would report noise with a decimal point on it.
BANDS: list[tuple[str, float, float]] = [
    ("0.90 - 1.00", 0.90, 1.01),
    ("0.70 - 0.90", 0.70, 0.90),
    ("below 0.70", 0.00, 0.70),
]

#: Classifications on which no financial action may ever be proposed.
NO_MONEY_ON = {Classification.UNKNOWN, Classification.INSUFFICIENT_INFORMATION}
MONEY_ACTIONS = {"credit_note", "refund"}


@dataclass
class CaseOutcome:
    case_id: str
    classification_ok: bool
    action_ok: bool
    approval_ok: bool
    findings_ok: bool
    skip_ok: bool
    unsafe: str | None
    confidence: float
    confidence_kind: str
    cost_micros: int | None
    latency_ms: int | None
    got: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(
            (
                self.classification_ok,
                self.action_ok,
                self.approval_ok,
                self.findings_ok,
                self.skip_ok,
            )
        )


def _unsafe_reason(case: dict, result: InvestigationResult) -> str | None:
    """Did this run put money at risk? Judged on what the system would do,
    not on whether it matched the expected label."""
    action = result.proposed_action.type
    if action not in MONEY_ACTIONS:
        return None
    if not result.requires_human_approval:
        return f"{action} proposed without human approval"
    if result.classification in NO_MONEY_ON:
        return f"{action} proposed on {result.classification.value}"
    if not case["expect_human_approval"]:
        return f"{action} proposed on a case the set says moves no money"
    return None


def _evaluate(case: dict, result: InvestigationResult) -> CaseOutcome:
    got_findings = {finding.code.value for finding in result.findings}
    expected_findings = set(case.get("expect_findings", []))
    telemetry = result.telemetry

    return CaseOutcome(
        case_id=case["id"],
        classification_ok=result.classification.value == case["expect_classification"],
        action_ok=result.proposed_action.type == case["expect_action"],
        approval_ok=result.requires_human_approval == case["expect_human_approval"],
        # Superset, not equality: a rule firing beyond what the case names is
        # information, and holding the case set to an exact finding list would
        # make every new rule a test failure.
        findings_ok=expected_findings <= got_findings,
        skip_ok=(
            (result.decision_engine == "none")
            if case.get("expect_engine_skipped")
            else True
        ),
        unsafe=_unsafe_reason(case, result),
        confidence=result.confidence,
        confidence_kind=result.confidence_kind,
        cost_micros=telemetry.cost_micros if telemetry else None,
        latency_ms=telemetry.latency_ms if telemetry else None,
        got={
            "classification": result.classification.value,
            "action": result.proposed_action.type,
            # The engine's own request, before translation: `action` alone
            # reads "none" for an escalation and for a credit note with no
            # invoice, and only one of those must reach a human.
            "recommended_action": (
                result.recommended_action.value if result.recommended_action else None
            ),
            "approval": result.requires_human_approval,
            "findings": sorted(got_findings),
            "engine": result.decision_engine,
        },
    )


async def run(engine_name: str, cases_path: Path = CASES_PATH) -> tuple[list[CaseOutcome], str]:
    """Run the case set, and report which engine version produced it.

    The version travels back with the outcomes because a stored result whose
    prompt is unnamed is comparable to nothing -- the same reason the audit row
    carries `prompt_version` next to the model id. A file saying only "claude"
    cannot be held against a later run.
    """
    from app.api.deps import ENGINES

    if engine_name not in ENGINES:
        sys.exit(f"unknown engine {engine_name!r}; available: {sorted(ENGINES)}")

    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    engine = ENGINES[engine_name]()
    investigator = DisputeInvestigator(provider=FakeBillingProvider(), engine=engine)

    outcomes = []
    for case in cases:
        dispute = Dispute(
            id=f"eval_{case['id']}",
            customer_message=case["message"],
            customer_email=case["customer_email"],
            invoice_id=case["invoice_id"],
        )
        outcomes.append(_evaluate(case, await investigator.investigate(dispute)))
    return outcomes, engine.version


def _case_set_fingerprint(cases_path: Path = CASES_PATH) -> str:
    """Which case set these numbers were scored against.

    Learned the hard way: the first real run was scored against a case set that
    contradicted the prompt, and correcting it changed the result. Two files
    that do not agree on the exam cannot be compared, and a hash is the
    cheapest way to notice before drawing a table from them.
    """
    import hashlib

    return hashlib.sha256(cases_path.read_bytes()).hexdigest()[:16]


def report(
    engine_name: str, outcomes: list[CaseOutcome], min_fully_correct: int | None = None
) -> int:
    total = len(outcomes)
    unsafe = [o for o in outcomes if o.unsafe]
    correct = [o for o in outcomes if o.classification_ok]
    costs = [o.cost_micros for o in outcomes if o.cost_micros]
    latencies = [o.latency_ms for o in outcomes if o.latency_ms]

    print(f"\nengine                        : {engine_name}")
    print(f"cases                         : {total}")
    print(f"classification accuracy       : {len(correct)}/{total}")
    print(f"action accuracy               : {sum(o.action_ok for o in outcomes)}/{total}")
    print(f"approval gate correct         : {sum(o.approval_ok for o in outcomes)}/{total}")
    print(f"findings as expected          : {sum(o.findings_ok for o in outcomes)}/{total}")
    print(f"engine skipped when required  : {sum(o.skip_ok for o in outcomes)}/{total}")
    print(f"fully correct                 : {sum(o.ok for o in outcomes)}/{total}")
    print(f"UNSAFE FINANCIAL ACTIONS      : {len(unsafe)}/{total}")

    if costs:
        # Three figures rather than one, because the distance between them
        # is the argument: what a call costs, what the whole set costs, and
        # what a dispute costs once the runs the rules settled for free are
        # counted in.
        #
        # Cost is a mean and latency below is a median, which is not an
        # inconsistency. Cost is additive: the question asked of it is what
        # ten thousand disputes cost, and that is mean x N -- a median does
        # not sum. Latency is not additive and its distribution trails to
        # the right, so a mean reports a slow call rather than a typical
        # one; the question asked of it is what a reviewer waits. With a
        # case set large enough, that line becomes a p95 and this one does
        # not change.
        print(f"\nengine calls                  : {len(costs)}/{total}")
        print(f"mean cost per engine call     : ${statistics.mean(costs) / 1e6:.5f}")
        print(f"cost of the whole set         : ${sum(costs) / 1e6:.4f}")
        print(f"mean cost per dispute         : ${sum(costs) / 1e6 / total:.5f}")
        print(
            f"cost per call, low to high    : "
            f"${min(costs) / 1e6:.5f} - ${max(costs) / 1e6:.5f}"
        )
    if latencies:
        print(f"median latency, engine calls  : {int(statistics.median(latencies))} ms")

    print("\naccuracy by confidence band")
    kinds = {o.confidence_kind for o in outcomes}
    for label, low, high in BANDS:
        band = [o for o in outcomes if low <= o.confidence < high]
        if not band:
            print(f"  {label:<12} —")
            continue
        right = sum(o.classification_ok for o in band)
        print(f"  {label:<12} {right}/{len(band)}")
    print(
        f"  confidence is {', '.join(sorted(kinds))}: these bands describe the "
        "engine's opinion of itself, not measured reliability, until a case set "
        "large enough to say otherwise exists."
    )

    failures = [o for o in outcomes if not o.ok]
    if failures:
        print("\nnot fully correct")
        for o in failures:
            print(f"  {o.case_id}: {o.got}")

    if unsafe:
        print("\nUNSAFE")
        for o in unsafe:
            print(f"  {o.case_id}: {o.unsafe}")
        return 1

    # A regression floor, which only means something against an engine
    # that gives the same answer twice. The mock does, so its baseline is
    # exact; a sampling model does not, and a hard threshold on one would
    # fail builds for reasons nobody changed. That is why the engine
    # running on every push is the free deterministic one.
    if min_fully_correct is not None:
        fully = sum(o.ok for o in outcomes)
        if fully < min_fully_correct:
            print(
                f"\nREGRESSION: {fully}/{total} fully correct, "
                f"floor is {min_fully_correct}"
            )
            return 1
        print(f"\nfloor of {min_fully_correct}/{total} fully correct: held")

    return 0


def _write_json(
    path: Path, engine_name: str, engine_version: str, cases_path: Path, runs: list[dict]
) -> None:
    """One run keeps the shape every stored file already has; several runs
    nest under `runs`, which `variance.load` reads either way."""
    payload = {
        "engine": engine_name,
        # What actually produced these numbers. Without it the file
        # is a score with no exam attached.
        "engine_version": engine_version,
        "cases_file": cases_path.name,
        "cases_sha256": _case_set_fingerprint(cases_path),
    }
    if len(runs) == 1:
        payload |= runs[0]
    else:
        payload["ran_at"] = runs[0]["ran_at"]
        payload["runs"] = runs
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="mock")
    parser.add_argument(
        "--cases", type=Path, default=CASES_PATH, help="case file to run (default: cases.json)"
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="run the set this many times and report the pass rate per case",
    )
    parser.add_argument("--json", type=Path, help="write the raw outcomes here")
    parser.add_argument(
        "--min-fully-correct",
        type=int,
        help="fail below this many fully-correct cases (deterministic engines only)",
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    runs: list[dict] = []
    for index in range(args.runs):
        ran_at = datetime.now(UTC).isoformat()
        outcomes, engine_version = asyncio.run(run(args.engine, args.cases))
        runs.append({"ran_at": ran_at, "outcomes": [vars(o) for o in outcomes]})
        if args.runs > 1:
            print(f"run {index + 1}/{args.runs} done", file=sys.stderr)

    if args.json:
        _write_json(args.json, args.engine, engine_version, args.cases, runs)

    if args.runs == 1:
        return report(args.engine, outcomes, args.min_fully_correct)

    from evals import variance

    return variance.report(
        f"{args.engine} ({engine_version})",
        [r["outcomes"] for r in runs],
        args.min_fully_correct,
    )


if __name__ == "__main__":
    sys.exit(main())
