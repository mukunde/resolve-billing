"""A decision engine backed by the Claude Messages API.

Four things this does that a bare API call would not:

**The model never sees a real identifier.** The context is pseudonymised, and
the references it cites are mapped back afterwards -- anything outside the set
we issued is dropped rather than trusted.

**The output shape is constrained server-side** and validated again on
arrival. A response that does not fit the schema is not patched up: the run is
handed to a human, which is the only safe reading of "the engine did not
answer".

**The prompt is a versioned file**, and its version lands in the audit row
next to the model id. Without that, an evaluation run from last week is
comparable to nothing.

**Every call reports what it cost and how long it took.** That turns
`deterministic before AI` from a principle into a number: the rule that avoids
this call costs zero.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from app.decision.anonymise import anonymise
from app.decision.base import (
    Classification,
    Decision,
    EngineTelemetry,
    RecommendedAction,
)
from app.domain.models import BillingContext, Dispute, RuleFinding

PROMPT_PATH = Path(__file__).parent / "prompts" / "decision_v1.md"
PROMPT_VERSION = "decision_v1"

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TIMEOUT_SECONDS = 60.0

#: The date the prices below were read off the vendor's pricing page.
#:
#: Carried explicitly because a cost figure without the date of the price list
#: it came from cannot be interpreted six months later -- the same reason
#: ``confidence_kind`` travels next to ``confidence``.
PRICING_AS_OF = "2026-06-24"

#: List prices in USD per million tokens (input, output), by model.
#:
#: Hard-coded, and that is not an oversight waiting to be fixed: **no provider
#: exposes a rate through an API**. The Models endpoint returns identifiers,
#: capabilities and context windows, never a price. Every observability tool
#: that displays a cost -- Langfuse, Helicone, LangSmith -- ships its own
#: hand-maintained table for exactly this reason. Delegating to one of them
#: changes who maintains the table, not whether a table is needed.
#:
#: These are **list prices, not billed amounts**. What lands in an audit row
#: is therefore an *estimate*. Reconciling estimated against billed -- via the
#: provider's usage and cost reporting -- is on the roadmap, and it is the
#: same problem this project solves on customer invoices, turned on our own
#: consumption.
#:
#: Two distinct facts live in this table, and conflating them was a bug:
#:
#: * a model **absent** from it has an *unknown* price -- we have no rate, and
#:   the honest record of the cost is "we do not know";
#: * a model mapped to **None** has *no per-call price*, which is the real and
#:   expected shape of a self-hosted or flat-rate model. Its cost per call is
#:   genuinely zero, and that is a result worth publishing rather than a gap.
#:
#: The difference matters most in the engine comparison: "free because it runs
#: on our own hardware" and "free because nobody added the rate" would
#: otherwise print identically -- one being a finding, the other a defect.
#:
#: No ``None`` entry exists today because no self-hosted engine does. Adding
#: one before the engine would be the empty-adapter smell this repo avoids.
PRICING_PER_MTOK: dict[str, tuple[float, float] | None] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


class _ModelVerdict(BaseModel):
    """Exactly what the model is allowed to fill in.

    Note what is absent: ``confidence_kind``. Whether a number is calibrated
    is not the model's to declare -- the engine sets it, and only an
    evaluation run can promote it.
    """

    classification: Classification
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_action: RecommendedAction
    explanation: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)


def _response_schema() -> dict:
    """JSON schema sent to the API, generated from the domain enums.

    Hand-built rather than derived from the Pydantic model so the enum values
    are flat and ``additionalProperties`` is explicit -- and so that adding a
    classification to the enum updates the schema automatically.
    """
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "classification": {
                    "type": "string",
                    "enum": [c.value for c in Classification],
                },
                # No minimum/maximum: the API rejects those keywords on a
                # number ("properties maximum, minimum are not supported").
                # The bound is enforced on arrival instead, by _ModelVerdict,
                # and a value outside it fails validation and escalates to a
                # human -- the same safety, checked one step later.
                "confidence": {"type": "number"},
                "recommended_action": {
                    "type": "string",
                    "enum": [a.value for a in RecommendedAction],
                },
                "explanation": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "classification",
                "confidence",
                "recommended_action",
                "explanation",
                "evidence_refs",
            ],
            "additionalProperties": False,
        },
    }


class ClaudeDecisionEngine:
    """Classifies a dispute with Claude, under a constrained output schema."""

    name = "claude"

    def __init__(
        self,
        client=None,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = "low",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model or os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)
        self.version = f"{self.model}/{PROMPT_VERSION}"
        self._max_tokens = max_tokens
        # Classification is not a task that repays deep thinking; the money is
        # in the deterministic findings, which have already run. Raise this
        # only if the evaluation suite shows it buys accuracy.
        self._effort = effort
        self._system = PROMPT_PATH.read_text(encoding="utf-8")
        self._client = client if client is not None else _default_client(timeout_seconds)

    async def decide(
        self,
        dispute: Dispute,
        context: BillingContext,
        findings: list[RuleFinding],
    ) -> Decision:
        payload, names = anonymise(context, findings)
        started = time.perf_counter()

        response = await self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            system=self._system,
            output_config={"effort": self._effort, "format": _response_schema()},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "<customer_message>\n"
                        f"{dispute.customer_message}\n"
                        "</customer_message>\n\n"
                        "<billing_context>\n"
                        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
                        "</billing_context>"
                    ),
                }
            ],
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        telemetry = self._telemetry(response, latency_ms)

        verdict = self._parse(response)
        if verdict is None:
            return self._to_human(telemetry)

        return Decision(
            classification=verdict.classification,
            confidence=verdict.confidence,
            # Self-reported, always. The model asserting 0.94 is the model's
            # opinion of itself; only the evaluation suite can turn that into
            # a calibrated number.
            confidence_kind="self_reported",
            recommended_action=verdict.recommended_action,
            # The engine writes prose about INVOICE_1; a reviewer needs to
            # read inv_002. Mapping only `evidence_refs` back left the
            # explanation unreadable the moment the alias map was discarded --
            # and the explanation is what reaches the audit row, the email and
            # the approval page.
            explanation=names.restore(verdict.explanation),
            evidence_refs=names.resolve_all(verdict.evidence_refs),
            telemetry=telemetry,
        )

    # -- helpers -----------------------------------------------------------

    def _parse(self, response) -> _ModelVerdict | None:
        """Validate the response, or return None to send the run to a human.

        Three ways this fails, all handled the same way: the model declined,
        the output was cut off by ``max_tokens``, or the JSON did not validate
        despite the constrained format. None of them is recoverable by
        guessing what was meant.
        """
        if getattr(response, "stop_reason", None) in {"refusal", "max_tokens"}:
            return None
        text = next(
            (block.text for block in response.content if getattr(block, "type", None) == "text"),
            None,
        )
        if not text:
            return None
        try:
            return _ModelVerdict.model_validate_json(text)
        except ValidationError:
            return None

    def _to_human(self, telemetry: EngineTelemetry) -> Decision:
        """The engine did not answer. That is a decision to escalate."""
        return Decision(
            classification=Classification.UNKNOWN,
            confidence=0.0,
            confidence_kind="self_reported",
            recommended_action=RecommendedAction.ESCALATE,
            explanation=(
                "Le moteur de décision n'a pas produit de sortie conforme au schéma. "
                "Le litige part en revue humaine."
            ),
            evidence_refs=[],
            telemetry=telemetry,
        )

    def _telemetry(self, response, latency_ms: int) -> EngineTelemetry:
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        cost = estimate_cost_micros(self.model, input_tokens, output_tokens)
        return EngineTelemetry(
            model_id=self.model,
            prompt_version=PROMPT_VERSION,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cached,
            cost_micros=cost,
            # Only when a rate was actually applied: quoting the date of a
            # table that does not contain this model would be misleading.
            pricing_as_of=PRICING_AS_OF if cost is not None else None,
            latency_ms=latency_ms,
        )


def estimate_cost_micros(
    model: str, input_tokens: int, output_tokens: int
) -> int | None:
    """Estimated cost of one call, in millionths of a dollar.

    *Estimated*, and the word is load-bearing: this applies the list prices of
    :data:`PRICING_PER_MTOK` as of :data:`PRICING_AS_OF`. It is not what the
    provider invoiced. Quote it as an order of magnitude and as a comparison
    between engines, never as an accounting figure.

    Returns ``None`` when the model is unknown to the table. Returning ``0``
    there would have the audit *assert* that the run was free, which is a
    stronger and different claim than admitting we have no rate -- and it
    would silently zero out the very figures the cost argument rests on.

    Integer units for the same reason money is: a per-run cost of $0.0007 in
    a float, summed over an evaluation set, drifts.
    """
    if model not in PRICING_PER_MTOK:
        return None

    prices = PRICING_PER_MTOK[model]
    if prices is None:
        return 0  # self-hosted or flat-rate: no per-call price, and we know it

    input_price, output_price = prices
    micros = (input_tokens * input_price + output_tokens * output_price)  # $/Mtok * tokens
    return round(micros)


def _default_client(timeout_seconds: float):
    """Built lazily, so importing this module never requires credentials."""
    import anthropic

    return anthropic.AsyncAnthropic(timeout=timeout_seconds)
