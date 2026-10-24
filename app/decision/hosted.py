"""The shared core of the engines evaluated on free tiers.

specs/004-second-engines. Three configurations -- `gemini-3.8-flash`
(Google), `mistral-large-4` and `zai-glm-5-3` (Mistral) -- decide a dispute
exactly as the Claude engine does: the context pseudonymised, the message
masked (D10), one request with the same prompt and the same payload, the
answer validated locally against the closed classification, the references
mapped back and those never issued dropped, and an unusable answer handed to
a person. Each vendor's client supplies one thing: `_complete`, the request
and what came back.

These engines are for the evaluation runner only, on the development cases:
they are not in the service's engine table (`app/api/deps.py`), and the
runner refuses them anywhere else (`evals/engines.py`).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from app.decision.anonymise import anonymise
from app.decision.base import Decision, EngineTelemetry
from app.decision.claude import PROMPT_PATH, PROMPT_VERSION
from app.decision.masking import ReferenceFinder, mask_message
from app.decision.verdict import ModelVerdict, escalation, verdict_schema
from app.domain.models import BillingContext, Dispute, RuleFinding

#: Where the prices below were read, and when.
MISTRAL_PRICES = "docs.mistral.ai/inference/pricing, read 2026-10-24"
GEMINI_PRICES = "ai.google.dev/gemini-api/docs/pricing (free tier), read 2026-10-24"


@dataclass(frozen=True)
class Configuration:
    """One engine configuration, closed: the exact model, where it is
    reached, with which key, and every parameter the request carries."""

    name: str
    vendor: Literal["google", "mistral"]
    developer: str
    endpoint: str
    key_variable: str
    model: str
    #: None: the vendor's default, recorded as such.
    temperature: float | None
    max_output_tokens: int
    reasoning: str
    safety: str
    tokens_per_minute: int
    requests_per_second: float
    #: Limits shared with other configurations of the same vendor account.
    shared_limits: str | None
    #: USD per million tokens (input, output); None under a free quota.
    price_per_mtok: tuple[float, float] | None
    price_source: str
    #: The maintainer's waiver (2026-10-24): reservations and costs are
    #: estimates, checked after each answer, never guarantees.
    reservation: Literal["estimated"]

    @property
    def prompt_version(self) -> str:
        return f"{self.name}/{PROMPT_VERSION}"


CONFIGURATIONS: dict[str, Configuration] = {
    config.name: config
    for config in (
        Configuration(
            name="gemini-3.8-flash",
            vendor="google",
            developer="Google",
            endpoint="https://generativelanguage.googleapis.com",
            key_variable="GEMINI_API_KEY",
            model="gemini-3.8-flash",
            temperature=None,
            max_output_tokens=4096,
            reasoning="low",
            safety="vendor default",
            # Conservative until the maintainer reads his project's limits in
            # AI Studio (T071): the docs publish none.
            tokens_per_minute=100_000,
            requests_per_second=0.08,
            shared_limits=None,
            price_per_mtok=None,
            price_source=GEMINI_PRICES,
            reservation="estimated",
        ),
        Configuration(
            name="mistral-large-4",
            vendor="mistral",
            developer="Mistral AI",
            endpoint="https://api.mistral.ai",
            key_variable="MISTRAL_API_KEY",
            model="mistral-large-4",
            temperature=None,
            max_output_tokens=4096,
            reasoning="none",
            safety="vendor default",
            tokens_per_minute=20_000,
            requests_per_second=0.83,
            shared_limits="mistral",
            price_per_mtok=(1.36, 4.18),
            price_source=MISTRAL_PRICES,
            reservation="estimated",
        ),
        Configuration(
            name="zai-glm-5-3",
            vendor="mistral",
            developer="Z.ai",
            endpoint="https://api.mistral.ai",
            key_variable="MISTRAL_API_KEY",
            model="zai-glm-5-3",
            temperature=None,
            max_output_tokens=4096,
            reasoning="low",
            safety="vendor default",
            tokens_per_minute=20_000,
            requests_per_second=1.0,
            shared_limits="mistral",
            price_per_mtok=(1.40, 4.40),
            price_source=MISTRAL_PRICES,
            reservation="estimated",
        ),
    )
}


def configuration(name: str) -> Configuration:
    """The configuration of that exact name; anything else is refused."""
    return CONFIGURATIONS[name]


@dataclass(frozen=True)
class Answer:
    """What a vendor sent back, reduced to what the core reads.

    `finish` is `stop` for a complete answer; `length` (cut off), `filtered`
    (a safety or content block) and `other` make it unusable. Token counts
    are None when the vendor did not report them. `notes` carry what the
    vendor said about the request -- a parameter ignored, say -- never its
    text about the dispute.
    """

    text: str | None
    finish: Literal["stop", "length", "filtered", "other"]
    input_tokens: int | None
    output_tokens: int | None
    model_version: str | None
    notes: tuple[str, ...] = ()


class HostedEngine:
    """A decision engine whose model sits behind a vendor's API."""

    def __init__(
        self,
        config: Configuration,
        invoice_references: ReferenceFinder | None = None,
    ) -> None:
        self.config = config
        self.name = config.name
        self.version = config.prompt_version
        self._system = PROMPT_PATH.read_text(encoding="utf-8")
        #: How the billing platform writes an invoice reference, to mask the
        #: ones a message cites (D10). Wired from the provider.
        self.invoice_references = invoice_references
        #: What the vendor reported across the run: notes on the request, and
        #: the model versions that answered.
        self.notes: list[str] = []
        self.model_versions: list[str] = []

    async def _complete(self, system: str, user: str) -> Answer:  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release the vendor's client, if it holds one."""

    async def decide(
        self,
        dispute: Dispute,
        context: BillingContext,
        findings: list[RuleFinding],
    ) -> Decision:
        payload, names = anonymise(context, findings)
        message = mask_message(dispute.customer_message, context, names, self.invoice_references)
        user = (
            "<customer_message>\n"
            f"{message}\n"
            "</customer_message>\n\n"
            "<billing_context>\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
            "</billing_context>"
        )
        started = time.perf_counter()
        answer = await self._complete(self._system, user)
        latency_ms = int((time.perf_counter() - started) * 1000)
        self.notes.extend(answer.notes)
        if answer.model_version:
            self.model_versions.append(answer.model_version)
        telemetry = EngineTelemetry(
            model_id=self.config.model,
            prompt_version=self.version,
            input_tokens=answer.input_tokens or 0,
            output_tokens=answer.output_tokens or 0,
            # Costed per run, against the vendor's regime, by the runner: a
            # per-call figure here would read as a billed amount.
            cost_micros=None,
            latency_ms=latency_ms,
        )
        verdict = _verdict(answer)
        if verdict is None:
            return escalation(telemetry)
        return Decision(
            classification=verdict.classification,
            confidence=verdict.confidence,
            confidence_kind="self_reported",
            recommended_action=verdict.recommended_action,
            explanation=names.restore(verdict.explanation),
            evidence_refs=names.resolve_all(verdict.evidence_refs),
            telemetry=telemetry,
        )

    @staticmethod
    def schema() -> dict:
        return verdict_schema()


def _verdict(answer: Answer) -> ModelVerdict | None:
    """The verdict, or None to send the run to a person: cut off, filtered,
    empty, or not valid against the closed sets."""
    if answer.finish != "stop" or not answer.text:
        return None
    try:
        return ModelVerdict.model_validate_json(answer.text)
    except ValidationError:
        return None
