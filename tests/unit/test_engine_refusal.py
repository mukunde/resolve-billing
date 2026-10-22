"""An engine that refuses the request ends an evaluation run with a diagnosis.

The provider refusing a request -- a bad request, a key it rejects, an
account out of credit -- is not an outage and must not become an escalation:
it fails the run. It fails it with one clear line and no result file, not a
traceback in the middle of a run (found on 2026-10-19, when the API account
ran out of credit on the first case).
"""

import json
import sys

import pytest

from app.api import deps
from app.decision.base import EngineRejected
from app.decision.mock import MockDecisionEngine
from evals import runner


class RefusingEngine(MockDecisionEngine):
    async def decide(self, dispute, context, findings):
        raise EngineRejected("HTTP 400: account out of credit")


@pytest.fixture
def one_case(tmp_path):
    dev = json.loads(runner.CASES_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "one.json"
    path.write_text(json.dumps([c for c in dev if c["id"] == "duplicate_fr"]), encoding="utf-8")
    return path


def test_the_run_stops_with_a_diagnosis_and_no_result_file(monkeypatch, one_case, tmp_path, capsys):
    monkeypatch.setitem(deps.ENGINES, "refusing", RefusingEngine)
    out = tmp_path / "result.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "--engine", "refusing", "--cases", str(one_case), "--json", str(out)],
    )

    assert runner.main() == 2
    err = capsys.readouterr().err
    assert "the engine refused the request" in err
    assert "HTTP 400: account out of credit" in err
    assert "Traceback" not in err
    assert not out.exists()


class RefusingOnSecondRun(MockDecisionEngine):
    built = 0

    def __init__(self):
        super().__init__()
        type(self).built += 1
        self.refuse = type(self).built > 1

    async def decide(self, dispute, context, findings):
        if self.refuse:
            raise EngineRejected("HTTP 401: authentication refused")
        return await super().decide(dispute, context, findings)


def test_a_series_stops_at_the_refused_run_and_writes_nothing(
    monkeypatch, one_case, tmp_path, capsys
):
    RefusingOnSecondRun.built = 0
    monkeypatch.setitem(deps.ENGINES, "second", RefusingOnSecondRun)
    out = tmp_path / "result.json"
    argv = [
        "runner",
        "--engine",
        "second",
        "--cases",
        str(one_case),
        "--runs",
        "3",
        "--json",
        str(out),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    assert runner.main() == 2
    err = capsys.readouterr().err
    assert "in run 2/3" in err and "The series stopped" in err
    assert not out.exists()


def test_an_existing_result_file_is_left_and_named(monkeypatch, one_case, tmp_path, capsys):
    monkeypatch.setitem(deps.ENGINES, "refusing", RefusingEngine)
    out = tmp_path / "result.json"
    out.write_text("earlier", encoding="utf-8")
    argv = ["runner", "--engine", "refusing", "--cases", str(one_case), "--json", str(out)]
    monkeypatch.setattr(sys, "argv", argv)

    assert runner.main() == 2
    assert out.read_text(encoding="utf-8") == "earlier"
    assert "left as it was" in capsys.readouterr().err


def test_the_investigation_does_not_turn_a_refusal_into_an_escalation():
    import asyncio

    from app.application.investigate_dispute import DisputeInvestigator
    from app.domain.models import Dispute
    from app.providers.fake.adapter import FakeBillingProvider

    investigator = DisputeInvestigator(provider=FakeBillingProvider(), engine=RefusingEngine())
    dispute = Dispute(
        id="dsp",
        customer_message="Facture en double ?",
        customer_email="alice@acme.fr",
        invoice_id="inv_002",
    )
    with pytest.raises(EngineRejected):
        asyncio.run(investigator.investigate(dispute))
