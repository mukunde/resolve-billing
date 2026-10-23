"""The Claude engine reports a refused request as a refusal, without the
provider's text (it can quote the request or a credential). Needs the SDK."""

from types import SimpleNamespace

import pytest

from app.decision.base import EngineRejected
from app.decision.claude import ClaudeDecisionEngine
from app.domain.models import BillingContext, Customer, Dispute

anthropic = pytest.importorskip("anthropic")
httpx = pytest.importorskip("httpx")

DISPUTE = Dispute(id="dsp", customer_message="Facture en double ?", invoice_id="inv_002")
CONTEXT = BillingContext(customer=Customer(id="cus_acme", email="alice@acme.fr"))
SENTINEL = (
    "sk-ant-SENTINEL-KEY https://internal.example/secret?token=SENTINEL\n"
    "second line SENTINEL " + "x" * 500
)


def status_error(code: int, message: str = SENTINEL, body=None):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(code, request=request)
    return anthropic.APIStatusError(message, response=response, body=body)


class Raising:
    def __init__(self, error):
        self.messages = SimpleNamespace(create=self._raise)
        self._error = error

    async def _raise(self, **kwargs):
        raise self._error


async def refusal(error) -> str:
    engine = ClaudeDecisionEngine(client=Raising(error), model="claude-opus-5")
    with pytest.raises(EngineRejected) as raised:
        await engine.decide(DISPUTE, CONTEXT, [])
    return str(raised.value)


@pytest.mark.parametrize(
    "code, category",
    [
        (400, "request refused"),
        (401, "authentication refused"),
        (403, "permission refused"),
        (404, "model or endpoint not found"),
    ],
)
async def test_a_refusal_carries_a_status_and_a_category(code, category):
    assert await refusal(status_error(code)) == f"HTTP {code}: {category}"


async def test_the_providers_text_never_comes_through():
    text = await refusal(status_error(400, body={"error": {"message": SENTINEL}}))
    assert "SENTINEL" not in text and "\n" not in text and len(text) < 80


async def test_an_account_out_of_credit_is_recognised_from_the_structured_body():
    body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "Your credit balance is too low to access the Anthropic API.",
        },
    }
    assert await refusal(status_error(400, message="whatever", body=body)) == (
        "HTTP 400: account out of credit"
    )
