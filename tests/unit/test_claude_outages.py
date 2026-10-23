"""ClaudeDecisionEngine outages (taxonomy S8), with the SDK's own exceptions.

In a file of its own because it needs the SDK, an optional dependency (the
`claude` extra): without it these tests are skipped, and the rest of the
engine's tests still run.
"""

from types import SimpleNamespace

import pytest

from app.decision.base import EngineRejected, EngineUnavailable
from app.decision.claude import ClaudeDecisionEngine
from app.domain.models import BillingContext, Customer, Dispute

DISPUTE = Dispute(id="dsp", customer_message="Facture en double ?", invoice_id="inv_002")
CONTEXT = BillingContext(customer=Customer(id="cus_acme", email="alice@acme.fr"))

anthropic = pytest.importorskip("anthropic")
httpx = pytest.importorskip("httpx")

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def status_error(code: int):
    response = httpx.Response(code, request=REQUEST)
    return anthropic.APIStatusError(f"HTTP {code}", response=response, body=None)


class Raising:
    def __init__(self, error: Exception):
        self.messages = SimpleNamespace(create=self._raise)
        self._error = error

    async def _raise(self, **kwargs):
        raise self._error


class APIConnectionError(Exception):
    """A lookalike from elsewhere: same name as the SDK's, not the SDK's."""


class TestOutages:
    @pytest.mark.parametrize(
        "error",
        [
            anthropic.APIConnectionError(request=REQUEST),
            anthropic.APITimeoutError(request=REQUEST),
            status_error(500),
            status_error(529),
            status_error(429),
            status_error(408),
        ],
        ids=["connection", "timeout", "500", "overloaded", "rate-limited", "request-timeout"],
    )
    async def test_an_outage_is_reported_as_one(self, error):
        engine = ClaudeDecisionEngine(client=Raising(error), model="claude-opus-5")
        with pytest.raises(EngineUnavailable):
            await engine.decide(DISPUTE, CONTEXT, [])

    @pytest.mark.parametrize(
        "error", [status_error(400), status_error(401)], ids=["bad-request", "unauthorised"]
    )
    async def test_a_refusal_is_not_an_outage(self, error):
        """A bad request or a rejected key fails visibly, as a refusal."""
        engine = ClaudeDecisionEngine(client=Raising(error), model="claude-opus-5")
        with pytest.raises(EngineRejected):
            await engine.decide(DISPUTE, CONTEXT, [])

    @pytest.mark.parametrize(
        "error", [ValueError("a bug"), APIConnectionError()], ids=["programming-error", "lookalike"]
    )
    async def test_an_error_stays_an_error(self, error):
        """A bug or a stranger's exception is neither an outage nor a refusal."""
        engine = ClaudeDecisionEngine(client=Raising(error), model="claude-opus-5")
        with pytest.raises(type(error)):
            await engine.decide(DISPUTE, CONTEXT, [])
