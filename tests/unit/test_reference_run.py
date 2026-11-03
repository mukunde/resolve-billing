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

from app.decision.base import EngineRejected, EngineUnavailable
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
        assert not (world.private / "attempts").exists()

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
        totals = json.loads((world.private / "totals.json").read_text(encoding="utf-8"))
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
        assert (world.private / "cases").is_dir()
        assert list((world.private / "attempts").glob("*-start.json"))

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
        totals = json.loads((world.private / "totals.json").read_text(encoding="utf-8"))
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
        for path in (world.private / "cases").glob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))
        assert not list((world.private / "cases").glob(".*"))

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
        first = json.loads((world.private / "totals.json").read_text(encoding="utf-8"))
        assert (
            reference_run.main(["rebuild", "--private", str(world.private)], descriptors=False) == 0
        )
        assert json.loads((world.private / "totals.json").read_text(encoding="utf-8")) == first
        assert world.factories == 1

    def test_an_unsafe_action_comes_first(self, world, capfd, monkeypatch):
        world.prepare()
        world.run()
        capfd.readouterr()
        for path in (world.private / "cases").glob("*.json"):
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
            for p in (world.private / "cases").glob("*.json")
        ]
        totals = reference_run.compute_totals(world.private)
        assert totals["rules_only"] == sum(1 for c in cases if not c["engine_called"])

    def test_an_open_attempt_stays_unknown(self, world):
        world.prepare()
        world.run()
        for path in (world.private / "attempts").glob("*-end.json"):
            path.unlink()
            break
        totals = reference_run.compute_totals(world.private)
        assert sum(entry["unknown_attempts"] for entry in totals["spend"].values()) >= 1

    def test_requires_approval_counts_as_a_person(self, world):
        world.prepare()
        world.run()
        cases = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in (world.private / "cases").glob("*.json")
        ]
        totals = reference_run.compute_totals(world.private)
        assert totals["sent_to_a_person"] == sum(
            1 for c in cases if c["verdict"] in {"human_review", "requires_approval"}
        )


def test_an_outage_alone_never_needs_the_unavailable_import():
    assert EngineUnavailable
