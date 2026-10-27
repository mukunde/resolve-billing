"""The evaluation registry and its guards (specs/004-second-engines, T030):
the engines on free tiers run in the evaluation runner, on the development
cases, and nowhere else."""

import hashlib
import os
import shutil

import pytest

from app.api import deps
from evals import engines, runner

HOSTED = (
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "mistral-medium-3-5",
    "mistral-medium-3-5-reasoning",
    # specs/007-openai-qwen-engines
    "gpt-6-luna",
    "gpt-6.1-sol",
    "qwen3.5-397b-a17b",
    # specs/008-deployment-model-choice
    "claude-haiku-5-5",
    "qwen3.6-35b-a3b",
    "deepseek-v4.1-flash",
)


class TestTheRefusedConfigurations:
    @pytest.mark.parametrize("name", ["mistral-large-4", "zai-glm-5-3"])
    def test_the_runner_and_the_service_refuse_them(self, name, monkeypatch):
        import asyncio

        assert name not in engines.evaluation_engines()
        with pytest.raises(SystemExit, match="unknown engine"):
            asyncio.run(runner.run(name))

        monkeypatch.setenv("DECISION_ENGINE", name)
        deps.get_engine.cache_clear()
        with pytest.raises(RuntimeError, match="unknown decision engine"):
            deps.get_engine()
        deps.get_engine.cache_clear()


class TestTheRegistry:
    def test_it_holds_the_services_engines_and_the_hosted_ones(self):
        assert set(engines.evaluation_engines()) == set(deps.ENGINES) | set(HOSTED)

    @pytest.mark.parametrize("name", HOSTED)
    def test_the_service_refuses_each(self, name, monkeypatch):
        monkeypatch.setenv("DECISION_ENGINE", name)
        deps.get_engine.cache_clear()

        with pytest.raises(RuntimeError, match="unknown decision engine"):
            deps.get_engine()

        deps.get_engine.cache_clear()

    @pytest.mark.parametrize(
        ("name", "key"),
        [
            ("gemini-3.8-flash", "GEMINI_API_KEY"),
            ("mistral-medium-3-5", "MISTRAL_API_KEY"),
            ("mistral-medium-3-5-reasoning", "MISTRAL_API_KEY"),
        ],
    )
    def test_a_missing_key_is_named_and_nothing_runs(self, name, key, monkeypatch):
        monkeypatch.delenv(key, raising=False)

        assert key in runner.missing_credential(name)

    def test_with_its_key_it_may_run(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "set-by-the-test")

        assert runner.missing_credential("gemini-3.8-flash") is None


class TestTheDevelopmentSetOnly:
    def test_the_recorded_hash_is_the_committed_files(self):
        data = engines.DEVELOPMENT_SET.read_bytes().replace(b"\r\n", b"\n")

        assert hashlib.sha256(data).hexdigest() == engines.DEVELOPMENT_SET_SHA256

    def test_the_development_set_is_read_and_parsed_from_the_same_bytes(self, monkeypatch):
        monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)

        cases = engines.development_cases(engines.DEVELOPMENT_SET)

        assert len(cases) == 34

    def test_another_path_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
        copy = tmp_path / "cases.json"
        shutil.copy(engines.DEVELOPMENT_SET, copy)

        with pytest.raises(engines.OffTheDevelopmentSet, match="development set"):
            engines.development_cases(copy)

    def test_extra_fixtures_are_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RESOLVE_EXTRA_FIXTURES_DIR", str(tmp_path))

        with pytest.raises(engines.OffTheDevelopmentSet, match="extra fixtures"):
            engines.development_cases(engines.DEVELOPMENT_SET)

    def test_an_altered_file_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
        altered = tmp_path / "cases.json"
        altered.write_bytes(engines.DEVELOPMENT_SET.read_bytes() + b" ")
        monkeypatch.setattr(engines, "DEVELOPMENT_SET", altered)

        with pytest.raises(engines.OffTheDevelopmentSet, match="altered"):
            engines.development_cases(altered)

    def test_a_link_leading_elsewhere_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
        elsewhere = tmp_path / "holdout.json"
        elsewhere.write_text("[]", encoding="utf-8")
        link = tmp_path / "cases.json"
        try:
            os.symlink(elsewhere, link)
        except (OSError, NotImplementedError):
            pytest.skip("this system does not let the test make a symbolic link")
        monkeypatch.setattr(engines, "DEVELOPMENT_SET", link)

        with pytest.raises(engines.OffTheDevelopmentSet):
            engines.development_cases(link)

    def test_the_runner_refuses_before_building_any_client(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "set-by-the-test")
        built = []
        monkeypatch.setitem(engines.HOSTED_ENGINES, "gemini-3.8-flash", lambda: built.append(1))
        copy = tmp_path / "cases.json"
        shutil.copy(engines.DEVELOPMENT_SET, copy)
        monkeypatch.setattr(
            "sys.argv", ["runner", "--engine", "gemini-3.8-flash", "--cases", str(copy)]
        )

        assert runner.main() == 2
        assert built == []

    def test_the_services_engines_keep_their_freedom(self, tmp_path, monkeypatch):
        """The guard is for the three: the mock still runs any case file."""
        copy = tmp_path / "cases.json"
        shutil.copy(engines.DEVELOPMENT_SET, copy)

        engines.guard("mock", copy)  # no refusal


def test_the_vendors_sdks_are_imported_only_by_their_clients():
    """Constitution III: a vendor is named only behind the port."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    allowed = {
        "app/decision/gemini.py",
        "app/decision/mistral.py",
        "app/decision/openai_compatible.py",
    }
    offenders = []
    for directory in ("app", "evals", "scripts"):
        for path in (root / directory).rglob("*.py"):
            name = path.relative_to(root).as_posix()
            if name in allowed:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    modules = [node.module or ""]
                vendors = ("google.genai", "google", "mistralai", "openai")
                if any(m.startswith(vendors) for m in modules):
                    offenders.append(name)
    assert offenders == []
