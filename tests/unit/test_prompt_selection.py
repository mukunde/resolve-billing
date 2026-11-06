"""The prompt, selectable per run, the service on its default
(specs/012-prompt-v4-usage, FR-004, FR-005). No network."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.decision import prompts
from app.decision.hosted import CONFIGURATIONS, configuration
from evals import engines, service_run
from evals.first_calls import FirstCalls, FirstCallsRefused, fingerprint

#: The fingerprints Luna's and Haiku's first calls were accepted on, before this
#: slice: a v3 configuration must keep them.
ACCEPTED_V3 = {
    "gpt-6-luna": "6f2271d2ee88b43727aef7fa72d858a7498113fd0009459596a0fbd6ec968719",
    "claude-haiku-5-5": "340937d123edc6537ae87ff6ca43b0f9ec7aaa822961c076b971ebe8ddd2feaf",
    "mistral-medium-3-5": "06167c9ee742329dfb120577c0fb49b4ac1b998e57175357f49f392037c782e8",
}


class TestThePromptFiles:
    def test_v4_is_v3_plus_one_usage_section(self):
        v3 = prompts.prompt_text("decision_v3")
        v4 = prompts.prompt_text("decision_v4")
        assert v4.count("\n# Usage\n") == 1
        section = v4.split("\n# Usage\n")[1].split("\n# ")[0]
        # Taking the section out gives v3 back: no other line moved.
        assert v4.replace("\n# Usage\n" + section, "", 1) == v3
        assert "SUSPICIOUS_USAGE_DELTA" in section
        assert "USAGE_UNAVAILABLE" in section

    def test_an_unknown_prompt_is_refused(self):
        with pytest.raises(prompts.UnknownPrompt):
            prompts.prompt_text("decision_v9")

    def test_the_default_is_v3(self):
        assert prompts.DEFAULT_PROMPT == "decision_v3"


class TestTheConfigurations:
    @pytest.mark.parametrize("name", sorted(ACCEPTED_V3))
    def test_a_v3_fingerprint_is_the_one_accepted(self, name):
        assert fingerprint(configuration(name)) == ACCEPTED_V3[name]
        assert configuration(name).prompt_version == f"{name}/decision_v3"

    def test_a_v4_configuration_is_another_request(self):
        v4 = replace(configuration("gpt-6-luna"), prompt="decision_v4")
        assert v4.prompt_version == "gpt-6-luna/decision_v4"
        assert fingerprint(v4) != ACCEPTED_V3["gpt-6-luna"]


class TestTheEngines:
    @pytest.mark.parametrize("name", ["gpt-6-luna", "claude-haiku-5-5", "gemini-3.8-flash"])
    def test_a_hosted_engine_sends_the_selected_prompt(self, name, monkeypatch):
        from app.decision.hosted import HostedEngine

        for prompt in ("decision_v3", "decision_v4"):
            engine = HostedEngine(replace(configuration(name), prompt=prompt))
            assert engine._system == prompts.prompt_text(prompt)
            assert engine.version == f"{name}/{prompt}"

    def test_claude_sends_the_selected_prompt(self):
        from app.decision.claude import ClaudeDecisionEngine

        engine = ClaudeDecisionEngine(
            client=object(), model="claude-haiku-5-5", prompt="decision_v4"
        )
        assert engine._system == prompts.prompt_text("decision_v4")
        assert "/decision_v4+" in engine.version
        default = ClaudeDecisionEngine(client=object(), model="claude-haiku-5-5")
        assert default._system == prompts.prompt_text("decision_v3")

    def test_the_claude_and_anthropic_hosted_systems_match_under_each(self):
        from app.decision.claude import ClaudeDecisionEngine
        from app.decision.hosted import HostedEngine

        for prompt in ("decision_v3", "decision_v4"):
            claude = ClaudeDecisionEngine(client=object(), model="claude-haiku-5-5", prompt=prompt)
            hosted = HostedEngine(replace(configuration("claude-haiku-5-5"), prompt=prompt))
            assert claude._system == hosted._system

    def test_the_factory_builds_with_the_prompt(self, monkeypatch):
        built = []
        monkeypatch.setattr(
            "app.decision.vendors.client_for", lambda config: built.append(config) or config
        )
        engines.hosted_engine("gpt-6-luna", "decision_v4")
        assert built[-1].prompt == "decision_v4"


class TestTheService:
    def test_the_service_reads_the_default_alone(self, monkeypatch):
        from app.api import deps

        monkeypatch.setenv("RESOLVE_PROMPT", "decision_v4")
        assert deps.PROMPT_VERSION == "decision_v3"
        assert "decision_v4" not in Path(deps.__file__).read_text(encoding="utf-8")

    def test_the_reference_run_keeps_v3(self):
        source = Path("evals/reference_run.py").read_text(encoding="utf-8")
        assert "decision_v4" not in source


class TestFirstCalls:
    def accept(self, config, tmp_path):
        result = tmp_path / f"{config.name}-{config.prompt}.json"
        result.write_text("{}", encoding="utf-8")
        FirstCalls().record(config, "model-x", result)

    def test_mistral_siblings_admit_each_other_under_v4(self, tmp_path, monkeypatch):
        from evals import state

        monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr("evals.first_calls.account", lambda config: {"id": "acct"})
        siblings = [c for c in CONFIGURATIONS.values() if c.shared_limits == "mistral"]
        v4 = [replace(c, prompt="decision_v4") for c in siblings]
        for config in v4:
            self.accept(config, tmp_path)
        FirstCalls().admit_full_run(v4[0])
        with pytest.raises(FirstCallsRefused):
            FirstCalls().admit_full_run(siblings[0])

    def test_v3_admission_is_unchanged(self, tmp_path, monkeypatch):
        from evals import state

        monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr("evals.first_calls.account", lambda config: {"id": "acct"})
        siblings = [c for c in CONFIGURATIONS.values() if c.shared_limits == "mistral"]
        for config in siblings:
            self.accept(config, tmp_path)
        FirstCalls().admit_full_run(siblings[0])


class TestTheRunner:
    def test_the_mock_refuses_v4(self, monkeypatch):
        from evals import runner

        monkeypatch.setattr(
            "sys.argv", ["runner", "--engine", "mock", "--prompt", "decision_v4"]
        )
        with pytest.raises(SystemExit) as stop:
            runner.main()
        assert stop.value.code == 2

    async def test_claude_is_built_with_the_prompt(self, monkeypatch):
        from evals import runner

        built = []

        class Stub:
            name = "claude"
            version = "stub"

            def __init__(self, prompt):
                built.append(prompt)

            async def decide(self, dispute, context, findings):
                raise RuntimeError("not called")

        monkeypatch.setattr(runner, "_claude_engine", lambda prompt: Stub(prompt))
        first = json.loads(Path("evals/cases.json").read_text("utf-8"))[:1]
        with pytest.raises(RuntimeError, match="not called"):
            await runner.run("claude", cases=first, prompt="decision_v4")
        assert built == ["decision_v4"]


class TestTheServiceRun:
    def test_the_prompt_reaches_the_engine_and_the_result(self, monkeypatch, tmp_path):
        from tests.unit.test_prepaid_runner import Scripted
        from tests.unit.test_service_run import Book

        monkeypatch.delenv("RESOLVE_EXTRA_FIXTURES_DIR", raising=False)
        for variable in ("OPENAI_CUSTOM_HEADERS", "OPENAI_LOG"):
            monkeypatch.delenv(variable, raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_ORG_ID", "org-test")
        monkeypatch.setenv("OPENAI_PROJECT_ID", "proj_test")
        from tests.unit.test_evaluation_limits import _no_wait

        monkeypatch.setattr("app.decision.limits.Limiter.acquire", _no_wait)
        monkeypatch.setattr("evals.state.AccountRun.wait", lambda self: 0.0)
        from evals import state

        monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
        admitted = []
        monkeypatch.setattr(
            service_run.FirstCalls, "admit_full_run", lambda self, config: admitted.append(config)
        )
        monkeypatch.setattr(service_run, "PrepaidLedger", lambda account, currency: Book())
        vendor = Scripted("gpt-6-luna")
        asked = []
        monkeypatch.setattr(
            engines, "hosted_engine", lambda name, prompt: asked.append((name, prompt)) or vendor
        )
        target = tmp_path / "gpt-6-luna-service.json"
        service_run.main(
            ["--data-terms", "contractual", "--json", str(target), "--prompt", "decision_v4"]
        )
        result = json.loads(sorted(tmp_path.glob("gpt-6-luna-service-*.json"))[-1].read_text())
        assert asked == [("gpt-6-luna", "decision_v4")]
        assert admitted[-1].prompt == "decision_v4"
        assert result["prompt_version"] == "decision_v4"
        assert result["engine_version"] == "gpt-6-luna/decision_v4"
        assert result["configuration_fingerprint"] == fingerprint(
            replace(configuration("gpt-6-luna"), prompt="decision_v4")
        )
