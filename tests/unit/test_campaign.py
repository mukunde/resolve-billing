"""The comparison's campaign: slots bound at launch, one lock, one log,
recovery of a lost run (specs/008-deployment-model-choice, T040)."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.decision.hosted import configuration
from evals import campaign
from evals.campaign import Campaign, CampaignRefused
from evals.first_calls import fingerprint

ROUNDS = [["gpt-6-luna", "qwen3.6-35b-a3b"]] * 3


def outcome(case_id, unsafe=None):
    """A recorded case's outcome, as the runner's `CaseOutcome` gives it."""
    from dataclasses import asdict

    from evals.runner import CaseOutcome

    return asdict(
        CaseOutcome(
            case_id=case_id,
            classification_ok=True,
            action_ok=True,
            approval_ok=True,
            findings_ok=True,
            skip_ok=True,
            unsafe=unsafe,
            confidence=0.9,
            confidence_kind="self_reported",
            cost_micros=None,
            latency_ms=10,
        )
    )


def manifest(tmp_path, rounds=ROUNDS, eligibility=None):
    names = sorted({name for r in rounds for name in r})
    data = {
        "comparison": "008",
        "candidates": {
            name: {
                "fingerprint": fingerprint(configuration(name)),
                "eligibility": (eligibility or {}).get(name, "eligible"),
            }
            for name in names
        },
        "comparison_points": {},
        "evidence": campaign.current_evidence(),
        "rounds": rounds,
        "round_1_date": "2026-10-28",
        "exchange_rate": {"eur_usd": 1.08, "source": "ECB reference rate", "date": "2026-10-28"},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def clean(monkeypatch):
    state = {"commit": "c0ffee", "clean_tree": True}
    monkeypatch.setattr(campaign, "git_state", lambda root=None: dict(state))
    monkeypatch.setattr(campaign, "manifest_commit", lambda path, root=None: "c0ffee")
    # The accounts the slots' configurations are run on.
    for variable, value in {
        "OPENAI_ORG_ID": "org-test",
        "OPENAI_PROJECT_ID": "proj_test",
        "SCALEWAY_ORGANIZATION_ID": "scw-org",
        "SCALEWAY_PROJECT_ID": "scw-project",
    }.items():
        monkeypatch.setenv(variable, value)
    return state


def make(tmp_path, **kwargs):
    results = tmp_path / "results"
    results.mkdir(exist_ok=True)
    return Campaign(manifest(tmp_path, **kwargs), results_dir=results)


def result(c, slot, attempt, complete=True, stop_reason=None, engine=None):
    """A run's result file, as the runner writes it."""
    engine = engine or slot.split("-", 1)[1]
    data = {
        "campaign": "008",
        "slot": slot,
        "attempt": attempt,
        "engine": engine,
        "complete": complete,
        "stop_reason": stop_reason,
        "stop_kind": campaign.stop_kind(stop_reason) if not complete else None,
        "commit": "c0ffee",
        "clean_tree": True,
        "outcomes": [],
        "statuses": {},
    }
    path = c.results_dir / f"{engine}-{slot}-{attempt}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def admit_and_finish(c, slot, **kwargs):
    with c.admit(slot.split("-", 1)[1], slot) as admitted:
        result(c, slot, admitted.attempt, **kwargs)
    return admitted


class TestAdmission:
    def test_the_first_slot_of_round_one_is_admitted(self, tmp_path, clean):
        c = make(tmp_path)

        with c.admit("gpt-6-luna", "r1-gpt-6-luna") as admitted:
            assert admitted.attempt == 1

        lines = c.log_lines()
        assert lines[-1]["slot"] == "r1-gpt-6-luna" and lines[-1]["attempt"] == 1
        assert lines[-1]["commit"] == "c0ffee"

    @pytest.mark.parametrize(
        ("engine", "slot", "why"),
        [
            ("gpt-6-luna", "r9-gpt-6-luna", "not declared"),
            ("qwen3.6-35b-a3b", "r1-gpt-6-luna", "another configuration"),
            ("qwen3.6-35b-a3b", "r1-qwen3.6-35b-a3b", "next slot"),
            ("gpt-6-luna", "r2-gpt-6-luna", "next slot"),
        ],
    )
    def test_a_slot_out_of_turn_is_refused(self, engine, slot, why, tmp_path, clean):
        c = make(tmp_path)

        with pytest.raises(CampaignRefused, match=why), c.admit(engine, slot):
            pass

        assert c.log_lines() == []

    def test_a_dirty_tree_is_refused(self, tmp_path, clean):
        clean["clean_tree"] = False
        c = make(tmp_path)

        with pytest.raises(CampaignRefused, match="clean"), c.admit("gpt-6-luna", "r1-gpt-6-luna"):
            pass

    def test_a_changed_configuration_is_refused(self, tmp_path, clean, monkeypatch):
        c = make(tmp_path)
        monkeypatch.setattr(campaign, "fingerprint", lambda config: "another")

        with (
            pytest.raises(CampaignRefused, match="fingerprint"),
            c.admit("gpt-6-luna", "r1-gpt-6-luna"),
        ):
            pass

    def test_provisional_limits_are_refused(self, tmp_path, clean, monkeypatch):
        from dataclasses import replace

        from app.decision.hosted import PROVISIONAL_LIMITS

        c = make(tmp_path)
        provisional = replace(configuration("gpt-6-luna"), limits_source=PROVISIONAL_LIMITS)
        monkeypatch.setattr(campaign, "configuration", lambda name: provisional)
        monkeypatch.setattr(
            campaign, "fingerprint", lambda config: fingerprint(configuration(config.name))
        )

        with (
            pytest.raises(CampaignRefused, match="provisional"),
            c.admit("gpt-6-luna", "r1-gpt-6-luna"),
        ):
            pass

    def test_rounds_in_order(self, tmp_path, clean):
        c = make(tmp_path)
        admit_and_finish(c, "r1-gpt-6-luna")
        admit_and_finish(c, "r1-qwen3.6-35b-a3b")

        admitted = admit_and_finish(c, "r2-gpt-6-luna")

        assert admitted.attempt == 1


class TestReplacements:
    def test_a_qualifying_interruption_is_replaced_once_next(self, tmp_path, clean):
        c = make(tmp_path)
        admit_and_finish(c, "r1-gpt-6-luna", complete=False, stop_reason="interrupted")

        with (
            pytest.raises(CampaignRefused, match="next slot"),
            c.admit("qwen3.6-35b-a3b", "r1-qwen3.6-35b-a3b"),
        ):
            pass
        admitted = admit_and_finish(c, "r1-gpt-6-luna")

        assert admitted.attempt == 2

    def test_a_second_interruption_retires_the_candidate(self, tmp_path, clean):
        c = make(tmp_path)
        admit_and_finish(c, "r1-gpt-6-luna", complete=False, stop_reason="interrupted")
        admit_and_finish(c, "r1-gpt-6-luna", complete=False, stop_reason="interrupted")

        admit_and_finish(c, "r1-qwen3.6-35b-a3b")

        state = c.state()
        assert "gpt-6-luna" in state.not_completing
        assert state.slots["r2-gpt-6-luna"] == "retired"
        assert state.slots["r3-gpt-6-luna"] == "retired"
        assert admit_and_finish(c, "r2-qwen3.6-35b-a3b").attempt == 1

    @pytest.mark.parametrize(
        "reason",
        [
            "gpt-6-luna: answered by another model (gpt-6-astra)",
            "gpt-6-luna: an answer came without its usage; the run stops",
            "the engine refused the request (400)",
            "the envelope of openai-org cannot cover this attempt",
        ],
    )
    def test_any_other_stop_retires_without_replacement(self, reason, tmp_path, clean):
        c = make(tmp_path)
        admit_and_finish(c, "r1-gpt-6-luna", complete=False, stop_reason=reason)

        with (
            pytest.raises(CampaignRefused, match="not completing"),
            c.admit("gpt-6-luna", "r1-gpt-6-luna"),
        ):
            pass
        assert "gpt-6-luna" in c.state().not_completing

    def test_qualifying_reasons(self):
        for reason in (
            "interrupted",
            "the run's deadline of 3600 s is reached",
            "rate limiting persisted past 3 attempts",
            "outage: server_error",
            "process lost; rebuilt from its per-case record",
        ):
            assert campaign.stop_kind(reason) == "qualifying", reason
        assert campaign.stop_kind("internal error (KeyError)") == "other"


class TestOneLockOneLog:
    def test_two_runs_never_admit_at_once(self, tmp_path, clean):
        c = make(tmp_path)

        def try_admit(_):
            try:
                with c.admit("gpt-6-luna", "r1-gpt-6-luna", lock_timeout=0.2):
                    import time

                    time.sleep(0.5)
                return "admitted"
            except CampaignRefused as refusal:
                return str(refusal)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(try_admit, range(2)))

        assert outcomes.count("admitted") == 1

    def test_the_log_line_is_on_disk_before_the_run(self, tmp_path, clean):
        c = make(tmp_path)

        with c.admit("gpt-6-luna", "r1-gpt-6-luna"):
            on_disk = (c.results_dir / campaign.LOG_NAME).read_text(encoding="utf-8")

        assert '"r1-gpt-6-luna"' in on_disk


class TestLostRuns:
    def test_a_killed_run_is_rebuilt_from_its_cases_and_replaced(self, tmp_path, clean):
        c = make(tmp_path)
        with c.admit("gpt-6-luna", "r1-gpt-6-luna") as admitted:
            unsafe = outcome("duplicate_fr", unsafe="money moved where none moves")
            admitted.record_case("duplicate_fr", unsafe, "decided")
            # killed here: no result written

        admitted = admit_and_finish(c, "r1-gpt-6-luna")

        assert admitted.attempt == 2
        rebuilt = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in c.results_dir.glob("*.json")
            if json.loads(p.read_text(encoding="utf-8")).get("rebuilt")
        ]
        assert len(rebuilt) == 1
        assert rebuilt[0]["complete"] is False
        assert rebuilt[0]["outcomes"][0]["unsafe"] == "money moved where none moves"
        assert rebuilt[0]["stop_kind"] == "qualifying"

    def test_a_killed_run_with_an_oversized_case_is_not_replaceable(self, tmp_path, clean):
        c = make(tmp_path)
        with c.admit("gpt-6-luna", "r1-gpt-6-luna") as admitted:
            admitted.record_case("duplicate_fr", outcome("duplicate_fr"), "not_sent_oversized")

        with (
            pytest.raises(CampaignRefused, match="not completing"),
            c.admit("gpt-6-luna", "r1-gpt-6-luna"),
        ):
            pass

        rebuilt = next(r for r in c.results().values() if r.get("rebuilt"))
        assert rebuilt["stop_kind"] == "other"
        assert rebuilt["stop_reason"].startswith("not sent:")

    def test_unreadable_evidence_is_lost_and_never_replaced(self, tmp_path, clean):
        c = make(tmp_path)
        with c.admit("gpt-6-luna", "r1-gpt-6-luna") as admitted:
            admitted.record_case("duplicate_fr", outcome("duplicate_fr"), "decided")
        for sidecar in (c.results_dir / campaign.CASES_DIR).glob("*"):
            sidecar.write_text("not json", encoding="utf-8")

        with (
            pytest.raises(CampaignRefused, match="not completing"),
            c.admit("gpt-6-luna", "r1-gpt-6-luna"),
        ):
            pass

        assert any(line.get("type") == "lost" for line in c.log_lines())
        assert "gpt-6-luna" in c.state().not_completing


class TestTheDeclaredEvidence:
    def test_another_evidence_is_refused(self, tmp_path, clean, monkeypatch):
        c = make(tmp_path)
        monkeypatch.setattr(campaign, "current_evidence", lambda: {"cases_sha256": "another"})

        with (
            pytest.raises(CampaignRefused, match="evidence"),
            c.admit("gpt-6-luna", "r1-gpt-6-luna"),
        ):
            pass

    def test_a_checkout_off_the_manifest_s_commit_is_refused(self, tmp_path, clean):
        clean["commit"] = "later"
        c = make(tmp_path)

        with (
            pytest.raises(CampaignRefused, match="manifest revision"),
            c.admit("gpt-6-luna", "r1-gpt-6-luna"),
        ):
            pass

        assert c.log_lines() == []


class TestRecoveryEvidence:
    def test_the_rebuilt_result_keeps_the_run_s_own_configuration(
        self, tmp_path, clean, monkeypatch
    ):
        c = make(tmp_path)
        with c.admit("gpt-6-luna", "r1-gpt-6-luna") as admitted:
            admitted.record_case(
                "duplicate_fr",
                outcome("duplicate_fr"),
                "decided",
                {"sends": 1, "cost": 0.001, "unknown_cost_attempts": 0, "usage": None},
            )
        at_start = fingerprint(configuration("gpt-6-luna"))
        # The configuration changes afterwards; the rebuilt result must not follow.
        monkeypatch.setattr(campaign, "fingerprint", lambda config: "changed")
        monkeypatch.setattr(campaign, "configuration", lambda name: configuration(name))

        c._recover()

        rebuilt = next(r for r in c.results().values() if r.get("rebuilt"))
        assert rebuilt["configuration_fingerprint"] == at_start
        assert rebuilt["sends"] == 1
        assert rebuilt["cost"]["settled_amount"] == 0.001
        assert rebuilt["cost"]["complete"] is False

    @pytest.mark.parametrize(
        "row",
        [
            {"case_id": "not_a_case", "outcome": {}, "status": "decided", "accounting": {}},
            {"case_id": "duplicate_fr", "outcome": {}, "status": "weird", "accounting": {}},
            {"case_id": "duplicate_fr", "status": "decided"},
            {"case_id": "duplicate_fr", "outcome": {}, "status": "decided", "accounting": {}},
        ],
    )
    def test_a_malformed_record_is_lost_not_a_crash(self, row, tmp_path, clean):
        c = make(tmp_path)
        with c.admit("gpt-6-luna", "r1-gpt-6-luna"):
            pass
        path = c.cases_path("r1-gpt-6-luna", 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")

        c._recover()

        assert any(line.get("type") == "lost" for line in c.log_lines())


class TestInFlightAccounting:
    def test_the_rebuilt_cost_comes_from_the_run_s_ledger_tickets(
        self, tmp_path, clean, monkeypatch
    ):
        from evals import state
        from evals.ledger import PrepaidLedger

        monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
        monkeypatch.setenv("OPENAI_ORG_ID", "org-test")
        monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_test")
        book = PrepaidLedger("openai-org-test", "USD")
        book.declare(5.0)
        c = make(tmp_path)
        with c.admit("gpt-6-luna", "r1-gpt-6-luna", run_id="RUN1") as admitted:
            book.reserve("RUN1/gpt-6-luna/duplicate_fr/1", 0.01)
            book.settle("RUN1/gpt-6-luna/duplicate_fr/1", 0.001)
            admitted.record_case("duplicate_fr", outcome("duplicate_fr"), "decided", {})
            # A request sent after the last recorded case, then the process dies.
            book.reserve("RUN1/gpt-6-luna/duplicate_en/1", 0.02)
        book.reserve("OTHER/gpt-6-luna/duplicate_fr/1", 0.5)
        # The account's variables changing after the run changes nothing.
        monkeypatch.setenv("OPENAI_ORG_ID", "org-another")

        c._recover()

        rebuilt = next(r for r in c.results().values() if r.get("rebuilt"))
        assert rebuilt["cost"]["from_ledger"] is True
        assert rebuilt["cost"]["settled_amount"] == pytest.approx(0.001)
        assert rebuilt["cost"]["unknown_cost_attempts"] == 1
        assert rebuilt["cost"]["unknown_cost_reserved"] == pytest.approx(0.02)
        assert rebuilt["sends"] == 2

    def test_an_unreadable_ledger_never_passes_for_complete_accounting(
        self, tmp_path, clean, monkeypatch
    ):
        from evals import state

        monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
        monkeypatch.setenv("OPENAI_ORG_ID", "org-test")
        monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_test")
        c = make(tmp_path)
        with c.admit("gpt-6-luna", "r1-gpt-6-luna", run_id="RUN1") as admitted:
            admitted.record_case(
                "duplicate_fr", outcome("duplicate_fr"), "decided", {"sends": 1, "cost": 0.001}
            )

        c._recover()

        rebuilt = next(r for r in c.results().values() if r.get("rebuilt"))
        assert rebuilt["cost"]["ledger_unavailable"] is True
        assert rebuilt["cost"]["from_ledger"] is False
        assert rebuilt["cost"]["complete"] is False


class TestOneHistory:
    def test_another_worktree_s_folder_is_refused(self, tmp_path, clean):
        first = make(tmp_path)
        with first.admit("gpt-6-luna", "r1-gpt-6-luna"):
            pass
        other = tmp_path / "other-worktree"
        other.mkdir()
        second = Campaign(tmp_path / "manifest.json", results_dir=other)

        with (
            pytest.raises(CampaignRefused, match="bound"),
            second.admit("gpt-6-luna", "r1-gpt-6-luna"),
        ):
            pass

        assert not (other / campaign.LOG_NAME).exists()


class TestGitState:
    def test_untracked_results_keep_the_tree_clean(self):
        assert campaign.clean_from_porcelain(["?? evals/results/x.json"]) is True

    @pytest.mark.parametrize(
        "line",
        [" M app/decision/hosted.py", "M  evals/runner.py", "?? notes.txt", "A  new.py"],
    )
    def test_anything_else_makes_it_dirty(self, line):
        assert campaign.clean_from_porcelain([line]) is False

    def test_git_is_asked_about_the_code_s_own_checkout(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        state = campaign.git_state()

        assert state["commit"] != "unknown"
