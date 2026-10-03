"""Reconciling our estimate against the provider's bill.

The arithmetic is the easy half. The tests that matter are the ones about what
the numbers are allowed to mean: an unknown cost is not a zero cost, a billed
amount with no estimate is not a 0 % gap, and a gap without a named cause is
a number somebody will argue with instead of fix.
"""

import json
import sqlite3
from datetime import date

import pytest

from app.infrastructure.audit import SqliteAuditRepository
from app.reconciliation.adapters import FileUsageReport
from app.reconciliation.base import BilledLine, cents_to_micros, parse_cost_report
from app.reconciliation.reconciler import (
    Estimated,
    estimated_from_audit,
    reconcile,
)

DAY = date(2026, 9, 22)


def billed(amount_micros: int, token_type: str, model: str = "claude-opus-5", **kw):
    return BilledLine(
        day=kw.pop("day", DAY),
        amount_micros=amount_micros,
        cost_type="tokens",
        description=f"{model} - {token_type}",
        model=model,
        token_type=token_type,
        **kw,
    )


class TestTheConversion:
    def test_cents_become_micros(self):
        """One cent is ten thousand millionths of a dollar."""
        assert cents_to_micros("1") == 10_000
        assert cents_to_micros("123.78912") == 1_237_891

    def test_rounding_is_half_up_like_money(self):
        """The two roundings in this codebase must not disagree.

        `Money.from_decimal` rounds half away from zero; so does this, so a
        half-micro cannot land differently depending on which one saw it.
        """
        assert cents_to_micros("0.00005") == 1
        assert cents_to_micros("0.000049") == 0

    def test_amounts_accumulate_exactly(self):
        """Integers in, integers out, no drift on the way.

        A single conversion through float would survive at these magnitudes;
        the accumulation is where it stops being harmless. `sum(0.07 for _ in
        range(100))` is 7.000000000000001, and a month of a reconciliation is
        a great many more additions than a hundred. Converting once, to an
        integer, removes the question rather than bounding it -- the same
        reason `cost_micros` and `Money` are integers.
        """
        assert cents_to_micros("0.07") == 700
        assert sum(cents_to_micros("0.07") for _ in range(100)) == 70_000
        assert sum(cents_to_micros("0.07") for _ in range(100_000)) == 70_000_000


class TestParsingTheReport:
    PAYLOAD = {
        "data": [
            {
                "starting_at": "2026-09-22T00:00:00Z",
                "ending_at": "2026-09-23T00:00:00Z",
                "results": [
                    {
                        "amount": "3.208",
                        "cost_type": "tokens",
                        "description": "Input Tokens",
                        "model": "claude-opus-5",
                        "token_type": "uncached_input_tokens",
                        "workspace_id": "wrkspc_1",
                    },
                    {
                        "amount": "0.62",
                        "cost_type": "tokens",
                        "description": "Cache Read",
                        "model": "claude-opus-5",
                        "token_type": "cache_read_input_tokens",
                        "workspace_id": "wrkspc_1",
                    },
                ],
            }
        ],
        "has_more": False,
    }

    def test_buckets_flatten_into_lines_dated_by_the_bucket(self):
        lines = parse_cost_report(self.PAYLOAD)

        assert len(lines) == 2
        assert {line.day for line in lines} == {DAY}
        assert lines[0].amount_micros == 32_080

    def test_a_result_with_no_amount_is_skipped_not_zeroed(self):
        payload = {"data": [{"starting_at": "2026-09-22T00:00:00Z", "results": [{}]}]}

        assert parse_cost_report(payload) == []

    def test_an_empty_report_is_not_an_error(self):
        assert parse_cost_report({"data": []}) == []
        assert parse_cost_report({}) == []


class TestTheComparison:
    def test_the_gap_is_billed_minus_estimated(self):
        result = reconcile(
            [Estimated(DAY, "claude-opus-5", 44_630, runs=2, priced_runs=2, cache_read_tokens=0)],
            [billed(32_080, "uncached_input_tokens"), billed(12_550, "output_tokens")],
        )

        assert result.estimated_micros == 44_630
        assert result.billed_micros == 44_630
        assert result.gap_micros == 0
        assert result.within_tolerance

    def test_a_cache_line_is_named_as_the_cause(self):
        """The point of the whole module: a gap with an address."""
        result = reconcile(
            [Estimated(DAY, "claude-opus-5", 44_630, runs=2, priced_runs=2, cache_read_tokens=900)],
            [
                billed(32_080, "uncached_input_tokens"),
                billed(12_550, "output_tokens"),
                billed(6_200, "cache_read_input_tokens"),
            ],
        )

        assert not result.within_tolerance
        assert result.gap_micros == 6_200
        assert "cache tokens" in " ".join(result.diagnosis())

    def test_billed_with_no_estimate_is_not_a_zero_percent_gap(self):
        """Dividing by nothing is not the same as agreeing."""
        result = reconcile([], [billed(5_000, "uncached_input_tokens")])

        assert result.rows[0].estimated_micros == 0
        assert result.rows[0].gap_pct is None
        assert result.gap_pct is None
        assert not result.within_tolerance

    def test_runs_we_could_not_price_are_counted_not_hidden(self):
        """`cost_micros IS NULL` means the model was unknown to the table.

        Summed as zero it would silently deepen the gap and look like the
        provider overcharging.
        """
        result = reconcile(
            [Estimated(DAY, "mystery-model", 0, runs=3, priced_runs=0, cache_read_tokens=0)],
            [billed(9_000, "uncached_input_tokens", model="mystery-model")],
        )

        assert result.unknown_estimate_runs == 3
        assert "no estimate at all" in " ".join(result.diagnosis())

    def test_another_workspace_s_spend_is_excluded(self):
        """The bill covers the account; the audit covers this project.

        Reconciling one against the other compares two populations and calls
        the difference an error.
        """
        ours = billed(10_000, "uncached_input_tokens", workspace_id="wrkspc_ours")
        theirs = billed(90_000, "uncached_input_tokens", workspace_id="wrkspc_other")

        result = reconcile(
            [Estimated(DAY, "claude-opus-5", 10_000, runs=1, priced_runs=1, cache_read_tokens=0)],
            [ours, theirs],
            workspace_id="wrkspc_ours",
        )

        assert result.billed_micros == 10_000
        assert result.within_tolerance


class TestReadingOurOwnLedger:
    @pytest.fixture
    def database(self, tmp_path):
        path = tmp_path / "audit.db"
        repository = SqliteAuditRepository(path)
        connection = sqlite3.connect(path)
        def row(run_id, created_at, model_id, cost_micros):
            return (
                run_id, created_at, "fake", "claude", "claude/decision_v3", "msg",
                "valid_invoice", 0.9, "self_reported", "explication", "[]",
                "reply", "auto", "rien a autoriser", 0, model_id, cost_micros,
            )

        connection.executemany(
            "INSERT INTO agent_runs (run_id, created_at, billing_provider, "
            "decision_engine, decision_engine_version, input_message, "
            "classification, confidence, confidence_kind, explanation, "
            "findings_json, proposed_action, verdict, authorization_reason, "
            "autonomy_available, model_id, cost_micros) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                row("r1", "2026-09-22T10:00:00+00:00", "claude-opus-5", 22_280),
                row("r2", "2026-09-22T11:00:00+00:00", "claude-opus-5", 22_350),
                # Unknown to the price table: recorded, not priced.
                row("r3", "2026-09-22T12:00:00+00:00", "some-new-model", None),
                # Outside the window.
                row("r4", "2026-09-01T10:00:00+00:00", "claude-opus-5", 99_999),
            ],
        )
        connection.commit()
        connection.close()
        repository.close()
        return str(path)

    def test_runs_group_by_day_and_model(self, database):
        entries = estimated_from_audit(database, DAY, date(2026, 9, 23))

        by_model = {e.model: e for e in entries}
        assert by_model["claude-opus-5"].cost_micros == 44_630
        assert by_model["claude-opus-5"].runs == 2

    def test_an_unpriced_run_contributes_nothing_but_is_counted(self, database):
        entries = estimated_from_audit(database, DAY, date(2026, 9, 23))

        unknown = next(e for e in entries if e.model == "some-new-model")
        assert unknown.cost_micros == 0
        assert unknown.runs == 1
        assert unknown.priced_runs == 0

    def test_the_window_is_respected(self, database):
        entries = estimated_from_audit(database, DAY, date(2026, 9, 23))

        assert all(e.day == DAY for e in entries)


class TestTheFileAdapter:
    def test_it_reads_a_saved_response_and_filters_the_window(self, tmp_path):
        path = tmp_path / "cost.json"
        path.write_text(json.dumps(TestParsingTheReport.PAYLOAD), encoding="utf-8")

        inside = FileUsageReport(path).billed(DAY, date(2026, 9, 23))
        outside = FileUsageReport(path).billed(date(2026, 1, 1), date(2026, 1, 2))

        assert len(inside) == 2
        assert outside == []

    def test_the_committed_fixture_says_it_is_synthetic(self):
        """It is shaped like a bill and must never be mistaken for one."""
        payload = json.loads(
            (
                __import__("pathlib").Path("fixtures/cost_report_synthetic.json")
            ).read_text(encoding="utf-8")
        )

        assert "SYNTHETIC" in payload["_note"]
