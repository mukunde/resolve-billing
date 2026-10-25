"""The shared core of the engines evaluated on free tiers.

specs/004-second-engines. Four configurations -- `gemini-3.8-flash` and
`gemini-3.5-flash-lite` (Google), `mistral-medium-3-5` and
`mistral-medium-3-5-reasoning` (Mistral, one model at two reasoning efforts)
-- decide a dispute
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
from app.decision.base import Decision, EngineTelemetry, EngineUnavailable
from app.decision.claude import PROMPT_PATH, PROMPT_VERSION
from app.decision.masking import ReferenceFinder, mask_message
from app.decision.verdict import ModelVerdict, escalation, verdict_schema
from app.domain.models import BillingContext, Dispute, RuleFinding

#: Where the limits below were read, and when (T071).
GEMINI_LIMITS = "AI Studio rate-limit page, project resolve-billing-eval, read 2026-10-24"
MISTRAL_LIMITS = (
    "Mistral admin console, limits page, read 2026-10-24: mistral-medium-latest, "
    "the only Medium 3.5 name the page lists, taken as mistral-medium-3-5's"
)

#: The one estimator of a request's tokens: its UTF-8 bytes, a margin, the
#: output cap (`estimated_tokens`). Named in every result.
ESTIMATOR = "utf8-bytes-v1"

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
    #: The API version the requests go to: set on Google's client; for
    #: Mistral, the one its SDK's paths carry.
    api_version: str
    key_variable: str
    model: str
    #: None: the vendor's default, recorded as such.
    temperature: float | None
    max_output_tokens: int
    reasoning: str
    safety: str
    tokens_per_minute: int
    requests_per_second: float
    #: Requests a quota day allows, and the zone the day is counted in; None
    #: where no daily limit applies.
    requests_per_day: int | None
    quota_timezone: str | None
    #: Attempts per case; and whether an outage on the last one stops the
    #: run rather than escalating the case and going on.
    attempts: int
    stop_on_outage: bool
    #: Where and when the limits above were read.
    limits_source: str
    #: Limits shared with other configurations of the same vendor account.
    shared_limits: str | None
    #: USD per million tokens (input, output); None under a free quota.
    price_per_mtok: tuple[float, float] | None
    price_source: str
    #: How far the reservation bounds what a request may use: `estimated`
    #: under the maintainer's waiver (2026-10-24) -- checked after each
    #: answer, never a guarantee; `uncapped` when reasoning has no billable
    #: cap at all, which the evaluation runner never sends.
    reservation: Literal["estimated", "uncapped"]
    reservation_estimator: str

    @property
    def prompt_version(self) -> str:
        return f"{self.name}/{PROMPT_VERSION}"


def _gemini(
    model: str,
    requests_per_minute: int,
    requests_per_day: int,
    attempts: int,
    stop_on_outage: bool,
) -> Configuration:
    """A Gemini configuration on the free tier: every Gemini one alike but
    for the model, its limits, its attempts and what an outage does."""
    return Configuration(
        name=model,
        vendor="google",
        developer="Google",
        endpoint="https://generativelanguage.googleapis.com",
        api_version="v1beta",
        key_variable="GEMINI_API_KEY",
        model=model,
        temperature=None,
        max_output_tokens=4096,
        # Flash's lowest level; Flash-Lite takes it too, so the two differ by
        # model, not by setting (the same label, not a proven equal budget).
        reasoning="low",
        safety="vendor default",
        tokens_per_minute=250_000,
        requests_per_second=requests_per_minute / 60,
        requests_per_day=requests_per_day,
        quota_timezone="America/Los_Angeles",
        attempts=attempts,
        stop_on_outage=stop_on_outage,
        limits_source=GEMINI_LIMITS,
        shared_limits=None,
        price_per_mtok=None,
        price_source=GEMINI_PRICES,
        reservation="estimated",
        reservation_estimator=ESTIMATOR,
    )


# 20 a day is one full run (20 cases reach the engine): one attempt per
# case, and an outage stops the run.
GEMINI = _gemini("gemini-3.8-flash", 5, 20, attempts=1, stop_on_outage=True)
# 500 a day leaves room to retry: three attempts, an outage escalates the
# case (spec 004, third amendment).
GEMINI_FLASH_LITE = _gemini("gemini-3.5-flash-lite", 15, 500, attempts=3, stop_on_outage=False)

CONFIGURATIONS: dict[str, Configuration] = {
    config.name: config
    for config in (
        GEMINI,
        GEMINI_FLASH_LITE,
        *(
            Configuration(
                name=name,
                vendor="mistral",
                developer="Mistral AI",
                endpoint="https://api.mistral.ai",
                api_version="v1",
                key_variable="MISTRAL_API_KEY",
                model="mistral-medium-3-5",
                temperature=None,
                max_output_tokens=4096,
                reasoning=reasoning,
                safety="vendor default",
                tokens_per_minute=20_000,
                requests_per_second=1.0,
                requests_per_day=None,
                quota_timezone=None,
                attempts=3,
                stop_on_outage=False,
                limits_source=MISTRAL_LIMITS,
                shared_limits="mistral",
                price_per_mtok=(1.5, 7.5),
                price_source=MISTRAL_PRICES,
                reservation="estimated",
                reservation_estimator=ESTIMATOR,
            )
            # Two configurations of one model, equal but for the reasoning effort
            # (spec 004, second amendment): the free level refused Large 4 and
            # GLM 5.3 (403, tier_not_allowed, 2026-10-24).
            for name, reasoning in (
                ("mistral-medium-3-5", "none"),
                ("mistral-medium-3-5-reasoning", "high"),
            )
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
        #: Answers that came without their token counts: the runner stops on
        #: one, since the reservation can no longer be checked.
        self.usage_missing = 0

    #: Whether `reservation` asks the vendor (a request of its own, paced and
    #: counted by the runner) or is computed locally.
    counts_remotely = False

    async def _complete(self, system: str, user: str) -> Answer:  # pragma: no cover
        raise NotImplementedError

    async def reservation(self, system: str, user: str) -> int:  # pragma: no cover
        """The tokens a request may use, input and output, reserved before it
        is sent. An estimate (the maintainer's waiver, 2026-10-24)."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release the vendor's client, if it holds one."""

    def request_texts(self, dispute: Dispute, context: BillingContext, findings) -> tuple:
        """The system prompt, the user payload a decision sends, and the
        pseudonyms that map its references back."""
        payload, names = anonymise(context, findings)
        message = mask_message(dispute.customer_message, context, names, self.invoice_references)
        return self._system, _user_payload(message, payload), names

    async def decide(
        self,
        dispute: Dispute,
        context: BillingContext,
        findings: list[RuleFinding],
    ) -> Decision:
        system, user, names = self.request_texts(dispute, context, findings)
        started = time.perf_counter()
        answer = await self._complete(system, user)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return self.decision_from(answer, names, latency_ms)

    def decision_from(self, answer: Answer, names, latency_ms: int) -> Decision:
        """The decision an answer makes: the verdict with its references
        mapped back, or an escalation when the answer is unusable."""
        self.notes.extend(answer.notes)
        if answer.input_tokens is None or answer.output_tokens is None:
            self.usage_missing += 1
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


def _user_payload(message: str, payload: dict) -> str:
    """The same user payload as the Claude engine's."""
    return (
        "<customer_message>\n"
        f"{message}\n"
        "</customer_message>\n\n"
        "<billing_context>\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "</billing_context>"
    )


def estimated_tokens(system: str, user: str, margin: int, max_output: int) -> int:
    """A request's tokens, estimated (`ESTIMATOR`): the UTF-8 bytes of all it
    carries -- system prompt, user payload, the verdict schema as sent --
    plus a margin for what the vendor adds around them, plus the output cap.
    An estimate, not a bound: neither vendor documents its tokenizer (the
    maintainer's waivers, 2026-10-24); each answer's reported usage is
    checked against it."""
    material = system + user + json.dumps(verdict_schema())
    return len(material.encode("utf-8")) + margin + max_output


def unavailable(category: str, response=None) -> EngineUnavailable:
    """The port's outage, with the wait the vendor asked for when its
    response carried a `Retry-After` in seconds (an HTTP date is ignored)."""
    outage = EngineUnavailable(category)
    headers = getattr(response, "headers", None) or {}
    try:
        seconds = float(headers.get("retry-after", ""))
    except (TypeError, ValueError):
        seconds = None
    outage.retry_after = seconds if seconds is not None and seconds >= 0 else None
    return outage


def usable(answer: Answer) -> bool:
    """Whether an answer carries a valid verdict."""
    return _verdict(answer) is not None


def _verdict(answer: Answer) -> ModelVerdict | None:
    """The verdict, or None to send the run to a person: cut off, filtered,
    empty, or not valid against the closed sets."""
    if answer.finish != "stop" or not answer.text:
        return None
    try:
        return ModelVerdict.model_validate_json(answer.text)
    except ValidationError:
        return None
