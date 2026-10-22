"""An engine that refuses the request ends an evaluation run with a diagnosis.

The provider refusing a request -- a bad request, a key it rejects, an
account out of credit -- is not an outage and must not become an escalation:
it fails the run. It fails it with one clear line and no result file, not a
traceback in the middle of a run (found on 2026-10-19, when the API account
ran out of credit on the first case).
"""

import json
import sys
from types import SimpleNamespace

import pytest

from app.api import deps
from app.decision.base import EngineRejected
from app.decision.claude import ClaudeDecisionEngine
from app.decision.mock import MockDecisionEngine
from app.domain.models import BillingContext, Customer, Dispute
from evals import runner

DISPUTE = Dispute(id="dsp", customer_message="Facture en double ?", invoice_id="inv_002")
CONTEXT = BillingContext(customer=Customer(id="cus_acme", email="alice@acme.fr"))


class RefusingEngine(MockDecisionEngine):
    async def decide(self, dispute, context, findings):
        raise EngineRejected("HTTP 400: Your credit balance is too low to access the API.")


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
    assert "credit balance is too low" in err
    assert "Traceback" not in err
    assert not out.exists()


anthropic = pytest.importorskip("anthropic")
httpx = pytest.importorskip("httpx")


def sdk_status_error(code: int, message: str):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(code, request=request)
    return anthropic.APIStatusError(message, response=response, body=None)


class Raising:
    def __init__(self, error):
        self.messages = SimpleNamespace(create=self._raise)
        self._error = error

    async def _raise(self, **kwargs):
        raise self._error


@pytest.mark.parametrize("code", [400, 401, 403, 404])
async def test_the_claude_engine_reports_a_refusal_as_one(code):
    error = sdk_status_error(code, "Your credit balance is too low to access the API.")
    engine = ClaudeDecisionEngine(client=Raising(error), model="claude-opus-5")
    with pytest.raises(EngineRejected, match=f"HTTP {code}: Your credit balance is too low"):
        await engine.decide(DISPUTE, CONTEXT, [])
