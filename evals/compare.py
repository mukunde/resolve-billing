"""The comparison that chooses the deployment model, from the result files
alone (specs/008-deployment-model-choice).

    python -m evals.compare evals/comparisons/008/manifest.json

It reads the manifest, the campaign log and the counted results in the
results folder -- nothing else, and nothing private -- and refuses any file
that is not one the manifest declared: another commit, a tree that was not
clean, other cases, prompt, schema, masking or configuration, a slot not
declared, an attempt the log does not hold, or one out of the order the
campaign admits. Then it applies the spec's criteria in their order:

0. eligibility, as the maintainer declared it;
1. no unsafe action in any attempt, replaced or not;
2. the mean of fully correct cases over the three complete runs, with each
   run, the worst, the cases that change and those always missed;
3. a candidate within one case of the best eligible mean is equivalent;
   among equivalent ones, in turn: invalid answers, automatic replies on
   missed cases, the cost per dispute (euros at the manifest's rate,
   unknown costs at their reservation), the median latency of engine calls.

The whole campaign's spend is reported apart. The maintainer chooses among
the candidates left. Output: a Markdown table and a JSON copy, beside the
manifest.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from evals import campaign
from evals.campaign import Campaign, CampaignRefused

#: Eligibility values that let a candidate be ranked.
RANKED = {"eligible", "eligible provisionally"}
#: Statuses of a case that entered the engine and was answered.
ENGINE_CALLED = {"decided", "abstained", "invalid"}
TIE_BAND = 1.0
DISPUTES = 102


class ComparisonRefused(Exception):  # noqa: N818 -- a refusal, not a defect
    """A file is not one the manifest declared, or the campaign does not hold."""


def _fully_correct(outcome: dict) -> bool:
    return all(
        outcome.get(check, True)
        for check in (
            "classification_ok",
            "action_ok",
            "approval_ok",
            "findings_ok",
            "skip_ok",
            "sender_ok",
            "reply_ok",
        )
    )


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


def _replay(c: Campaign, results: dict) -> None:
    """The log, replayed as the campaign admits: every attempt has its file,
    every file is logged, and attempts come in the admitted order."""
    declared = c.declared_slots()
    status = dict.fromkeys(declared, "open")
    logged = set()
    for line in c.log_lines():
        kind = line.get("type")
        if kind == "retired":
            status[line["slot"]] = "retired"
            continue
        key = (line.get("slot"), line.get("attempt"))
        if kind == "lost":
            status[key[0]] = "failed"
            logged.add(key)
            continue
        if kind != "attempt":
            continue
        if key[0] not in declared:
            raise ComparisonRefused(f"the log holds slot {key[0]}, not declared")
        expected = next(
            (s for s, v in status.items() if v not in {"complete", "retired", "failed"}), None
        )
        if key[0] != expected:
            raise ComparisonRefused(f"slot {key[0]} was run out of order (expected {expected})")
        logged.add(key)
        held = results.get(key)
        lost_later = any(
            other.get("type") == "lost" and (other["slot"], other["attempt"]) == key
            for other in c.log_lines()
        )
        if held is None:
            if lost_later:
                continue
            raise ComparisonRefused(f"slot {key[0]}, attempt {key[1]}: no result in the folder")
        if held.get("complete"):
            status[key[0]] = "complete"
        elif key[1] >= 2 or campaign.stop_kind(held.get("stop_reason")) != "qualifying":
            status[key[0]] = "failed"
        else:
            status[key[0]] = "pending_replacement"
    for key, held in results.items():
        if key not in logged:
            raise ComparisonRefused(f"{held.get('_file')}: not in the campaign log")


def _usd(cost: dict, rate: float) -> float:
    amount = (cost.get("settled_amount") or cost.get("usd") or 0.0) + (
        cost.get("unknown_cost_reserved") or 0.0
    )
    return amount * rate if cost.get("currency") == "EUR" else amount


def compare(manifest_path: Path, results_dir: Path) -> dict:
    try:
        c = Campaign(manifest_path, results_dir=results_dir)
        results = c.results()
    except CampaignRefused as refusal:
        raise ComparisonRefused(str(refusal)) from None
    _check_evidence(c, results)
    _replay(c, results)
    held = c.state()
    rate = c.manifest["exchange_rate"]["eur_usd"]
    rows: dict[str, dict] = {}
    spend = 0.0
    for name in [*c.manifest["candidates"], *c.manifest["comparison_points"]]:
        slots = [s for s, n in held.engines.items() if n == name]
        attempts = [results[k] for k in sorted(results) if k[0] in slots]
        complete = [r for r in attempts if r.get("complete")]
        spend += sum(_usd(r.get("cost") or {}, rate) for r in attempts)
        per_run = [sum(_fully_correct(o) for o in r["outcomes"]) for r in complete]
        correct_sets = [
            {o["case_id"] for o in r["outcomes"] if _fully_correct(o)} for r in complete
        ]
        all_cases = {o["case_id"] for r in complete for o in r["outcomes"]}
        latencies = [
            o.get("latency_ms")
            for r in complete
            for o in r["outcomes"]
            if r["statuses"].get(o["case_id"]) in ENGINE_CALLED and o.get("latency_ms") is not None
        ]
        entry = c.manifest["candidates"].get(name)
        rows[name] = {
            "role": "candidate" if entry else "comparison point",
            "eligibility": entry.get("eligibility") if entry else "ineligible",
            "completing": name not in held.not_completing and len(complete) == len(slots),
            "unsafe": sum(1 for r in attempts for o in r["outcomes"] if o.get("unsafe")),
            "runs": per_run,
            "mean": statistics.mean(per_run) if per_run else None,
            "worst": min(per_run) if per_run else None,
            "changing_cases": sorted(set.union(*correct_sets) - set.intersection(*correct_sets))
            if correct_sets
            else [],
            "always_missed": sorted(all_cases - set.union(*correct_sets)) if correct_sets else [],
            "invalid": sum(1 for r in complete for s in r["statuses"].values() if s == "invalid"),
            "automatic_replies_on_misses": sum(
                1
                for r in complete
                for o in r["outcomes"]
                if not _fully_correct(o)
                and (o.get("got") or {}).get("automatic_reply")
                and not o.get("reply_ok", True)
            ),
            "cost_per_dispute_usd": (
                sum(_usd(r.get("cost") or {}, rate) for r in complete) / DISPUTES
                if complete
                else None
            ),
            "median_latency_ms": statistics.median(latencies) if latencies else None,
        }
    return _rank(rows) | {"candidates": rows, "campaign_spend_usd": spend}


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
        lines.append(
            f"| `{name}` | {row['role']} | {row['eligibility']} | "
            f"{'yes' if row['completing'] else 'no'} | {row['unsafe']} | "
            f"{', '.join(map(str, row['runs'])) or '--'} | {mean} | {row['worst'] or '--'} | "
            f"{row['invalid']} | {row['automatic_replies_on_misses']} | "
            f"{f'{cost:.6f}' if cost is not None else '--'} | "
            f"{row['median_latency_ms'] if row['median_latency_ms'] is not None else '--'} |"
        )
    lines += [
        "",
        f"Equivalent on correctness (within {TIE_BAND:g} case of the best eligible mean): "
        + (", ".join(f"`{n}`" for n in result["tied"]) or "none"),
        "",
        "Left by the rules, for the maintainer's choice: "
        + (", ".join(f"`{n}`" for n in result["left"]) or "none"),
        "",
        f"The whole campaign's spend, apart: about ${result['campaign_spend_usd']:.4f}.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path, nargs="?", default=campaign.MANIFEST)
    parser.add_argument("--results", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        result = compare(args.manifest, args.results or campaign.RESULTS_DIR)
    except ComparisonRefused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    text = markdown(result)
    (args.manifest.parent / "comparison.md").write_text(text, encoding="utf-8")
    (args.manifest.parent / "comparison.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
