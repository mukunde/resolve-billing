"""The prepaid envelope of a vendor account, kept on disk
(specs/007-openai-qwen-engines, T042)."""

import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

import pytest

from app.decision.hosted import Answer, configuration
from evals import state
from evals.governed import InconsistentUsage, attempt_cost
from evals.ledger import Ledger, LedgerRefused, PrepaidLedger
from evals.limits import RunStopped


def book(tmp_path, account="openai-org-test", currency="USD"):
    return PrepaidLedger(account, currency, path=tmp_path / f"envelope-{account}.json")


class TestDeclaring:
    def test_the_first_declaration_opens_the_ledger(self, tmp_path):
        ledger = book(tmp_path)

        ledger.declare(5.0)

        held = ledger.state()
        assert held["currency"] == "USD"
        assert (held["total"], held["spent"], held["left"]) == (5.0, 0.0, 5.0)

    def test_nothing_is_reserved_before_a_declaration(self, tmp_path):
        with pytest.raises(LedgerRefused, match="--envelope"):
            book(tmp_path).reserve("a", 0.01)

    def test_a_raise_is_a_new_declaration_the_old_kept(self, tmp_path):
        ledger = book(tmp_path)
        ledger.declare(5.0)
        ledger.reserve("a", 1.0)

        ledger.declare(8.0)

        held = ledger.state()
        assert held["total"] == 8.0
        assert [d["total"] for d in held["declarations"]] == [5.0, 8.0]
        assert held["spent"] == 1.0

    @pytest.mark.parametrize("total", [5.0, 4.0, 0.5])
    def test_a_declaration_never_lowers_or_repeats_the_total(self, tmp_path, total):
        ledger = book(tmp_path)
        ledger.declare(5.0)
        ledger.reserve("a", 1.0)

        with pytest.raises(LedgerRefused):
            ledger.declare(total)

        assert ledger.state()["total"] == 5.0
        assert ledger.state()["spent"] == 1.0

    @pytest.mark.parametrize("total", [0, -1, float("nan"), float("inf")])
    def test_a_total_must_be_a_positive_amount(self, tmp_path, total):
        with pytest.raises(LedgerRefused):
            book(tmp_path).declare(total)


class TestReserving:
    def test_an_attempt_beyond_what_remains_is_not_sent(self, tmp_path):
        ledger = book(tmp_path)
        ledger.declare(1.0)
        ledger.reserve("a", 0.7)

        with pytest.raises(RunStopped, match="envelope"):
            ledger.reserve("b", 0.4)

        assert ledger.state()["spent"] == 0.7

    def test_settling_replaces_the_reservation(self, tmp_path):
        ledger = book(tmp_path)
        ledger.declare(1.0)
        ledger.reserve("a", 0.7)

        ledger.settle("a", 0.1)

        assert ledger.state()["spent"] == pytest.approx(0.1)

    def test_an_interrupted_attempt_stays_spent_after_a_restart(self, tmp_path):
        ledger = book(tmp_path)
        ledger.declare(1.0)
        ledger.reserve("a", 0.7)

        reopened = book(tmp_path)

        assert reopened.state()["spent"] == 0.7
        assert reopened.state()["unsettled"] == 1

    def test_one_attempt_is_reserved_once(self, tmp_path):
        ledger = book(tmp_path)
        ledger.declare(1.0)
        ledger.reserve("a", 0.1)

        with pytest.raises(LedgerRefused):
            ledger.reserve("a", 0.1)


def _reserve_in_another_process(
    path: str, attempt: str, amount: float, lock_timeout: float = 30.0
) -> str | bool:
    from pathlib import Path

    try:
        ledger = PrepaidLedger("openai-org-test", "USD", path=Path(path), lock_timeout=lock_timeout)
        ledger.reserve(attempt, amount)
    except RunStopped:
        return False
    except LedgerRefused as refusal:
        return str(refusal)
    return True


class TestConcurrency:
    def test_two_processes_never_spend_more_than_the_envelope(self, tmp_path):
        ledger = book(tmp_path)
        ledger.declare(1.0)

        with ProcessPoolExecutor(max_workers=8) as pool:
            admitted = list(
                pool.map(
                    _reserve_in_another_process,
                    [str(ledger.path)] * 8,
                    [f"attempt-{i}" for i in range(8)],
                    [0.3] * 8,
                )
            )

        assert sum(admitted) == 3
        assert ledger.state()["spent"] == pytest.approx(0.9)

    def test_a_held_lock_keeps_another_process_out_until_released(self, tmp_path):
        ledger = book(tmp_path)
        ledger.declare(1.0)
        before = ledger.path.read_bytes()

        with ledger._locked(), ProcessPoolExecutor(max_workers=1) as pool:
            refused = pool.submit(
                _reserve_in_another_process, str(ledger.path), "a", 0.1, 0.5
            ).result()
            unchanged = ledger.path.read_bytes()
        with ProcessPoolExecutor(max_workers=1) as pool:
            admitted = pool.submit(_reserve_in_another_process, str(ledger.path), "a", 0.1).result()

        assert "held by another run" in refused
        assert unchanged == before
        assert admitted is True
        assert ledger.state()["spent"] == pytest.approx(0.1)

    @pytest.mark.parametrize("amount", [-0.1, float("nan"), float("inf"), "0.1", True])
    def test_an_invalid_amount_writes_nothing(self, tmp_path, amount):
        ledger = book(tmp_path)
        ledger.declare(1.0)
        ledger.reserve("a", 0.1)
        before = ledger.path.read_bytes()

        with pytest.raises(LedgerRefused):
            ledger.reserve("b", amount)
        with pytest.raises(LedgerRefused):
            ledger.settle("a", amount)

        assert ledger.path.read_bytes() == before


class TestWhatIsKeptApart:
    def test_two_organisations_keep_two_envelopes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(state, "STATE_DIR", tmp_path)
        a = PrepaidLedger("openai-org-a", "USD")
        b = PrepaidLedger("openai-org-b", "USD")
        a.declare(1.0)
        b.declare(1.0)

        a.reserve("x", 0.9)

        assert b.state()["spent"] == 0.0
        assert a.path != b.path

    def test_dollars_and_euros_are_kept_apart(self, tmp_path):
        ledger = book(tmp_path)
        ledger.declare(1.0)

        with pytest.raises(LedgerRefused, match="currency"):
            PrepaidLedger("openai-org-test", "EUR", path=ledger.path).state()

    def test_the_file_lives_with_the_accounts_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(state, "STATE_DIR", tmp_path)

        assert PrepaidLedger("scaleway-org", "EUR").path == (
            tmp_path / "envelope-scaleway-org.json"
        )

    def test_mistral_s_monthly_ledger_is_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(state, "STATE_DIR", tmp_path)
        mistral = Ledger()
        mistral.declare(10.0)

        held = json.loads(mistral.path.read_text(encoding="utf-8"))

        assert mistral.path.name == "mistral-envelope.json"
        assert set(held) == {"month", "allowance_usd", "attempts"}


class TestRefusedNeverReset:
    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            json.dumps({"account": "openai-org-test"}),
            json.dumps(
                {
                    "account": "openai-org-test",
                    "currency": "USD",
                    "declarations": [{"at": "2026-10-26T00:00:00+00:00", "total": -1}],
                    "attempts": {},
                }
            ),
            json.dumps(
                {
                    "account": "openai-org-test",
                    "currency": ["USD"],
                    "declarations": [{"at": "2026-10-26T00:00:00+00:00", "total": 5}],
                    "attempts": {},
                }
            ),
            json.dumps(
                {
                    "account": "openai-org-test",
                    "currency": "USD",
                    "declarations": [{"at": "not a date", "total": 5}],
                    "attempts": {},
                }
            ),
            json.dumps(
                {
                    "account": "openai-org-test",
                    "currency": "USD",
                    "declarations": [
                        {"at": "2026-10-26T00:00:00+00:00", "total": 5},
                        {"at": "2026-10-26T01:00:00+00:00", "total": 3},
                    ],
                    "attempts": {},
                }
            ),
        ],
    )
    def test_an_unreadable_or_inconsistent_ledger(self, tmp_path, content):
        ledger = book(tmp_path)
        ledger.path.write_text(content, encoding="utf-8")

        with pytest.raises(LedgerRefused):
            ledger.state()
        with pytest.raises(LedgerRefused):
            ledger.declare(9.0)

        assert ledger.path.read_text(encoding="utf-8") == content

    def test_another_account_s_file_is_refused(self, tmp_path):
        ledger = book(tmp_path)
        ledger.declare(1.0)

        with pytest.raises(LedgerRefused, match="account"):
            PrepaidLedger("openai-org-other", "USD", path=ledger.path).state()


def answer(input_tokens=1000, output_tokens=200, cached=None, written=None, reasoning=None):
    return Answer(
        "{}",
        "stop",
        input_tokens,
        output_tokens,
        "gpt-6.1-sol",
        cached_input_tokens=cached,
        cache_write_tokens=written,
        reasoning_tokens=reasoning,
    )


SOL = configuration("gpt-6.1-sol")  # 2 / 10, cached 0.10, cache write 2.50


class TestSettlementByCategory:
    def test_every_category_reported(self):
        cost = attempt_cost(answer(cached=100, written=50, reasoning=40), SOL)

        expected = (100 * 0.10 + 50 * 2.50 + 850 * 2.0 + 200 * 10.0) / 1e6
        assert cost == pytest.approx(expected)

    def test_reasoning_is_never_priced_twice(self):
        with_reasoning = attempt_cost(answer(cached=0, written=0, reasoning=150), SOL)
        without = attempt_cost(answer(cached=0, written=0), SOL)

        assert with_reasoning == without

    def test_no_detail_reported_prices_all_input_at_the_cache_write_rate(self):
        cost = attempt_cost(answer(), SOL)

        assert cost == pytest.approx((1000 * 2.50 + 200 * 10.0) / 1e6)

    def test_cached_reported_without_cache_writes(self):
        cost = attempt_cost(answer(cached=100), SOL)

        assert cost == pytest.approx((100 * 0.10 + 900 * 2.50 + 200 * 10.0) / 1e6)

    def test_cache_writes_reported_without_cached(self):
        cost = attempt_cost(answer(written=50), SOL)

        assert cost == pytest.approx((50 * 2.50 + 950 * 2.50 + 200 * 10.0) / 1e6)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"cached": 900, "written": 200},
            {"cached": 1001},
            {"reasoning": 201},
        ],
    )
    def test_an_inconsistent_breakdown_is_refused(self, kwargs):
        with pytest.raises(InconsistentUsage):
            attempt_cost(answer(**kwargs), SOL)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"written": -1000},
            {"cached": -1},
            {"reasoning": -5},
            {"input_tokens": -1},
            {"output_tokens": -1},
            {"cached": True},
        ],
    )
    def test_counts_that_are_not_counts_are_refused(self, kwargs):
        with pytest.raises(InconsistentUsage):
            attempt_cost(answer(**kwargs), SOL)

    def test_an_impossible_unpriced_category_is_refused(self):
        qwen = configuration("qwen3.5-397b-a17b")

        with pytest.raises(InconsistentUsage):
            attempt_cost(answer(cached=5000), qwen)

    def test_a_vendor_without_priced_categories_prices_input_at_its_rate(self):
        qwen = configuration("qwen3.5-397b-a17b")  # 0.60 / 3.60, no cached rate

        cost = attempt_cost(answer(cached=100, reasoning=40), qwen)

        assert cost == pytest.approx((1000 * 0.60 + 200 * 3.60) / 1e6)

    def test_mistral_s_cost_is_unchanged(self):
        mistral = configuration("mistral-medium-3-5")

        cost = attempt_cost(answer(), mistral)

        assert cost == pytest.approx((1000 * 1.5 + 200 * 7.5) / 1e6)

    def test_the_reservation_uses_the_highest_rate(self):
        from evals.governed import reservation_cost

        assert reservation_cost(1000, SOL) == pytest.approx(1000 * 10.0 / 1e6)
        assert reservation_cost(1000, replace(SOL, price_per_mtok=(2.0, 1.0))) == (
            pytest.approx(1000 * 2.50 / 1e6)
        )
