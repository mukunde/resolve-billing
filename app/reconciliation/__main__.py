"""Reconcile estimated model cost against billed model cost.

    python -m app.reconciliation --billed cost_report.json --db audit.db

The exit code is the useful part for a scheduled run: 0 when the two ledgers
agree within tolerance, 1 when they do not. A cost drift that nobody is told
about is a cost drift that compounds.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.reconciliation.adapters import AnthropicAdminUsageReport, FileUsageReport
from app.reconciliation.reconciler import (
    TOLERANCE_PCT,
    Reconciliation,
    estimated_from_audit,
    reconcile,
)


def usd(micros: int) -> str:
    return f"${micros / 1e6:.5f}"


def report(result: Reconciliation, source: str) -> int:
    print(f"\nbilled source                 : {source}")
    print(f"rows                          : {len(result.rows)}")

    if result.rows:
        print(f"\n{'day':<12} {'model':<20} {'runs':>5} {'estimated':>12} "
              f"{'billed':>12} {'gap':>12} {'gap %':>8}")
        print("-" * 86)
        for row in result.rows:
            pct = "—" if row.gap_pct is None else f"{row.gap_pct:+.1f}%"
            print(
                f"{row.day.isoformat():<12} {row.model:<20} {row.runs:>5} "
                f"{usd(row.estimated_micros):>12} {usd(row.billed_micros):>12} "
                f"{usd(row.gap_micros):>12} {pct:>8}"
            )

    print(f"\n{'estimated (ours)':<30}: {usd(result.estimated_micros)}")
    print(f"{'billed (provider)':<30}: {usd(result.billed_micros)}")
    gap_pct = "—" if result.gap_pct is None else f"{result.gap_pct:+.1f}%"
    print(f"{'gap':<30}: {usd(result.gap_micros)}  ({gap_pct})")
    print(f"{'tolerance':<30}: ±{TOLERANCE_PCT}%")

    print("\nwhere the gap sits")
    for finding in result.diagnosis():
        print(f"  - {finding}")

    print(
        "\nEstimates apply dated list prices, not invoiced amounts, and the two "
        "\nledgers agree on the event but not the grain: runs are per dispute, "
        "\nthe bill is per day. Read the gap as a direction, not a balance."
    )

    if result.within_tolerance:
        print("\nWITHIN TOLERANCE")
        return 0
    print("\nOUT OF TOLERANCE")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="audit.db", help="the audit database")
    parser.add_argument(
        "--billed",
        type=Path,
        help="a saved cost-report response; omit to call the Admin API",
    )
    parser.add_argument("--days", type=int, default=7, help="how far back to look")
    parser.add_argument("--from", dest="start", help="YYYY-MM-DD, overrides --days")
    parser.add_argument("--to", dest="end", help="YYYY-MM-DD, exclusive")
    parser.add_argument("--engine", help="only runs from this decision engine")
    parser.add_argument("--workspace", help="only this workspace's billed lines")
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else datetime.now(UTC).date() + timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days)

    if args.billed:
        source = FileUsageReport(args.billed)
    else:
        try:
            source = AnthropicAdminUsageReport()
        except RuntimeError as error:
            sys.exit(f"{error}\n\nPass --billed with a saved cost report instead.")

    estimated = estimated_from_audit(args.db, start, end, engine=args.engine)
    billed = source.billed(start, end)
    result = reconcile(estimated, billed, workspace_id=args.workspace)

    print(f"period                        : {start} to {end} (exclusive)")
    return report(result, source.name)


if __name__ == "__main__":
    sys.exit(main())
