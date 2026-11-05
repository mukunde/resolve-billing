"""The holdout's reference run (specs/011-reference-run).

On stand-in sets only, built from development cases with a sentinel in every
field: no test reads the holdout, and none makes a paid call.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.decision.base import EngineRejected
from app.decision.bounded import BoundedEngine
from app.decision.failover import PRIMARY_BOUNDS, SECONDARY_BOUNDS, FailoverEngine
from app.decision.hosted import configuration
from evals import reference_run, state
from tests.unit.test_bounded_engine import FakeEngine, answer, outage
from tests.unit.test_service_run import Book

SENTINEL = "SENTINEL-holdout-text"
HAIKU = configuration("claude-haiku-5-5")
REPO = Path(__file__).resolve().parents[2]


def stand_in(folder: Path, count=4) -> tuple[Path, Path]:
    cases = json.loads((REPO / "evals" / "cases.json").read_text(encoding="utf-8"))
    chosen = []
    for case in cases[:count]:
        chosen.append(
            {
                **case,
                "id": f"{SENTINEL}-{case['id']}",
                "why": f"{SENTINEL} {case['why']}",
                "message": f"{case['message']} {SENTINEL}",
            }
        )
    path = folder / "cases.json"
    path.write_text(json.dumps(chosen, ensure_ascii=False), encoding="utf-8")
    extra = folder / "fixtures"
    extra.mkdir()
    return path, extra


def haiku(*script):
    engine = FakeEngine(*script)
    engine.config = HAIKU
    engine.name = HAIKU.name
    engine.version = HAIKU.prompt_version
    return engine


class World:
    """Everything outside the tool, faked: accounts, ledgers, first calls,
    the repository's state, the engines."""

    def __init__(self, monkeypatch, tmp_path):
        self.tmp = tmp_path
        self.books = {}
        for name, value in (
            ("OPENAI_API_KEY", "sk-test"),
            ("OPENAI_ORG_ID", "org-test"),
            ("OPENAI_PROJECT_ID", "proj_test"),
            ("ANTHROPIC_API_KEY", "sk-ant-test"),
            ("RESOLVE_ANTHROPIC_ORG_ID", "anth-org"),
        ):
            monkeypatch.setenv(name, value)
        for name in list(os.environ):
            if name.startswith("OTEL_"):
                monkeypatch.delenv(name)
        # Another test module may have installed a provider in this process;
        # the refusal is tested on its own.
        monkeypatch.setattr(reference_run, "tracer_installed", lambda: False)
        monkeypatch.setattr(reference_run, "STATE_DIR", tmp_path / "home" / "reference")
        monkeypatch.setattr(state, "STATE_DIR", tmp_path / "home")
        monkeypatch.setattr("evals.state.AccountRun.wait", lambda self: 0.0)
        monkeypatch.setattr(
            "evals.first_calls.FirstCalls.admit_full_run", lambda self, config: None
        )
        monkeypatch.setattr(
            reference_run,
            "repository_state",
            lambda: {"commit": "c0ffee", "clean_tree": True, "uncertainty_fix": True},
        )
        monkeypatch.setattr("evals.ledger.PrepaidLedger", self._ledger)
        self.private = tmp_path / "private"
        self.private.mkdir()
        self.cases, self.fixtures = stand_in(tmp_path)
        self.luna_script = []
        self.haiku_script = []
        self.factories = 0
        self.short = False

    def _ledger(self, account, currency):
        if self.short:
            return Book(total=0.0)
        return self.books.setdefault(account, Book())

    def legs(self):
        return [FakeEngine(), haiku()]

    def factory(self, recorder):
        self.factories += 1
        return FailoverEngine(
            BoundedEngine(FakeEngine(*self.luna_script), recorder, bounds=PRIMARY_BOUNDS),
            BoundedEngine(haiku(*self.haiku_script), recorder, bounds=SECONDARY_BOUNDS),
            name="gpt-6-luna+claude-haiku-5-5",
            version="v",
        )

    def prepare(self, **extra):
        argv = [
            "prepare",
            "--cases",
            str(self.cases),
            "--fixtures",
            str(self.fixtures),
            "--private",
            str(self.private),
        ]
        return reference_run.main(argv, descriptors=False, legs=self.legs(), **extra)

    def run_dir(self):
        return reference_run.the_run(self.private)

    def freeze(self):
        return reference_run.freeze_hash(
            json.loads((self.private / "freeze.json").read_text(encoding="utf-8"))
        )

    def run(self, typed="RUN ONCE", freeze=None, private=None):
        argv = [
            "run",
            "--freeze",
            freeze or self.freeze(),
            "--private",
            str(private or self.private),
        ]
        return reference_run.main(
            argv,
            descriptors=False,
            ask=lambda prompt: typed,
            engine_factory=self.factory,
        )


@pytest.fixture
def world(monkeypatch, tmp_path):
    return World(monkeypatch, tmp_path)


def no_sentinel(capfd):
    out, err = capfd.readouterr()
    assert SENTINEL not in out
    assert SENTINEL not in err
    return out


class TestTheLockdown:
    def test_descriptors_and_loggers_go_to_the_log(self, tmp_path):
        script = f"""
import logging, os, sys
from pathlib import Path
from evals.reference_run import Lockdown
named = logging.getLogger("vendor.sdk")
named.addHandler(logging.StreamHandler(sys.__stderr__))
named.propagate = False
with Lockdown(Path(r"{tmp_path}")) as locked:
    print("{SENTINEL} print")
    sys.stderr.write("{SENTINEL} stderr")
    os.write(1, b"{SENTINEL} fd1")
    os.write(2, b"{SENTINEL} fd2")
    logging.getLogger("x").warning("{SENTINEL} log")
    named.warning("{SENTINEL} named")
    locked.say("ALLOWED")
"""
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO
        )
        assert done.stdout.strip() == "ALLOWED"
        assert done.stderr == ""
        log = (tmp_path / "log.txt").read_text(encoding="utf-8")
        for where in ("print", "stderr", "fd1", "fd2", "log", "named"):
            assert f"{SENTINEL} {where}" in log

    def test_an_exporter_refuses(self, world, monkeypatch, capfd):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
        assert world.prepare() == 2
        assert "trace exporter" in capfd.readouterr().out

    def test_an_installed_provider_refuses(self, monkeypatch):
        monkeypatch.setattr(reference_run, "tracer_installed", lambda: True)
        with pytest.raises(reference_run.Refused, match="tracer provider"):
            reference_run.refuse_tracing()

    def test_app_main_is_never_imported(self, world):
        sys.modules.pop("app.main", None)
        world.prepare()
        assert "app.main" not in sys.modules

    @pytest.mark.parametrize("where", ["missing", "repository", "file"])
    def test_a_bad_private_folder_refuses(self, where, tmp_path):
        target = {
            "missing": tmp_path / "nowhere",
            "repository": REPO / "evals",
            "file": REPO / "README.md",
        }[where]
        with pytest.raises(reference_run.Refused):
            reference_run.private_folder(str(target))

    def test_a_link_into_the_repository_refuses(self, tmp_path):
        link = tmp_path / "link"
        try:
            link.symlink_to(REPO / "evals", target_is_directory=True)
        except OSError:
            pytest.skip("links are not allowed here")
        with pytest.raises(reference_run.Refused):
            reference_run.private_folder(str(link))


class TestPrepare:
    def test_it_writes_the_freeze_record_and_sends_nothing(self, world, capfd):
        assert world.prepare() == 0
        record = json.loads((world.private / "freeze.json").read_text(encoding="utf-8"))
        for field in (
            "commit",
            "clean_tree",
            "uncertainty_fix",
            "configurations",
            "composite",
            "prompt_version",
            "schema_sha256",
            "masking_version",
            "case_sha256",
            "files",
            "case_count",
            "evaluation_date",
            "scoring_sha256",
            "threshold",
            "budget",
        ):
            assert field in record
        assert any(name.startswith("base/") for name in record["files"])
        assert record["case_count"] == 4
        held = json.loads(reference_run.state_path().read_text(encoding="utf-8"))
        assert held["state"] == "prepared"
        out = no_sentinel(capfd)
        assert "nothing was sent" in out
        assert not list(world.private.glob("reference-*"))

    def test_a_failing_check_says_nothing_case_level(self, world, capfd):
        world.cases.write_text(json.dumps([{"id": SENTINEL}]), encoding="utf-8")
        assert world.prepare() == 2
        assert "the check failed" in no_sentinel(capfd)

    def test_without_the_fix_it_refuses(self, world, monkeypatch):
        monkeypatch.setattr(
            reference_run,
            "repository_state",
            lambda: {"commit": "c", "clean_tree": True, "uncertainty_fix": False},
        )
        assert world.prepare() == 2

    def test_a_short_envelope_refuses(self, world):
        world.short = True
        assert world.prepare() == 2

    def test_other_inputs_are_refused_once_prepared(self, world):
        assert world.prepare() == 0
        cases = json.loads(world.cases.read_text(encoding="utf-8"))
        world.cases.write_text(json.dumps(cases[:3]), encoding="utf-8")
        assert world.prepare() == 2

    def test_the_snapshot_is_what_runs(self, world, capfd):
        assert world.prepare() == 0
        world.cases.write_text("[]", encoding="utf-8")
        (world.fixtures / "customers.json").write_text("[]", encoding="utf-8")
        assert world.run() == 0
        totals = json.loads((world.run_dir() / "totals.json").read_text(encoding="utf-8"))
        assert totals["cases_run"] == 4

    def test_the_services_fixtures_are_unchanged(self):
        from app.providers.fake.adapter import FakeBillingProvider
        from app.providers.fake.fixtures import load_all

        assert len(FakeBillingProvider()._customers) == len(load_all().customers)


class TestRun:
    def test_a_complete_run(self, world, capfd):
        world.prepare()
        capfd.readouterr()
        assert world.run() == 0
        out = no_sentinel(capfd)
        assert "fully correct:" in out
        held = json.loads(reference_run.state_path().read_text(encoding="utf-8"))
        assert held["state"] == "consumed"
        assert (world.run_dir() / "cases").is_dir()
        assert list((world.run_dir() / "attempts").glob("*-start.json"))

    def test_anything_but_the_words_stops_still_prepared(self, world):
        world.prepare()
        assert world.run(typed="yes") == 2
        held = json.loads(reference_run.state_path().read_text(encoding="utf-8"))
        assert held["state"] == "prepared"
        assert world.factories == 0

    def test_a_wrong_hash_refuses(self, world):
        world.prepare()
        assert world.run(freeze="0" * 64) == 2
        assert world.factories == 0

    def test_a_second_run_is_refused_whatever_it_names(self, world, tmp_path):
        world.prepare()
        assert world.run() == 0
        other = tmp_path / "other"
        other.mkdir()
        assert world.run(private=other) == 2
        assert world.run() == 2
        assert world.prepare() == 2
        assert world.factories == 1

    def test_an_unreadable_state_counts_as_consumed(self, world):
        world.prepare()
        reference_run.state_path().write_text("{not json", encoding="utf-8")
        assert world.run() == 2
        assert world.factories == 0

    def test_a_short_envelope_at_start_leaves_it_prepared(self, world):
        world.prepare()
        world.short = True
        assert world.run() == 2
        held = json.loads(reference_run.state_path().read_text(encoding="utf-8"))
        assert held["state"] == "prepared"

    def test_consumed_before_the_first_send(self, world):
        world.prepare()
        seen = []

        def factory(recorder):
            seen.append(json.loads(reference_run.state_path().read_text())["state"])
            return world.factory(recorder)

        reference_run.main(
            ["run", "--freeze", world.freeze(), "--private", str(world.private)],
            descriptors=False,
            ask=lambda prompt: "RUN ONCE",
            engine_factory=factory,
        )
        assert seen == ["consumed"]

    def test_an_outage_is_counted_and_the_run_goes_on(self, world):
        world.prepare()
        world.luna_script = [outage("transport")] * 10
        world.haiku_script = [answer(model="claude-haiku-5-5")] * 10
        assert world.run() == 0
        totals = json.loads((world.run_dir() / "totals.json").read_text(encoding="utf-8"))
        assert totals["complete"] is True
        assert totals["luna_failed_haiku_answered"] >= 1
        assert totals["answered_by"].get("claude-haiku-5-5", 0) >= 1

    def test_a_refusal_ends_it_incomplete(self, world, capfd):
        world.prepare()
        world.luna_script = [EngineRejected("401")]
        assert world.run() == 3
        out = no_sentinel(capfd)
        assert "INCOMPLETE" in out
        assert "fully correct:" not in out

    def test_a_defect_prints_its_type_only(self, world, capfd):
        world.prepare()
        world.luna_script = [RuntimeError(SENTINEL)]
        assert world.run() == 3
        no_sentinel(capfd)
        assert SENTINEL in (world.private / "log.txt").read_text(encoding="utf-8")

    def test_records_are_whole_files(self, world):
        world.prepare()
        world.run()
        for path in (world.run_dir() / "cases").glob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))
        assert not list((world.run_dir() / "cases").glob(".*"))

    def test_nothing_is_written_in_the_repository(self, world):
        before = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=REPO
        ).stdout
        world.prepare()
        world.run()
        after = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=REPO
        ).stdout
        assert before == after


class TestTotals:
    def test_rebuild_gives_the_same_totals_and_calls_nothing(self, world):
        world.prepare()
        world.run()
        first = json.loads((world.run_dir() / "totals.json").read_text(encoding="utf-8"))
        assert (
            reference_run.main(["rebuild", "--private", str(world.private)], descriptors=False) == 0
        )
        assert json.loads((world.run_dir() / "totals.json").read_text(encoding="utf-8")) == first
        assert world.factories == 1

    def test_an_unsafe_action_comes_first(self, world, capfd, monkeypatch):
        world.prepare()
        world.run()
        capfd.readouterr()
        for path in (world.run_dir() / "cases").glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            record["outcome"]["unsafe"] = "planted"
            path.write_text(json.dumps(record), encoding="utf-8")
            break
        reference_run.main(["rebuild", "--private", str(world.private)], descriptors=False)
        out = capfd.readouterr().out
        assert out.startswith("UNSAFE ACTIONS")

    def test_a_failure_before_the_send_is_not_rules_only(self, world):
        world.prepare()
        world.luna_script = []
        world.run()
        cases = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in (world.run_dir() / "cases").glob("*.json")
        ]
        totals = reference_run.compute_totals(world.private)
        assert totals["rules_only"] == sum(1 for c in cases if not c["engine_called"])

    def test_an_open_attempt_stays_unknown(self, world):
        world.prepare()
        world.run()
        for path in (world.run_dir() / "attempts").glob("*-end.json"):
            path.unlink()
            break
        totals = reference_run.compute_totals(world.private)
        assert sum(entry["unknown_attempts"] for entry in totals["spend"].values()) >= 1

    def test_requires_approval_counts_as_a_person(self, world):
        world.prepare()
        world.run()
        cases = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in (world.run_dir() / "cases").glob("*.json")
        ]
        totals = reference_run.compute_totals(world.private)
        assert totals["sent_to_a_person"] == sum(
            1 for c in cases if c["verdict"] in {"human_review", "requires_approval"}
        )


class TestReviewR301:
    def test_an_argument_error_names_no_value(self, capfd):
        with pytest.raises(SystemExit):
            reference_run.main(["prepare", "--threshold", SENTINEL], descriptors=False)
        out = no_sentinel(capfd)
        assert "invalid arguments" in out

    def test_a_linked_log_refuses(self, world, capfd):
        target = REPO / "evals" / "not-a-log.txt"
        try:
            (world.private / "log.txt").symlink_to(target)
        except OSError:
            pytest.skip("links are not allowed here")
        assert world.prepare() == 2
        assert not target.exists()

    def test_a_changed_account_refuses_the_run(self, world, monkeypatch):
        world.prepare()
        monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_other")
        assert world.run() == 2
        held = json.loads(reference_run.state_path().read_text(encoding="utf-8"))
        assert held["state"] == "prepared"

    def test_a_competing_prepare_leaves_the_snapshot_alone(self, world):
        world.prepare()
        before = reference_run.inputs_hashes(world.private / "snapshot")
        cases = json.loads(world.cases.read_text(encoding="utf-8"))
        world.cases.write_text(json.dumps(cases[:2]), encoding="utf-8")
        assert world.prepare() == 2
        assert reference_run.inputs_hashes(world.private / "snapshot") == before

    def test_the_run_reads_its_own_copy(self, world):
        world.prepare()
        snapshot = world.private / "snapshot" / "cases.json"

        def factory(recorder):
            snapshot.write_text("[]", encoding="utf-8")
            return world.factory(recorder)

        reference_run.main(
            ["run", "--freeze", world.freeze(), "--private", str(world.private)],
            descriptors=False,
            ask=lambda prompt: "RUN ONCE",
            engine_factory=factory,
        )
        totals = json.loads((world.run_dir() / "totals.json").read_text(encoding="utf-8"))
        assert totals["cases_run"] == 4

    def test_a_record_of_another_run_refuses(self, world):
        world.prepare()
        world.run()
        stray = world.run_dir() / "cases" / "99999.json"
        stray.write_text(json.dumps({"run_id": "other", "number": 99999}), encoding="utf-8")
        with pytest.raises(reference_run.Refused):
            reference_run.compute_totals(world.private)

    def test_a_duplicated_case_refuses(self, world):
        world.prepare()
        world.run()
        first = sorted((world.run_dir() / "cases").glob("*.json"))[0]
        (world.run_dir() / "cases" / "00099.json").write_text(
            first.read_text(encoding="utf-8"), encoding="utf-8"
        )
        with pytest.raises(reference_run.Refused):
            reference_run.compute_totals(world.private)

    def test_both_failed_only_when_a_person_got_it(self, world):
        world.prepare()
        world.luna_script = [outage("transport")] * 10
        world.haiku_script = [outage("transport")] * 10
        world.run()
        totals = reference_run.compute_totals(world.private)
        assert totals["both_failed"] <= totals["of_which_outage"]
        assert totals["both_failed"] >= 1


class TestReviewR302:
    def test_a_declined_run_leaves_nothing_behind(self, world):
        world.prepare()
        assert world.run(typed="no") == 2
        assert not list(world.private.glob("reference-*"))
        assert not list(world.private.glob(".pending-*"))
        assert world.run() == 0
        assert reference_run.compute_totals(world.private)["complete"] is True

    def test_a_busy_account_leaves_nothing_behind(self, world, monkeypatch):
        from evals.state import AccountBusy

        world.prepare()

        def busy(self):
            raise AccountBusy("busy")

        monkeypatch.setattr("evals.state.AccountRun.__enter__", busy)
        assert world.run() == 2
        assert not list(world.private.glob("reference-*"))

    def test_the_probe_follows_no_link_and_keeps_existing_files(self, tmp_path):
        kept = tmp_path / ".write-probe"
        kept.write_text("keep", encoding="utf-8")
        reference_run.private_folder(str(tmp_path))
        assert kept.read_text(encoding="utf-8") == "keep"

    def test_a_reservation_made_before_a_failure_stays_open(self, tmp_path):
        class HalfDone:
            cost = 0.0
            open_reservations = {}

            def __init__(self):
                self._reserved = {}

            def started(self, attempt):
                self._reserved[attempt.attempt_id] = ("t", 1, 1.0)
                raise OSError("the pace file")

        routing = reference_run.RoutingRecorder(
            {"openai": HalfDone()}, tmp_path / "attempts", tmp_path, "reference-x"
        )
        from app.decision.base import AttemptStart

        with pytest.raises(OSError):
            routing.started(AttemptStart("a1", "i1", "d1", 1, "openai", "m", "t", 1))
        end = json.loads((tmp_path / "attempts" / "a1-end.json").read_text(encoding="utf-8"))
        assert end["ledger"] == "open"
        assert end["settled_usd"] is None

    def test_a_duplicated_attempt_refuses(self, world):
        world.prepare()
        world.run()
        end = sorted((world.run_dir() / "attempts").glob("*-end.json"))[0]
        copy = end.with_name("zz-copy-end.json")
        copy.write_text(end.read_text(encoding="utf-8"), encoding="utf-8")
        with pytest.raises(reference_run.Refused):
            reference_run.compute_totals(world.private)


class TestReviewR303:
    @pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"])
    def test_a_missing_key_refuses_before_consumption(self, world, monkeypatch, key):
        world.prepare()
        monkeypatch.delenv(key)
        assert world.run() == 2
        held = json.loads(reference_run.state_path().read_text(encoding="utf-8"))
        assert held["state"] == "prepared"
        assert world.factories == 0


class TestTheRecorderBoundaries:
    def recorder(self, tmp_path, ledger_recorder):
        return reference_run.RoutingRecorder(
            {"openai": ledger_recorder}, tmp_path / "attempts", tmp_path, "reference-x"
        )

    def start(self):
        from app.decision.base import AttemptStart

        return AttemptStart("a1", "i1", "d1", 1, "openai", "gpt-6-luna", "t", 100)

    def test_a_refused_reservation_leaves_a_closed_record(self, tmp_path):
        from evals.ledger import LedgerRefused

        class Refusing:
            cost = 0.0
            open_reservations = {}
            _reserved = {}

            def started(self, attempt):
                raise LedgerRefused("beyond")

        routing = self.recorder(tmp_path, Refusing())
        with pytest.raises(LedgerRefused):
            routing.started(self.start())
        end = json.loads((tmp_path / "attempts" / "a1-end.json").read_text(encoding="utf-8"))
        assert end["outcome"] == "not_reserved"

    def test_a_stop_at_settlement_still_writes_the_end(self, tmp_path):
        from app.decision.base import AttemptEnd, AttemptOutcome
        from evals.limits import RunStopped

        class Stopping:
            cost = 0.0
            open_reservations = {"reference-x/a1": 1.0}

            def started(self, attempt):
                pass

            def finished(self, attempt):
                raise RunStopped("usage above the reservation")

        routing = self.recorder(tmp_path, Stopping())
        routing.started(self.start())
        with pytest.raises(RunStopped):
            routing.finished(AttemptEnd("a1", AttemptOutcome.ANSWERED, "t", 1))
        end = json.loads((tmp_path / "attempts" / "a1-end.json").read_text(encoding="utf-8"))
        assert end["ledger"] == "open"
        assert end["settled_usd"] is None

    def test_a_crash_before_the_end_reads_as_unknown(self, world):
        world.prepare()
        world.run()
        for path in (world.run_dir() / "attempts").glob("*-end.json"):
            path.unlink()
        totals = reference_run.compute_totals(world.private)
        assert totals["unfinished_attempts"] >= 1


class TestTheExport:
    """Totals only, to a file outside the private folder (FR-011a)."""

    def exported(self, world, tmp_path, command="run"):
        target = tmp_path / "out" / "totals.json"
        target.parent.mkdir()
        world.prepare()
        if command == "run":
            code = reference_run.main(
                [
                    "run",
                    "--freeze",
                    world.freeze(),
                    "--private",
                    str(world.private),
                    "--export",
                    str(target),
                ],
                descriptors=False,
                ask=lambda prompt: "RUN ONCE",
                engine_factory=world.factory,
            )
        else:
            world.run()
            code = reference_run.main(
                ["rebuild", "--private", str(world.private), "--export", str(target)],
                descriptors=False,
            )
        return code, target

    def test_only_the_allowlisted_totals_leave(self, world, tmp_path, capfd):
        code, target = self.exported(world, tmp_path)
        assert code == 0
        text = target.read_text(encoding="utf-8")
        assert SENTINEL not in text
        payload = json.loads(text)
        assert set(payload) == {
            "identity",
            "freeze_hash",
            "commit",
            "run_id",
            "exported_at",
            "complete",
            "stop",
            *reference_run._COUNTS,
            "answered_by",
            "spend",
            "threshold",
        }
        assert payload["freeze_hash"] == world.freeze()
        assert payload["run_id"] == world.run_dir().name
        assert "totals exported" in no_sentinel(capfd)

    def test_rebuild_exports_the_same_totals(self, world, tmp_path):
        code, target = self.exported(world, tmp_path, command="rebuild")
        assert code == 0
        payload = json.loads(target.read_text(encoding="utf-8"))
        totals = reference_run.compute_totals(world.private)
        assert all(payload[key] == totals[key] for key in reference_run._COUNTS)

    @pytest.mark.parametrize(
        "planted",
        [
            {"stop": f"{SENTINEL} stopped"},
            {"answered_by": {f"{SENTINEL} model": 1}},
            {"spend": {SENTINEL: {"settled_usd": 0.0, "unknown_attempts": 0}}},
            {"fully_correct": SENTINEL},
            {"threshold": SENTINEL},
            {"stop": "internal error (private_case_id)"},
            {"answered_by": {"private_case_id": 1}},
            {"threshold": float("nan")},
            {"spend": {"openai": {"settled_usd": float("inf"), "unknown_attempts": 0}}},
        ],
    )
    def test_anything_else_refuses(self, world, planted):
        world.prepare()
        world.run()
        totals = {**reference_run.compute_totals(world.private), **planted}
        with pytest.raises(reference_run.Refused):
            reference_run.export_payload(totals, world.private)

    def test_an_extra_key_never_leaves(self, world):
        world.prepare()
        world.run()
        totals = {**reference_run.compute_totals(world.private), "case_ids": [SENTINEL]}
        assert "case_ids" not in reference_run.export_payload(totals, world.private)

    def test_inside_the_private_folder_refuses(self, world):
        world.prepare()
        world.run()
        totals = reference_run.compute_totals(world.private)
        with pytest.raises(reference_run.Refused):
            reference_run.export(totals, world.private, world.private / "t.json", print)

    def test_not_json_refuses(self, world, tmp_path):
        world.prepare()
        world.run()
        totals = reference_run.compute_totals(world.private)
        with pytest.raises(reference_run.Refused):
            reference_run.export(totals, world.private, tmp_path / "t.txt", print)

    def test_through_a_link_refuses(self, world, tmp_path):
        world.prepare()
        world.run()
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError:
            pytest.skip("links are not allowed here")
        totals = reference_run.compute_totals(world.private)
        with pytest.raises(reference_run.Refused):
            reference_run.export(totals, world.private, link / "t.json", print)


class TestTheExportBinding:
    def test_an_edited_freeze_record_refuses(self, world):
        world.prepare()
        world.run()
        path = world.private / "freeze.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["threshold"] = 99
        path.write_text(json.dumps(record), encoding="utf-8")
        totals = reference_run.compute_totals(world.private)
        with pytest.raises(reference_run.Refused):
            reference_run.export_payload(totals, world.private)

    def test_a_defect_exports_a_category_only(self, world, tmp_path):
        world.prepare()
        world.luna_script = [RuntimeError(SENTINEL)]
        target = tmp_path / "out.json"
        reference_run.main(
            [
                "run",
                "--freeze",
                world.freeze(),
                "--private",
                str(world.private),
                "--export",
                str(target),
            ],
            descriptors=False,
            ask=lambda prompt: "RUN ONCE",
            engine_factory=world.factory,
        )
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["stop"] == "internal error"
        assert SENTINEL not in target.read_text(encoding="utf-8")
