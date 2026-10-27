"""The comparison script: the spec's criteria, from the result files alone
(specs/008-deployment-model-choice, T050)."""

import json

import pytest

from app.decision.hosted import configuration
from evals import campaign, compare
from evals.compare import ComparisonRefused
from evals.first_calls import fingerprint
from tests.unit.test_campaign import outcome

CASES = [f"case_{i:02d}" for i in range(34)]
A, B, C = "gpt-6-luna", "qwen3.6-35b-a3b", "deepseek-v4.1-flash"


@pytest.fixture(autouse=True)
def at_the_manifest_s_commit(monkeypatch):
    monkeypatch.setattr(campaign, "manifest_commit", lambda path, root=None: "c0ffee")


def write_manifest(tmp_path, names=(A, B, C), eligibility=None, points=()):
    data = {
        "comparison": "008",
        "candidates": {
            n: {
                "fingerprint": fingerprint(configuration(n)),
                "eligibility": (eligibility or {}).get(n, "eligible provisionally"),
            }
            for n in names
        },
        "comparison_points": {n: {"fingerprint": fingerprint(configuration(n))} for n in points},
        "evidence": campaign.current_evidence(),
        "rounds": [list(names) + list(points)] * 3,
        "round_1_date": "2026-10-28",
        "exchange_rate": {"eur_usd": 1.10, "source": "ECB reference rate", "date": "2026-10-28"},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class Book:
    """Writes runs and their log lines as the runner and the campaign do."""

    def __init__(self, tmp_path):
        self.dir = tmp_path / "results"
        self.dir.mkdir()
        self.log = []

    def run(
        self,
        name,
        round_,
        attempt=1,
        correct=34,
        unsafe=0,
        invalid=0,
        auto_on_miss=0,
        cost=0.01,
        currency="USD",
        latency=100,
        complete=True,
        stop_reason=None,
        **overrides,
    ):
        slot = f"r{round_}-{name}"
        outcomes, statuses = [], {}
        for i, case in enumerate(CASES):
            o = outcome(case, unsafe="unsafe" if i < unsafe else None)
            o["latency_ms"] = latency
            if i >= correct:
                o["classification_ok"] = False
                if i - correct < auto_on_miss:
                    o["got"] = {"automatic_reply": True}
                    o["reply_ok"] = False
            outcomes.append(o)
            statuses[case] = "invalid" if i < invalid else "decided"
        data = {
            "campaign": "008",
            "engine": name,
            "slot": slot,
            "attempt": attempt,
            "complete": complete,
            "stop_reason": stop_reason,
            "stop_kind": campaign.stop_kind(stop_reason) if not complete else None,
            "commit": "c0ffee",
            "clean_tree": True,
            "configuration_fingerprint": fingerprint(configuration(name)),
            "outcomes": outcomes,
            "statuses": statuses,
            "cost": {
                "settled_amount": cost,
                "currency": currency,
                "unknown_cost_attempts": 0,
                "unknown_cost_reserved": 0.0,
            },
        } | campaign.current_evidence()
        data |= overrides
        (self.dir / f"{name}-{slot}-{attempt}.json").write_text(json.dumps(data), encoding="utf-8")
        self.log.append({"type": "attempt", "slot": slot, "attempt": attempt, "engine": name})
        self.flush()

    def flush(self):
        (self.dir / campaign.LOG_NAME).write_text(
            "".join(json.dumps(line) + "\n" for line in self.log), encoding="utf-8"
        )

    def all_rounds(self, per_name):
        """per_name: {name: [kwargs round 1, round 2, round 3]}"""
        for round_ in (1, 2, 3):
            for name, runs in per_name.items():
                self.run(name, round_, **runs[round_ - 1])


def table(tmp_path, book, **kwargs):
    return compare.compare(write_manifest(tmp_path, **kwargs), book.dir)


class TestRefusals:
    @pytest.mark.parametrize(
        "override",
        [
            {"commit": "later"},
            {"clean_tree": False},
            {"cases_sha256": "another"},
            {"prompt_version": "decision_v4"},
            {"schema_sha256": "another"},
            {"masking_version": "masking_v2"},
            {"configuration_fingerprint": "another"},
        ],
    )
    def test_a_file_off_the_declared_evidence(self, override, tmp_path):
        book = Book(tmp_path)
        book.run(A, 1, **override)

        with pytest.raises(ComparisonRefused):
            table(tmp_path, book)

    def test_an_undeclared_slot(self, tmp_path):
        book = Book(tmp_path)
        book.run(A, 9)

        with pytest.raises(ComparisonRefused, match="declared"):
            table(tmp_path, book)

    def test_a_file_not_in_the_log(self, tmp_path):
        book = Book(tmp_path)
        book.run(A, 1)
        book.log.clear()
        book.flush()

        with pytest.raises(ComparisonRefused, match="log"):
            table(tmp_path, book)

    def test_a_logged_attempt_without_its_file(self, tmp_path):
        book = Book(tmp_path)
        book.log.append({"type": "attempt", "slot": f"r1-{A}", "attempt": 1, "engine": A})
        book.flush()

        with pytest.raises(ComparisonRefused, match="no result"):
            table(tmp_path, book)

    def test_an_out_of_order_attempt(self, tmp_path):
        book = Book(tmp_path)
        book.run(A, 2)

        with pytest.raises(ComparisonRefused, match="order"):
            table(tmp_path, book)

    def test_a_manifest_edited_after_the_runs(self, tmp_path, monkeypatch):
        book = Book(tmp_path)
        book.run(A, 1)
        monkeypatch.setattr(campaign, "manifest_commit", lambda path, root=None: "edited")

        with pytest.raises(ComparisonRefused, match="commit"):
            table(tmp_path, book)


def three(**kwargs):
    return [dict(kwargs), dict(kwargs), dict(kwargs)]


class TestTheCriteria:
    def test_unsafe_in_any_attempt_removes_a_candidate(self, tmp_path):
        book = Book(tmp_path)
        book.all_rounds(
            {
                A: [{"unsafe": 1}, {}, {}],
                B: three(correct=30),
                C: three(correct=28),
            }
        )

        result = table(tmp_path, book)

        assert result["candidates"][A]["unsafe"] == 1
        assert A not in result["left"]
        assert result["left"] == [B]

    def test_the_mean_the_worst_run_and_the_cases(self, tmp_path):
        book = Book(tmp_path)
        book.all_rounds({A: [{"correct": 30}, {"correct": 29}, {"correct": 31}]})

        a = table(tmp_path, book, names=(A,))["candidates"][A]

        assert a["runs"] == [30, 29, 31]
        assert a["mean"] == pytest.approx(30.0)
        assert a["worst"] == 29
        assert a["changing_cases"] == ["case_29", "case_30"]
        assert "case_33" in a["always_missed"]

    def test_the_tie_band_is_one_case(self, tmp_path):
        book = Book(tmp_path)
        book.all_rounds(
            {
                A: three(correct=31),
                B: three(correct=30),
                C: [{"correct": 30}, {"correct": 29}, {"correct": 30}],
            }
        )

        result = table(tmp_path, book)

        assert result["tied"] == [A, B]

    def test_tie_breaks_in_order(self, tmp_path):
        book = Book(tmp_path)
        book.all_rounds(
            {
                A: three(correct=30, invalid=1),
                B: three(correct=30, auto_on_miss=1),
                C: three(correct=30, cost=0.001),
            }
        )

        result = table(tmp_path, book)

        # A has invalid answers; B and C have none; B replied automatically
        # on a missed case, C did not.
        assert result["left"] == [C]
        assert result["tie_breaks"][0]["kept"] == [B, C]
        assert result["tie_breaks"][1]["kept"] == [C]

    def test_cost_per_dispute_converts_euros_and_counts_unknown_costs(self, tmp_path):
        book = Book(tmp_path)
        book.all_rounds(
            {
                A: three(cost=0.102),
                B: three(cost=0.051, currency="EUR"),
            }
        )

        result = table(tmp_path, book, names=(A, B))

        assert result["candidates"][A]["cost_per_dispute_usd"] == pytest.approx(0.003)
        assert result["candidates"][B]["cost_per_dispute_usd"] == pytest.approx(
            0.051 * 3 * 1.10 / 102
        )

    def test_latency_is_the_median_of_engine_calls(self, tmp_path):
        book = Book(tmp_path)
        book.all_rounds({A: [{"latency": 100}, {"latency": 300}, {"latency": 200}]})

        assert table(tmp_path, book, names=(A,))["candidates"][A]["median_latency_ms"] == 200

    def test_eligibility_comes_first(self, tmp_path):
        book = Book(tmp_path)
        book.all_rounds({A: three(correct=34), B: three(correct=30)})

        result = table(tmp_path, book, names=(A, B), eligibility={A: "ineligible"})

        assert result["left"] == [B]

    def test_a_replaced_run_counts_once_and_its_unsafe_still_counts(self, tmp_path):
        book = Book(tmp_path)
        book.run(A, 1, complete=False, stop_reason="interrupted", unsafe=1)
        book.run(A, 1, attempt=2)
        book.run(A, 2)
        book.run(A, 3)

        a = table(tmp_path, book, names=(A,))["candidates"][A]

        assert a["runs"] == [34, 34, 34]
        assert a["unsafe"] == 1

    def test_a_not_completing_candidate_is_listed_never_ranked(self, tmp_path):
        book = Book(tmp_path)
        book.run(A, 1, complete=False, stop_reason="the engine refused the request (400)")
        book.log.append({"type": "retired", "slot": f"r2-{A}"})
        book.log.append({"type": "retired", "slot": f"r3-{A}"})
        book.flush()
        for round_ in (1, 2, 3):
            book.run(B, round_)

        result = table(tmp_path, book, names=(A, B))

        assert result["candidates"][A]["completing"] is False
        assert result["left"] == [B]

    def test_the_campaign_spend_is_reported_apart(self, tmp_path):
        book = Book(tmp_path)
        book.run(A, 1, complete=False, stop_reason="interrupted", cost=0.5)
        book.run(A, 1, attempt=2, cost=0.01)
        book.run(A, 2, cost=0.01)
        book.run(A, 3, cost=0.01)

        result = table(tmp_path, book, names=(A,))

        assert result["campaign_spend_usd"] == pytest.approx(0.53)
        assert result["candidates"][A]["cost_per_dispute_usd"] == pytest.approx(0.03 / 102)


class TestOutputs:
    def test_markdown_and_json_beside_the_manifest(self, tmp_path):
        book = Book(tmp_path)
        book.all_rounds({A: three()})
        manifest = write_manifest(tmp_path, names=(A,))

        compare.main([str(manifest), "--results", str(book.dir)])

        assert (tmp_path / "comparison.md").read_text(encoding="utf-8").startswith("#")
        held = json.loads((tmp_path / "comparison.json").read_text(encoding="utf-8"))
        assert held["left"] == [A]
