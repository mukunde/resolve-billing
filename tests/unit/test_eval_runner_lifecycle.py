"""The runner releases the engine it built, at the end of each run.

`--runs N` calls `asyncio.run` N times. Whatever an engine holds -- an HTTP
client and its connections -- belongs to the loop of the run that built it,
and has to be closed before that loop is.
"""

import json

import pytest

from app.api import deps
from app.decision.mock import MockDecisionEngine
from evals import runner

released: list[str] = []


class ReleasingEngine(MockDecisionEngine):
    async def aclose(self) -> None:
        released.append("closed")


@pytest.fixture
def one_case(tmp_path):
    dev = json.loads(runner.CASES_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "one.json"
    path.write_text(json.dumps([c for c in dev if c["id"] == "duplicate_fr"]), encoding="utf-8")
    return path


async def test_each_run_closes_the_engine_it_built(monkeypatch, one_case):
    monkeypatch.setitem(deps.ENGINES, "releasing", ReleasingEngine)
    released.clear()

    await runner.run("releasing", one_case)
    await runner.run("releasing", one_case)

    assert released == ["closed", "closed"]


async def test_an_engine_with_nothing_to_close_runs_as_before(one_case):
    outcomes, _ = await runner.run("mock", one_case)

    assert len(outcomes) == 1
