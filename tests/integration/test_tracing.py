"""Tracing: that it is silent by default, and mute about the customer.

Two guarantees, and only the second is interesting.

The first is that an exporter nobody configured changes nothing. Observability
that alters the behaviour it observes is worse than none, and on a system that
moves money it is worse than that.

The second is the one this module exists to hold. Unlike an evaluation run,
which is offline and synthetic, traces leave continuously from production. A
span carrying a dispute message, a billing context or our prose about either
would send customer data to whichever backend a client wired up -- while the
README argues that the architecture lets you choose a vendor by data
residency. That contradiction is checked here rather than promised.
"""

import inspect

import pytest

pytest.importorskip("opentelemetry", reason="tracing is an optional extra")

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from app.application.investigate_dispute import DisputeInvestigator  # noqa: E402
from app.decision.mock import MockDecisionEngine  # noqa: E402
from app.domain.models import Dispute  # noqa: E402
from app.observability import tracing  # noqa: E402
from app.providers.fake.adapter import FakeBillingProvider  # noqa: E402

SECRET = "Je crois que j'ai été facturé deux fois, c'est inadmissible"

CASE_A = Dispute(
    id="trace_a",
    customer_message=SECRET,
    customer_email="alice@acme.fr",
    invoice_id="inv_002",
)


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    """A provider for this module. Installed once: it is process-global."""
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    trace.set_tracer_provider(provider)
    return memory


@pytest.fixture
async def spans(exporter):
    exporter.clear()
    investigator = DisputeInvestigator(provider=FakeBillingProvider(), engine=MockDecisionEngine())
    await investigator.investigate(CASE_A)
    return exporter.get_finished_spans()


class TestSilentByDefault:
    def test_no_endpoint_means_no_provider(self):
        """`configure()` declines rather than guessing a destination."""
        assert tracing.configure(endpoint=None) is False

    def test_the_module_reports_only_what_it_configured(self):
        """Narrow on purpose.

        This fixture installs its own tracer provider, so spans *are* being
        recorded while this returns False. The question the function answers
        is "did our configuration find an endpoint", which is the one a health
        check can act on.
        """
        assert tracing.configured() is False

    def test_unknown_values_are_dropped_rather_than_stringified(self):
        """`None` is not `0` and it is not the string "None".

        A cost of None means no rate was applied. Exporting it as text would
        put a literal "None" in a cost column somewhere downstream.
        """
        assert tracing.usage_attributes(None) == {}


class TestWhatASpanCarries:
    async def test_the_run_is_one_span_with_its_verdict(self, spans):
        names = [s.name for s in spans]
        assert "invoke_workflow investigate_dispute" in names

        run = next(s for s in spans if s.name == "invoke_workflow investigate_dispute")
        assert run.attributes["gen_ai.operation.name"] == "invoke_workflow"
        assert run.attributes["billing.classification"] == "duplicate_billing"
        assert run.attributes["billing.verdict"] == "requires_approval"
        assert run.attributes["billing.requires_human_approval"] is True
        assert "DUPLICATE_INVOICE" in run.attributes["billing.findings"]

    async def test_confidence_travels_with_its_kind(self, spans):
        """The same rule as the audit row and the approval page: a number
        without the kind of number it is cannot be read."""
        run = next(s for s in spans if s.name.startswith("invoke_workflow"))

        assert "billing.confidence" in run.attributes
        assert run.attributes["billing.confidence_kind"] in {
            "self_reported",
            "distribution_derived",
            "calibrated",
        }


class TestNothingAboutTheCustomerLeaves:
    async def test_no_span_carries_the_customer_s_words(self, spans):
        """The message the customer wrote must appear in no attribute."""
        for span in spans:
            for key, value in span.attributes.items():
                assert SECRET not in str(value), f"{span.name}.{key}"
                assert "facturé deux fois" not in str(value), f"{span.name}.{key}"

    async def test_no_forbidden_field_is_attached(self, spans):
        for span in spans:
            for key in span.attributes:
                assert not any(bad in key for bad in tracing.FORBIDDEN), (
                    f"{span.name} carries {key}"
                )

    async def test_no_e_mail_address_leaves(self, spans):
        for span in spans:
            for key, value in span.attributes.items():
                assert "@" not in str(value), f"{span.name}.{key} = {value}"

    def test_the_instrumented_modules_name_no_forbidden_field(self):
        """Checked against the source, not the behaviour.

        A field that merely is not exported by today's code path is a weaker
        claim than one the instrumentation never names. This is the same test
        the MCP server gets for the mutating call, for the same reason.
        """
        from app.application import investigate_dispute
        from app.decision import claude

        for module in (investigate_dispute, claude):
            source = inspect.getsource(module)
            # Only the lines that build spans, not the whole module: the
            # engine legitimately reads dispute.customer_message to send it
            # to the model.
            traced = [
                line
                for line in source.splitlines()
                if "tracing." in line or "set_attributes" in line
            ]
            assert traced, f"{module.__name__} is not instrumented"
            for line in traced:
                for bad in tracing.FORBIDDEN:
                    assert bad not in line, f"{module.__name__}: {line.strip()}"


def test_an_exception_leaves_its_type_not_its_text(exporter):
    """A span keeps the error's type; its message may carry a URL or a token."""
    exporter.clear()
    with pytest.raises(RuntimeError), tracing.span("failing"):
        raise RuntimeError("GET https://billing.example.test/?token=SECRET-TOKEN")
    (span,) = exporter.get_finished_spans()
    assert span.attributes["error.type"] == "RuntimeError"
    everything = str(span.attributes) + str([e.attributes for e in span.events])
    assert "SECRET-TOKEN" not in everything
    assert span.status.status_code is trace.StatusCode.ERROR
    assert not span.status.description
