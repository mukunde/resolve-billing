"""OpenTelemetry GenAI instrumentation. The interface, not the vendor.

This is the `DecisionEngine` argument applied to telemetry. Traces are emitted
against the **OpenTelemetry GenAI semantic conventions** and exported over
OTLP, so the backend is a line of configuration: a console in development,
Langfuse or Phoenix self-hosted, Cloud Trace or Datadog at a client. Wiring a
vendor's SDK into the decision engine would be the second lock that ADR-006
spends its length arguing against.

Three properties, in the order they matter.

**Off unless asked.** With no ``OTEL_EXPORTER_OTLP_ENDPOINT`` set, no provider
is installed, OpenTelemetry hands back its no-op tracer, and the code path is
the one that ran before this module existed. Observability that changes the
behaviour it observes is worse than none.

**Optional at import.** The package is an extra. Without it installed the
helpers below degrade to nothing, so ``pip install -e ".[api]"`` keeps working
unchanged.

**No customer data leaves in a span.** This is the one that is not a
convenience. Unlike an evaluation run, which is offline and synthetic, traces
flow continuously from production and would carry dispute text, amounts and
billing context. Exporting those to a vendor would contradict the whole
data-residency argument this repository makes -- so spans carry identifiers,
counts, classifications and costs, and never prose. :data:`FORBIDDEN` names
the fields that must never appear, and a test holds the instrumentation to it.

Enabling it, for example against a local Langfuse (OTLP over HTTP; Langfuse
does not accept gRPC):

    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel
    export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $AUTH,x-langfuse-ingestion-version=4"
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

try:  # the extra is optional; absence must cost nothing
    from opentelemetry import trace
    from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as genai

    AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - exercised by not installing it
    AVAILABLE = False
    trace = None  # type: ignore[assignment]
    genai = None  # type: ignore[assignment]

SERVICE_NAME = "resolve-billing"

#: Field names that must never be attached to a span.
#:
#: Not a lint preference: these carry the customer's words, our prose about
#: them, and the account they belong to. A trace pipeline is a continuous
#: export to somebody else's storage, and the pseudonymisation that protects
#: the model has to protect the trace as well or it protects nothing.
FORBIDDEN = (
    "customer_message",
    "input_message",
    "explanation",
    "customer_email",
    "billing_context",
    "evidence",
)

_configured = False


def configure(endpoint: str | None = None) -> bool:
    """Install a tracer provider, if an endpoint was configured.

    Returns whether an exporter was installed, so a caller can say so in a
    health check rather than guess. Called twice, it does nothing the second
    time.
    """
    global _configured
    if _configured or not AVAILABLE:
        return _configured

    endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", SERVICE_NAME)})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _configured = True
    return True


def configured() -> bool:
    """Whether *this module* installed an exporter.

    Deliberately not "is tracing on": a host application may install its own
    tracer provider, and then spans are recorded while this returns False. The
    narrow question is the useful one for a health check -- did our
    configuration find an endpoint.
    """
    return _configured


@contextmanager
def span(name: str, **attributes: Any):
    """A span, or nothing at all.

    Attributes whose value is ``None`` are dropped rather than exported as a
    string "None" -- the difference between *unknown* and *zero* is load-bearing
    in this codebase and a trace is no place to lose it.
    """
    if not AVAILABLE:
        yield None
        return

    tracer = trace.get_tracer(SERVICE_NAME)
    # An exception's text can carry a URL, a token or the platform's answer:
    # the span keeps its type and its status, never its message (ADR-009).
    with tracer.start_as_current_span(name, record_exception=False) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as error:
            current.set_attribute("error.type", type(error).__name__)
            raise


def set_attributes(current, **attributes: Any) -> None:
    """Add attributes to a span already open, skipping the unknown ones."""
    if current is None:
        return
    for key, value in attributes.items():
        if value is not None:
            current.set_attribute(key, value)


# -- attribute builders ----------------------------------------------------
#
# Built here rather than at the call sites so that the semantic convention
# names live in one place. They are still marked incubating upstream; when
# they move, one module changes.


def workflow_attributes(*, run_id: str, provider: str, engine: str) -> dict[str, Any]:
    """The run as a whole: one dispute, start to authorisation."""
    if not AVAILABLE:
        return {}
    return {
        genai.GEN_AI_OPERATION_NAME: genai.GenAiOperationNameValues.INVOKE_WORKFLOW.value,
        genai.GEN_AI_CONVERSATION_ID: run_id,
        "billing.provider": provider,
        "billing.decision_engine": engine,
    }


def chat_attributes(*, model: str, prompt_version: str, max_tokens: int) -> dict[str, Any]:
    """The single model call, before its answer is known."""
    if not AVAILABLE:
        return {}
    return {
        genai.GEN_AI_OPERATION_NAME: genai.GenAiOperationNameValues.CHAT.value,
        genai.GEN_AI_PROVIDER_NAME: genai.GenAiProviderNameValues.ANTHROPIC.value,
        genai.GEN_AI_REQUEST_MODEL: model,
        genai.GEN_AI_REQUEST_MAX_TOKENS: max_tokens,
        genai.GEN_AI_PROMPT_NAME: prompt_version,
        genai.GEN_AI_OUTPUT_TYPE: "json",
    }


def usage_attributes(telemetry) -> dict[str, Any]:
    """What the call consumed, and what we estimate it cost.

    ``cost_micros`` is ours rather than a convention's: OpenTelemetry GenAI
    records *tokens*, never a price, because no provider exposes a rate through
    an API. The arithmetic stays ours or the backend's -- which is the same
    conclusion the pricing table in the engine reaches.
    """
    if not AVAILABLE or telemetry is None:
        return {}
    return {
        genai.GEN_AI_RESPONSE_MODEL: telemetry.model_id,
        genai.GEN_AI_USAGE_INPUT_TOKENS: telemetry.input_tokens,
        genai.GEN_AI_USAGE_OUTPUT_TOKENS: telemetry.output_tokens,
        genai.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS: telemetry.cache_read_input_tokens,
        "billing.cost_micros": telemetry.cost_micros,
        "billing.pricing_as_of": telemetry.pricing_as_of,
        "billing.latency_ms": telemetry.latency_ms,
    }


def decision_attributes(
    *, classification: str, confidence: float, confidence_kind: str
) -> dict[str, Any]:
    """The verdict, as values from closed sets. Never the reasoning."""
    return {
        "billing.classification": classification,
        "billing.confidence": confidence,
        "billing.confidence_kind": confidence_kind,
    }
