"""The comparison that chooses the deployment model, from the result files
alone (specs/008-deployment-model-choice).

    python -m evals.compare evals/comparisons/008/manifest.json
    python -m evals.compare evals/comparisons/008/manifest.json --preview

It reads the manifest, the campaign log and the counted results in the
results folder -- nothing else, and nothing private -- and refuses any file
that is not one the manifest declared: another commit, a tree that was not
clean, other cases, prompt, schema, masking or configuration, a slot not
declared, an attempt the log does not hold, one out of the order or the
numbering the campaign admits, a complete run that does not hold exactly the
declared cases. Then it applies the spec's criteria in their order:

0. eligibility, as the maintainer declared it;
1. no unsafe action in any attempt, replaced or not;
2. the mean of fully correct cases over the three complete runs, with each
   run, the worst, the cases whose correctness or verdict changes and those
   always missed;
3. a candidate within one case of the best eligible mean is equivalent;
   among equivalent ones, in turn: invalid answers, automatic replies on
   missed cases, the cost per dispute (euros at the manifest's rate,
   unknown costs at their reservation), the median latency of engine calls.
   A tie-break whose evidence is missing for a candidate is not applied:
   the candidates still equal are left to the maintainer, the gap said.

The whole campaign's spend is reported apart, a known subtotal when part of
it is unknown. The maintainer chooses among the candidates left. The table
and its JSON copy are written beside the manifest only once every slot is
over; before that, `--preview` prints them and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

from evals import campaign, engines
from evals.campaign import OUTCOME_FIELDS, STATUSES, Campaign, CampaignRefused

#: Eligibility values that let a candidate be ranked.
RANKED = {"eligible", "eligible provisionally"}
#: Statuses of a case that entered the engine and was answered.
ENGINE_CALLED = {"decided", "abstained", "invalid"}
#: Statuses a complete run may not hold: a case with no answer to score.
UNSCORED = {"unavailable", "not_sent_oversized"}
#: The checks that make a case fully correct (`CaseOutcome.ok`).
CHECKS = tuple(sorted(OUTCOME_FIELDS - {"case_id", "unsafe"}))
TIE_BAND = 1.0
RUNS = 3


class ComparisonRefused(Exception):  # noqa: N818 -- a refusal, not a defect
    """A file is not one the manifest declared, or the campaign does not hold."""


def development_case_ids() -> list[str]:
    return [case["id"] for case in json.loads(engines.DEVELOPMENT_SET.read_bytes())]


def _fully_correct(outcome: dict) -> bool:
    return all(outcome[check] for check in CHECKS)


def _check_evidence(c: Campaign, results: dict) -> None:
    manifest = c.manifest
    commit = campaign.manifest_commit(c.manifest_path)
    declared = c.declared_slots()
    entries = manifest["candidates"] | manifest["comparison_points"]
    for (slot, _attempt), held in results.items():
        name = held.get("_file")
        if slot not in declared:
            raise ComparisonRefused(f"{name}: slot {slot} is not declared in the manifest")
        if held.get("engine") != declared[slot]:
            raise ComparisonRefused(f"{name}: slot {slot} runs {declared[slot]}")
        if commit is None or held.get("commit") != commit:
            raise ComparisonRefused(f"{name}: not run on the manifest's commit")
        if held.get("clean_tree") is not True:
            raise ComparisonRefused(f"{name}: not run on a clean tree")
        for key, value in manifest["evidence"].items():
            if held.get(key) != value:
                raise ComparisonRefused(f"{name}: its {key} is not the manifest's")
        if held.get("configuration_fingerprint") != entries[held["engine"]]["fingerprint"]:
            raise ComparisonRefused(f"{name}: its configuration is not the manifest's")


def _check_cases(held: dict, cases: list[str]) -> None:
    """Every outcome well formed; a complete run holds exactly the cases."""
    name = held.get("_file")
    outcomes, statuses = held.get("outcomes"), held.get("statuses")
    if not isinstance(outcomes, list) or not isinstance(statuses, dict):
        raise ComparisonRefused(f"{name}: no outcomes or statuses")
    ids = []
    for o in outcomes:
        if not (isinstance(o, dict) and set(o) >= OUTCOME_FIELDS):
            raise ComparisonRefused(f"{name}: an outcome lacks its checks")
        if not all(isinstance(o[check], bool) for check in CHECKS):
            raise ComparisonRefused(f"{name}: an outcome's checks are not true or false")
        if not (o["unsafe"] is None or (isinstance(o["unsafe"], str) and o["unsafe"])):
            raise ComparisonRefused(f"{name}: an outcome's unsafe flag is malformed")
        ids.append(o["case_id"])
    if len(ids) != len(set(ids)) or not set(ids) <= set(cases):
        raise ComparisonRefused(f"{name}: duplicate or unknown cases")
    if set(statuses) != set(ids) or not set(statuses.values()) <= STATUSES:
        raise ComparisonRefused(f"{name}: its statuses do not match its cases")
    if held.get("complete"):
        if set(ids) != set(cases):
            raise ComparisonRefused(f"{name}: a complete run must hold every declared case")
        if UNSCORED & set(statuses.values()):
            raise ComparisonRefused(f"{name}: a complete run holds unscored cases")


def _replay(c: Campaign, results: dict) -> None:
    """The log, replayed under `Campaign.admit`'s rules: slots in order,
    attempts numbered 1 then, after a qualifying stop only, 2; a loss only
    for a logged attempt with no result; a retirement only for an open slot
    of a candidate that has stopped completing. Every attempt has its file
    unless lost, and every file is logged."""
    declared = c.declared_slots()
    status = dict.fromkeys(declared, "open")
    count = dict.fromkeys(declared, 0)
    failed_engines: set[str] = set()
    logged = set()

    def fail(slot: str) -> None:
        status[slot] = "failed"
        failed_engines.add(declared[slot])

    for line in c.log_lines():
        kind = line.get("type")
        slot = line.get("slot")
        if slot not in declared:
            raise ComparisonRefused(f"the log holds slot {slot}, not declared")
        if kind == "retired":
            if status[slot] != "open" or declared[slot] not in failed_engines:
                raise ComparisonRefused(f"slot {slot} retired without its candidate failing")
            status[slot] = "retired"
            continue
        key = (slot, line.get("attempt"))
        if kind == "lost":
            if key not in logged or status[slot] != "unresolved":
                raise ComparisonRefused(f"slot {slot}: a loss for an attempt that has a result")
            fail(slot)
            continue
        if kind != "attempt":
            raise ComparisonRefused(f"the log holds a line of unknown kind ({kind})")
        expected = next(
            (s for s, v in status.items() if v not in {"complete", "retired", "failed"}), None
        )
        if slot != expected:
            raise ComparisonRefused(f"slot {slot} was run out of order (expected {expected})")
        allowed = 1 if status[slot] == "open" else 2 if status[slot] == "pending_replacement" else 0
        if key[1] != count[slot] + 1 or key[1] != allowed:
            raise ComparisonRefused(f"slot {slot}: attempt {key[1]} is not the one admitted")
        count[slot] += 1
        logged.add(key)
        held = results.get(key)
        if held is None:
            status[slot] = "unresolved"
        elif held.get("complete"):
            status[slot] = "complete"
        elif key[1] >= 2 or campaign.stop_kind(held.get("stop_reason")) != "qualifying":
            fail(slot)
        else:
            status[slot] = "pending_replacement"
    for slot, value in status.items():
        if value == "unresolved":
            raise ComparisonRefused(f"slot {slot}: no result in the folder")
    for key, held in results.items():
        if key not in logged:
            raise ComparisonRefused(f"{held.get('_file')}: not in the campaign log")


def _amount(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _usd(held: dict, rate: float) -> float | None:
    """A run's estimated cost in dollars, unknown costs at their reservation;
    None when it cannot be known."""
    cost = held.get("cost")
    if not isinstance(cost, dict) or cost.get("ledger_unavailable"):
        return None
    if cost.get("basis") == "estimated zero under the free quota":
        return 0.0
    amount = cost.get("settled_amount", cost.get("usd"))
    unknown = cost.get("unknown_cost_reserved", 0.0)
    if not (_amount(amount) and amount >= 0 and _amount(unknown) and unknown >= 0):
        return None
    currency = cost.get("currency", "USD")
    if currency not in {"USD", "EUR", None}:
        return None
    total = amount + unknown
    return total * rate if currency == "EUR" else total


def _verdict(outcome: dict) -> str:
    return json.dumps(outcome.get("got") or {}, sort_keys=True)


def compare(manifest_path: Path, results_dir: Path, cases: list[str] | None = None) -> dict:
    cases = cases if cases is not None else development_case_ids()
    try:
        c = Campaign(manifest_path, results_dir=results_dir)
        results = c.results()
    except CampaignRefused as refusal:
        raise ComparisonRefused(str(refusal)) from None
    _check_evidence(c, results)
    for held in results.values():
        _check_cases(held, cases)
    _replay(c, results)
    state = c.state()
    rate = c.manifest["exchange_rate"]["eur_usd"]
    rows: dict[str, dict] = {}
    spend_known, spend_unknown = 0.0, 0
    for name in [*c.manifest["candidates"], *c.manifest["comparison_points"]]:
        slots = [s for s, n in state.engines.items() if n == name]
        attempts = [results[k] for k in sorted(results) if k[0] in slots]
        complete = [r for r in attempts if r.get("complete")]
        for r in attempts:
            amount = _usd(r, rate)
            if amount is None:
                spend_unknown += 1
            else:
                spend_known += amount
        per_run = [sum(_fully_correct(o) for o in r["outcomes"]) for r in complete]
        correct = [{o["case_id"] for o in r["outcomes"] if _fully_correct(o)} for r in complete]
        verdicts = [{o["case_id"]: _verdict(o) for o in r["outcomes"]} for r in complete]
        latencies = [
            o["latency_ms"]
            for r in complete
            for o in r["outcomes"]
            if r["statuses"][o["case_id"]] in ENGINE_CALLED and _amount(o.get("latency_ms"))
        ]
        costs = [_usd(r, rate) for r in complete]
        entry = c.manifest["candidates"].get(name)
        rows[name] = {
            "role": "candidate" if entry else "comparison point",
            "eligibility": entry.get("eligibility") if entry else "ineligible",
            "completing": name not in state.not_completing and len(complete) == RUNS,
            "unsafe": sum(1 for r in attempts for o in r["outcomes"] if o["unsafe"]),
            "runs": per_run,
            "mean": statistics.mean(per_run) if per_run else None,
            "worst": min(per_run) if per_run else None,
            "correctness_changing": sorted(set.union(*correct) - set.intersection(*correct))
            if correct
            else [],
            "verdict_changing": sorted(
                case for case in cases if len({v.get(case) for v in verdicts}) > 1
            )
            if verdicts
            else [],
            "always_missed": sorted(set(cases) - set.union(*correct)) if correct else [],
            "invalid": sum(1 for r in complete for s in r["statuses"].values() if s == "invalid"),
            "automatic_replies_on_misses": sum(
                1
                for r in complete
                for o in r["outcomes"]
                if not _fully_correct(o)
                and (o.get("got") or {}).get("automatic_reply")
                and o["reply_ok"] is False
            ),
            "cost_per_dispute_usd": (
                sum(costs) / (len(cases) * RUNS) if costs and None not in costs else None
            ),
            "median_latency_ms": statistics.median(latencies) if latencies else None,
        }
    return _rank(rows) | {
        "candidates": rows,
        "campaign_spend_usd": spend_known,
        "campaign_spend_unknown_attempts": spend_unknown,
        "campaign_over": state.next_slot() is None,
    }


def _rank(rows: dict) -> dict:
    ranked = [
        name
        for name, row in rows.items()
        if row["role"] == "candidate"
        and row["eligibility"] in RANKED
        and row["completing"]
        and row["unsafe"] == 0
    ]
    if not ranked:
        return {"tied": [], "tie_breaks": [], "left": []}
    best = max(rows[n]["mean"] for n in ranked)
    tied = [n for n in ranked if best - rows[n]["mean"] <= TIE_BAND]
    breaks = []
    kept = tied
    for criterion in (
        "invalid",
        "automatic_replies_on_misses",
        "cost_per_dispute_usd",
        "median_latency_ms",
    ):
        if len(kept) <= 1:
            break
        missing = [n for n in kept if rows[n][criterion] is None]
        if missing:
            # Not applied: missing evidence is never read as best or worst.
            breaks.append({"criterion": criterion, "kept": kept, "not_comparable": missing})
            break
        lowest = min(rows[n][criterion] for n in kept)
        kept = [n for n in kept if rows[n][criterion] == lowest]
        breaks.append({"criterion": criterion, "kept": kept})
    return {"tied": tied, "tie_breaks": breaks, "left": kept}


def markdown(result: dict) -> str:
    lines = [
        "# The comparison of spec 008",
        "",
        "| configuration | role | eligibility | completing | unsafe | runs | mean | worst "
        "| invalid | auto-replies on misses | cost per dispute (USD) | median latency (ms) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, row in result["candidates"].items():
        mean = f"{row['mean']:.2f}" if row["mean"] is not None else "--"
        cost = row["cost_per_dispute_usd"]
        worst = row["worst"] if row["worst"] is not None else "--"
        latency = row["median_latency_ms"] if row["median_latency_ms"] is not None else "--"
        lines.append(
            f"| `{name}` | {row['role']} | {row['eligibility']} | "
            f"{'yes' if row['completing'] else 'no'} | {row['unsafe']} | "
            f"{', '.join(map(str, row['runs'])) or '--'} | {mean} | {worst} | "
            f"{row['invalid']} | {row['automatic_replies_on_misses']} | "
            f"{f'{cost:.6f}' if cost is not None else 'unknown'} | {latency} |"
        )
    lines.append("")
    for name, row in result["candidates"].items():
        lines += [
            f"**`{name}`** -- correctness changing: "
            + (", ".join(row["correctness_changing"]) or "none")
            + "; verdict changing: "
            + (", ".join(row["verdict_changing"]) or "none")
            + "; always missed: "
            + (", ".join(row["always_missed"]) or "none")
            + ".",
            "",
        ]
    for step in result["tie_breaks"]:
        if "not_comparable" in step:
            lines += [
                f"Tie-break on {step['criterion']} not applied: no evidence for "
                + ", ".join(f"`{n}`" for n in step["not_comparable"])
                + ".",
                "",
            ]
    unknown = result["campaign_spend_unknown_attempts"]
    lines += [
        f"Equivalent on correctness (within {TIE_BAND:g} case of the best eligible mean): "
        + (", ".join(f"`{n}`" for n in result["tied"]) or "none"),
        "",
        "Left by the rules, for the maintainer's choice: "
        + (", ".join(f"`{n}`" for n in result["left"]) or "none"),
        "",
        f"The whole campaign's spend, apart: about ${result['campaign_spend_usd']:.4f}"
        + (f", plus {unknown} attempts of unknown cost." if unknown else "."),
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path, nargs="?", default=None)
    parser.add_argument("--results", type=Path, default=None)
    parser.add_argument("--preview", action="store_true", help="print only, write nothing")
    args = parser.parse_args(argv)
    manifest = args.manifest or campaign.MANIFEST
    try:
        result = compare(manifest, args.results or campaign.RESULTS_DIR)
    except ComparisonRefused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    text = markdown(result)
    print(text)
    if args.preview:
        return 0
    if not result["campaign_over"]:
        # Writing beside the manifest would dirty the tree the remaining
        # slots must run on.
        print("refused: the campaign is not over; use --preview", file=sys.stderr)
        return 2
    (manifest.parent / "comparison.md").write_text(text, encoding="utf-8")
    (manifest.parent / "comparison.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
